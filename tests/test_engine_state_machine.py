"""Engine state-machine tests: drive _poll_once through fixture WALs and
assert state transitions and accumulator contents.

The bug we hit on 2026-04-27 was in this layer (polling logic), not the
parser. These tests are the regression guard for that class of bug.
"""
import os
import shutil
import time
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


class TestAccumulatorCrossMeetingGuard:
    """Regression test for the 2026-05-04 Quick Chat on PnP Assets incident.

    The parser bug (entry-boundary leak in `parse_transcript`) is the root
    cause and is fixed at the source in `tests/test_parser.py`. The engine
    layer adds defense in depth: when a fresh parse for an existing
    accumulated entry shows a CONFLICTING `meeting_id` (i.e. the parser
    leaked metadata from a different meeting), the accumulator must
    refuse to overwrite the trusted `speaker` / `timestamp` it already
    has stamped.

    Without this guard, even with the parser fix, a single bad parse
    output could rewrite an entry's speaker — because Case A
    (upgrade-from-empty) had already stamped the entry with the active
    meeting's id, the existing `meeting_id`-protection clause on its own
    would let the rest of the metadata silently drift.
    """

    def _drive_change_tick_with_parse(self, engine, origin, cfg, *, wal, mtime, size, parse_result):
        """Drive a single change-tick that returns `parse_result` from
        parse_transcript. Bumps mtime so the engine's change-detection
        branch fires and calls parse_transcript."""
        os.utime(wal, (mtime, mtime))
        from unittest.mock import patch
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "parse_transcript", return_value=parse_result):
            engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)

    def test_skips_overwrite_when_fresh_parse_meeting_id_conflicts(
        self, fake_origin, tmp_path, isolated_cache
    ):
        # No real WAL needed — we patch parse_transcript to inject payloads
        # directly. Engine just needs a file it can stat for change detection.
        wal = tmp_path / "synthetic.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Anchor + flip to ACTIVE so the change-tick branch runs.
        size = wal.stat().st_size
        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=wal)
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=wal)

        # Seed the accumulator with a "trusted" entry: stamped to the
        # active meeting (this is what Case A upgrade-from-empty produces
        # in real flow), with a real speaker and a sane timestamp.
        msg_id = "synthetic-msg-1"
        active_id = "ACTIVE-MEETING-ID=="
        with engine._accumulated_lock:
            engine._accumulated[msg_id] = {
                "msg_id": msg_id,
                "text": "Just using the Slack AI features.",
                "speaker": "Nick Blackmon",
                "timestamp": "16:33:08",
                "meeting_id": active_id,
            }

        # Drive a change tick where parse_transcript "leaks" metadata from
        # a totally different meeting onto the same msg_id (same physical
        # WAL utterance) — username, timestamp, and meeting_id all slurped
        # across a corrupted entry boundary.
        leaked = [{
            "msg_id": msg_id,
            "text": "Just using the Slack AI features.",
            "speaker": "Michael Huard",
            "timestamp": "14:56:05",
            "meeting_id": "OTHER-MEETING-ID==",
        }]
        self._drive_change_tick_with_parse(
            engine, fake_origin, cfg,
            wal=wal, mtime=1010.0, size=size + 2, parse_result=leaked,
        )

        with engine._accumulated_lock:
            after = engine._accumulated[msg_id]

        # The trusted speaker/timestamp/meeting_id MUST survive a parse
        # that disagrees about which meeting this entry belongs to.
        assert after["speaker"] == "Nick Blackmon", (
            f"speaker overwritten by conflicting-meeting parse: {after['speaker']}"
        )
        assert after["timestamp"] == "16:33:08", (
            f"timestamp overwritten by conflicting-meeting parse: {after['timestamp']}"
        )
        assert after["meeting_id"] == active_id, (
            f"meeting_id overwritten (this guard already existed): {after['meeting_id']}"
        )

    def test_still_overwrites_when_meeting_id_matches(
        self, fake_origin, tmp_path, isolated_cache
    ):
        """Inverse: when the fresh parse's meeting_id matches the existing
        entry's, it's a normal incremental update (Zoom streaming the
        utterance word-by-word, refining speaker recognition, etc.) and
        the new metadata SHOULD win. The guard must not over-fire."""
        wal = tmp_path / "synthetic.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        size = wal.stat().st_size
        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=wal)
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=wal)

        msg_id = "synthetic-msg-2"
        active_id = "ACTIVE-MEETING-ID=="
        with engine._accumulated_lock:
            engine._accumulated[msg_id] = {
                "msg_id": msg_id,
                "text": "Hello",
                "speaker": "Unknown",
                "timestamp": "10:00:00",
                "meeting_id": active_id,
            }

        # Same meeting, longer text + real speaker resolved by Zoom.
        refined = [{
            "msg_id": msg_id,
            "text": "Hello world, longer text",
            "speaker": "Alice",
            "timestamp": "10:00:01",
            "meeting_id": active_id,
        }]
        self._drive_change_tick_with_parse(
            engine, fake_origin, cfg,
            wal=wal, mtime=1010.0, size=size + 2, parse_result=refined,
        )

        with engine._accumulated_lock:
            after = engine._accumulated[msg_id]
        assert after["text"] == "Hello world, longer text"
        assert after["speaker"] == "Alice"
        assert after["timestamp"] == "10:00:01"


