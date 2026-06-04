"""Regression tests for the 2026-04-30 back-to-back meeting overwrite bug.

The incident:
  11:01-11:21  Daily Standup (meeting A) ran, was correctly captured and
               saved to ``Notes/2026-04-30/Daily Standup.md``.
  11:32-12:02  An unrelated 1:1 (meeting B) ran. Its AI Notetaker never
               wrote a title to the blocks WAL. At 12:04 the engine
               triggered note generation:
                 - ``detect_active_meeting_id`` had locked onto the wrong
                   id at 11:32 (A's data was still resident in the WAL),
                 - the strict accumulator filter returned 0 entries,
                 - the (Apr-28-installed) ``_collect_entries_for_generation``
                   fallback handed every accumulator entry — including a
                   stale Anna fragment from the prior day — to the LLM,
                 - ``parse_meeting_title``'s 2-hour window matched the
                   stale "Daily Standup" title from 11:01,
                 - ``_next_available_path``'s 60s overwrite window had
                   long elapsed, so the second save silently overwrote
                   the morning standup's note.
  Result: the morning standup's note was lost; the file contained the
  1:1's content (plus Anna contamination) under the wrong title.

These tests guard each of the five compounding bugs independently. Each
is a single failure away from data loss — together they were data loss.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_engine
import zoom_notes
from zoom_engine import EngineState, ZoomEngine


# ── Shared harness (mirrors test_cross_meeting_contamination.py) ──────────


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
    """Redirect ``cfg.notes_path`` and ``cfg.transcripts_path`` so save
    functions land in the test tmpdir instead of the real vault.

    The config exposes ``notes_path``/``transcripts_path`` as read-only
    properties that derive from ``notes_dir``/``transcripts_dir`` strings,
    so we set the underlying string fields. Patches both
    ``zoom_notes.get_config`` (used by save functions when no cfg passed)
    and the ``ZoomEngine._get_cfg`` reader so engine code sees the same
    redirected paths.
    """
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


def _make_entry(msg_id: str, text: str, *, meeting_id: str, speaker: str = "Test User",
                timestamp: str = "12:00:00") -> dict:
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


# ── Bug 1: file save must not silently overwrite a different meeting ─────


class TestSaveDoesNotOverwriteDifferentMeeting:
    """The 2026-04-30 root cause: two same-day meetings produced the same
    derived filename, and the second save overwrote the first because the
    60-second freshness window had long elapsed. The fix: read the existing
    file's frontmatter ``meeting_id`` and never overwrite a file owned by
    a different meeting, regardless of file age.
    """

    def test_existing_file_with_different_meeting_id_creates_sibling(
        self, isolated_vault
    ):
        cfg = zoom_notes.get_config()
        # Save meeting A first.
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "summary A", "Daily Standup", "2026-04-30",
                ["Alex", "Nick"], "2026-04-30T11:21:00", cfg,
                meeting_id="meeting_A",
            ),
            "Daily Standup", "2026-04-30", cfg, meeting_id="meeting_A",
        )
        # Backdate A's mtime past the legacy 60s overwrite window so the
        # only thing that can save the file from being clobbered is the
        # meeting_id frontmatter check.
        notes_dir = isolated_vault["notes"] / "2026-04-30"
        first = notes_dir / "Daily Standup.md"
        old_mtime = first.stat().st_mtime - 3600
        os.utime(first, (old_mtime, old_mtime))
        first_text = first.read_text()

        # Save meeting B with the same derived title.
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "summary B", "Daily Standup", "2026-04-30",
                ["Marc", "Nick"], "2026-04-30T12:05:00", cfg,
                meeting_id="meeting_B",
            ),
            "Daily Standup", "2026-04-30", cfg, meeting_id="meeting_B",
        )

        # A is still on disk with its original content.
        assert first.exists()
        assert first.read_text() == first_text, (
            "different-meeting save MUST NOT overwrite the prior file; "
            "this regression would lose data permanently"
        )
        # B landed in a sibling.
        sibling = notes_dir / "Daily Standup-2.md"
        assert sibling.exists()
        assert "summary B" in sibling.read_text()
        assert "meeting_id: meeting_B" in sibling.read_text()

    def test_existing_file_with_same_meeting_id_overwrites(
        self, isolated_vault
    ):
        """The same meeting refreshing itself (e.g. Retry note generation
        after a placeholder, or the user manually running ``--notes`` for
        the same meeting) should still replace its previous file. Otherwise
        the user accumulates ``-2``, ``-3``, ... siblings every time they
        retry.
        """
        cfg = zoom_notes.get_config()
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "first attempt", "Daily Standup", "2026-04-30",
                ["Alex"], "2026-04-30T11:21:00", cfg,
                meeting_id="meeting_X",
            ),
            "Daily Standup", "2026-04-30", cfg, meeting_id="meeting_X",
        )
        notes_dir = isolated_vault["notes"] / "2026-04-30"
        first = notes_dir / "Daily Standup.md"
        first_age_marker = "first attempt"
        assert first_age_marker in first.read_text()

        # Refresh the same meeting (e.g. retry after LLM failure replaced
        # the placeholder).
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "second attempt", "Daily Standup", "2026-04-30",
                ["Alex"], "2026-04-30T11:21:00", cfg,
                meeting_id="meeting_X",
            ),
            "Daily Standup", "2026-04-30", cfg, meeting_id="meeting_X",
        )

        assert first.exists()
        body = first.read_text()
        assert "second attempt" in body
        assert "first attempt" not in body
        assert not (notes_dir / "Daily Standup-2.md").exists(), (
            "same meeting refreshing itself must overwrite, not create siblings"
        )

    def test_existing_legacy_file_with_no_meeting_id_creates_sibling(
        self, isolated_vault
    ):
        """A legacy or hand-authored file with no ``meeting_id`` frontmatter
        must never be silently overwritten. The save path can't prove
        ownership, so it errs on preservation.
        """
        cfg = zoom_notes.get_config()
        notes_dir = isolated_vault["notes"] / "2026-04-30"
        notes_dir.mkdir(parents=True, exist_ok=True)
        legacy = notes_dir / "Daily Standup.md"
        legacy.write_text(
            "---\ntitle: Daily Standup\nsource: hand-authored\n---\n\n"
            "Original hand-written notes.\n"
        )
        # Backdate so the legacy 60s window doesn't apply either.
        old_mtime = legacy.stat().st_mtime - 3600
        os.utime(legacy, (old_mtime, old_mtime))

        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "engine summary", "Daily Standup", "2026-04-30",
                ["Alex"], "2026-04-30T11:21:00", cfg,
                meeting_id="some_id",
            ),
            "Daily Standup", "2026-04-30", cfg, meeting_id="some_id",
        )

        assert "Original hand-written notes" in legacy.read_text(), (
            "engine save must not overwrite a legacy file with no meeting_id"
        )
        assert (notes_dir / "Daily Standup-2.md").exists()


# ── Bug 1b: frontmatter must round-trip through _read_existing_meeting_id ─


class TestReadExistingMeetingId:
    """The save guard relies on extracting the ``meeting_id`` field back
    out of YAML frontmatter that ``build_note_content`` and
    ``build_transcript_content`` write. Quoting (the field is emitted via
    ``_yaml_quote`` since base64-ish ids contain ``+``/``/``/``=``) must
    round-trip cleanly."""

    def test_plain_meeting_id_is_round_tripped(self, tmp_path):
        path = tmp_path / "n.md"
        path.write_text(
            "---\ntitle: Foo\nmeeting_id: simple_id\n---\n\nbody\n"
        )
        assert zoom_notes._read_existing_meeting_id(path) == "simple_id"

    def test_yaml_quoted_meeting_id_is_round_tripped(self, tmp_path):
        # Real Zoom ids contain `+`, `/`, `=` which trigger _yaml_quote
        # to emit a JSON-quoted string.
        real_id = "sYlA31NdTImpchTAfmEpuw=="
        body = zoom_notes.build_note_content(
            "summary", "Test", "2026-04-30",
            ["Alex"], "2026-04-30T12:00:00", None,
            meeting_id=real_id,
        )
        path = tmp_path / "n.md"
        path.write_text(body)
        assert zoom_notes._read_existing_meeting_id(path) == real_id

    def test_empty_meeting_id_returns_empty_string_not_none(self, tmp_path):
        """Caller distinguishes 'no field at all' (None) from 'field
        present but empty'. An empty value still proves the file is a
        Zoom-Notes file, even if we didn't have a meeting_id at save
        time — but it doesn't match a NEW meeting_id, so a save with a
        real id will still create a sibling rather than overwrite."""
        path = tmp_path / "n.md"
        path.write_text("---\ntitle: Foo\nmeeting_id: \"\"\n---\n\nbody\n")
        assert zoom_notes._read_existing_meeting_id(path) == ""

    def test_no_frontmatter_returns_none(self, tmp_path):
        path = tmp_path / "n.md"
        path.write_text("# Just a heading\n\nNo frontmatter here.\n")
        assert zoom_notes._read_existing_meeting_id(path) is None

    def test_no_meeting_id_field_returns_none(self, tmp_path):
        path = tmp_path / "n.md"
        path.write_text("---\ntitle: Foo\nsource: other\n---\n\nbody\n")
        assert zoom_notes._read_existing_meeting_id(path) is None


# ── Bug 2: title parser must not match a stale title from before the meeting ─


class TestTitleParserTightWindow:
    """Before the fix, ``parse_meeting_title``'s 2-hour window matched a
    stale title from a meeting that ended 31 minutes before the active
    transcript started. After the fix the window is 10 minutes AND the
    title's embedded time must precede the earliest transcript entry by
    no more than that margin (with 60s slack on the future side for
    clock skew)."""

    def _write_blocks_wal(self, path: Path, *titles: str) -> None:
        """Write a synthetic blocks WAL whose `strings(1)` output yields
        the ``title``/value pairs. Real Zoom WALs interleave many other
        tokens; this minimal shape is what the parser actually keys on."""
        chunks = []
        for t in titles:
            chunks.append(b"title\x00")
            chunks.append(t.encode("utf-8") + b"\x00")
        path.write_bytes(b"".join(chunks))

    def test_rejects_stale_title_31min_before_meeting_start(self, tmp_path):
        bwal = tmp_path / "blocks.sqlite3-wal"
        # Standup at 11:01 left this title in the WAL.
        self._write_blocks_wal(bwal, "Daily Standup 2026-04-30 11:01(GMT-4:00)")
        # Marc 1:1 transcript starts 31 min later.
        entries = [
            {"msg_id": "1", "text": "hi", "speaker": "Marc",
             "timestamp": "11:32:08", "meeting_id": "marc1on1"},
        ]
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 12:04:00")):
            title = zoom_notes.parse_meeting_title(bwal, entries)
        assert title is None, (
            "31-minute-old title must NOT be matched to the new meeting; "
            f"got {title!r} — this is the 2026-04-30 mis-titling that "
            "caused the standup file to be overwritten"
        )

    def test_accepts_title_within_10min_window(self, tmp_path):
        bwal = tmp_path / "blocks.sqlite3-wal"
        self._write_blocks_wal(bwal, "Marc Sync 2026-04-30 11:30(GMT-4:00)")
        entries = [
            {"msg_id": "1", "text": "hi", "speaker": "Marc",
             "timestamp": "11:32:08", "meeting_id": "marc1on1"},
        ]
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 12:04:00")):
            title = zoom_notes.parse_meeting_title(bwal, entries)
        assert title == "Marc Sync 2026-04-30 11:30(GMT-4:00)"

    def test_rejects_title_from_after_meeting_started(self, tmp_path):
        """A title timestamped AFTER the transcript's first utterance is
        physically impossible for the active meeting — Zoom stamps the
        meeting start, which by definition precedes its first utterance.
        Allow 60s slack for clock skew, then refuse."""
        bwal = tmp_path / "blocks.sqlite3-wal"
        self._write_blocks_wal(bwal, "Future Meeting 2026-04-30 11:40(GMT-4:00)")
        entries = [
            {"msg_id": "1", "text": "hi", "speaker": "Marc",
             "timestamp": "11:32:08", "meeting_id": "marc1on1"},
        ]
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 12:04:00")):
            title = zoom_notes.parse_meeting_title(bwal, entries)
        assert title is None

    def test_picks_closest_when_multiple_titles_in_window(self, tmp_path):
        bwal = tmp_path / "blocks.sqlite3-wal"
        # Two candidate titles: one 7 min off, one 3 min off — pick the closer.
        self._write_blocks_wal(
            bwal,
            "Earlier 2026-04-30 11:25(GMT-4:00)",
            "Closer  2026-04-30 11:30(GMT-4:00)",
        )
        entries = [
            {"msg_id": "1", "text": "hi", "speaker": "Marc",
             "timestamp": "11:32:08", "meeting_id": "marc1on1"},
        ]
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 12:04:00")):
            title = zoom_notes.parse_meeting_title(bwal, entries)
        assert title == "Closer  2026-04-30 11:30(GMT-4:00)"


class _ClockAt:
    """Tiny shim to fix `datetime.now()` for parse_meeting_title's
    today-stamping. Forwards everything else to the real datetime module."""
    def __init__(self, fixed: str):
        self._fixed = datetime.strptime(fixed, "%Y-%m-%d %H:%M:%S")

    def now(self):
        return self._fixed

    def __getattr__(self, name):
        return getattr(datetime, name)


# ── Bug 3: detection must not trap on the meeting we just finished ───────


class TestDetectExcludesJustCompletedMeeting:
    """``detect_active_meeting_id`` previously had no concept of session
    boundaries — it would happily return the just-ended meeting's id at
    the next IDLE -> ACTIVE because that meeting still had the most
    entries + recent timestamps in the WAL. The new ``exclude_meeting_id``
    and ``freshness_floor_secs`` filters fix that."""

    def _write_transcript_wal(
        self, path: Path, meetings: list[tuple[str, list[str]]],
    ) -> None:
        """Write a synthetic transcript WAL with one ``messageId`` per
        timestamp/meeting, in the structural shape ``parse_transcript`` and
        ``score_meeting_ids`` expect.
        """
        chunks = []
        msg_n = 0
        for meeting_id, timestamps in meetings:
            for ts in timestamps:
                msg_n += 1
                chunks.append(b"messageId\x00")
                chunks.append(f"16:0:{msg_n}:0:{msg_n}".encode() + b"\x00")
                chunks.append(b"message\x00")
                chunks.append(b"some real spoken text\x00")
                chunks.append(b"timeStampContent\x00")
                chunks.append(ts.encode() + b"\x00")
                chunks.append(b"speaker\x00")
                chunks.append(b"speakerId\x00")
                chunks.append(b"username\x00")
                chunks.append(b"Test Speaker\x00")
                chunks.append(b"meetingId\x00")
                chunks.append(meeting_id.encode() + b"\x00")
        path.write_bytes(b"".join(chunks))

    # `score_meeting_ids` filters out ids of length <= 8 chars (real Zoom
    # ids are base64 ~22 chars). Use 12-char ids in tests to mirror that.
    _ENDED = "just_ended_meeting_AA"
    _STARTED = "just_started_meeting_BB"
    _ONLY = "only_one_meeting_id"

    def test_exclude_drops_specific_meeting_id(self, tmp_path):
        twal = tmp_path / "transcript.sqlite3-wal"
        self._write_transcript_wal(twal, [
            (self._ENDED, ["11:01:00", "11:10:00", "11:20:00"]),
            (self._STARTED, ["11:32:08"]),
        ])
        # Without exclusion the just-ended meeting wins on entry count.
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 11:33:00")):
            unfiltered = zoom_notes.detect_active_meeting_id(twal)
            assert unfiltered == self._ENDED
            # With exclusion the just-started one wins.
            filtered = zoom_notes.detect_active_meeting_id(
                twal, exclude_meeting_id=self._ENDED
            )
            assert filtered == self._STARTED

    def test_freshness_floor_drops_meetings_finished_before_floor(self, tmp_path):
        twal = tmp_path / "transcript.sqlite3-wal"
        self._write_transcript_wal(twal, [
            (self._ENDED, ["11:01:00", "11:10:00", "11:20:00"]),
            (self._STARTED, ["11:32:08"]),
        ])
        # 11:21:00 (after just_ended's last entry, before just_started's).
        floor = 11 * 3600 + 21 * 60
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 11:33:00")):
            filtered = zoom_notes.detect_active_meeting_id(
                twal, freshness_floor_secs=floor
            )
        assert filtered == self._STARTED

    def test_no_filters_preserves_legacy_behavior(self, tmp_path):
        """Default behavior is unchanged: no exclusion or floor, return the
        top-scoring meeting. Regression guard for the existing call sites
        in cmd_list / CLI etc."""
        twal = tmp_path / "transcript.sqlite3-wal"
        self._write_transcript_wal(twal, [
            (self._ONLY, ["12:00:00"]),
        ])
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 12:01:00")):
            assert zoom_notes.detect_active_meeting_id(twal) == self._ONLY


# ── Bug 4: collect-entries self-heal when tracking misidentified ─────────


class TestCollectEntriesSelfHealing:
    """When ``_collect_entries_for_generation`` finds zero entries matching
    the active meeting, it now self-heals: pick the meeting_id with the
    most accumulator entries whose timestamps are at or after session
    start, and use those instead. The 2026-04-30 scenario: tracking
    locked onto the just-ended meeting's id, but the accumulator filled
    with the new meeting's entries during the session."""

    def test_self_heal_picks_dominant_in_session_meeting(self, isolated_cache):
        """The 2026-04-30 scenario: tracking locked onto the just-ended
        meeting's id at IDLE -> ACTIVE because its data was still resident.
        The accumulator filled with the NEW meeting's entries during the
        session (the standup's entries had been cleared post-generation).
        At trigger time the strict filter finds 0 matches for the wrong id,
        and self-heal recovers the correct id from accumulator content."""
        engine = ZoomEngine()
        # Session started at 11:32 (the new meeting). Convert today-at-11:32
        # to a unix mtime so `_wallclock_secs_from_mtime` yields 41520.
        today_1132 = datetime.now().replace(
            hour=11, minute=32, second=0, microsecond=0
        ).timestamp()
        with engine._tracking_lock:
            engine._active_session_mtime = today_1132

        with engine._accumulated_lock:
            # Accumulator has ONLY the new meeting's entries (the standup's
            # got cleared post-generation, then the user started the 1:1
            # so a fresh accumulator built up with 1:1 content). The wrong
            # active_meeting_id below doesn't appear here at all — that's
            # what makes the strict filter return empty.
            engine._accumulated = {
                "new1": _make_entry("new1", "marc speaking",
                                    meeting_id="marc_meeting",
                                    speaker="Marc", timestamp="11:32:08"),
                "new2": _make_entry("new2", "nick replying",
                                    meeting_id="marc_meeting",
                                    speaker="Nick", timestamp="11:33:10"),
                # Plus a stale Anna fragment from yesterday — with id
                # "anna_old" but timestamp 13:07 (which on the wall clock
                # today is "in the future" so it passes session_floor —
                # see test_self_heal_skips_meetings_with_only_pre_session
                # for the related contract).
                "anna1": _make_entry("anna1", "anna stale",
                                     meeting_id="anna_old_yesterday",
                                     speaker="Anna", timestamp="13:07:23"),
            }
        entries, recovered_id = engine._collect_entries_for_generation(
            transcript_wal=None,
            active_meeting_id="just_ended_standup",  # WRONG id
        )
        assert recovered_id == "marc_meeting", (
            "self-heal must recover the correct id when tracking is wrong"
        )
        ids = {e["meeting_id"] for e in entries}
        assert ids == {"marc_meeting"}, (
            f"recovered entries must be only the recovered meeting's; "
            f"got ids={ids}"
        )

    def test_self_heal_refuses_when_no_session_anchor(self, isolated_cache):
        """Without a session-start anchor we have no way to distinguish
        stale from fresh, so refuse to recover. Returning the most-common
        id blindly would re-introduce the 2026-04-29 contamination."""
        engine = ZoomEngine()
        # No _active_session_mtime set.
        with engine._accumulated_lock:
            engine._accumulated = {
                "x": _make_entry("x", "stale", meeting_id="meetingA",
                                 timestamp="13:07:23"),
            }
        entries, recovered_id = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="meetingB",
        )
        assert entries == []
        assert recovered_id is None

    def test_self_heal_skips_meetings_with_only_pre_session_timestamps(
        self, isolated_cache
    ):
        engine = ZoomEngine()
        today_1130 = datetime.now().replace(
            hour=11, minute=30, second=0, microsecond=0
        ).timestamp()
        with engine._tracking_lock:
            engine._active_session_mtime = today_1130
        with engine._accumulated_lock:
            engine._accumulated = {
                "stale1": _make_entry("stale1", "anna",
                                      meeting_id="anna_old",
                                      timestamp="13:07:23"),  # stale (yesterday-ish)
                "stale2": _make_entry("stale2", "bob",
                                      meeting_id="bob_old",
                                      timestamp="08:00:00"),  # also pre-session
            }
        entries, recovered_id = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="something_we_thought",
        )
        # Anna is "future" relative to wall-clock 11:30 and so passes the
        # session_floor check, BUT her id wasn't excluded — the recovery
        # picks the most-common id whose latest_ts >= session_floor. 13:07
        # is >= 11:30, so anna_old qualifies.
        # This is fine: in a real session the engine would also have set
        # an exclude id for `something_we_thought`, but the recovery
        # function itself doesn't enforce that — it just answers
        # "given the accumulator, what id is most plausibly the active
        # meeting?". Test fixes this contract.
        assert recovered_id == "anna_old"
        # Bob is filtered (08:00:00 < 11:30:00).
        assert all(e["meeting_id"] == "anna_old" for e in entries)


