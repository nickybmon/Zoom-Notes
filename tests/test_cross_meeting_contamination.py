"""Regression tests for the 2026-04-27 cross-meeting contamination bug.

The original symptom: a meeting from earlier in the day ("Anna Punihaole at
13:07") kept appearing in the menu bar's "Recover unfinished meeting"
submenu for hours, surfaced fresh on every engine restart, and on
inspection the cached snapshot keyed under that meeting's slug actually
contained entries from BOTH meetings — Anna's and a later meeting with
different speakers. This had two underlying causes:

  1. When `_poll_once` reevaluated the active meeting mid-session and
     switched from meeting A to meeting B, it cleared the in-memory
     accumulator but left A's on-disk snapshot in `~/.cache/zoom-notes/`
     forever (or until a 24h purge that never reliably fired because
     subsequent persists for a different slug didn't bump A's mtime).
     That orphan was the source of the persistent "Recover unfinished
     meeting" ghost.

  2. Before reevaluation flipped the active ID, the engine's permissive
     accumulator update happily added entries from BOTH meetings to the
     in-memory dict, then `persist_accumulator` wrote that mixed bag to
     disk under whichever meeting ID the engine currently believed was
     active. So even a fresh "live" snapshot under meeting B's slug
     could contain Anna's entries — feeding contaminated transcripts
     into the LLM if the user clicked Retry.

These tests guard both behaviours independently so a future refactor
can't silently re-open the door. They use a synthetic WAL (no fixture
needed) so they always run in CI / fresh checkouts.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import EngineState, ZoomEngine


# ── Shared harness (mirrors test_engine_persistence.py for consistency) ──


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


def _set_wal_size(wal: Path, size: int) -> None:
    with open(wal, "r+b") as f:
        f.truncate(size)


def _make_entry(msg_id: str, text: str, *, meeting_id: str, speaker: str = "Test User",
                timestamp: str = "12:00:00") -> dict:
    return {
        "msg_id": msg_id,
        "text": text,
        "speaker": speaker,
        "timestamp": timestamp,
        "meeting_id": meeting_id,
    }


def _drive_tick(
    engine: ZoomEngine,
    origin: Path,
    cfg,
    *,
    wal: Path,
    mtime: float,
    size: int,
    entries: list[dict],
    detected_meeting_id: str,
) -> None:
    """One _poll_once tick with full control over WAL stat, parser output,
    and what `detect_active_meeting_id` says is the best-scoring meeting."""
    _set_wal_size(wal, size)
    os.utime(wal, (mtime, mtime))
    with patch.object(zoom_engine, "find_wal", return_value=wal), \
         patch.object(zoom_engine, "parse_transcript", return_value=entries), \
         patch.object(zoom_engine, "detect_active_meeting_id", return_value=detected_meeting_id):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)


# ── Persistence-layer filter (Fix 2) ─────────────────────────────────────


class TestPersistenceFiltersByMeetingId:
    """`persist_accumulator(meeting_id, entries)` is the boundary between
    the permissive in-memory accumulator and the on-disk snapshot. The
    in-memory dict may legitimately hold entries from multiple meetings
    during the brief window where active-reevaluation hasn't caught up
    yet, but the snapshot must only contain entries belonging to (or
    plausibly belonging to) the keying meeting."""

    def test_persist_drops_entries_with_mismatched_meeting_id(self, isolated_cache):
        entries = {
            "m1": _make_entry("m1", "anna talking",      meeting_id="meetingA"),
            "m2": _make_entry("m2", "anna talking more", meeting_id="meetingA"),
            "m3": _make_entry("m3", "alex talking",      meeting_id="meetingB", speaker="Alex"),
            "m4": _make_entry("m4", "nick replying",     meeting_id="meetingB", speaker="Nick"),
        }
        zoom_notes.persist_accumulator("meetingB", entries)

        snapshot_path = isolated_cache / "in-progress-meetingB.json"
        assert snapshot_path.exists()
        import json as _json
        persisted = _json.loads(snapshot_path.read_text())
        ids = {e["meeting_id"] for e in persisted}
        assert ids == {"meetingB"}, (
            f"persist_accumulator must drop entries from other meetings; "
            f"snapshot contains: {ids}"
        )
        assert {e["msg_id"] for e in persisted} == {"m3", "m4"}

    def test_persist_keeps_entries_with_no_meeting_id(self, isolated_cache):
        """Early WAL pages may not yet carry the meetingId field. Those
        entries must be kept — they're almost always part of the active
        meeting (msg_id ordering will reconcile when the field arrives)
        and dropping them would silently lose real utterances."""
        entries = {
            "m1": _make_entry("m1", "no id yet",   meeting_id=""),
            "m2": _make_entry("m2", "now has id",  meeting_id="meetingB"),
        }
        # `meeting_id=""` is what `_make_entry` produces when we want None-equivalent.
        # Use literal None to be unambiguous about the field semantics.
        entries["m1"]["meeting_id"] = None
        zoom_notes.persist_accumulator("meetingB", entries)

        snapshot_path = isolated_cache / "in-progress-meetingB.json"
        import json as _json
        persisted = _json.loads(snapshot_path.read_text())
        msg_ids = {e["msg_id"] for e in persisted}
        assert msg_ids == {"m1", "m2"}, (
            f"persist must preserve entries with missing meeting_id; got {msg_ids}"
        )

    def test_persist_human_readable_md_only_contains_kept_entries(self, isolated_cache):
        """The .md mirror is what users see in the menu bar's preview and
        what crash recovery hands to support. It must not include
        cross-meeting entries either."""
        entries = {
            "m1": _make_entry("m1", "anna stale",   meeting_id="meetingA", speaker="Anna"),
            "m2": _make_entry("m2", "alex current", meeting_id="meetingB", speaker="Alex"),
        }
        zoom_notes.persist_accumulator("meetingB", entries)

        md_path = isolated_cache / "in-progress-meetingB.md"
        body = md_path.read_text()
        assert "Anna" not in body, (
            "human-readable mirror must not include cross-meeting speaker"
        )
        assert "Alex" in body
        assert "alex current" in body

    def test_in_memory_accumulator_unchanged_by_persist(self, isolated_cache):
        """The filter is at the persistence boundary — the caller's dict
        must NOT be mutated, so the in-memory accumulator stays permissive
        (preserving the contract from test_no_silent_drop_when_meeting_id_is_stale)."""
        entries = {
            "m1": _make_entry("m1", "from A", meeting_id="meetingA"),
            "m2": _make_entry("m2", "from B", meeting_id="meetingB"),
        }
        snapshot_before = {k: dict(v) for k, v in entries.items()}
        zoom_notes.persist_accumulator("meetingB", entries)

        assert entries == snapshot_before, (
            "persist_accumulator must not mutate the caller's accumulator dict"
        )


# ── Engine-layer orphan cleanup (Fix 1) ──────────────────────────────────


class TestActiveReevaluationDeletesOrphanSnapshot:
    """When the engine is ACTIVE under meeting A and the WAL begins to
    score meeting B higher, `_poll_once` switches the tracked meeting and
    clears the in-memory accumulator. It must ALSO delete A's on-disk
    snapshot — leaving it would surface as a perpetual "Recover
    unfinished meeting" item in the menu bar."""

    def test_old_snapshot_deleted_when_meeting_switches_mid_active(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        # Tick 1: anchor (IDLE).
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
            detected_meeting_id="meetingA",
        )
        # Tick 2: WAL grows, IDLE → ACTIVE under meetingA. Accumulator
        # picks up meetingA's entry and persists it under meetingA's slug.
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("a1", "anna talking", meeting_id="meetingA", speaker="Anna")],
            detected_meeting_id="meetingA",
        )
        assert engine._get_state() == EngineState.ACTIVE
        old_snapshot = isolated_cache / "in-progress-meetingA.json"
        assert old_snapshot.exists(), (
            "precondition: meetingA's snapshot should exist after first ACTIVE tick"
        )

        # Tick 3: WAL changes again and now scores meetingB higher. The
        # reevaluation branch fires: meetingA's snapshot must be removed.
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1010.0, size=100_002,
            entries=[
                _make_entry("a1", "anna talking",  meeting_id="meetingA", speaker="Anna"),
                _make_entry("b1", "alex starting", meeting_id="meetingB", speaker="Alex"),
            ],
            detected_meeting_id="meetingB",
        )

        _, _, _, current_id = engine._read_tracking()
        assert current_id == "meetingB", "engine should have switched to meetingB"
        assert not old_snapshot.exists(), (
            "active-reevaluation must delete the prior meeting's on-disk snapshot; "
            f"orphan still present at {old_snapshot}"
        )

    def test_new_meeting_snapshot_does_not_contain_old_meeting_entries(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """The combined fix: after reevaluation switches A → B, the snapshot
        keyed under B must only contain B's entries, even though the WAL
        still has A's entries in it (which the in-memory accumulator
        legitimately captures during the same tick)."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
            detected_meeting_id="meetingA",
        )
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("a1", "anna talking", meeting_id="meetingA", speaker="Anna")],
            detected_meeting_id="meetingA",
        )
        # Tick 3: reevaluation to meetingB; WAL still contains A's entry.
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1010.0, size=100_002,
            entries=[
                _make_entry("a1", "anna talking",  meeting_id="meetingA", speaker="Anna"),
                _make_entry("b1", "alex starting", meeting_id="meetingB", speaker="Alex"),
            ],
            detected_meeting_id="meetingB",
        )

        new_snapshot = isolated_cache / "in-progress-meetingB.json"
        assert new_snapshot.exists(), "meetingB snapshot should have been written"
        import json as _json
        persisted = _json.loads(new_snapshot.read_text())
        ids = {e["meeting_id"] for e in persisted}
        assert ids == {"meetingB"}, (
            f"meetingB's snapshot must not contain meetingA entries; "
            f"got meeting_ids={ids}, msg_ids={[e['msg_id'] for e in persisted]}"
        )

    def test_in_memory_accumulator_still_permissive_after_reevaluation(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        """Belt-and-suspenders: confirm we did NOT regress the
        permissiveness contract. After reevaluation flips A → B, the
        in-memory accumulator may still hold A's entries (which the
        WAL still contains and the parser still emits). The contract is
        that disk persistence drops them, not that memory does.

        Note: in this specific harness the IDLE→ACTIVE branch ALSO clears
        the accumulator and re-seeds, so the in-memory state right after
        reevaluation contains only what's been re-added since the clear.
        That's fine — what matters is that nothing in the engine code
        path actively *filters out* meetingA entries before they hit the
        in-memory dict."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()

        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
            detected_meeting_id="meetingA",
        )
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("a1", "anna talking", meeting_id="meetingA", speaker="Anna")],
            detected_meeting_id="meetingA",
        )
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1010.0, size=100_002,
            entries=[
                _make_entry("a1", "anna talking",  meeting_id="meetingA", speaker="Anna"),
                _make_entry("b1", "alex starting", meeting_id="meetingB", speaker="Alex"),
            ],
            detected_meeting_id="meetingB",
        )

        with engine._accumulated_lock:
            ids_in_acc = {e.get("meeting_id") for e in engine._accumulated.values()}
        # After reevaluation clears the accumulator, the same tick re-adds
        # entries from parse_transcript (which returns BOTH meetings). The
        # accumulator should hold both — that's the permissive contract.
        assert "meetingA" in ids_in_acc and "meetingB" in ids_in_acc, (
            f"in-memory accumulator must remain permissive after reevaluation; "
            f"got {ids_in_acc}"
        )


