"""Phase 8 — failed-meeting retention.

When LLM summarization fails, the in-progress accumulator gets promoted
from the root cache (24h purge window) into a `failed/` subfolder
(30-day window) along with a sidecar carrying title/error metadata.

These tests cover:
  - mark_meeting_failed: file moves, sidecar contents, idempotence
  - clear_failed_meeting: cleanup
  - delete_persisted_accumulator: clears both root AND failed/
  - load_persisted_accumulator: dual-lookup root → failed/
  - list_recoverable_meetings: merges both buckets, failed/ wins on dup
  - purge_stale_accumulators: separate windows for root vs failed/
"""
import json
import os
import time
from pathlib import Path

import pytest

import zoom_notes
from zoom_notes import (
    _FAILED_PURGE_SECS,
    _FAILED_SIDECAR_NAME,
    _failed_dir,
    _safe_meeting_id_slug,
    clear_failed_meeting,
    delete_persisted_accumulator,
    list_recoverable_meetings,
    load_persisted_accumulator,
    mark_meeting_failed,
    persist_accumulator,
    purge_stale_accumulators,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the on-disk accumulator cache to a tmp dir."""
    cache = tmp_path / "zoom-notes-cache"
    cache.mkdir()
    monkeypatch.setattr(zoom_notes, "_CACHE_DIR", cache)
    yield cache


def _make_entry(meeting_id: str, msg_id: str, speaker: str = "Alice",
                ts: str = "00:00:01", text: str = "hello") -> dict:
    return {
        "msg_id": msg_id,
        "meeting_id": meeting_id,
        "speaker": speaker,
        "timestamp": ts,
        "text": text,
    }


def _write_root_snapshot(cache: Path, meeting_id: str, n: int = 2) -> Path:
    """Helper that mimics what persist_accumulator writes into root cache."""
    entries = {f"m{i}": _make_entry(meeting_id, f"m{i}", text=f"line{i}") for i in range(n)}
    persist_accumulator(meeting_id, entries)
    slug = _safe_meeting_id_slug(meeting_id)
    return cache / f"in-progress-{slug}.json"


# ── mark_meeting_failed ──────────────────────────────────────────────────────


def test_mark_meeting_failed_moves_files_into_failed_subdir(isolated_cache):
    mid = "M-fail-1"
    _write_root_snapshot(isolated_cache, mid)
    slug = _safe_meeting_id_slug(mid)

    assert (isolated_cache / f"in-progress-{slug}.json").exists()

    mark_meeting_failed(mid, metadata={"title": "Sales Sync", "message": "API down"})

    # Root cleared
    assert not (isolated_cache / f"in-progress-{slug}.json").exists()
    assert not (isolated_cache / f"in-progress-{slug}.md").exists()

    # Failed/ populated with both files + sidecar
    failed = isolated_cache / "failed"
    assert (failed / f"in-progress-{slug}.json").exists()
    assert (failed / f"in-progress-{slug}.md").exists()
    assert (failed / f"{slug}.{_FAILED_SIDECAR_NAME}").exists()


def test_mark_meeting_failed_writes_sidecar_with_metadata_and_failed_at(isolated_cache):
    mid = "M-fail-2"
    _write_root_snapshot(isolated_cache, mid)

    mark_meeting_failed(mid, metadata={
        "title": "Q4 Planning",
        "message": "rate limit exceeded",
        "transcript_path": "/tmp/transcript.md",
        "note_path": "/tmp/note.md",
        "attendees": ["Alice", "Bob"],
    })

    slug = _safe_meeting_id_slug(mid)
    sidecar = json.loads(
        (isolated_cache / "failed" / f"{slug}.{_FAILED_SIDECAR_NAME}").read_text()
    )
    assert sidecar["title"] == "Q4 Planning"
    assert sidecar["message"] == "rate limit exceeded"
    assert sidecar["transcript_path"] == "/tmp/transcript.md"
    assert sidecar["meeting_id"] == mid
    assert sidecar["failed_at"]  # always set
    assert "T" in sidecar["failed_at"]  # ISO format


def test_mark_meeting_failed_is_idempotent_refreshes_sidecar(isolated_cache):
    """A second failure (e.g. retry-of-failed) should bump failed_at and
    update the message without crashing."""
    mid = "M-fail-3"
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={"message": "first error"})
    slug = _safe_meeting_id_slug(mid)
    sidecar_path = isolated_cache / "failed" / f"{slug}.{_FAILED_SIDECAR_NAME}"
    first_failed_at = json.loads(sidecar_path.read_text())["failed_at"]

    # Mtime resolution can collide — sleep enough to guarantee a different
    # `failed_at` second.
    time.sleep(1.1)
    mark_meeting_failed(mid, metadata={"message": "second error"})
    second = json.loads(sidecar_path.read_text())
    assert second["message"] == "second error"
    assert second["failed_at"] != first_failed_at


def test_mark_meeting_failed_no_root_snapshot_still_writes_sidecar(isolated_cache):
    """If a retry-of-failed happens, the snapshot is already in failed/,
    so the .json/.md "moves" are no-ops. The sidecar must still get
    refreshed (this is what the retry-failed branch in the engine relies on)."""
    mid = "M-fail-4"
    # First fail to land it in failed/
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={"message": "first"})

    # No root snapshot exists; second fail should still update sidecar
    mark_meeting_failed(mid, metadata={"message": "second"})
    slug = _safe_meeting_id_slug(mid)
    sidecar = json.loads(
        (isolated_cache / "failed" / f"{slug}.{_FAILED_SIDECAR_NAME}").read_text()
    )
    assert sidecar["message"] == "second"


# ── clear_failed_meeting ─────────────────────────────────────────────────────


def test_clear_failed_meeting_removes_all_files(isolated_cache):
    mid = "M-clear"
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={"message": "x"})

    clear_failed_meeting(mid)

    failed = isolated_cache / "failed"
    slug = _safe_meeting_id_slug(mid)
    assert not (failed / f"in-progress-{slug}.json").exists()
    assert not (failed / f"in-progress-{slug}.md").exists()
    assert not (failed / f"{slug}.{_FAILED_SIDECAR_NAME}").exists()


def test_clear_failed_meeting_no_op_when_nothing_to_clear(isolated_cache):
    """Should not crash if there's nothing in failed/ for this meeting."""
    clear_failed_meeting("never-failed")  # must not raise


# ── delete_persisted_accumulator clears BOTH ─────────────────────────────────


def test_delete_persisted_accumulator_clears_root_and_failed(isolated_cache):
    """The retry-success path calls this and must clean both buckets."""
    mid = "M-both"
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={"message": "x"})

    # Re-persist a root snapshot to simulate a meeting that reactivates while
    # an old failed/ entry still exists for the same id (edge case).
    _write_root_snapshot(isolated_cache, mid)

    slug = _safe_meeting_id_slug(mid)
    assert (isolated_cache / f"in-progress-{slug}.json").exists()
    assert (isolated_cache / "failed" / f"in-progress-{slug}.json").exists()

    delete_persisted_accumulator(mid)
    assert not (isolated_cache / f"in-progress-{slug}.json").exists()
    assert not (isolated_cache / "failed" / f"in-progress-{slug}.json").exists()
    assert not (isolated_cache / "failed" / f"{slug}.{_FAILED_SIDECAR_NAME}").exists()