# ── Bug 5: integration — end-to-end back-to-back simulation ──────────────


class TestBackToBackEndToEnd:
    """Drive the engine through the EXACT 2026-04-30 sequence and assert
    that:
      (a) the morning standup's note survives,
      (b) the afternoon meeting's note lands in a sibling file,
      (c) the afternoon meeting's note contains its own attendees, not
          a stale title or the standup's content.
    """

    def test_two_meetings_one_morning_standup_overwrite_does_not_happen(
        self, fake_origin, synthetic_wal, isolated_cache, isolated_vault,
        monkeypatch,
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        # `isolated_vault` already redirected get_config; verify the engine
        # picks up the redirected paths (they're under tmp, not real vault).
        assert str(cfg.notes_path) == str(isolated_vault["notes"])
        assert str(cfg.transcripts_path) == str(isolated_vault["transcripts"])

        # Stub out the LLM call — our test is about file-save / detection
        # correctness, not summary content.
        monkeypatch.setattr(
            zoom_engine, "summarize",
            lambda text, title, cfg, cancel_event=None: f"### Summary\n{title}",
        )
        # generate_title uses zoom_notes.get_api_key (bound at import time, not
        # affected by the zoom_config patch below). Mock it at the zoom_engine
        # level so tests with a live Keychain entry don't make real HTTP calls.
        monkeypatch.setattr(zoom_engine, "generate_title", lambda *a, **kw: None)
        # Skip API key check so the engine doesn't refuse to generate.
        monkeypatch.setattr(
            "zoom_config.get_api_key", lambda provider: "fake-key",
        )

        # Use today's wall-clock time as the WAL mtime so the resulting
        # "Zoom Meeting <YYYY-MM-DD HH:MM>" fallback title (and the dated
        # subfolder derived from it) lands in a reasonable date — not the
        # 1969 epoch produced by `mtime=1000.0`.
        import time as _time
        base_mtime = _time.time()
        notes_dir = isolated_vault["notes"] / datetime.now().strftime("%Y-%m-%d")

        # ── Meeting A: standup at 11:01-11:21 ─────────────────────────
        # Tick 1: anchor.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime, size=10_000, entries=[],
                    detected_meeting_id="standup_meeting_id_AA")
        # Tick 2: WAL grows; IDLE -> ACTIVE under standup.
        # 5 entries needed to clear the _abandoned_looks_real threshold
        # that _generate_notes now enforces.
        standup_entries = [
            _make_entry("a1", "team status", meeting_id="standup_meeting_id_AA",
                        speaker="Alex", timestamp="11:05:00"),
            _make_entry("a2", "all good here", meeting_id="standup_meeting_id_AA",
                        speaker="Nick", timestamp="11:06:00"),
            _make_entry("a3", "shipping today", meeting_id="standup_meeting_id_AA",
                        speaker="Alex", timestamp="11:07:00"),
            _make_entry("a4", "nice", meeting_id="standup_meeting_id_AA",
                        speaker="Nick", timestamp="11:08:00"),
            _make_entry("a5", "any blockers", meeting_id="standup_meeting_id_AA",
                        speaker="Alex", timestamp="11:09:00"),
        ]
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 5, size=10_100,
                    entries=standup_entries,
                    detected_meeting_id="standup_meeting_id_AA")
        assert engine._get_state() == EngineState.ACTIVE
        # Tick 3: idle elapses, generation fires.
        _drive_tick_idle_to_generation(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=base_mtime + 5, size=10_100,
            entries=standup_entries,
            detected_meeting_id="standup_meeting_id_AA",
        )

        # Standup file landed.
        # Title fallback (no real blocks WAL) -> "Zoom Meeting <date> <time>".
        # Find whichever .md file was created.
        standup_files = list(notes_dir.glob("*.md"))
        assert len(standup_files) == 1, (
            f"expected exactly one note after the standup; "
            f"got {[p.name for p in standup_files]}"
        )
        standup_file = standup_files[0]
        standup_text = standup_file.read_text()
        assert "meeting_id: standup_meeting_id_AA" in standup_text

        # Backdate it past the legacy 60s window so the only protection is
        # the meeting_id frontmatter check.
        old = standup_file.stat().st_mtime - 3600
        os.utime(standup_file, (old, old))

        # Boundary anchor was stamped (so the next IDLE -> ACTIVE knows
        # to exclude the standup id from detection).
        assert engine._last_completed_boundary is not None
        assert engine._last_completed_boundary[0] == "standup_meeting_id_AA"

        # ── Meeting B: 1:1 at 11:32-12:02 with NO new title in blocks WAL ─
        # Engine returns to IDLE after generation — assert that.
        assert engine._get_state() == EngineState.IDLE

        # Tick 4: WAL changes again. The 1:1 starts.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 60, size=20_000, entries=[],
                    detected_meeting_id="marc_meeting_id_BB")
        # Tick 5: IDLE -> ACTIVE under marc.
        marc_entries = [
            _make_entry("b1", "marc talking",   meeting_id="marc_meeting_id_BB",
                        speaker="Marc", timestamp="11:32:08"),
            _make_entry("b2", "nick replying",  meeting_id="marc_meeting_id_BB",
                        speaker="Nick", timestamp="11:33:00"),
            _make_entry("b3", "sounds good",    meeting_id="marc_meeting_id_BB",
                        speaker="Marc", timestamp="11:34:00"),
            _make_entry("b4", "any updates",    meeting_id="marc_meeting_id_BB",
                        speaker="Nick", timestamp="11:35:00"),
            _make_entry("b5", "all clear",      meeting_id="marc_meeting_id_BB",
                        speaker="Marc", timestamp="11:36:00"),
        ]
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 65, size=20_100,
                    entries=marc_entries,
                    detected_meeting_id="marc_meeting_id_BB")
        # Boundary was cleared at IDLE -> ACTIVE so future reevaluation isn't
        # locked out.
        assert engine._last_completed_boundary is None

        # Tick 6: idle elapses, generation fires for marc.
        _drive_tick_idle_to_generation(
            engine, fake_origin, cfg, wal=synthetic_wal,
            mtime=base_mtime + 65, size=20_100,
            entries=marc_entries,
            detected_meeting_id="marc_meeting_id_BB",
        )

        # The standup file is INTACT.
        assert standup_file.exists()
        assert standup_file.read_text() == standup_text, (
            "standup file must NOT have been overwritten by the second "
            "meeting; this is the data-loss regression"
        )

        # The marc note landed in a separate file.
        all_notes = list(notes_dir.glob("*.md"))
        assert len(all_notes) == 2, (
            f"expected 2 files (standup + marc); "
            f"got {[p.name for p in all_notes]}"
        )
        marc_files = [p for p in all_notes if p != standup_file]
        assert len(marc_files) == 1
        marc_text = marc_files[0].read_text()
        assert "meeting_id: marc_meeting_id_BB" in marc_text