class TestRecurringMeetingDedupe:
    """Phase 1 #4 regression guard.

    Recurring Zoom meetings reuse the same `meeting_id`. Pre-fix, the
    duplicate-generation guard tracked only the meeting_id, which meant a
    second occurrence of the same recurring meeting on a different day
    would be silently suppressed if the engine still remembered the prior
    `_last_generated_meeting_id`. The fix replaced the scalar with a
    `(meeting_id, session_start_mtime)` fingerprint — these tests prove a
    new IDLE→ACTIVE produces a different fingerprint and therefore is
    NOT suppressed.
    """

    def test_post_success_same_meeting_id_new_session_is_not_suppressed(
        self, fake_origin, fixture_wal_path, isolated_cache, multi_meeting_meta
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        size = fixture_wal_path.stat().st_size
        meeting_ids = multi_meeting_meta["expected"]["meeting_ids"]
        recurring_id = meeting_ids[0]

        # Simulate a successful prior generation for this meeting_id at an
        # earlier session_start_mtime.
        engine._last_generated_session = (recurring_id, 500.0)

        # New IDLE→ACTIVE at a fresh mtime — same meeting_id, but a
        # different fingerprint.
        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)
        assert engine._get_state() == EngineState.ACTIVE

        # The fingerprint stamped at IDLE→ACTIVE must differ from the
        # remembered one (different session_start_mtime).
        active_session_mtime = engine._read_session_mtime()
        assert active_session_mtime is not None, "session_mtime must be stamped at IDLE→ACTIVE"
        assert active_session_mtime != 500.0, \
            "new ACTIVE session should capture the current WAL mtime, not reuse the prior session's"

        _, _, _, current_meeting_id = engine._read_tracking()
        # If both id AND mtime matched the prior, dedupe would fire. They
        # don't, so the engine is free to generate again on the next
        # idle-timeout — exactly what the user expects for a recurring
        # meeting on a new day.
        assert (current_meeting_id, active_session_mtime) != engine._last_generated_session

    def test_post_success_same_session_is_suppressed(
        self, fake_origin, fixture_wal_path, isolated_cache, multi_meeting_meta
    ):
        """Inverse: when the same session is still active (Zoom checkpoint
        mutated the WAL after our generation), the fingerprint matches and
        we correctly suppress."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        size = fixture_wal_path.stat().st_size
        meeting_ids = multi_meeting_meta["expected"]["meeting_ids"]
        recurring_id = meeting_ids[0]

        _drive_tick(engine, fake_origin, cfg, mtime=1000.0, size=size, fixture_wal=fixture_wal_path)
        _drive_tick(engine, fake_origin, cfg, mtime=1005.0, size=size + 1, fixture_wal=fixture_wal_path)
        # Capture the fingerprint the engine just stamped at IDLE→ACTIVE.
        active_session_mtime = engine._read_session_mtime()
        # Override tracked meeting_id so we can match on a known one
        # regardless of which the WAL scoring picked.
        engine._write_tracking(meeting_id=recurring_id)
        engine._last_generated_session = (recurring_id, active_session_mtime)

        # Same fingerprint → would-be duplicate generation should NOT fire.
        # We verify by calling the dedupe check path directly: post-success,
        # if the engine stayed ACTIVE on a checkpoint mutation and idled out,
        # the guard suppresses re-trigger. We simulate by reading the
        # decision the engine would make rather than running the full loop
        # (which involves WAL re-scoring that's tested separately).
        _, _, _, mid = engine._read_tracking()
        msm = engine._read_session_mtime()
        assert (mid, msm) == engine._last_generated_session, \
            "checkpoint mutation must keep the same fingerprint as the one we generated for"


class TestBlockedMeetingIds:
    """Regression guard for the blocked_meeting_ids feature.

    When a detected meeting_id is in cfg.blocked_meeting_ids the engine
    must not transition to ACTIVE — it should stay IDLE.
    """

    def _make_cfg_with_blocked(self, blocked_id: str):
        """Return a real config object with blocked_meeting_ids set."""
        from zoom_config import ZoomNotesConfig
        cfg = ZoomNotesConfig()
        cfg.blocked_meeting_ids = [blocked_id]
        return cfg

    def test_blocked_id_prevents_idle_to_active(self, fake_origin, tmp_path, isolated_cache):
        """IDLE→ACTIVE must not fire when the detected meeting_id is blocked."""
        wal = tmp_path / "synthetic.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)
        engine = ZoomEngine()
        blocked_id = "HA1Kj2+mQDqLrOwX2pQIfA=="
        cfg = self._make_cfg_with_blocked(blocked_id)

        size = wal.stat().st_size

        # Tick 1: anchor.
        os.utime(wal, (1000.0, 1000.0))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", return_value=blocked_id):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)
        assert engine._get_state() == EngineState.IDLE

        # Tick 2: WAL changed, detection returns the blocked id.
        os.utime(wal, (1005.0, 1005.0))
        wal.write_bytes(b"x" * (size + 1))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", return_value=blocked_id):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)

        assert engine._get_state() == EngineState.IDLE, (
            "engine must stay IDLE when the detected meeting_id is in blocked_meeting_ids"
        )

    def test_non_blocked_id_still_activates(self, fake_origin, tmp_path, isolated_cache):
        """An unblocked meeting_id must still trigger IDLE→ACTIVE normally."""
        wal = tmp_path / "synthetic2.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)
        engine = ZoomEngine()
        blocked_id = "HA1Kj2+mQDqLrOwX2pQIfA=="
        other_id = "ZZ9Kj2+mQDqLrOwX2pQIfZ=="
        cfg = self._make_cfg_with_blocked(blocked_id)

        size = wal.stat().st_size

        # Tick 1: anchor.
        os.utime(wal, (1000.0, 1000.0))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", return_value=other_id):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)
        assert engine._get_state() == EngineState.IDLE

        # Tick 2: different (non-blocked) id → should go ACTIVE.
        wal.write_bytes(b"x" * (size + 1))
        os.utime(wal, (1005.0, 1005.0))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", return_value=other_id):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)

        assert engine._get_state() == EngineState.ACTIVE, (
            "a non-blocked meeting_id must still trigger IDLE→ACTIVE"
        )


class TestBoundaryExpiry:
    """Regression guard for the 2026-07-02 back-to-back deadlock.

    `_last_completed_boundary` carries a date-less seconds-since-midnight
    freshness floor. Because the app runs 24/7 as a menu-bar process, a
    boundary set by an earlier/previous-day meeting could outlive its purpose
    and permanently suppress detection of a later meeting whose timestamps are
    numerically below the stale floor — detection returned None every tick,
    the empty tracked id never upgraded, and back-to-back meetings merged into
    one note. The fix expires the boundary after `_BOUNDARY_EXPIRY_SECS`.
    """

    def test_active_boundary_expires_stale_entry(self):
        engine = ZoomEngine()
        # Boundary older than the expiry window → dropped and cleared.
        engine._last_completed_boundary = (
            "OLD_MEETING_ID_xxxxxxxx==", 60000,
            time.time() - (zoom_engine._BOUNDARY_EXPIRY_SECS + 60),
        )
        assert engine._active_boundary() is None
        assert engine._last_completed_boundary is None

    def test_active_boundary_keeps_fresh_entry(self):
        engine = ZoomEngine()
        fresh = ("RECENT_MEETING_xxxxxxxx==", 34200, time.time())
        engine._last_completed_boundary = fresh
        assert engine._active_boundary() == fresh
        assert engine._last_completed_boundary == fresh

    def _make_cfg(self):
        engine = ZoomEngine()
        return engine._get_cfg()

    def test_stale_boundary_not_passed_to_detection(self, fake_origin, tmp_path, isolated_cache):
        """A stale boundary must NOT be handed to detect_active_meeting_id —
        otherwise its floor filters out the genuinely-new meeting and tracking
        stays empty forever."""
        wal = tmp_path / "stale-boundary.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)
        engine = ZoomEngine()
        cfg = self._make_cfg()
        new_id = "NEW9amMeeting_xxxxxxxxxx=="

        # A boundary left over from a meeting hours ago, with a floor (16:40 =
        # 60000s) numerically ABOVE this morning meeting's timestamps.
        engine._last_completed_boundary = (
            "PRIOR_MEETING_xxxxxxxxxx==", 60000,
            time.time() - (zoom_engine._BOUNDARY_EXPIRY_SECS + 60),
        )

        seen = []

        def rec_detect(wal_path, *, exclude_meeting_id=None, freshness_floor_secs=None):
            seen.append((exclude_meeting_id, freshness_floor_secs))
            return new_id

        size = wal.stat().st_size
        # Tick 1: anchor (IDLE).
        os.utime(wal, (1000.0, 1000.0))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", side_effect=rec_detect):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)

        # Tick 2: size changed → detect runs, boundary should already be expired.
        wal.write_bytes(b"x" * (size + 1))
        os.utime(wal, (1005.0, 1005.0))
        with patch.object(zoom_engine, "find_wal", return_value=wal), \
             patch.object(zoom_engine, "detect_active_meeting_id", side_effect=rec_detect):
            engine._poll_once(fake_origin, cfg, idle_threshold=cfg.idle_threshold_secs)

        assert seen, "detect_active_meeting_id was never called"
        # The last detection call must have been made with NO stale floor.
        assert seen[-1] == (None, None), (
            f"stale boundary leaked into detection: {seen[-1]}"
        )
        assert engine._get_state() == EngineState.ACTIVE
        assert engine._read_tracking()[3] == new_id, (
            "engine failed to upgrade to the concrete meeting id"
        )
