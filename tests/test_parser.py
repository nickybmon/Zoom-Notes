"""Parser-level regression tests for zoom_notes.parse_transcript.

These verify that the WAL string-extraction parser correctly partitions a
WAL containing multiple meetings, deduplicates entries, and behaves
sensibly under edge cases. The parser is the single most fragile part of
the system because it depends on Zoom's internal byte layout.
"""
from zoom_notes import (
    deduplicate,
    parse_transcript,
    detect_active_meeting_id,
    score_meeting_ids,
    count_meeting_ids,
    slugify_title,
    _safe_meeting_id_slug,
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


class TestUtilities:
    def test_slugify_strips_zoom_timestamp(self):
        assert slugify_title("Standup 2026-04-27 09:30(GMT-04:00)") == "Standup"

    def test_slugify_falls_back_when_empty(self):
        assert slugify_title("", fallback_date="2026-04-27") == "Meeting 2026-04-27"

    def test_safe_meeting_id_slug_replaces_unsafe_chars(self):
        assert _safe_meeting_id_slug("a/b+c=d==") == "a_b_c_d__"
        assert _safe_meeting_id_slug("AbC-123_") == "AbC-123_"


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