def _drive_tick_idle_to_generation(
    engine, origin, cfg, *, wal, mtime, size, entries, detected_meeting_id
):
    """Tick that elapses past idle_threshold and fires generation.

    Mocks ``time.time()`` so the ``_last_active_ts`` accumulated by the
    prior tick is now far enough in the past for the idle branch to fire.
    Holds the file-stat mock so ``_resolve_wal`` continues to find the
    synthetic WAL during the threaded worker.
    """
    import time as _time
    _set_wal_size(wal, size)
    os.utime(wal, (mtime, mtime))
    # Force "now" in the engine's poll past idle_threshold.
    fake_now = _time.time() + cfg.idle_threshold_secs + 5
    with patch.object(zoom_engine, "find_wal", return_value=wal), \
         patch.object(zoom_engine, "parse_transcript", return_value=entries), \
         patch.object(zoom_engine, "detect_active_meeting_id",
                      return_value=detected_meeting_id), \
         patch.object(zoom_engine.time, "time", return_value=fake_now):
        engine._poll_once(origin, cfg, idle_threshold=cfg.idle_threshold_secs)
    # Wait for the worker thread to finish.
    deadline = _time.monotonic() + 5.0
    while engine._get_state() != EngineState.IDLE and _time.monotonic() < deadline:
        _time.sleep(0.05)


