"""Parser-level regression tests for zoom_notes.parse_transcript.

These verify that the WAL string-extraction parser correctly partitions a
WAL containing multiple meetings, deduplicates entries, and behaves
sensibly under edge cases. The parser is the single most fragile part of
the system because it depends on Zoom's internal byte layout.
"""
import zoom_notes
from zoom_notes import (
    deduplicate,
    parse_transcript,
    detect_active_meeting_id,
    score_meeting_ids,
    count_meeting_ids,
    slugify_title,
    _safe_meeting_id_slug,
    _title_has_hash_token,
)


class TestParseTranscript:
    def test_returns_entries_with_required_fields(self, multi_meeting_wal):
        entries = parse_transcript(multi_meeting_wal)
        assert entries, "expected at least one entry from fixture"
        sample = entries[0]
        assert "msg_id" in sample
        assert "text" in sample
        assert "speaker" in sample
        assert "timestamp" in sample
        assert "meeting_id" in sample

    def test_entries_are_time_sorted(self, multi_meeting_wal):
        entries = parse_transcript(multi_meeting_wal)
        timestamps = [e.get("timestamp") for e in entries if e.get("timestamp")]
        assert timestamps == sorted(timestamps), \
            "parse_transcript must return entries sorted by timestamp"

    def test_meeting_id_filter_partitions_correctly(self, multi_meeting_wal, multi_meeting_meta):
        """The exact scenario that bit us on 2026-04-27.

        Without filtering, the parser returns entries from BOTH meetings.
        With a meeting_id filter, it should return only that meeting's entries.
        """
        all_entries = parse_transcript(multi_meeting_wal)
        all_meeting_ids = {e.get("meeting_id") for e in all_entries if e.get("meeting_id")}
        assert len(all_meeting_ids) >= 2, \
            "fixture must contain ≥2 meetings — recapture if Zoom rolled over"

        first_id = next(iter(all_meeting_ids))
        filtered = parse_transcript(multi_meeting_wal, meeting_id_filter=first_id)
        assert filtered, "filter must not wipe everything for a known-present id"
        assert all(e.get("meeting_id") == first_id for e in filtered), \
            "filter must drop entries from other meetings"

    def test_filter_with_unknown_id_returns_empty(self, multi_meeting_wal):
        filtered = parse_transcript(multi_meeting_wal, meeting_id_filter="nonexistent==")
        assert filtered == []