# ── Late-joiner gate (2026-04-29) ────────────────────────────────────────


class TestLateJoinerDoesNotTriggerGeneration:
    """Regression tests for the 2026-04-29 contamination.

    Scenario: user joined Zoom alone, AI Notetaker initialized which made
    the WAL "active" from the engine's perspective. The other participant
    was 2 minutes late, so no real transcript entries arrived. The 90s
    idle threshold elapsed and `_trigger_generate` fired anyway. The
    accumulator at that moment held only stale cross-meeting entries
    (Anna Punihaole [13:07:23] from a meeting hours earlier — Zoom keeps
    old utterances in WAL pages until checkpoint), and the LLM produced
    a "Mike Nick 11" note with Anna as the only speaker.

    Post-fix: a gate before `_trigger_generate` checks the accumulator
    for at least one entry matching `active_meeting_id`. Without one,
    the engine disarms and returns to IDLE rather than handing stale
    data to the LLM.
    """

    def test_idle_does_not_trigger_when_accumulator_has_only_stale_entries(
        self, fake_origin, synthetic_wal, isolated_cache
    ):
        from unittest.mock import MagicMock

        engine = ZoomEngine()
        cfg = engine._get_cfg()
        # Sentinel: any call to _trigger_generate is a regression of the
        # 2026-04-29 bug. Replace with a MagicMock that fails loudly.
        engine._trigger_generate = MagicMock(side_effect=AssertionError(
            "engine must NOT call _trigger_generate when the active meeting "
            "has no entries in the accumulator (late-joiner / WAL-noise gate)"
        ))

        # Tick 1: anchor (no state change yet).
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
            detected_meeting_id="active_meeting",
        )

        # Tick 2: WAL grows; engine flips IDLE → ACTIVE under
        # "active_meeting". But the parser only returns a stale entry from
        # a DIFFERENT meeting (the leftover Anna fragment scenario).
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("stale1", "anna talking",
                                 meeting_id="stale_meeting", speaker="Anna")],
            detected_meeting_id="active_meeting",
        )
        assert engine._get_state() == EngineState.ACTIVE
        # Sanity: accumulator has the stale entry, none for active_meeting.
        with engine._accumulated_lock:
            assert any(e.get("meeting_id") == "stale_meeting"
                       for e in engine._accumulated.values()), \
                "stale entry should be in the accumulator (permissive contract)"
            assert not any(e.get("meeting_id") == "active_meeting"
                           for e in engine._accumulated.values()), \
                "no real content for active_meeting yet"

        # Tick 3: WAL stops changing (late participant still hasn't
        # arrived) and we age `_last_active_ts` past the idle threshold.
        # The "not changed" branch should evaluate the gate and bail out
        # cleanly instead of calling _trigger_generate.
        engine._write_tracking(active_ts=0.0)  # ancient ts → idle elapsed
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("stale1", "anna talking",
                                 meeting_id="stale_meeting", speaker="Anna")],
            detected_meeting_id="active_meeting",
        )

        engine._trigger_generate.assert_not_called()
        assert engine._get_state() == EngineState.IDLE, \
            "gate must disarm to IDLE so the next WAL change can re-arm cleanly"
        _, _, active_ts, _ = engine._read_tracking()
        assert active_ts is None, \
            "active_ts must be cleared so we don't immediately re-fire on next idle check"

    def test_idle_does_trigger_when_accumulator_has_real_content(
        self, fake_origin, synthetic_wal, isolated_cache, monkeypatch
    ):
        """Inverse: the gate must NOT block a legitimate idle-out where
        the active meeting actually had content. This is the normal,
        post-meeting auto-summarize flow."""
        from unittest.mock import MagicMock

        engine = ZoomEngine()
        cfg = engine._get_cfg()
        engine._trigger_generate = MagicMock()

        # Provide an API key so the existing API-key check (which runs
        # AFTER the new gate) doesn't return early and mask a regression.
        import zoom_config
        monkeypatch.setattr(zoom_config, "get_api_key", lambda *_: "sk-test")

        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1000.0, size=100_000, entries=[],
            detected_meeting_id="active_meeting",
        )
        # Tick 2: WAL grows AND parser returns real content for active_meeting.
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("a1", "real conversation",
                                 meeting_id="active_meeting", speaker="Alex")],
            detected_meeting_id="active_meeting",
        )
        # Sanity: accumulator has at least one entry for active_meeting.
        with engine._accumulated_lock:
            assert any(e.get("meeting_id") == "active_meeting"
                       for e in engine._accumulated.values())

        # Tick 3: idle elapsed.
        engine._write_tracking(active_ts=0.0)
        _drive_tick(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=1005.0, size=100_001,
            entries=[_make_entry("a1", "real conversation",
                                 meeting_id="active_meeting", speaker="Alex")],
            detected_meeting_id="active_meeting",
        )
        assert engine._trigger_generate.called, (
            "gate must allow trigger when accumulator has real content for "
            "active meeting"
        )
        assert engine._trigger_generate.call_count == 1