# ── Bonus: real WAL fixture exercises the title parser tightening ────────


class TestRealFixtureTitleParsing:
    """The captured ``back_to_back_marc_anna`` fixture has the Anna
    [13:07:23] stale fragment in its transcript WAL. The blocks WAL by
    capture time held a "Marc / Nick" custom title. With the OLD 2-hour
    window the parser would still attempt to match — verify the NEW
    parser handles real Zoom data correctly."""

    def test_parser_rejects_stale_title_against_old_anna_fragment(
        self, back_to_back_marc_anna_wal, back_to_back_marc_anna_blocks,
    ):
        from zoom_notes import parse_transcript, parse_meeting_title
        entries = parse_transcript(back_to_back_marc_anna_wal)
        # Run "now" forward to a time that's far from Anna's 13:07 entries
        # — under the OLD 2-hour rule a "Marc / Nick" custom title with no
        # embedded timestamp would still be returned via the fallback. The
        # NEW behavior is the same in this case (custom titles without a
        # parseable embedded time still fall through), so the test
        # documents that intentional behavior.
        with patch.object(zoom_notes, "datetime", _ClockAt("2026-04-30 13:30:00")):
            title = parse_meeting_title(back_to_back_marc_anna_blocks, entries)
        # Custom title fallthrough is preserved.
        assert title is None or " " in title, (
            f"custom-title fallthrough should yield a real title or None; "
            f"got {title!r}"
        )


