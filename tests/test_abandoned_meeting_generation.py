"""Regression tests for the 2026-05-04 AEO GA back-to-back meeting bug.

The incident:
  1:31 PM   AEO GA started (engine flipped IDLE -> ACTIVE).
  ~2:00 PM  AEO GA ended; user joined Nick / Marissa immediately.
  2:01:56   Engine detected the new meeting_id and ran "Case B"
            (active_reevaluation): cleared the in-memory accumulator
            AND deleted AEO GA's persisted on-disk snapshot, throwing
            away ~30 minutes of real meeting transcript content.
  2:24:39   Nick / Marissa idled out, generated a normal note.
  Result:   AEO GA was never saved as a note. Transcript bytes were
            checkpointed away by Zoom shortly after; recovery from
            local disk was not possible.

The fix:
  When Case B fires with a "real-looking" abandoned accumulator
  (>= 5 entries with at least one non-Unknown speaker), snapshot it
  and kick off a background generation worker that runs the full
  three-stage pipeline (save transcript, run LLM, save note) for the
  abandoned meeting BEFORE the in-memory accumulator is cleared.

These tests guard each piece of that fix:
  - The threshold helper distinguishes real meetings from misdetections.
  - Case B correctly snapshots and dispatches when the threshold passes.
  - Case B falls back to the original delete-only behavior when below.
  - The generation worker actually saves transcript + note to disk.
  - The worker does NOT freeze the engine main loop (state stays ACTIVE).
"""
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import EngineState, ZoomEngine


# ── Fixtures (mirror test_back_to_back_meetings.py) ──────────────────────


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    cache = tmp_path / "zoom-notes-cache"
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", cache)
    yield cache


@pytest.fixture
def fake_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    return origin


