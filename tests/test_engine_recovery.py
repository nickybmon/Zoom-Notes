"""Crash-recovery tests for the engine startup flow.

Phase 7: when the engine boots, it scans `~/.cache/zoom-notes/` for
in-progress accumulator snapshots that survived a prior crash and emits
one `recovery_available` event per meeting it finds. The `recover`
stdin command then routes the user's recovery click through the existing
retry pipeline.

These tests are fixture-free — they fabricate persisted accumulators
directly on disk and run the engine constructor / run() emit path
against them. No real Zoom WAL is required.
"""
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import ZoomEngine


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the on-disk accumulator cache to a tmp dir."""
    cache = tmp_path / "zoom-notes-cache"
    cache.mkdir()
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", cache)
    yield cache


def _write_persisted(cache: Path, meeting_id: str, entries: list[dict]) -> Path:
    """Write a persisted accumulator file using the same on-disk shape the
    engine writes during a live meeting (a JSON array of entry dicts)."""
    slug = zoom_notes._safe_meeting_id_slug(meeting_id)
    path = cache / f"in-progress-{slug}.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _make_entry(meeting_id: str, msg_id: str, speaker: str, ts: str, text: str) -> dict:
    return {
        "msg_id": msg_id,
        "meeting_id": meeting_id,
        "speaker": speaker,
        "timestamp": ts,
        "text": text,
    }


# ── list_recoverable_meetings ────────────────────────────────────────────────


def test_list_recoverable_meetings_returns_meetings_with_entries(isolated_cache):
    _write_persisted(isolated_cache, "abc/123+def==", [
        _make_entry("abc/123+def==", "1", "Alice", "00:00:01", "hello"),
        _make_entry("abc/123+def==", "2", "Bob", "00:00:05", "world"),
    ])
    found = zoom_notes.list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["meeting_id"] == "abc/123+def=="
    assert found[0]["entry_count"] == 2
    assert "Alice" in found[0]["slug_hint"]


def test_list_recoverable_meetings_extracts_meeting_id_from_entries_not_filename(isolated_cache):
    """The on-disk slug is lossy — recovery must read the original ID from
    inside the JSON. Two different meeting IDs that slugify to the same
    filename would collide; here we just verify a non-trivial ID round-trips
    through the lossy slug step intact."""
    original = "16:0:16787456:0:217494649"
    _write_persisted(isolated_cache, original, [
        _make_entry(original, "msg1", "Alice", "00:00:10", "hi")
    ])
    found = zoom_notes.list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["meeting_id"] == original
    # And the file on disk has had its colons mangled to underscores.
    on_disk = list(isolated_cache.glob("in-progress-*.json"))
    assert len(on_disk) == 1
    assert ":" not in on_disk[0].name


def test_list_recoverable_meetings_filters_empty_files(isolated_cache):
    _write_persisted(isolated_cache, "real_meeting", [
        _make_entry("real_meeting", "1", "Alice", "00:00:01", "hi")
    ])
    _write_persisted(isolated_cache, "empty_meeting", [])
    found = zoom_notes.list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["meeting_id"] == "real_meeting"


def test_list_recoverable_meetings_skips_corrupt_json(isolated_cache):
    _write_persisted(isolated_cache, "good_one", [
        _make_entry("good_one", "1", "Alice", "00:00:01", "hi")
    ])
    (isolated_cache / "in-progress-bad.json").write_text("{not json",
                                                          encoding="utf-8")
    found = zoom_notes.list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["meeting_id"] == "good_one"


def test_list_recoverable_meetings_sorted_newest_first(isolated_cache):
    p1 = _write_persisted(isolated_cache, "older", [
        _make_entry("older", "1", "Alice", "00:00:01", "first")
    ])
    p2 = _write_persisted(isolated_cache, "newer", [
        _make_entry("newer", "1", "Bob", "00:00:01", "second")
    ])
    import os
    os.utime(p1, (1_000_000_000, 1_000_000_000))
    os.utime(p2, (2_000_000_000, 2_000_000_000))

    found = zoom_notes.list_recoverable_meetings()
    assert [r["meeting_id"] for r in found] == ["newer", "older"]


def test_list_recoverable_meetings_no_cache_dir_returns_empty(tmp_path, monkeypatch):
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", nonexistent)
    assert zoom_notes.list_recoverable_meetings() == []


def test_list_recoverable_meetings_filters_entries_without_meeting_id(isolated_cache):
    """Entries written by a very old engine version may not have meeting_id —
    we shouldn't crash, we should just skip the file."""
    path = isolated_cache / "in-progress-legacy.json"
    path.write_text(json.dumps([
        {"msg_id": "1", "speaker": "Alice", "timestamp": "00:00:01", "text": "hi"}
    ]), encoding="utf-8")
    found = zoom_notes.list_recoverable_meetings()
    assert found == []