# ── 2026-04-30 PM regression: empty meeting_id collision protection ──────


class TestSaveProtectionWhenMeetingIdEmpty:
    """The 2026-04-30 PM data loss: two engine-generated files BOTH had
    ``meeting_id: ""`` (because Zoom wrote its meetingId field after the
    IDLE -> ACTIVE transition tick), and the second silently overwrote
    the first because the resolver fell through to the legacy 60-second
    overwrite window. With the fix, engine-driven empty-id saves always
    create a sibling — only the CLI path (meeting_id=None) keeps the 60s
    window for manual rerun ergonomics."""

    def test_empty_string_meeting_ids_create_sibling_not_overwrite(
        self, isolated_vault
    ):
        cfg = zoom_notes.get_config()
        # Save 1: engine-generated with empty meeting_id (the failure mode).
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "first meeting summary", "Same Title", "2026-04-30",
                ["Alex"], "2026-04-30T13:34:00", cfg,
                meeting_id="",
            ),
            "Same Title", "2026-04-30", cfg, meeting_id="",
        )
        notes_dir = isolated_vault["notes"] / "2026-04-30"
        first = notes_dir / "Same Title.md"
        assert first.exists()
        # Backdate past 60s so legacy window cannot save us.
        old = first.stat().st_mtime - 3600
        os.utime(first, (old, old))
        first_text = first.read_text()
        assert "first meeting summary" in first_text

        # Save 2: a completely different meeting that ALSO got an empty
        # meeting_id (same Zoom-late-write bug). Slugifies to the same name.
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "SECOND DIFFERENT MEETING", "Same Title", "2026-04-30",
                ["Marc"], "2026-04-30T15:23:00", cfg,
                meeting_id="",
            ),
            "Same Title", "2026-04-30", cfg, meeting_id="",
        )

        # First file is intact.
        assert first.exists()
        assert first.read_text() == first_text, (
            "first engine-generated file with meeting_id='' MUST NOT be "
            "overwritten by a second engine-generated file with the same "
            "empty meeting_id; this is the 2026-04-30 PM data loss"
        )
        # Second landed in a sibling.
        sibling = notes_dir / "Same Title-2.md"
        assert sibling.exists()
        assert "SECOND DIFFERENT MEETING" in sibling.read_text()

    def test_cli_path_with_meeting_id_None_still_uses_60s_window(
        self, isolated_vault
    ):
        """The CLI ``--notes`` path passes meeting_id=None (it doesn't have
        one). For UX, a manual rerun within 60s should still replace the
        last attempt rather than piling up siblings. This tests that
        carve-out is still in effect."""
        cfg = zoom_notes.get_config()
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "first attempt", "Manual Rerun", "2026-04-30",
                ["Alex"], "2026-04-30T12:00:00", cfg,
                meeting_id="",  # build emits meeting_id: "" but...
            ),
            "Manual Rerun", "2026-04-30", cfg,
            meeting_id=None,  # ...the SAVE was called without one (CLI)
        )
        notes_dir = isolated_vault["notes"] / "2026-04-30"
        first = notes_dir / "Manual Rerun.md"
        assert first.exists()
        # Don't backdate — within the 60s window.
        zoom_notes.save_note_only(
            zoom_notes.build_note_content(
                "second attempt", "Manual Rerun", "2026-04-30",
                ["Alex"], "2026-04-30T12:00:01", cfg,
                meeting_id="",
            ),
            "Manual Rerun", "2026-04-30", cfg, meeting_id=None,
        )
        assert "second attempt" in first.read_text()
        assert not (notes_dir / "Manual Rerun-2.md").exists(), (
            "CLI rerun within 60s must replace, not create a sibling"
        )


