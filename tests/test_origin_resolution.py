"""Tests for `ZoomEngine._resolve_origin` — the periodic IDLE re-check that
guards against Zoom silently switching which WebKit bucket it writes to
mid-session (e.g. a sign-out/sign-in cycle).

Regression guard for the 2026-09-08 bug where the engine cached its
resolved origin at startup, Zoom later moved to a different signed-in
bucket, and the engine sat IDLE forever polling a now-frozen WAL while
real meetings recorded into the new bucket.
"""
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
def origin_a(tmp_path):
    origin = tmp_path / "origin_a"
    origin.mkdir()
    return origin


@pytest.fixture
def origin_b(tmp_path):
    origin = tmp_path / "origin_b"
    origin.mkdir()
    return origin


class TestResolveOrigin:
    def test_none_origin_always_resolves(self, isolated_cache, origin_a):
        engine = ZoomEngine()
        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_a) as m:
            result = engine._resolve_origin(None)
        assert result == origin_a
        m.assert_called_once()

    def test_invalidated_cache_resolves_regardless_of_state(self, isolated_cache, origin_a, origin_b):
        engine = ZoomEngine()
        engine._set_state(EngineState.ACTIVE, meeting_id="m1")
        engine._origin_invalidated = True
        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_b) as m:
            result = engine._resolve_origin(origin_a)
        assert result == origin_b
        m.assert_called_once()

    def test_active_state_never_rechecks(self, isolated_cache, origin_a, origin_b):
        """Mid-meeting, a bucket switch must not be applied — switching
        origin while ACTIVE risks dropping in-flight transcript data."""
        engine = ZoomEngine()
        engine._set_state(EngineState.ACTIVE, meeting_id="m1")
        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_b) as m:
            result = engine._resolve_origin(origin_a)
        assert result == origin_a
        m.assert_not_called()

    def test_idle_switches_to_fresher_origin(self, isolated_cache, origin_a, origin_b):
        engine = ZoomEngine()
        engine._set_state(EngineState.IDLE)
        # Prime a cache entry / setup-error guard for the OLD origin so we
        # can assert they get cleared on switch.
        engine._wal_cache[(str(origin_a), "transcript")] = origin_a / "fake.wal"
        engine._setup_error_emitted = True

        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_b):
            result = engine._resolve_origin(origin_a)

        assert result == origin_b
        assert engine._wal_cache == {}
        assert engine._setup_error_emitted is False

    def test_idle_keeps_origin_when_no_fresher_candidate(self, isolated_cache, origin_a):
        engine = ZoomEngine()
        engine._set_state(EngineState.IDLE)
        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_a):
            result = engine._resolve_origin(origin_a)
        assert result == origin_a

    def test_idle_recheck_is_throttled(self, isolated_cache, origin_a, origin_b):
        """Back-to-back IDLE ticks within the throttle window shouldn't
        re-scan the filesystem every 5-second poll for hours on end."""
        engine = ZoomEngine()
        engine._set_state(EngineState.IDLE)

        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_b) as m:
            first = engine._resolve_origin(origin_a)
            second = engine._resolve_origin(first)

        assert first == origin_b
        assert second == origin_b  # already switched, still returned as-is
        m.assert_called_once()  # second call was throttled, no re-scan

    def test_idle_rechecks_again_after_throttle_window_elapses(self, isolated_cache, origin_a, origin_b):
        engine = ZoomEngine()
        engine._set_state(EngineState.IDLE)

        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_a):
            engine._resolve_origin(origin_a)

        # Simulate the throttle window having elapsed.
        engine._last_origin_recheck_monotonic -= (
            ZoomEngine._ORIGIN_RECHECK_INTERVAL_SECS + 1
        )

        with patch.object(zoom_engine, "find_origin_dir", return_value=origin_b) as m:
            result = engine._resolve_origin(origin_a)

        assert result == origin_b
        m.assert_called_once()
