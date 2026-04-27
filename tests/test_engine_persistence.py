"""Persistence-tier tests for the engine: truncate detection and periodic
forced snapshots.

These tests don't depend on a captured WAL fixture — they synthesize a
"WAL" file (a regular tmp file with controllable size + mtime) and patch
`parse_transcript` to return a deterministic entry list. That keeps the
persistence-layer regression coverage running in CI / fresh checkouts even
when no live fixture has been captured.

The bug class these guard against: a SQLite WAL checkpoint that truncates
the journal mid-meeting. The in-RAM accumulator is fine, but the on-disk
snapshot would otherwise stay stale until the next *new* utterance arrives,
leaving a window where an engine crash loses recent entries.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import (
    EngineState,
    ZoomEngine,
    _PERIODIC_PERSIST_TICKS,
    _TRUNCATE_RATIO,
)


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the on-disk accumulator cache to a tmp dir."""
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
    """A regular file standing in for a WAL whose size we control directly."""
    wal = tmp_path / "synthetic.sqlite3-wal"
    wal.write_bytes(b"\x00" * 4096)
    return wal


def _set_wal_size(wal: Path, size: int) -> None:
    """Resize the synthetic WAL to exactly `size` bytes."""
    with open(wal, "r+b") as f:
        f.truncate(size)


def _drive_tick_with_entries(
    engine: ZoomEngine,
    origin: Path,
    cfg,
    *,
    wal: Path,
    mtime: float,
    size: int,
    entries: list[dict],
    meeting_id: str = "M1",
) -> None:
    """Drive one _poll_once tick with full control over WAL stat + parse output.

    We patch `parse_transcript` and `detect_active_meeting_id` because the
    synthetic WAL has no real Zoom bytes — the parser would return [] and
    detection would yield None for it. The persistence logic we're testing
    doesn't care what entries look like, only that they're present and that
    a meeting_id is associated with them.
    """
    _set_wal_size(wal, size)
    os.utime(wal, (mtime, mtime))
    with patch.object(zoom_engine, "find_wal", return_value=wal), \
         patch.object(zoom_engine, "parse_transcript", return_value=entries), \
         patch.object(zoom_engine, "detect_active_meeting_id", return_value=meeting_id):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)


def _make_entry(msg_id: str, text: str, meeting_id: str = "M1") -> dict:
    return {
        "msg_id": msg_id,
        "text": text,
        "speaker": "Test User",
        "timestamp": "12:00:00",
        "meeting_id": meeting_id,
    }


# ── Truncate detection ───────────────────────────────────────────────────────


class TestTruncateDetection:
    def test_truncate_forces_persist_even_when_parser_yields_nothing_new(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """The core regression: WAL shrinks dramatically, parser returns no
        new entries, and yet we must refresh the on-disk snapshot anyway."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Tick 1: anchor at 100k bytes, IDLE → IDLE (no transition yet).
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
        )
        # Tick 2: grow to 100k+1, flips to ACTIVE, accumulator gets one entry.
        entries = [_make_entry("m1", "hello world")]
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001, entries=entries,
        )
        assert engine._get_state() == EngineState.ACTIVE

        # The previous tick's `changed` persist already wrote a snapshot.
        # Capture that file's mtime so we can prove a *fresh* persist happens
        # on the truncate tick, even though parse_transcript returns nothing.
        snapshot_path = isolated_cache / "in-progress-M1.json"
        assert snapshot_path.exists(), "expected initial persist from prior tick"
        original_mtime = snapshot_path.stat().st_mtime

        # Tick 3: truncate — new size is well below half of last_size, and the
        # parser returns the SAME entry (so changed_in_acc would be False).
        # Pre-fix: no persist happens. Post-fix: forced persist happens.
        # Sleep a hair so a fresh write has a measurably different mtime.
        import time as _time
        _time.sleep(0.01)
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1010.0, size=4_096, entries=entries,
        )

        new_mtime = snapshot_path.stat().st_mtime
        assert new_mtime > original_mtime, (
            "truncate tick must force a fresh persist; mtime did not advance"
        )

    def test_minor_shrink_below_threshold_does_not_count_as_truncate(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """A small frame-rotation shrink (≥ TRUNCATE_RATIO of old size) must
        NOT trigger a truncate-flagged persist. Only catastrophic shrinkage
        (well under half) should."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Build up to ACTIVE with one accumulated entry.
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
        )
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001, entries=[_make_entry("m1", "hi")],
        )
        engine._ticks_since_persist = 0  # reset to isolate this assertion

        # Sub-threshold shrink: new size is 75% of last (above TRUNCATE_RATIO=0.5).
        new_size = int(100_001 * 0.75)
        captured = []
        with patch.object(
            zoom_engine, "persist_accumulator",
            side_effect=lambda mid, snap: captured.append((mid, dict(snap))),
        ):
            _drive_tick_with_entries(
                engine, fake_origin, cfg, wal=synthetic_wal,
                mtime=1010.0, size=new_size, entries=[_make_entry("m1", "hi")],
            )

        # No truncate-flagged persist should fire — and since the entry text
        # is unchanged, no `changed` persist should fire either.
        assert captured == [], (
            f"sub-threshold shrink must not force a persist; got {len(captured)} call(s)"
        )

    def test_truncate_ratio_threshold_is_strict(self, synthetic_wal):
        """Document the exact threshold so future tweaks are deliberate."""
        # Sanity check on the ratio constant — protects against accidental
        # edits that would silently flip the threshold.
        assert _TRUNCATE_RATIO == 0.5, (
            "_TRUNCATE_RATIO is part of this test's contract; if you intentionally "
            "change it, update this assertion and the threshold tests."
        )