class TestEntryBoundaryRespected:
    """Regression tests for the 2026-05-04 Quick Chat on PnP Assets incident.

    `strings -n 2` occasionally truncates the leading bytes of the next WAL
    entry's `messageId` line down to a fragment (the prefix bytes of the id
    are short or non-printable). Pre-fix, the parser's forward-walk would
    sail past the boundary and slurp the next entry's `username`,
    `timeStampContent`, and `meetingId` into the previous entry, producing
    catastrophically misattributed transcripts (Nick's first utterance
    showed up as Michael Huard from a totally different meeting, the title
    resolver then saw a stale earliest timestamp and fell back to the
    wrong meeting's title).
    """

    def _patch_strings(self, monkeypatch, lines):
        """Make `read_wal_strings` return a fixed list — lets us synthesize
        the corrupted-boundary layout without needing a real WAL fixture."""
        monkeypatch.setattr(zoom_notes, "read_wal_strings", lambda _path: lines)

    def test_does_not_leak_username_meetingid_across_truncated_boundary(self, monkeypatch, tmp_path):
        # Synthetic layout mirroring the real 2026-05-04 WAL byte pattern:
        # entry A's `messageId\n<id>\nmessage\n<text>\ntimeStampContent\n<ts>`
        # then a truncated fragment where entry B's `messageId\n<id>` should
        # have been (the prefix bytes were eaten), then entry B's full
        # message/username/meetingId block.
        lines = [
            "messageId",
            "4:0:16787456:0:599398745",   # entry A id (Nick's utterance)
            "message",
            "Just using the Slack AI features to skip channel noise.",
            "timeStampContent",
            "16:33:08",
            # ── corrupted boundary: entry B's `messageId\n<id>` got eaten;
            # only a fragment of the id survived `strings -n 2`.
            "3n",
            "dO",
            "Y80:0:1951987897",
            # ── entry B starts here (wrong attribution territory):
            "message",
            "I've, uh, emailed, we just need to follow up on it.",
            "timeStampContent",
            "14:56:05",
            "timeStampSeconds",
            "i",
            "uniqueUserId",
            "16788480-0",
            "username",
            "Michael Huard",
            "meetingId",
            "HA1Kj2+mQDqLrOwX2pQIfA==",
        ]
        self._patch_strings(monkeypatch, lines)

        entries = parse_transcript(tmp_path / "fake.wal")
        a = next(e for e in entries if e["msg_id"] == "4:0:16787456:0:599398745")

        # The crux of the regression: entry A must NOT inherit entry B's
        # username, timestamp, or meetingId.
        assert a["speaker"] == "Unknown", (
            f"entry A leaked speaker from entry B across boundary: {a['speaker']}"
        )
        assert a["meeting_id"] is None, (
            f"entry A leaked meeting_id from entry B across boundary: {a['meeting_id']}"
        )
        assert a["timestamp"] == "16:33:08", (
            f"entry A's own timestamp got overwritten by entry B's: {a['timestamp']}"
        )
        # And entry A's text is preserved correctly.
        assert "Slack AI" in a["text"]

    def test_rejects_malformed_timestamp_without_seconds(self, monkeypatch, tmp_path):
        """When `strings -n 2` truncates the seconds digits, the value is
        unusable (it will lexicographically sort to the wrong place and
        poison title resolution). The parser must drop it as if it were
        missing entirely."""
        lines = [
            "messageId",
            "msg-1",
            "message",
            "Hello there.",
            "timeStampContent",
            "16:33:",  # malformed — seconds digits got eaten
            "username",
            "Alice",
            "meetingId",
            "real-meeting-id",
        ]
        self._patch_strings(monkeypatch, lines)

        entries = parse_transcript(tmp_path / "fake.wal")
        assert len(entries) == 1
        assert entries[0]["timestamp"] is None, (
            f"malformed timestamp '16:33:' should be rejected, got {entries[0]['timestamp']}"
        )
        # The other valid metadata should still be picked up.
        assert entries[0]["speaker"] == "Alice"
        assert entries[0]["meeting_id"] == "real-meeting-id"

    def test_clean_entry_still_parses_correctly(self, monkeypatch, tmp_path):
        """Sanity check: the boundary-stop must NOT break the common case
        where the WAL is well-formed."""
        lines = [
            "messageId",
            "msg-1",
            "message",
            "First utterance.",
            "timeStampContent",
            "10:00:00",
            "timeStampSeconds",
            "i",
            "uniqueUserId",
            "1-0",
            "username",
            "Alice",
            "meetingId",
            "meeting-A",
            "messageId",
            "msg-2",
            "message",
            "Second utterance.",
            "timeStampContent",
            "10:00:05",
            "username",
            "Bob",
            "meetingId",
            "meeting-A",
        ]
        self._patch_strings(monkeypatch, lines)

        entries = parse_transcript(tmp_path / "fake.wal")
        assert len(entries) == 2
        a = next(e for e in entries if e["msg_id"] == "msg-1")
        b = next(e for e in entries if e["msg_id"] == "msg-2")
        assert (a["speaker"], a["timestamp"], a["meeting_id"]) == ("Alice", "10:00:00", "meeting-A")
        assert (b["speaker"], b["timestamp"], b["meeting_id"]) == ("Bob", "10:00:05", "meeting-A")


class TestDeduplicate:
    def test_collapses_progressive_text_updates(self):
        """Zoom streams word-by-word — same speaker, progressively longer text."""
        entries = [
            {"speaker": "Alice", "text": "Hello", "timestamp": "10:00:00", "msg_id": "1"},
            {"speaker": "Alice", "text": "Hello world", "timestamp": "10:00:01", "msg_id": "2"},
            {"speaker": "Alice", "text": "Hello world how are you", "timestamp": "10:00:03", "msg_id": "3"},
        ]
        result = deduplicate(entries)
        assert len(result) == 1
        assert result[0]["text"] == "Hello world how are you"
        assert result[0]["timestamp"] == "10:00:03"

    def test_preserves_distinct_utterances(self):
        entries = [
            {"speaker": "Alice", "text": "Hello", "timestamp": "10:00:00", "msg_id": "1"},
            {"speaker": "Bob", "text": "Hi", "timestamp": "10:00:01", "msg_id": "2"},
            {"speaker": "Alice", "text": "Goodbye", "timestamp": "10:00:02", "msg_id": "3"},
        ]
        result = deduplicate(entries)
        assert len(result) == 3