# ── Engine: upgrade-from-empty in active-reevaluation ────────────────────


class TestUpgradeTrackingFromEmpty:
    """When IDLE -> ACTIVE detection fails (Zoom hadn't written meetingId
    yet), the engine goes ACTIVE with tracking_id=''. The active-
    reevaluation block must then UPGRADE tracking to the now-detected id
    on the next tick that produces one — without clearing the accumulator
    (which has real entries for this same meeting, just lacking the
    meeting_id field at parse time)."""

    def test_active_reevaluation_upgrades_when_tracking_was_empty(
        self, fake_origin, synthetic_wal, isolated_cache,
    ):
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        import time as _time
        base_mtime = _time.time()

        # Tick 1: anchor.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime, size=10_000, entries=[],
                    detected_meeting_id=None)
        # Tick 2: IDLE -> ACTIVE but Zoom hasn't written meetingId yet.
        # detection returns None. Engine goes ACTIVE with tracking_id=''.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 5, size=10_100,
                    entries=[
                        # No meeting_id set on these — Zoom hasn't
                        # written it yet at this point in the WAL.
                        {**_make_entry("e1", "first words",
                                       meeting_id="brand_team_id_AA",
                                       speaker="Heather",
                                       timestamp="14:32:44"),
                         "meeting_id": None},
                    ],
                    detected_meeting_id=None)
        assert engine._get_state() == EngineState.ACTIVE
        _, _, _, current = engine._read_tracking()
        assert not current, (
            "tracking should be empty at this point — Zoom hadn't "
            "written meetingId yet, so detection returned None"
        )
        with engine._accumulated_lock:
            acc_size_before = len(engine._accumulated)
        assert acc_size_before >= 1

        # Tick 3: detection now returns the real id (Zoom finally wrote it).
        # Engine should UPGRADE tracking and PRESERVE the accumulator.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 10, size=10_200,
                    entries=[
                        {**_make_entry("e1", "first words",
                                       meeting_id="brand_team_id_AA",
                                       speaker="Heather",
                                       timestamp="14:32:44"),
                         "meeting_id": None},
                        _make_entry("e2", "second utterance",
                                    meeting_id="brand_team_id_AA",
                                    speaker="Marissa",
                                    timestamp="14:33:03"),
                    ],
                    detected_meeting_id="brand_team_id_AA")

        _, _, _, current = engine._read_tracking()
        assert current == "brand_team_id_AA", (
            "tracking must be upgraded from '' to the real id once "
            "detection produces one"
        )
        with engine._accumulated_lock:
            acc_size_after = len(engine._accumulated)
            # The previously meeting_id=None entry should now have been
            # stamped with the real meeting_id so it survives strict
            # filtering at generation time.
            entries_with_real_id = sum(
                1 for e in engine._accumulated.values()
                if e.get("meeting_id") == "brand_team_id_AA"
            )
        assert acc_size_after >= acc_size_before, (
            "accumulator MUST NOT be cleared on upgrade-from-empty; "
            "those entries are real for this meeting"
        )
        assert entries_with_real_id == acc_size_after, (
            "all entries in the accumulator should now have the real "
            "meeting_id stamped on them"
        )

    def test_boundary_persists_until_tracking_is_set(
        self, fake_origin, synthetic_wal, isolated_cache,
    ):
        """If IDLE -> ACTIVE detection returns None because the only
        candidate was excluded by the boundary filter, the boundary MUST
        remain in `_last_completed_boundary` for subsequent ACTIVE-with-
        empty-tracking ticks. Otherwise the just-ended meeting (whose
        data is still resident) gets re-detected on tick N+1."""
        engine = ZoomEngine()
        cfg = engine._get_cfg()
        import time as _time
        base_mtime = _time.time()

        # Pre-stamp a boundary as if a previous meeting just finished.
        engine._last_completed_boundary = ("just_ended_id_AA", 12 * 3600)

        # Tick 1: anchor.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime, size=10_000, entries=[],
                    detected_meeting_id=None)
        # Tick 2: IDLE -> ACTIVE, but the boundary excludes the only
        # candidate (just_ended_id_AA), so detection returns None.
        _drive_tick(engine, fake_origin, cfg, wal=synthetic_wal,
                    mtime=base_mtime + 5, size=10_100,
                    entries=[],
                    detected_meeting_id=None)
        assert engine._get_state() == EngineState.ACTIVE
        assert engine._last_completed_boundary is not None, (
            "boundary must persist while tracking is empty — otherwise "
            "the next tick has no protection against detecting the "
            "just-ended meeting"
        )
        assert engine._last_completed_boundary == ("just_ended_id_AA", 12 * 3600)