class TestCollectEntriesForGenerationFiltersStrictly:
    """Direct unit tests on `_collect_entries_for_generation`. Must NEVER
    fall back to the unfiltered accumulator when the meeting-id filter
    returns empty — that fallback was the 2026-04-29 contamination bug
    where stale cross-meeting WAL entries became the entire transcript
    handed to the LLM."""

    def test_returns_empty_when_no_entries_match_active_meeting(self, isolated_cache):
        engine = ZoomEngine()
        with engine._accumulated_lock:
            engine._accumulated = {
                "stale1": _make_entry("stale1", "anna",
                                      meeting_id="meetingA", speaker="Anna"),
                "stale2": _make_entry("stale2", "more anna",
                                      meeting_id="meetingA", speaker="Anna"),
            }
        result = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="meetingB"
        )
        assert result == [], (
            "must return [] when no accumulator entries match the active "
            "meeting; falling back to the unfiltered snapshot was the "
            "2026-04-29 contamination bug"
        )

    def test_returns_filtered_entries_when_some_match(self, isolated_cache):
        engine = ZoomEngine()
        with engine._accumulated_lock:
            engine._accumulated = {
                "stale1": _make_entry("stale1", "anna",
                                      meeting_id="meetingA", speaker="Anna"),
                "real1":  _make_entry("real1", "alex",
                                      meeting_id="meetingB", speaker="Alex"),
            }
        result = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="meetingB"
        )
        assert len(result) == 1
        assert result[0]["msg_id"] == "real1"
        assert result[0]["meeting_id"] == "meetingB"

    def test_includes_entries_with_no_meeting_id(self, isolated_cache):
        """Early-WAL-page entries (meeting_id=None) must be kept — they're
        almost certainly part of the active meeting (the meetingId field
        just hasn't been written yet) and dropping them would lose real
        opening utterances. This mirrors `persist_accumulator`'s rule."""
        engine = ZoomEngine()
        with engine._accumulated_lock:
            engine._accumulated = {
                "early1": {**_make_entry("early1", "hello",
                                         meeting_id="x", speaker="Alex"),
                           "meeting_id": None},
                "real1":  _make_entry("real1", "thanks",
                                      meeting_id="meetingB", speaker="Alex"),
            }
        result = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="meetingB"
        )
        msg_ids = {e["msg_id"] for e in result}
        assert msg_ids == {"early1", "real1"}, (
            "entries with meeting_id=None (early WAL pages) must be kept "
            "alongside entries that match the active_meeting_id"
        )