class TestActiveMeetingDetection:
    def test_returns_one_of_present_meeting_ids(self, multi_meeting_wal, multi_meeting_meta):
        active = detect_active_meeting_id(multi_meeting_wal)
        assert active in multi_meeting_meta["expected"]["meeting_ids"]

    def test_score_meeting_ids_assigns_score_per_meeting(self, multi_meeting_wal, multi_meeting_meta):
        scores = score_meeting_ids(multi_meeting_wal)
        for mid in multi_meeting_meta["expected"]["meeting_ids"]:
            assert mid in scores
            assert scores[mid] > 0

    def test_count_meeting_ids_is_deterministic(self, multi_meeting_wal):
        a = count_meeting_ids(multi_meeting_wal)
        b = count_meeting_ids(multi_meeting_wal)
        assert a == b

    def test_resumed_meeting_re_detected_after_silent_period(self, monkeypatch, tmp_path):
        """Regression guard for the 2026-05-08 FigJam/whiteboard premature-idle bug.

        When a meeting has a silent collaboration period (FigJam, whiteboard,
        screen-share) longer than the 90-second idle threshold, the engine fires
        note generation mid-meeting and stamps a boundary:
          _last_completed_boundary = (meeting_id, freshness_floor_secs)

        On the next IDLE -> ACTIVE transition, detect_active_meeting_id is called
        with exclude_meeting_id=meeting_id.  Before this fix, the ID was blocked
        unconditionally, so the engine could never re-latch onto the same meeting
        once speech resumed.  With the fix, the meeting is allowed back through
        when its latest_ts_secs exceeds the freshness floor.
        """
        MID = "FigJam+SessionABC=="
        FLOOR_SECS = 13 * 3600 + 6 * 60 + 27  # 13:06:27 — last entry before silence

        # Synthetic WAL: entries from the SAME meeting but with timestamps
        # clearly beyond the freshness floor (speech resumed at 13:12).
        resumed_lines = [
            "messageId",
            "99:0:99999:0:111111",
            "message",
            "Great stickies everyone, let's discuss.",
            "timeStampContent",
            "13:12:31",
            "timeStampSeconds",
            "i",
            "uniqueUserId",
            "99-0",
            "username",
            "Alex Diner",
            "meetingId",
            MID,
        ]
        monkeypatch.setattr(zoom_notes, "read_wal_strings", lambda _path: resumed_lines)

        fake_wal = tmp_path / "fake.wal"
        fake_wal.write_bytes(b"x" * 64)

        # Without fix: detect_active_meeting_id returns None (meeting excluded)
        # With fix: returns MID because latest_ts_secs (13:12:31 = 47551) > floor (47187)
        result = detect_active_meeting_id(
            fake_wal,
            exclude_meeting_id=MID,
            freshness_floor_secs=FLOOR_SECS,
        )
        assert result == MID, (
            f"Expected resumed meeting {MID!r} to be re-detected after silent period, "
            f"got {result!r}. The exclude_meeting_id guard must allow re-detection "
            f"when latest_ts_secs ({13*3600+12*60+31}) > freshness_floor ({FLOOR_SECS})."
        )

    def test_old_content_still_excluded_after_generation(self, monkeypatch, tmp_path):
        """The freshness exception must not re-admit checkpoint replays.

        If the WAL's latest timestamp for the excluded meeting is at or below
        the freshness floor, it should remain excluded — that's old content
        from the same session we already processed, replayed by a SQLite
        WAL checkpoint.
        """
        MID = "FigJam+SessionABC=="
        FLOOR_SECS = 13 * 3600 + 6 * 60 + 27  # 13:06:27

        # WAL content: same meeting ID but timestamps OLDER than the floor
        old_lines = [
            "messageId",
            "1:0:11111:0:222222",
            "message",
            "Let me introduce today's topic.",
            "timeStampContent",
            "13:05:10",  # <= floor
            "timeStampSeconds",
            "i",
            "uniqueUserId",
            "1-0",
            "username",
            "Alex Diner",
            "meetingId",
            MID,
        ]
        monkeypatch.setattr(zoom_notes, "read_wal_strings", lambda _path: old_lines)

        fake_wal = tmp_path / "fake.wal"
        fake_wal.write_bytes(b"x" * 64)

        result = detect_active_meeting_id(
            fake_wal,
            exclude_meeting_id=MID,
            freshness_floor_secs=FLOOR_SECS,
        )
        assert result is None, (
            f"Old checkpoint content for {MID!r} must remain excluded; got {result!r}."
        )


class TestUtilities:
    def test_slugify_strips_zoom_timestamp(self):
        assert slugify_title("Standup 2026-04-27 09:30(GMT-04:00)") == "Standup"

    def test_slugify_falls_back_when_empty(self):
        assert slugify_title("", fallback_date="2026-04-27") == "Meeting 2026-04-27"

    def test_safe_meeting_id_slug_replaces_unsafe_chars(self):
        assert _safe_meeting_id_slug("a/b+c=d==") == "a_b_c_d__"
        assert _safe_meeting_id_slug("AbC-123_") == "AbC-123_"