# ── Periodic force-persist ───────────────────────────────────────────────────


class TestPeriodicPersist:
    def test_persists_after_N_ticks_even_without_changes(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """After _PERIODIC_PERSIST_TICKS ACTIVE polls with no accumulator
        change, a forced persist must fire — guarding the gap where the
        parser yields nothing new for a long stretch."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Reach ACTIVE with one entry.
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
        )
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001, entries=[_make_entry("m1", "hi")],
        )
        # Reset the counter as if we just finished a "changed" persist on the
        # previous tick — that's the realistic starting point.
        engine._ticks_since_persist = 0

        captured = []
        with patch.object(
            zoom_engine, "persist_accumulator",
            side_effect=lambda mid, snap: captured.append((mid, len(snap))),
        ):
            # Drive _PERIODIC_PERSIST_TICKS no-change ticks. Each tick has the
            # same mtime/size as the last so `changed=False` and the only
            # path to a persist is the periodic forcing.
            for i in range(_PERIODIC_PERSIST_TICKS):
                _drive_tick_with_entries(
                    engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=1005.0, size=100_001,
                    entries=[_make_entry("m1", "hi")],
                )

        # Exactly one periodic persist should have fired (on the tick where
        # the counter reached the threshold). Subsequent ticks reset the
        # counter post-persist, so they don't re-fire within the loop.
        assert len(captured) == 1, (
            f"expected exactly one periodic persist after {_PERIODIC_PERSIST_TICKS} "
            f"no-change ticks, got {len(captured)}"
        )
        assert captured[0][0] == "M1"

    def test_idle_state_does_not_increment_persist_counter(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """The periodic persist should only fire while ACTIVE — IDLE ticks
        must not advance the counter, otherwise we'd persist an empty
        accumulator the moment we transitioned to ACTIVE."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Stay IDLE: anchor only, then no-change ticks.
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
        )
        for _ in range(_PERIODIC_PERSIST_TICKS + 2):
            _drive_tick_with_entries(
                engine, fake_origin, cfg, wal=synthetic_wal,
                mtime=1000.0, size=100_000, entries=[],
            )

        assert engine._get_state() == EngineState.IDLE
        assert engine._ticks_since_persist == 0, (
            "IDLE ticks must not advance _ticks_since_persist"
        )

    def test_counter_resets_to_zero_on_idle_to_active_transition(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """Going ACTIVE must restart the periodic-persist clock so the very
        first ACTIVE tick doesn't immediately force a snapshot."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Manually pump the counter as if a long ACTIVE period had elapsed in
        # some imaginary prior life, then reset tracking and go IDLE→ACTIVE.
        engine._ticks_since_persist = _PERIODIC_PERSIST_TICKS - 1

        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
        )
        # The IDLE→ACTIVE tick must reset the counter.
        _drive_tick_with_entries(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001, entries=[_make_entry("m1", "hi")],
        )
        # Counter goes through: reset to 0 in IDLE→ACTIVE branch, then
        # incremented to 1 by the force-persist block at the top of this
        # same tick? No — the force-persist block runs BEFORE the change
        # branch and at that point state was still IDLE. So counter stays 0.
        # On a subsequent ACTIVE tick the counter would increment to 1.
        assert engine._ticks_since_persist <= 1, (
            "counter must restart near 0 after IDLE→ACTIVE, "
            f"got {engine._ticks_since_persist}"
        )