@pytest.fixture
def synthetic_wal(tmp_path):
    wal = tmp_path / "synthetic.sqlite3-wal"
    wal.write_bytes(b"\x00" * 4096)
    return wal


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Redirect output dirs to tmpdir so save functions don't touch the
    real vault. Mirrors the fixture in test_back_to_back_meetings.py."""
    notes_dir = tmp_path / "vault" / "Notes"
    transcripts_dir = tmp_path / "vault" / "Transcripts"
    notes_dir.mkdir(parents=True)
    transcripts_dir.mkdir(parents=True)

    real_get_config = zoom_notes.get_config

    def fake_get_config():
        cfg = real_get_config()
        cfg.notes_dir = str(notes_dir)
        cfg.transcripts_dir = str(transcripts_dir)
        return cfg

    monkeypatch.setattr(zoom_notes, "get_config", fake_get_config)
    monkeypatch.setattr(zoom_engine, "get_config", fake_get_config)
    yield {"notes": notes_dir, "transcripts": transcripts_dir}


def _make_entry(msg_id: str, text: str, *, meeting_id: str,
                speaker: str = "Test User", timestamp: str = "12:00:00") -> dict:
    return {
        "msg_id": msg_id,
        "text": text,
        "speaker": speaker,
        "timestamp": timestamp,
        "meeting_id": meeting_id,
    }


def _set_wal_size(wal: Path, size: int) -> None:
    with open(wal, "r+b") as f:
        f.truncate(size)


def _drive_tick(engine: ZoomEngine, origin: Path, cfg, *, wal: Path,
                mtime: float, size: int, entries: list[dict],
                detected_meeting_id: str | None) -> None:
    _set_wal_size(wal, size)
    os.utime(wal, (mtime, mtime))
    with patch.object(zoom_engine, "find_wal", return_value=wal), \
         patch.object(zoom_engine, "parse_transcript", return_value=entries), \
         patch.object(zoom_engine, "detect_active_meeting_id",
                      return_value=detected_meeting_id):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)


def _make_realistic_accumulator(meeting_id: str, count: int = 6) -> dict:
    """Build a snapshot that looks like a real (back-to-back) meeting:
    multiple entries, real speakers, sequential timestamps."""
    speakers = ["Alice", "Bob", "Carol"]
    return {
        f"msg-{i}": _make_entry(
            f"msg-{i}",
            f"Utterance number {i}.",
            meeting_id=meeting_id,
            speaker=speakers[i % len(speakers)],
            timestamp=f"13:3{i}:00",
        )
        for i in range(count)
    }


# ── Threshold helper ──────────────────────────────────────────────────────


class TestAbandonedLooksReal:
    """The gate between back-to-back-meeting (generate) and
    misidentification (discard). False positives cost LLM calls; false
    negatives lose data. Heuristic must correctly classify both."""

    def test_real_meeting_passes(self):
        snap = _make_realistic_accumulator("aeo-ga", count=6)
        assert ZoomEngine._abandoned_looks_real(snap) is True

    def test_too_few_entries_fails(self):
        snap = _make_realistic_accumulator("aeo-ga", count=3)
        assert ZoomEngine._abandoned_looks_real(snap) is False

    def test_only_unknown_speakers_fails(self):
        snap = {
            f"msg-{i}": _make_entry(
                f"msg-{i}", f"text {i}", meeting_id="x", speaker="Unknown",
                timestamp=f"12:0{i}:00",
            )
            for i in range(8)
        }
        assert ZoomEngine._abandoned_looks_real(snap) is False

    def test_empty_snapshot_fails(self):
        assert ZoomEngine._abandoned_looks_real({}) is False

    def test_at_least_one_real_speaker_at_threshold_passes(self):
        # Exactly 5 entries, only 1 with a real speaker — boundary case.
        snap = {
            f"msg-{i}": _make_entry(
                f"msg-{i}", f"text {i}", meeting_id="x",
                speaker=("Alice" if i == 0 else "Unknown"),
                timestamp=f"12:0{i}:00",
            )
            for i in range(5)
        }
        assert ZoomEngine._abandoned_looks_real(snap) is True


# ── Case B dispatch ───────────────────────────────────────────────────────


class TestCaseBDispatch:
    """Case B in `_poll_once` must snapshot + dispatch when the abandoned
    accumulator looks real, and fall back to delete-only when it doesn't."""

    def test_case_b_kicks_off_abandoned_generation_when_threshold_met(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Tick 1: anchor at meeting A.
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=4096, entries=[],
            detected_meeting_id="MEETING-A==",
        )
        # Tick 2: flip to ACTIVE for meeting A with a populated accumulator.
        a_entries = list(_make_realistic_accumulator("MEETING-A==", count=6).values())
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=8192, entries=a_entries,
            detected_meeting_id="MEETING-A==",
        )
        with engine._accumulated_lock:
            pre_count = len(engine._accumulated)
        assert pre_count >= 5, "fixture sanity: accumulator must have meeting A's entries"

        # Tick 3: Case B — meeting B detected. Patch the abandoned-generation
        # entry point so we can assert it was called with meeting A's data.
        b_entries = list(_make_realistic_accumulator("MEETING-B==", count=2).values())
        with patch.object(ZoomEngine, "_trigger_abandoned_generation") as mock_dispatch:
            _drive_tick(
                engine, fake_origin, cfg, wal=synthetic_wal,
                mtime=1010.0, size=12288, entries=b_entries,
                detected_meeting_id="MEETING-B==",
            )
            mock_dispatch.assert_called_once()
            args = mock_dispatch.call_args[0]
            assert args[0] == "MEETING-A==", (
                f"expected dispatch for abandoned meeting A, got {args[0]}"
            )
            snapshot = args[1]
            assert len(snapshot) >= 5
            assert all(e["meeting_id"] == "MEETING-A==" for e in snapshot.values())

        # And the new meeting's accumulator should start fresh (the existing
        # Case B clear-and-switch behavior must not regress).
        with engine._accumulated_lock:
            ids_after = {e.get("meeting_id") for e in engine._accumulated.values()}
        assert ids_after <= {"MEETING-B=="}, (
            f"new meeting's accumulator polluted with abandoned data: {ids_after}"
        )

    def test_case_b_skips_generation_when_below_threshold(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault
    ):
        """Misidentification path: the previous tracked meeting only had
        2 entries (briefly mistaken for active before scoring corrected).
        Should NOT trigger an LLM call — discard like the original behavior."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=4096, entries=[],
            detected_meeting_id="MIS-IDENTIFIED==",
        )
        a_entries = list(_make_realistic_accumulator("MIS-IDENTIFIED==", count=2).values())
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=8192, entries=a_entries,
            detected_meeting_id="MIS-IDENTIFIED==",
        )

        b_entries = list(_make_realistic_accumulator("REAL-MEETING==", count=2).values())
        with patch.object(ZoomEngine, "_trigger_abandoned_generation") as mock_dispatch:
            _drive_tick(
                engine, fake_origin, cfg, wal=synthetic_wal,
                mtime=1010.0, size=12288, entries=b_entries,
                detected_meeting_id="REAL-MEETING==",
            )
            mock_dispatch.assert_not_called()


# ── Worker behavior ───────────────────────────────────────────────────────


class TestAbandonedGenerationWorker:
    """The generation worker must save BOTH transcript and note (Stage 1
    + Stage 3 of `_generate_notes`) and must not freeze the engine."""

    def _seed_active(self, engine, fake_origin, synthetic_wal, cfg):
        """Drive engine into ACTIVE so the lock + state machinery is live."""
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=4096, entries=[],
            detected_meeting_id="ACTIVE==",
        )
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=8192,
            entries=list(_make_realistic_accumulator("ACTIVE==", count=2).values()),
            detected_meeting_id="ACTIVE==",
        )

    def test_worker_saves_transcript_and_note_to_vault(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        self._seed_active(engine, fake_origin, synthetic_wal, cfg)

        snapshot = _make_realistic_accumulator("ABANDONED==", count=8)
        finished = threading.Event()
        # Patch summarize so we don't hit the network. Stub returns a
        # short fake note body.
        def fake_summarize(transcript_text, meeting_title, cfg, cancel_event=None):
            return "## Overview\n\nFake LLM-generated summary for tests."

        # Need find_origin_dir to return something so the worker can
        # resolve blocks/transcript WALs (it'll get None back from
        # _resolve_wal which is fine — _derive_meeting_title falls back
        # to a "Zoom Meeting <date> <time>" label).
        with patch.object(zoom_engine, "summarize", side_effect=fake_summarize), \
             patch.object(zoom_engine, "find_origin_dir", return_value=fake_origin), \
             patch.object(engine, "_resolve_wal", return_value=None):
            engine._trigger_abandoned_generation("ABANDONED==", snapshot)
            # Worker is daemon — wait for it to release the lock to know
            # it finished. acquire(timeout=5) returns True once the worker
            # is done and has released.
            assert engine._generating_lock.acquire(timeout=5.0), (
                "abandoned-generation worker did not finish within 5s"
            )
            engine._generating_lock.release()

        # Transcript file must exist somewhere under the vault.
        transcript_files = list(Path(isolated_vault["transcripts"]).rglob("*.md"))
        note_files = list(Path(isolated_vault["notes"]).rglob("*.md"))
        assert len(transcript_files) == 1, (
            f"expected exactly one transcript file, got: {transcript_files}"
        )
        assert len(note_files) == 1, (
            f"expected exactly one note file, got: {note_files}"
        )
        # Note must contain the fake LLM body — proves we actually wrote
        # the LLM result, not a placeholder.
        assert "Fake LLM-generated summary" in note_files[0].read_text()

    def test_worker_does_not_freeze_engine_state(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault
    ):
        """The whole point of the design: the engine must stay ACTIVE for
        the new meeting while the abandoned one's note is being generated.
        If the worker called `_set_state(GENERATING)`, the main loop would
        stop polling and lose new-meeting transcript content."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        self._seed_active(engine, fake_origin, synthetic_wal, cfg)
        assert engine._get_state() == EngineState.ACTIVE

        snapshot = _make_realistic_accumulator("ABANDONED==", count=8)
        # Use a slow summarize so the worker is still running when we check.
        slow = threading.Event()
        def slow_summarize(*args, **kwargs):
            slow.wait(timeout=2.0)
            return "## Overview\n\nDone."

        with patch.object(zoom_engine, "summarize", side_effect=slow_summarize), \
             patch.object(zoom_engine, "find_origin_dir", return_value=fake_origin), \
             patch.object(engine, "_resolve_wal", return_value=None):
            engine._trigger_abandoned_generation("ABANDONED==", snapshot)
            # Worker is now running summarize() and blocked on `slow`.
            # Engine state must still be ACTIVE — not GENERATING.
            assert engine._get_state() == EngineState.ACTIVE, (
                f"engine flipped state during abandoned generation: {engine._get_state()}"
            )
            # Release the worker so it finishes cleanly.
            slow.set()
            assert engine._generating_lock.acquire(timeout=5.0)
            engine._generating_lock.release()

    def test_back_to_back_boundaries_serialize_through_lock(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault
    ):
        """Two abandoned-generation calls in quick succession (A -> B -> C
        scenario) must both run, serializing through `_generating_lock`."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        self._seed_active(engine, fake_origin, synthetic_wal, cfg)

        invocations = []
        invocation_lock = threading.Lock()

        def tracking_summarize(transcript_text, meeting_title, cfg, cancel_event=None):
            # Record which meeting we're summarizing for (extracted from title
            # or transcript). Use the meeting_title as a proxy; in this test
            # both will match the same date-stamp fallback, so we record by
            # transcript content instead — each snapshot has unique text.
            with invocation_lock:
                # Find which of the two meeting ids we're generating for
                # by looking at the transcript content.
                if "ABANDONED-A" in transcript_text:
                    invocations.append("A")
                elif "ABANDONED-B" in transcript_text:
                    invocations.append("B")
            return "## Overview\n\nDone."

        def tagged_snapshot(tag: str) -> dict:
            return {
                f"{tag}-msg-{i}": _make_entry(
                    f"{tag}-msg-{i}",
                    f"{tag} content number {i}.",  # unique per tag
                    meeting_id=f"{tag}==",
                    speaker="Alice",
                    timestamp=f"13:0{i}:00",
                )
                for i in range(6)
            }

        snap_a = tagged_snapshot("ABANDONED-A")
        snap_b = tagged_snapshot("ABANDONED-B")

        with patch.object(zoom_engine, "summarize", side_effect=tracking_summarize), \
             patch.object(zoom_engine, "find_origin_dir", return_value=fake_origin), \
             patch.object(engine, "_resolve_wal", return_value=None):
            engine._trigger_abandoned_generation("ABANDONED-A==", snap_a)
            engine._trigger_abandoned_generation("ABANDONED-B==", snap_b)

            # Both workers are queued on `_generating_lock`. Wait for both
            # to finish by polling until two invocations have been recorded.
            import time
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                with invocation_lock:
                    if len(invocations) == 2:
                        break
                time.sleep(0.05)

        with invocation_lock:
            assert len(invocations) == 2, (
                f"expected both A and B to run, got: {invocations}"
            )
            assert sorted(invocations) == ["A", "B"], (
                f"expected one A and one B invocation, got: {invocations}"
            )