class TestTitleHashFilter:
    """Regression guard for the 2026-05-07 garbled-title bug.

    Q&A questions from Zoom note blocks get stored adjacent to meeting-ID
    hashes in the WAL.  When strings(1) extracts them the hash fuses to the
    last word of the question (e.g. "one?26oit2v1HSQSi5kic4VLE7kQ") and the
    `?` punctuation previously broke the alphanumeric regex, letting the
    candidate slip through as a meeting title.
    """

    def test_rejects_hash_fused_without_punctuation(self):
        assert _title_has_hash_token(
            "0How do you translate animations in another tool into this one26oit2v1HSQSi5kic4VLE7kQ"
        )

    def test_rejects_hash_fused_via_question_mark(self):
        assert _title_has_hash_token(
            "0How do you translate animations in another tool into this one?26oit2v1HSQSi5kic4VLE7kQ"
        )

    def test_accepts_real_meeting_title_with_slash(self):
        assert not _title_has_hash_token("Vanessa/Nick Monthly 2026-05-07 15:01(GMT-4:00)")

    def test_accepts_real_meeting_title_plain(self):
        assert not _title_has_hash_token("Daily Standup 2026-05-07 11:00(GMT-4:00)")


class TestFindOriginDir:
    """Phase 1 #5 regression guard.

    `find_origin_dir` used to return the FIRST matching origin hash, which
    on multi-account / multi-profile Zoom setups could lock onto a stale
    origin whose WAL hasn't been touched in weeks. The fix scores
    candidates by the freshness of their transcript WAL.
    """

    def _make_origin(self, root, name: str, *, wal_mtime: float | None,
                     prefix: str = "1CB477F679D6"):
        """Build a fake origin layout matching Zoom's expected nesting.

        Result: <root>/<name>/<name>/IndexedDB/<prefix>...01/IndexedDB.sqlite3-wal
        with the WAL stat'd to `wal_mtime` if provided.
        """
        nested = root / name / name
        idb = nested / "IndexedDB" / f"{prefix}_FAKE01"
        idb.mkdir(parents=True, exist_ok=True)
        wal = idb / "IndexedDB.sqlite3-wal"
        wal.write_bytes(b"x" * 1024)  # >256 bytes so find_wal accepts it
        if wal_mtime is not None:
            import os
            os.utime(wal, (wal_mtime, wal_mtime))
        return nested

    def test_picks_freshest_wal_when_multiple_origins_exist(self, tmp_path, monkeypatch):
        from zoom_notes import find_origin_dir
        import zoom_notes

        origins_root = tmp_path / "Origins"
        origins_root.mkdir()
        monkeypatch.setattr(zoom_notes, "MY_NOTES_ORIGINS", origins_root)

        stale = self._make_origin(origins_root, "aaaaa_stale", wal_mtime=1000.0)
        fresh = self._make_origin(origins_root, "bbbbb_fresh", wal_mtime=999_000.0)

        chosen = find_origin_dir()
        assert chosen == fresh, \
            f"expected freshest origin {fresh}, got {chosen} (would have been wrong with first-match)"

    def test_falls_back_to_first_match_when_no_wal_yet(self, tmp_path, monkeypatch):
        """Fresh install / cold-start: no transcript WAL exists yet. We
        should still return SOMETHING rather than None."""
        from zoom_notes import find_origin_dir
        import zoom_notes

        origins_root = tmp_path / "Origins"
        origins_root.mkdir()
        monkeypatch.setattr(zoom_notes, "MY_NOTES_ORIGINS", origins_root)

        # IndexedDB exists but no matching WAL — find_wal returns None
        # for both candidates.
        nested_a = origins_root / "aaaaa" / "aaaaa"
        (nested_a / "IndexedDB").mkdir(parents=True)
        nested_b = origins_root / "bbbbb" / "bbbbb"
        (nested_b / "IndexedDB").mkdir(parents=True)

        chosen = find_origin_dir()
        assert chosen in (nested_a, nested_b), "must return some valid origin"

    def test_returns_none_when_no_origins(self, tmp_path, monkeypatch):
        from zoom_notes import find_origin_dir
        import zoom_notes

        empty = tmp_path / "DoesNotExist"
        monkeypatch.setattr(zoom_notes, "MY_NOTES_ORIGINS", empty)
        assert find_origin_dir() is None
