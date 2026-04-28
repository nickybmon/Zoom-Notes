"""Content-based WAL discovery tests.

The IndexedDB folder hash that Zoom assigns to its transcript and blocks
stores is per-account, not stable across machines. The original engine
hardcoded a prefix that only matched on the developer's account, leaving
every other user silently stuck in IDLE forever.

These tests guard the content-based discovery path in
`find_wal_by_content` and the prefix-fallback wiring in `find_wal`. They
use the captured `multi_meeting_wal` fixture's transcript and blocks
WALs, planted into a synthetic Origin/IndexedDB tree under arbitrary
folder names so the tests cover the case the production prefix WON'T
match — i.e. exactly Blake's situation.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from zoom_notes import (
    _MIN_SIGNATURE_HITS,
    _score_wal_for_kind,
    find_wal,
    find_wal_by_content,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _plant_origin(
    tmp_path: Path,
    transcript_wal: Path | None,
    blocks_wal: Path | None,
    *,
    transcript_folder: str = "ABCDEF1234567890",
    blocks_folder: str = "FEDCBA0987654321",
    extra_folders: list[str] | None = None,
) -> Path:
    """Build a fake `origin/IndexedDB/<hash>/IndexedDB.sqlite3-wal` tree.

    The folder names deliberately do NOT start with the production prefix
    `1CB477F679D6` / `DDEC8414E29A`, so the prefix fast-path in
    `find_wal()` won't match and content discovery is forced to take over
    — the same situation any non-developer Zoom user ends up in.
    """
    origin = tmp_path / "origin"
    idb = origin / "IndexedDB"
    idb.mkdir(parents=True)

    if transcript_wal is not None:
        d = idb / transcript_folder
        d.mkdir()
        shutil.copy2(transcript_wal, d / "IndexedDB.sqlite3-wal")
    if blocks_wal is not None:
        d = idb / blocks_folder
        d.mkdir()
        shutil.copy2(blocks_wal, d / "IndexedDB.sqlite3-wal")

    # Plant some unrelated WALs to simulate the noise of other Zoom
    # IndexedDB stores (caches, settings, etc) that should NOT match
    # either signature.
    for name in (extra_folders or []):
        d = idb / name
        d.mkdir()
        # 1KB of arbitrary bytes — enough to clear the size floor in
        # `find_wal()` (>256), but not containing the signature tokens.
        (d / "IndexedDB.sqlite3-wal").write_bytes(b"\x00garbage\x00" * 200)

    return origin


# ── Scoring ───────────────────────────────────────────────────────────────


class TestScoreWal:
    """The signature-token scorer is the discovery primitive."""

    def test_transcript_wal_scores_high_for_transcript_kind(
        self, multi_meeting_wal,
    ):
        score = _score_wal_for_kind(multi_meeting_wal, "transcript")
        assert score >= _MIN_SIGNATURE_HITS, (
            f"transcript fixture must score at least "
            f"{_MIN_SIGNATURE_HITS} for kind='transcript', got {score}"
        )

    def test_blocks_wal_scores_low_for_transcript_kind(
        self, multi_meeting_blocks,
    ):
        """The blocks WAL may technically contain `messageId` once or
        twice via embedded JSON, but never at the rate the transcript
        WAL does — and any cross-talk is dwarfed by the actual transcript
        WAL when both are scored."""
        transcript_score = _score_wal_for_kind(
            multi_meeting_blocks, "transcript"
        )
        # Soft assertion — the requirement is "lower than the transcript
        # WAL's score", not "literally zero". The block-WAL's transcript
        # score should be much smaller than the transcript-WAL's, which
        # is what the picker actually relies on.
        assert transcript_score < 100

    def test_blocks_wal_scores_high_for_blocks_kind(self, multi_meeting_blocks):
        score = _score_wal_for_kind(multi_meeting_blocks, "blocks")
        assert score >= _MIN_SIGNATURE_HITS, (
            f"blocks fixture must score at least "
            f"{_MIN_SIGNATURE_HITS} for kind='blocks', got {score}"
        )

    def test_garbage_wal_scores_zero(self, tmp_path):
        wal = tmp_path / "garbage.sqlite3-wal"
        wal.write_bytes(b"\x01random bytes that arent tokens\x02" * 50)
        assert _score_wal_for_kind(wal, "transcript") == 0
        assert _score_wal_for_kind(wal, "blocks") == 0

    def test_synthetic_transcript_wal_scores_above_threshold(self, tmp_path):
        """Discovery has to work even without the captured-fixture, so
        we synthesize the minimum content shape that real Zoom WALs
        produce: alternating null-separators around the signature
        tokens, which `strings(1)` will surface as standalone lines."""
        wal = tmp_path / "synthetic-transcript.sqlite3-wal"
        body = b"\x00messageId\x00abc-123\x00message\x00hello\x00" * 5
        wal.write_bytes(body)
        score = _score_wal_for_kind(wal, "transcript")
        assert score >= _MIN_SIGNATURE_HITS, (
            f"synthetic WAL with 5 messageId tokens must score at least "
            f"{_MIN_SIGNATURE_HITS}, got {score}"
        )

    def test_synthetic_blocks_wal_scores_above_threshold(self, tmp_path):
        wal = tmp_path / "synthetic-blocks.sqlite3-wal"
        body = b"\x00title\x00Some Meeting Name\x00BLOCK_HEADER\x00" * 5
        wal.write_bytes(body)
        score = _score_wal_for_kind(wal, "blocks")
        assert score >= _MIN_SIGNATURE_HITS


# ── Content discovery ────────────────────────────────────────────────────


class TestFindWalByContent:
    """End-to-end discovery against a synthetic origin tree.

    These tests intentionally use folder names that don't match the
    production prefix, so the discovery has to identify each WAL purely
    by its contents — which is the actual production behavior on every
    user's machine except the developer's.
    """

    def test_finds_transcript_wal_under_unknown_folder_name(
        self, tmp_path, multi_meeting_wal, multi_meeting_blocks,
    ):
        origin = _plant_origin(
            tmp_path, multi_meeting_wal, multi_meeting_blocks,
        )
        found = find_wal_by_content(origin, "transcript")
        assert found is not None, "must find transcript WAL by content"
        assert found.parent.name == "ABCDEF1234567890", (
            "must pick the transcript WAL, not the blocks WAL"
        )

    def test_finds_blocks_wal_under_unknown_folder_name(
        self, tmp_path, multi_meeting_wal, multi_meeting_blocks,
    ):
        origin = _plant_origin(
            tmp_path, multi_meeting_wal, multi_meeting_blocks,
        )
        found = find_wal_by_content(origin, "blocks")
        assert found is not None, "must find blocks WAL by content"
        assert found.parent.name == "FEDCBA0987654321"

    def test_ignores_garbage_wals_alongside_real_ones(
        self, tmp_path, multi_meeting_wal, multi_meeting_blocks,
    ):
        """Blake's machine has 6 IndexedDB folders. Only one is the
        transcript store; the others are caches, settings, etc. The
        scanner must pick the right one even when surrounded by noise.
        """
        origin = _plant_origin(
            tmp_path,
            multi_meeting_wal,
            multi_meeting_blocks,
            extra_folders=[
                "1D31BD3F79F078D61B19504FAA2CF36F30D2590E1EFB4F4C132EB4D51BAB496E",
                "2929F357B812B92E02B484D793F3E18668E78BEBD0ED1E1E8E0FC5ADECBFF8AD",
                "639ED50045C749C897AE16EACA592B4DA44633A6BEC2AD341ED6E0F9DD7A727F",
                "D875033C0941E0404D3095BA2101BB2497676B443EE782DA598F563A720441F2",
            ],
        )
        found = find_wal_by_content(origin, "transcript")
        assert found is not None
        assert found.parent.name == "ABCDEF1234567890"

    def test_returns_none_when_no_wal_has_signature_hits(self, tmp_path):
        """The 'Zoom installed but Notetaker never used' case.

        find_wal_by_content must return None (not crash, not pick a
        random WAL) so the caller can surface a real setup error.
        """
        origin = _plant_origin(
            tmp_path,
            transcript_wal=None,
            blocks_wal=None,
            extra_folders=["ABC", "DEF", "GHI"],
        )
        assert find_wal_by_content(origin, "transcript") is None
        assert find_wal_by_content(origin, "blocks") is None

    def test_returns_none_when_origin_has_no_indexeddb_dir(self, tmp_path):
        bare_origin = tmp_path / "bare-origin"
        bare_origin.mkdir()
        assert find_wal_by_content(bare_origin, "transcript") is None
        assert find_wal_by_content(bare_origin, "blocks") is None

    def test_picks_correct_synthetic_wal_among_multiple(self, tmp_path):
        """End-to-end discovery test that doesn't need the captured
        fixture. Two folders, neither matching the production prefix:
        one with synthetic transcript content, one with synthetic
        blocks content. Discovery must route each `kind` query to the
        correct folder.
        """
        origin = tmp_path / "origin"
        idb = origin / "IndexedDB"
        idb.mkdir(parents=True)

        transcript_folder = idb / "RANDOM_TRANSCRIPT_HASH"
        transcript_folder.mkdir()
        transcript_body = (
            b"\x00messageId\x0016:0:1:0:1\x00message\x00hello\x00"
            b"\x00messageId\x0016:0:1:0:2\x00message\x00world\x00"
            b"\x00messageId\x0016:0:1:0:3\x00message\x00ok\x00"
        )
        (transcript_folder / "IndexedDB.sqlite3-wal").write_bytes(
            transcript_body * 4  # ensure size > 256
        )

        blocks_folder = idb / "RANDOM_BLOCKS_HASH"
        blocks_folder.mkdir()
        blocks_body = (
            b"\x00title\x00First Meeting 2026-04-28 16:00\x00"
            b"\x00title\x00Second Meeting 2026-04-28 17:00\x00"
            b"\x00title\x00Third Meeting 2026-04-28 18:00\x00"
        )
        (blocks_folder / "IndexedDB.sqlite3-wal").write_bytes(
            blocks_body * 4
        )

        # An unrelated WAL with neither token, simulating cache stores.
        noise_folder = idb / "UNRELATED_NOISE"
        noise_folder.mkdir()
        (noise_folder / "IndexedDB.sqlite3-wal").write_bytes(
            b"\x00cacheKey\x00stuff\x00etag\x00v1\x00" * 50
        )

        t_found = find_wal_by_content(origin, "transcript")
        assert t_found is not None
        assert t_found.parent.name == "RANDOM_TRANSCRIPT_HASH"

        b_found = find_wal_by_content(origin, "blocks")
        assert b_found is not None
        assert b_found.parent.name == "RANDOM_BLOCKS_HASH"


# ── find_wal() prefix → content fallback ─────────────────────────────────


class TestFindWalFallback:
    """`find_wal()` is the public entry point. It must:

    1. Return prefix matches when they exist (preserves Nick's machine)
    2. Fall back to content discovery when the prefix matches nothing
       (covers Blake and every other future user)
    """

    def test_prefix_match_wins_when_folder_starts_with_prefix(
        self, tmp_path, multi_meeting_wal, multi_meeting_blocks,
    ):
        origin = _plant_origin(
            tmp_path,
            multi_meeting_wal,
            multi_meeting_blocks,
            transcript_folder="1CB477F679D6abc123",
            blocks_folder="DDEC8414E29Aabc123",
        )
        found = find_wal(origin, "1CB477F679D6")
        assert found is not None
        assert found.parent.name == "1CB477F679D6abc123", (
            "prefix match must win over content scan"
        )

    def test_falls_back_to_content_when_prefix_matches_nothing(
        self, tmp_path, multi_meeting_wal, multi_meeting_blocks,
    ):
        """Blake's case: hardcoded prefix doesn't match any folder.
        The transcript still has to be findable by content."""
        origin = _plant_origin(
            tmp_path,
            multi_meeting_wal,
            multi_meeting_blocks,
            transcript_folder="WHATEVER_XXX",
            blocks_folder="ALSO_WHATEVER",
        )
        found_transcript = find_wal(
            origin, "1CB477F679D6", kind="transcript",
        )
        assert found_transcript is not None, (
            "find_wal must fall back to content discovery when the "
            "prefix matches no folder"
        )
        assert found_transcript.parent.name == "WHATEVER_XXX"

        found_blocks = find_wal(
            origin, "DDEC8414E29A", kind="blocks",
        )
        assert found_blocks is not None
        assert found_blocks.parent.name == "ALSO_WHATEVER"

    def test_bogus_blocks_prefix_with_explicit_kind_finds_blocks_wal(
        self, tmp_path,
    ):
        """Regression for the bug caught during live validation:

        When the user has overridden `blocks_db_prefix` in settings to
        something arbitrary that matches no folder, `find_wal()` must
        still return the BLOCKS WAL — not the transcript WAL — when
        the caller passes `kind="blocks"` explicitly.

        Before the fix, kind was inferred only from the prefix string,
        and any value other than the legacy default "DDEC8414E29A"
        defaulted to `kind="transcript"`. So a user-overridden bogus
        blocks prefix silently retrieved the transcript WAL instead.
        """
        origin = tmp_path / "origin"
        idb = origin / "IndexedDB"
        idb.mkdir(parents=True)

        transcript_folder = idb / "T_HASH"
        transcript_folder.mkdir()
        (transcript_folder / "IndexedDB.sqlite3-wal").write_bytes(
            (
                b"\x00messageId\x00abc\x00message\x00hi\x00"
                b"\x00messageId\x00def\x00message\x00ok\x00"
            ) * 10
        )

        blocks_folder = idb / "B_HASH"
        blocks_folder.mkdir()
        (blocks_folder / "IndexedDB.sqlite3-wal").write_bytes(
            b"\x00title\x00First Meeting 2026-04-28 16:00\x00" * 10
        )

        # Bogus prefix, kind="blocks" — must return blocks, not transcript.
        found = find_wal(origin, "USER_BOGUS_PREFIX", kind="blocks")
        assert found is not None
        assert found.parent.name == "B_HASH", (
            "find_wal with kind='blocks' must route to the blocks WAL "
            "even when the configured prefix matches no folder"
        )

        # Same with kind="transcript" — must return transcript.
        found_t = find_wal(origin, "USER_BOGUS_PREFIX", kind="transcript")
        assert found_t is not None
        assert found_t.parent.name == "T_HASH"

    def test_returns_none_when_origin_has_no_wals_at_all(
        self, tmp_path,
    ):
        origin = _plant_origin(
            tmp_path,
            transcript_wal=None,
            blocks_wal=None,
        )
        assert find_wal(origin, "1CB477F679D6") is None

    def test_returns_none_when_origin_has_only_unrelated_wals(
        self, tmp_path,
    ):
        """Origin exists, contains WALs, but none have transcript-shaped
        content. This is the 'Notetaker not enabled' state — the caller
        is expected to surface a real setup error."""
        origin = _plant_origin(
            tmp_path,
            transcript_wal=None,
            blocks_wal=None,
            extra_folders=[
                "AAAAAA1111111111",
                "BBBBBB2222222222",
                "CCCCCC3333333333",
            ],
        )
        assert find_wal(origin, "1CB477F679D6") is None


# ── Engine-level cache + setup error ─────────────────────────────────────


class TestEngineSetupError:
    """The engine must emit a real diagnostic when origin is found but
    the transcript WAL is not — instead of silently sitting in IDLE
    forever, which is the bug we're patching."""

    def test_setup_error_emitted_when_origin_has_no_transcript_wal(
        self, tmp_path,
    ):
        from zoom_engine import ZoomEngine

        origin = _plant_origin(
            tmp_path,
            transcript_wal=None,
            blocks_wal=None,
            extra_folders=["AAAA1111", "BBBB2222"],
        )

        events: list[dict] = []
        engine = ZoomEngine.__new__(ZoomEngine)
        engine._setup_error_emitted = False

        # Capture emit() output without spinning up the full engine.
        import zoom_engine as ze
        original_emit = ze.emit
        ze.emit = lambda payload: events.append(payload)
        try:
            engine._maybe_emit_setup_error(origin)
            engine._maybe_emit_setup_error(origin)  # idempotent
            engine._maybe_emit_setup_error(origin)
        finally:
            ze.emit = original_emit

        error_events = [e for e in events if e.get("event") == "error"]
        assert len(error_events) == 1, (
            "setup error must be emitted exactly once across multiple "
            f"calls (got {len(error_events)})"
        )
        assert "Notetaker" in error_events[0]["message"], (
            "error message must mention Notetaker so the user knows "
            "what to enable"
        )

    def test_setup_error_not_emitted_when_origin_is_none(self):
        from zoom_engine import ZoomEngine

        events: list[dict] = []
        engine = ZoomEngine.__new__(ZoomEngine)
        engine._setup_error_emitted = False

        import zoom_engine as ze
        original_emit = ze.emit
        ze.emit = lambda payload: events.append(payload)
        try:
            engine._maybe_emit_setup_error(None)
        finally:
            ze.emit = original_emit

        assert events == [], (
            "setup error must NOT fire when origin is None — the "
            "ready-event path already surfaces 'Zoom not installed' for "
            "that case"
        )