# ── load_persisted_accumulator dual lookup ───────────────────────────────────


def test_load_persisted_accumulator_finds_in_failed_when_root_empty(isolated_cache):
    mid = "M-load-failed"
    _write_root_snapshot(isolated_cache, mid, n=3)
    mark_meeting_failed(mid, metadata={"message": "x"})

    # No root snapshot, only failed/
    slug = _safe_meeting_id_slug(mid)
    assert not (isolated_cache / f"in-progress-{slug}.json").exists()

    loaded = load_persisted_accumulator(mid)
    assert loaded is not None
    assert len(loaded) == 3


def test_load_persisted_accumulator_prefers_root_over_failed(isolated_cache):
    """If both exist (rare edge case), root is the live one and wins."""
    mid = "M-load-both"
    _write_root_snapshot(isolated_cache, mid, n=2)
    mark_meeting_failed(mid, metadata={"message": "x"})  # moves to failed
    _write_root_snapshot(isolated_cache, mid, n=5)  # new root, larger

    loaded = load_persisted_accumulator(mid)
    assert loaded is not None
    assert len(loaded) == 5  # root won


# ── list_recoverable_meetings merges buckets ─────────────────────────────────


def test_list_recoverable_meetings_includes_failed_with_sidecar_metadata(isolated_cache):
    mid = "M-list-failed"
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={
        "title": "Acme Quarterly Review",
        "message": "OpenAI 429",
    })

    found = list_recoverable_meetings()
    assert len(found) == 1
    rec = found[0]
    assert rec["meeting_id"] == mid
    assert rec["location"] == "failed"
    assert rec["title"] == "Acme Quarterly Review"
    assert rec["slug_hint"] == "Acme Quarterly Review"  # title overrides hint
    assert rec["last_error"] == "OpenAI 429"
    assert rec["failed_at"]