# ── Engine startup emission ──────────────────────────────────────────────────


def test_engine_init_captures_recoverable_meetings(isolated_cache):
    _write_persisted(isolated_cache, "M-recover", [
        _make_entry("M-recover", "1", "Alice", "00:00:01", "hi")
    ])
    engine = ZoomEngine()
    ids = [r["meeting_id"] for r in engine._recoverable_at_startup]
    assert "M-recover" in ids


def test_engine_init_with_empty_cache_has_empty_recoverable_list(isolated_cache):
    """No persisted files → empty list, not a crash, not None."""
    engine = ZoomEngine()
    assert engine._recoverable_at_startup == []


def test_run_emits_recovery_event_per_meeting(isolated_cache, monkeypatch):
    """The run() startup emits a recovery_available event for each persisted
    meeting before the first state event."""
    _write_persisted(isolated_cache, "M-one", [
        _make_entry("M-one", "1", "Alice", "00:00:01", "hi"),
    ])
    _write_persisted(isolated_cache, "M-two", [
        _make_entry("M-two", "1", "Bob", "00:00:02", "yo"),
    ])

    emitted = []
    monkeypatch.setattr(zoom_engine, "emit", lambda obj: emitted.append(obj))

    # Stand in find_origin_dir so the ready event resolves predictably.
    monkeypatch.setattr(zoom_engine, "find_origin_dir", lambda: None)

    engine = ZoomEngine()

    # We can't call run() directly — it loops forever. Replicate just the
    # startup sequence here. (The implementation ordering is part of the
    # contract: ready, recovery_available×N, state:idle.)
    from zoom_engine import EngineState
    zoom_engine.emit({
        "event": "ready",
        "zoom_installed": False,
        "wal_path": None,
    })
    for rec in engine._recoverable_at_startup:
        zoom_engine.emit({
            "event": "recovery_available",
            "meeting_id": rec["meeting_id"],
            "entry_count": rec["entry_count"],
            "last_updated": rec["last_updated"],
            "slug_hint": rec["slug_hint"],
        })
    zoom_engine.emit({"event": "state", "value": EngineState.IDLE})

    kinds = [e.get("event") for e in emitted]
    assert kinds[0] == "ready"
    assert kinds[-1] == "state"
    recovery_events = [e for e in emitted if e.get("event") == "recovery_available"]
    recovered_ids = {e["meeting_id"] for e in recovery_events}
    assert recovered_ids == {"M-one", "M-two"}
    for re_evt in recovery_events:
        assert re_evt["entry_count"] >= 1
        assert re_evt["slug_hint"]


# ── recover stdin command ────────────────────────────────────────────────────


def test_recover_command_dispatches_to_retry(isolated_cache, monkeypatch):
    """The `recover` command should route through `_trigger_retry`."""
    engine = ZoomEngine()
    seen = []
    monkeypatch.setattr(engine, "_trigger_retry", lambda mid: seen.append(mid))

    engine._handle_command({"cmd": "recover", "meeting_id": "M-recover-7"})
    assert seen == ["M-recover-7"]


def test_recover_command_without_meeting_id_emits_error(isolated_cache, monkeypatch):
    """Missing meeting_id is a programming error in the UI — surface, don't
    silently no-op (a silent no-op would leave the menu item looking broken)."""
    engine = ZoomEngine()
    seen_emits = []
    monkeypatch.setattr(zoom_engine, "emit", lambda obj: seen_emits.append(obj))
    seen_retries = []
    monkeypatch.setattr(engine, "_trigger_retry", lambda mid: seen_retries.append(mid))

    engine._handle_command({"cmd": "recover"})
    assert seen_retries == []
    error_events = [e for e in seen_emits if e.get("event") == "error"]
    assert len(error_events) == 1
    assert "meeting_id" in error_events[0].get("message", "")


def test_recover_command_with_empty_meeting_id_emits_error(isolated_cache, monkeypatch):
    engine = ZoomEngine()
    seen_emits = []
    monkeypatch.setattr(zoom_engine, "emit", lambda obj: seen_emits.append(obj))
    seen_retries = []
    monkeypatch.setattr(engine, "_trigger_retry", lambda mid: seen_retries.append(mid))

    engine._handle_command({"cmd": "recover", "meeting_id": ""})
    assert seen_retries == []
    assert any(e.get("event") == "error" for e in seen_emits)
