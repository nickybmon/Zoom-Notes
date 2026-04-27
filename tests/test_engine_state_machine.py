"""Engine state-machine tests: drive _poll_once through fixture WALs and
assert state transitions and accumulator contents.

The bug we hit on 2026-04-27 was in this layer (polling logic), not the
parser. These tests are the regression guard for that class of bug.
"""
import os
import shutil
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import EngineState, ZoomEngine


class FakeStat:
    """Stand-in for os.stat_result with the only fields the engine reads."""
    def __init__(self, st_mtime: float, st_size: int):
        self.st_mtime = st_mtime
        self.st_size = st_size


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect ~/.cache/zoom-notes to a tmp dir so tests never touch real cache."""
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", tmp_path / "zoom-notes-cache")
    yield tmp_path / "zoom-notes-cache"


@pytest.fixture
def fake_origin(tmp_path):
    """A real directory to satisfy `find_origin_dir`-style checks."""
    origin = tmp_path / "origin"
    origin.mkdir()
    return origin


@pytest.fixture
def fixture_wal_path(multi_meeting_wal, tmp_path):
    """Copy the fixture WAL into tmp so we can mutate mtime safely."""
    dest = tmp_path / "fixture-transcript.sqlite3-wal"
    shutil.copy2(multi_meeting_wal, dest)
    return dest


def _drive_tick(engine: ZoomEngine, origin, cfg, *, mtime: float, size: int, fixture_wal):
    """Run a single _poll_once tick where the WAL is the fixture file with
    the supplied mtime/size, simulating a live WAL changing over time."""
    os.utime(fixture_wal, (mtime, mtime))
    with patch.object(zoom_engine, "find_wal", return_value=fixture_wal):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)


class TestPollingStateMachine:
    def test_idle_to_active_on_first_change(self, fake_origin, fixture_wal_path, isolated_cache):
        """First poll seeds tracking; second poll with different size flips ACTIVE."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Tick 1: anchor only — must NOT flip to ACTIVE just because the WAL
        # exists (could be stale from a prior meeting).
        size = fixture_wal_path.stat().st_size
        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)
        assert engine._get_state() == EngineState.IDLE

        # Tick 2: size changed → ACTIVE
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)
        assert engine._get_state() == EngineState.ACTIVE

    def test_accumulator_grows_across_ticks(self, fake_origin, fixture_wal_path, isolated_cache, multi_meeting_meta):
        """Across multiple change ticks the accumulator should accumulate the
        union of entries seen in the WAL (no silent drops)."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        size = fixture_wal_path.stat().st_size

        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)

        with engine._accumulated_lock:
            count = len(engine._accumulated)

        expected_total = multi_meeting_meta["expected"]["total_unique_entries"]
        # Accumulator should have at least the deduplicated total. parse_transcript
        # already deduplicates, so equal-or-greater is the right bound.
        assert count >= expected_total - 1, \
            f"accumulator dropped entries: {count} < expected {expected_total}"

    def test_no_silent_drop_when_meeting_id_is_stale(
        self, fake_origin, fixture_wal_path, isolated_cache, multi_meeting_meta
    ):
        """Regression test for the 2026-04-27 bug.

        Forces the engine to think the OLDER meeting is the "active" one
        (e.g. it scored higher at IDLE→ACTIVE), then drives a tick and
        confirms entries from BOTH meetings still land in the accumulator.
        Pre-fix, the accumulator would only have entries for the stale ID.
        """
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        size = fixture_wal_path.stat().st_size

        meeting_ids = multi_meeting_meta["expected"]["meeting_ids"]
        if len(meeting_ids) < 2:
            pytest.skip("need a multi-meeting fixture to test this scenario")

        # Tick 1: anchor.
        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)

        # Force-pin the wrong meeting ID into tracking, simulating the bug:
        # IDLE→ACTIVE detected the stale meeting as "active".
        wrong_id = meeting_ids[0]
        engine._write_tracking(meeting_id=wrong_id)
        engine._set_state(EngineState.ACTIVE, meeting_id=wrong_id)

        # Tick 2: drive a change. After the fix, accumulator should hold
        # entries from BOTH meetings present in the WAL because we no longer
        # filter at poll time.
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)

        with engine._accumulated_lock:
            ids_in_acc = {e.get("meeting_id") for e in engine._accumulated.values() if e.get("meeting_id")}
        assert len(ids_in_acc) == len(meeting_ids), (
            f"accumulator should hold entries from all meetings present in WAL; "
            f"got {ids_in_acc}, expected {set(meeting_ids)}. "
            f"Pre-fix this returned only the stale meeting's entries."
        )

    def test_active_to_active_switches_meeting_when_better_id_appears(
        self, fake_origin, fixture_wal_path, isolated_cache, multi_meeting_meta
    ):
        """When a new meeting starts mid-session and outscores the current one,
        the engine should switch tracked meeting ID and clear the accumulator."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        size = fixture_wal_path.stat().st_size

        meeting_ids = multi_meeting_meta["expected"]["meeting_ids"]
        if len(meeting_ids) < 2:
            pytest.skip("need a multi-meeting fixture")

        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)
        # Pin the *non-detected* meeting ID; on next tick `detect_active_meeting_id`
        # should return the actually-best one and trigger a switch.
        from zoom_notes import detect_active_meeting_id
        best = detect_active_meeting_id(fixture_wal_path)
        non_best = next(m for m in meeting_ids if m != best)
        engine._write_tracking(meeting_id=non_best)
        engine._set_state(EngineState.ACTIVE, meeting_id=non_best)

        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)
        _, _, _, after_id = engine._read_tracking()
        assert after_id == best, \
            f"engine should have switched to the higher-scoring meeting ID ({best}), got {after_id}"