def test_list_recoverable_meetings_returns_root_with_root_location(isolated_cache):
    mid = "M-root-only"
    _write_root_snapshot(isolated_cache, mid)

    found = list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["location"] == "root"
    assert "title" not in found[0]
    assert "failed_at" not in found[0]


def test_list_recoverable_meetings_dedupes_by_meeting_id_failed_wins(isolated_cache):
    """If somehow the same meeting_id is in BOTH buckets, the failed/ entry
    wins because it has the title metadata that produces a better label."""
    mid = "M-dup"
    _write_root_snapshot(isolated_cache, mid)
    mark_meeting_failed(mid, metadata={"title": "Real Title"})
    _write_root_snapshot(isolated_cache, mid)  # add root again

    found = list_recoverable_meetings()
    assert len(found) == 1
    assert found[0]["location"] == "failed"
    assert found[0]["title"] == "Real Title"


def test_list_recoverable_meetings_returns_both_when_different_meetings(isolated_cache):
    _write_root_snapshot(isolated_cache, "M-live")
    _write_root_snapshot(isolated_cache, "M-dead")
    mark_meeting_failed("M-dead", metadata={"title": "Old failed meeting"})

    found = list_recoverable_meetings()
    ids_to_loc = {r["meeting_id"]: r["location"] for r in found}
    assert ids_to_loc == {"M-live": "root", "M-dead": "failed"}


# ── purge windows ────────────────────────────────────────────────────────────


def test_purge_demotes_prior_day_root_snapshots_to_failed(isolated_cache):
    """A prior-day root snapshot is moved to failed/ (not deleted) so it
    remains recoverable via the menu bar, while a fresh today snapshot stays
    in root untouched.  A 7-day-old failed/ entry survives (<30d)."""
    # Write a meeting and promote it to failed
    _write_root_snapshot(isolated_cache, "M-failed-keep")
    mark_meeting_failed("M-failed-keep", metadata={"message": "x"})

    # Write a fresh root snapshot for today (should be left alone)
    _write_root_snapshot(isolated_cache, "M-root-keep")

    # Write a prior-day root snapshot (should be demoted to failed/)
    _write_root_snapshot(isolated_cache, "M-root-demote")
    slug_root_demote = _safe_meeting_id_slug("M-root-demote")
    yesterday = time.time() - (25 * 3600)  # always a prior calendar day
    os.utime(
        isolated_cache / f"in-progress-{slug_root_demote}.json",
        (yesterday, yesterday),
    )
    os.utime(
        isolated_cache / f"in-progress-{slug_root_demote}.md",
        (yesterday, yesterday),
    )

    slug_failed_keep = _safe_meeting_id_slug("M-failed-keep")
    failed = isolated_cache / "failed"
    seven_days = time.time() - (7 * 24 * 3600)
    for f in failed.iterdir():
        os.utime(f, (seven_days, seven_days))

    purge_stale_accumulators()

    # M-root-demote moved out of root and into failed/ (with auto sidecar)
    assert not (isolated_cache / f"in-progress-{slug_root_demote}.json").exists()
    assert not (isolated_cache / f"in-progress-{slug_root_demote}.md").exists()
    assert (failed / f"in-progress-{slug_root_demote}.json").exists()
    assert (failed / f"{slug_root_demote}.{_FAILED_SIDECAR_NAME}").exists()

    # M-root-keep (today's snapshot) still in root
    slug_root_keep = _safe_meeting_id_slug("M-root-keep")
    assert (isolated_cache / f"in-progress-{slug_root_keep}.json").exists()

    # M-failed-keep (7 days old, <30d) untouched in failed/
    assert (failed / f"in-progress-{slug_failed_keep}.json").exists()
    assert (failed / f"{slug_failed_keep}.{_FAILED_SIDECAR_NAME}").exists()


def test_purge_failed_purges_entries_older_than_30_days(isolated_cache):
    _write_root_snapshot(isolated_cache, "M-ancient")
    mark_meeting_failed("M-ancient", metadata={"message": "x"})

    failed = isolated_cache / "failed"
    ancient_ts = time.time() - (_FAILED_PURGE_SECS + 3600)
    for f in failed.iterdir():
        os.utime(f, (ancient_ts, ancient_ts))

    purge_stale_accumulators()

    assert list(failed.glob("in-progress-*")) == []
    assert list(failed.glob(f"*.{_FAILED_SIDECAR_NAME}")) == []


def test_purge_with_no_failed_dir_does_not_crash(isolated_cache):
    """If failed/ never got created (no failures yet), purge must be a no-op
    on that side, not raise."""
    _write_root_snapshot(isolated_cache, "M-only-root")
    assert not _failed_dir().exists()
    purge_stale_accumulators()  # must not raise
