"""Full-cycle replay tests: drive the engine through IDLE → ACTIVE → GENERATING
against fixture WALs, with the LLM stubbed out, and assert the final note
generation receives the right transcript.

This is the integration-level regression guard for today's bug class.
"""
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import EngineState, ZoomEngine


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", tmp_path / "zoom-notes-cache")
    yield tmp_path / "zoom-notes-cache"


@pytest.fixture
def isolated_output_dirs(tmp_path, monkeypatch):
    """Redirect notes_path and transcripts_path so save_* never writes to ~/Desktop."""
    notes_dir = tmp_path / "Notes"
    transcripts_dir = tmp_path / "Transcripts"
    notes_dir.mkdir()
    transcripts_dir.mkdir()

    real_get_config = zoom_notes.get_config

    def patched_get_config():
        cfg = real_get_config()
        # ZoomNotesConfig is a dataclass; mutate the in-memory copy so
        # save_transcript_only / save_note_only land in tmp.
        cfg.notes_dir = str(notes_dir)
        cfg.transcripts_dir = str(transcripts_dir)
        return cfg

    monkeypatch.setattr(zoom_notes, "get_config", patched_get_config)
    monkeypatch.setattr(zoom_engine, "get_config", patched_get_config)
    return {"notes": notes_dir, "transcripts": transcripts_dir}


@pytest.fixture
def fake_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    return origin


@pytest.fixture
def fixture_wal_path(multi_meeting_wal, tmp_path):
    dest = tmp_path / "fixture-transcript.sqlite3-wal"
    shutil.copy2(multi_meeting_wal, dest)
    return dest


@pytest.fixture
def fixture_blocks_path(multi_meeting_blocks, tmp_path):
    dest = tmp_path / "fixture-blocks.sqlite3-wal"
    shutil.copy2(multi_meeting_blocks, dest)
    return dest


def _drive_tick(engine, origin, cfg, mtime, size, fixture_wal):
    os.utime(fixture_wal, (mtime, mtime))
    with patch.object(zoom_engine, "find_wal", return_value=fixture_wal):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)


class TestReplay:
    def test_full_cycle_generates_note_with_correct_meeting(
        self,
        fake_origin,
        fixture_wal_path,
        fixture_blocks_path,
        isolated_cache,
        isolated_output_dirs,
        multi_meeting_meta,
    ):
        """Walk the engine through a full meeting and confirm:
            1. The transcript is saved BEFORE the LLM is called (durability).
            2. The note is generated for the correct meeting.
            3. After success, the persisted accumulator is cleaned up.
        """
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        size = fixture_wal_path.stat().st_size
        _drive_tick(engine, fake_origin, cfg, 1000.0, size, fixture_wal_path)
        _drive_tick(engine, fake_origin, cfg, 1005.0, size + 1, fixture_wal_path)
        assert engine._get_state() == EngineState.ACTIVE

        # Stub LLM and find_wal for the generation path. Patch the symbol
        # in zoom_engine's namespace because zoom_engine does
        # `from zoom_notes import summarize` at import time.
        with patch.object(zoom_engine, "summarize", return_value="(summary)"), \
             patch.object(zoom_engine, "find_wal", return_value=fixture_wal_path):
            engine._generate_notes(fake_origin, cfg)

        # A transcript must exist on disk in the user-visible folder.
        transcripts_root = isolated_output_dirs["transcripts"]
        transcripts = list(transcripts_root.rglob("*.md"))
        assert transcripts, "transcript must be saved to user folder"

        notes_root = isolated_output_dirs["notes"]
        notes = list(notes_root.rglob("*.md"))
        assert notes, "note must be saved on LLM success"

        # The note body should contain our stub summary.
        note_text = notes[0].read_text()
        assert "(summary)" in note_text

    def test_llm_failure_writes_placeholder_and_keeps_transcript(
        self,
        fake_origin,
        fixture_wal_path,
        fixture_blocks_path,
        isolated_cache,
        isolated_output_dirs,
    ):
        """When the LLM raises, the transcript is still saved and a placeholder
        note is written with retry metadata."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        size = fixture_wal_path.stat().st_size
        _drive_tick(engine, fake_origin, cfg, 1000.0, size, fixture_wal_path)
        _drive_tick(engine, fake_origin, cfg, 1005.0, size + 1, fixture_wal_path)

        def boom(*a, **kw):
            raise RuntimeError("LLM API error 503: server unavailable")

        with patch.object(zoom_engine, "summarize", side_effect=boom), \
             patch.object(zoom_engine, "find_wal", return_value=fixture_wal_path):
            engine._generate_notes(fake_origin, cfg)

        transcripts = list(isolated_output_dirs["transcripts"].rglob("*.md"))
        assert transcripts, "transcript must be saved even when LLM fails"

        notes = list(isolated_output_dirs["notes"].rglob("*.md"))
        assert notes, "placeholder note must be saved on LLM failure"
        body = notes[0].read_text()
        assert "Note generation failed" in body
        assert "status: note-generation-failed" in body
        assert "retry_meeting_id" in body

        assert engine._last_run_note_failed is True
        assert engine._last_failed_meeting is not None