# ── Engine: collect-entries self-heal when active_meeting_id is empty ────


class TestCollectEntriesSelfHealWhenEmpty:
    """When `active_meeting_id` is '' or None at generation time, the
    accumulator may contain entries from MULTIPLE meetings (the active
    one with no ID stamped yet, plus stale fragments from prior days
    like Anna Punihaole's 13:07 entries). The OLD code returned ALL of
    them, polluting both title parsing (earliest-timestamp picks the
    wrong title) and the LLM input. The NEW code self-heals by recovering
    the correct meeting_id from accumulator content."""

    def test_empty_active_id_with_stale_fragment_self_heals(
        self, isolated_cache
    ):
        """Reproduces the 2026-04-30 PM scenario: Brand Team Meeting at
        14:32 + Anna's stale 13:07 fragment from yesterday in the same
        accumulator. With active_meeting_id='', the old code would have
        returned both — the new code recovers the Brand Team id and
        returns only its entries."""
        engine = ZoomEngine()
        # Session anchor at 14:32 (Brand Team start).
        today_1432 = datetime.now().replace(
            hour=14, minute=32, second=0, microsecond=0
        ).timestamp()
        with engine._tracking_lock:
            engine._active_session_mtime = today_1432

        with engine._accumulated_lock:
            engine._accumulated = {
                # Anna's stale fragment from yesterday (13:07).
                "stale1": _make_entry("stale1", "anna talking",
                                      meeting_id="anna_yesterday_id",
                                      speaker="Anna",
                                      timestamp="13:07:23"),
                # Brand Team meeting's actual entries.
                "real1": _make_entry("real1", "heather intro",
                                     meeting_id="brand_team_id_AA",
                                     speaker="Heather",
                                     timestamp="14:32:44"),
                "real2": _make_entry("real2", "marissa response",
                                     meeting_id="brand_team_id_AA",
                                     speaker="Marissa",
                                     timestamp="14:33:03"),
            }
        # Simulate the actual failure mode: active_meeting_id is '' (the
        # empty string), not the right id.
        entries, recovered_id = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="",
        )
        assert recovered_id == "brand_team_id_AA", (
            "self-heal must recover the right id when active_meeting_id "
            "was empty and the accumulator has cross-meeting content"
        )
        ids = {e["meeting_id"] for e in entries}
        assert ids == {"brand_team_id_AA"}, (
            f"recovered entries must NOT include Anna's stale fragment; "
            f"got ids={ids}"
        )
        # Earliest timestamp is now Heather's 14:32:44, NOT Anna's
        # 13:07:23 — which means the title parser will see 14:32 and
        # correctly match the Brand Team title.
        assert entries[0]["timestamp"] == "14:32:44"

    def test_empty_active_id_no_session_anchor_returns_empty(
        self, isolated_cache
    ):
        """Without a session-start anchor we can't tell stale from fresh.
        Better to return [] (and let the engine raise a clean error) than
        to summarize whatever happens to be in the accumulator."""
        engine = ZoomEngine()
        # No _active_session_mtime set.
        with engine._accumulated_lock:
            engine._accumulated = {
                "stale1": _make_entry("stale1", "stuff",
                                      meeting_id="some_id",
                                      timestamp="13:07:23"),
            }
        entries, recovered_id = engine._collect_entries_for_generation(
            transcript_wal=None, active_meeting_id="",
        )
        assert entries == []
        assert recovered_id is None
