#!/usr/bin/env python3
"""
Zoom Notes
Reads Zoom's live AI Notetaker WAL files to extract transcripts,
then summarizes them via your configured LLM and saves to your output directory.

Usage:
  python zoom_notes.py --list          # List recent meetings found in WAL
  python zoom_notes.py --dump          # Print current transcript to stdout
  python zoom_notes.py --watch         # Live-follow transcript during a meeting
  python zoom_notes.py --notes         # Generate and save meeting notes
  python zoom_notes.py --notes --dry-run  # Preview notes without saving
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


from zoom_config import (
    ZoomNotesConfig,
    get_config,
    get_api_key,
    resolve_subfolder,
    resolve_filename,
    DEFAULT_SYSTEM_PROMPT,
)


# ── Paths ─────────────────────────────────────────────────────────────────────

ZOOM_BASE = Path.home() / "Library/Application Support/zoom.us/data"
MY_NOTES_ORIGINS = (
    ZOOM_BASE
    / "UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins"
)


# ── WAL Discovery ──────────────────────────────────────────────────────────────

def find_origin_dir() -> Path | None:
    """Find the docs.zoom.us origin directory (hash-named folder).

    Most users have a single origin hash, but multi-account / multi-profile
    Zoom setups can leave several behind in `MY_NOTES_ORIGINS`. Prefer the
    one whose transcript WAL has been modified most recently — the origin
    whose Zoom is actively writing to it. Fall back to "first match" when
    no candidate has a transcript WAL yet (cold-start, or after a fresh
    re-install where Zoom hasn't begun writing).
    """
    if not MY_NOTES_ORIGINS.exists():
        return None

    candidates: list[Path] = []
    for top in MY_NOTES_ORIGINS.iterdir():
        if top.is_dir():
            nested = top / top.name
            if (nested / "IndexedDB").exists():
                candidates.append(nested)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Resolve the freshest transcript WAL across all candidates. We avoid
    # importing zoom_config here (circular) so we look up the prefix
    # directly from its dataclass default — callers that override the
    # prefix in settings.json will still get a working WAL because
    # `find_wal` is invoked separately downstream with the configured
    # prefix; this picker just needs to know which origin is alive.
    try:
        from zoom_config import ZoomNotesConfig
        prefix = ZoomNotesConfig().transcript_db_prefix
    except Exception:
        prefix = "1CB477F679D6"

    def freshness(origin: Path) -> float:
        wal = find_wal(origin, prefix)
        if wal is None:
            return -1.0
        try:
            return wal.stat().st_mtime
        except OSError:
            return -1.0

    scored = [(freshness(c), c) for c in candidates]
    scored.sort(key=lambda t: t[0], reverse=True)
    if scored[0][0] >= 0:
        return scored[0][1]
    # No candidate has a transcript WAL yet — fall back to first match
    # (preserves previous behaviour on fresh installs).
    return candidates[0]


def find_wal(
    origin: Path,
    db_prefix: str,
    kind: str | None = None,
) -> Path | None:
    """Find the WAL file for a given IndexedDB prefix.

    Tries the configured `db_prefix` first (fast path — historically the
    correct match on the original developer's account). When no folder
    matches the prefix, falls back to scanning the WAL contents to
    identify the right database — see `find_wal_by_content`. The IndexedDB
    folder name is a per-account hash that varies across Zoom accounts and
    profiles, so the prefix can't be relied on for any user but the one it
    was captured against.

    `kind` (`"transcript"` or `"blocks"`) is required for correct
    fallback when the user has overridden the prefix in settings to
    something that matches neither default. When omitted, kind is inferred
    from the prefix matching one of the built-in defaults; if the prefix
    matches neither default and kind is omitted, the function assumes
    `"transcript"` because that's the more common call site (single
    `kind` arg covers most uses; the CLI's `--list` command queries
    transcript first and is the only legacy caller).
    """
    idb_dir = origin / "IndexedDB"
    if not idb_dir.exists():
        return None
    candidates = []
    checkpointed: list[Path] = []  # prefix match but WAL currently 0-byte
    for db_dir in idb_dir.iterdir():
        if db_dir.name.startswith(db_prefix):
            wal = db_dir / "IndexedDB.sqlite3-wal"
            if wal.exists() and wal.stat().st_size > 256:
                candidates.append(wal)
            elif wal.exists():
                # WAL exists but is 0-byte — Zoom checkpointed it between
                # meetings. Check if the main DB is substantial: if so the
                # store definitely exists and Notetaker is configured; we
                # just aren't in a meeting right now.
                sqlite3 = db_dir / "IndexedDB.sqlite3"
                try:
                    if sqlite3.exists() and sqlite3.stat().st_size > 50_000:
                        checkpointed.append(wal)
                except OSError:
                    pass
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    if checkpointed:
        # Return the checkpointed WAL so callers know the DB is present and
        # configured — prevents a false-positive "Notetaker not set up" error
        # when the engine restarts between meetings.
        return max(checkpointed, key=lambda p: p.stat().st_mtime)
    # Prefix didn't match any folder. Fall back to identifying the WAL by
    # the tokens it contains. This is the path that any non-developer user
    # actually hits — the configured prefix is effectively a hint for the
    # one machine it was originally captured on.
    if kind is None:
        if db_prefix == "DDEC8414E29A":
            kind = "blocks"
        else:
            # Includes both "1CB477F679D6" (the legacy transcript prefix)
            # and any user-supplied custom prefix. The latter is the bug
            # we're explicitly fixing — the safest default is transcript,
            # and engine callers always pass `kind` explicitly anyway.
            kind = "transcript"
    return find_wal_by_content(origin, kind)


# Tokens that mark a WAL as belonging to a particular IndexedDB store.
# `messageId` is emitted once per transcript utterance, so the transcript
# WAL contains many of them. `title` appears around each Zoom Notes block
# header, so the blocks WAL contains many. Other Zoom IndexedDB stores
# (e.g. caches, settings) don't cluster either token.
_TRANSCRIPT_SIGNATURE_TOKEN = "messageId"
_BLOCKS_SIGNATURE_TOKEN = "title"
_MIN_SIGNATURE_HITS = 2  # require at least 2 hits to count as a match —
# guards against a single stray `title` literal landing in a non-blocks
# store from JSON metadata or similar.


def _score_wal_for_kind(wal_path: Path, kind: str) -> int:
    """Count signature-token occurrences in a WAL.

    Returns 0 on read error. The caller picks the highest-scoring WAL.
    """
    token = (
        _TRANSCRIPT_SIGNATURE_TOKEN
        if kind == "transcript"
        else _BLOCKS_SIGNATURE_TOKEN
    )
    try:
        lines = read_wal_strings(wal_path)
    except Exception:
        return 0
    return sum(1 for ln in lines if ln == token)


def find_wal_by_content(origin: Path, kind: str) -> Path | None:
    """Find the transcript or blocks WAL by scanning every WAL's contents.

    `kind` is `"transcript"` or `"blocks"`. Walks every `IndexedDB.sqlite3-wal`
    under `origin/IndexedDB/`, scores each by the count of the kind's
    signature token, and returns the highest-scoring WAL with at least
    `_MIN_SIGNATURE_HITS` hits. Ties are broken by mtime (freshest wins).

    Returns None if no WAL has any signature hits — that's the signal for
    "Zoom is installed but the user hasn't run a meeting with this kind of
    data yet" (e.g. AI Notetaker not enabled). The caller should surface a
    setup error in that case rather than silently sitting in IDLE forever.
    """
    idb_dir = origin / "IndexedDB"
    if not idb_dir.exists():
        return None

    scored: list[tuple[int, float, Path]] = []
    for db_dir in idb_dir.iterdir():
        if not db_dir.is_dir():
            continue
        wal = db_dir / "IndexedDB.sqlite3-wal"
        if not wal.exists() or wal.stat().st_size <= 256:
            continue
        score = _score_wal_for_kind(wal, kind)
        if score >= _MIN_SIGNATURE_HITS:
            try:
                mtime = wal.stat().st_mtime
            except OSError:
                continue
            scored.append((score, mtime, wal))

    if not scored:
        # WAL-based discovery found nothing. For the blocks store this is
        # common: Zoom frequently checkpoints the WAL (flushing all title
        # data into the main .sqlite3 file), leaving the WAL at 0 bytes.
        # Fall back to the main DB file — read_wal_strings / strings(1)
        # works on it identically. Only makes sense for blocks (the
        # transcript store's data is short-lived in the WAL; the DB file
        # accumulates years of data that's too noisy to distinguish).
        if kind == "blocks":
            db_scored: list[tuple[int, float, Path]] = []
            for db_dir in idb_dir.iterdir():
                if not db_dir.is_dir():
                    continue
                db = db_dir / "IndexedDB.sqlite3"
                if not db.exists() or db.stat().st_size < 4096:
                    continue
                score = _score_wal_for_kind(db, kind)
                if score >= _MIN_SIGNATURE_HITS:
                    try:
                        mtime = db.stat().st_mtime
                    except OSError:
                        continue
                    db_scored.append((score, mtime, db))
            if db_scored:
                db_scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
                return db_scored[0][2]
        return None
    # Highest score wins; mtime breaks ties (freshest writes are the
    # in-progress meeting).
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


# ── WAL Parsing ────────────────────────────────────────────────────────────────

def read_wal_strings(wal_path: Path) -> list[str]:
    """Copy WAL to temp file and extract printable strings.

    Runs `strings(1)` with `-n 2` instead of the default `-n 4`. macOS's
    default 4-byte minimum drops short utterances ("OK", "Hi", "No",
    "Hmm") and short speaker names ("Li", "An", "Bo") — they simply
    never make it into the parser input, so transcripts come back with
    the speaker labeled "Unknown" and one-word answers missing entirely.

    Lowering to 2 means more raw lines come through, but the parser is
    structural — it only treats a line as a value when the line BEFORE
    it is a known token like `messageId`, `message`, `username`, or
    `meetingId`. Random two-byte ASCII garbage that happens to live
    inside a WAL page doesn't cluster around those tokens, so the noise
    floor stays low. The downstream `is_real_text` validator (length,
    junk-word, must-contain-letter) catches anything that does slip
    through.

    The cost is a modestly larger strings() output (a few % in practice
    on the Zoom WAL — measured empirically). Worth it for the
    correctness win on short answers and short names.
    """
    with tempfile.NamedTemporaryFile(suffix=".wal", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        shutil.copy2(wal_path, tmp)
        result = subprocess.run(
            ["/usr/bin/strings", "-n", "2", str(tmp)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return [l.strip() for l in result.stdout.split("\n") if l.strip()]
    finally:
        tmp.unlink(missing_ok=True)


def count_meeting_ids(wal_path: Path) -> dict[str, int]:
    """Return a dict of meetingId → occurrence count for all meetings in the WAL."""
    lines = read_wal_strings(wal_path)
    counts: dict[str, int] = {}
    for i, line in enumerate(lines):
        if line == "meetingId" and i + 1 < len(lines):
            mid = lines[i + 1]
            if mid and len(mid) > 8:
                counts[mid] = counts.get(mid, 0) + 1
    return counts


def score_meeting_ids(wal_path: Path) -> dict[str, float]:
    """Score each meetingId using entry count, recency, and speaker presence.

    Weights:
    - Base: 1 point per WAL entry
    - Recency bonus: +100,000 if any entry has a timestamp within 30 min of now
    - Speaker bonus: +1,000,000 if ZOOM_NOTES_USER_NAME matches a speaker in the meeting

    Also returns auxiliary fields used by `detect_active_meeting_id` for
    boundary-aware filtering: `latest_ts_secs` is the max wall-clock timestamp
    seen for the meeting (HH:MM:SS converted to seconds-since-midnight), used
    to drop a meeting that has already finished from active-meeting selection.

    Returns {meetingId: score}. Use max(scores, key=scores.get) to pick best.
    """
    user_name = os.environ.get("ZOOM_NOTES_USER_NAME", "").strip().lower()
    lines = read_wal_strings(wal_path)

    meeting_data: dict[str, dict] = {}
    now_hms = datetime.now().strftime("%H:%M:%S")
    try:
        now_secs = sum(int(x) * m for x, m in zip(now_hms.split(":"), [3600, 60, 1]))
    except ValueError:
        now_secs = 0
    # Recency window tightened to 30 min so a stale prior meeting still
    # resident in the WAL can't outscore a freshly-started one whose first
    # few entries were just written.
    _RECENCY_WINDOW_SECS = 30 * 60

    i = 0
    while i < len(lines):
        if lines[i] == "meetingId" and i + 1 < len(lines):
            mid = lines[i + 1]
            if mid and len(mid) > 8:
                if mid not in meeting_data:
                    meeting_data[mid] = {
                        "count": 0,
                        "has_recent": False,
                        "has_user": False,
                        "latest_ts_secs": -1,
                    }
                meeting_data[mid]["count"] += 1
                for back in range(i - 1, max(i - 60, -1), -1):
                    if lines[back] == "timeStampContent" and back + 1 < len(lines):
                        try:
                            ts_secs = sum(int(x) * m for x, m in zip(lines[back + 1].split(":"), [3600, 60, 1]))
                            if ts_secs > meeting_data[mid]["latest_ts_secs"]:
                                meeting_data[mid]["latest_ts_secs"] = ts_secs
                            if abs(now_secs - ts_secs) <= _RECENCY_WINDOW_SECS:
                                meeting_data[mid]["has_recent"] = True
                        except (ValueError, AttributeError):
                            pass
                    elif lines[back] == "username" and back + 1 < len(lines):
                        if user_name and lines[back + 1].strip().lower() == user_name:
                            meeting_data[mid]["has_user"] = True
        i += 1

    scores: dict[str, float] = {}
    for mid, data in meeting_data.items():
        score = float(data["count"])
        # Recency bonus must dominate raw entry count — a freshly-started
        # meeting may only have a handful of entries while a stale one in
        # the WAL has thousands. Without a large bonus, the stale one wins.
        if data["has_recent"]:
            score += 100_000
        if data["has_user"]:
            score += 1_000_000
        scores[mid] = score
    return scores


def score_meeting_ids_detailed(wal_path: Path) -> dict[str, dict]:
    """Like `score_meeting_ids` but returns the full per-meeting record.

    Each value is `{"count": int, "has_recent": bool, "has_user": bool,
    "latest_ts_secs": int, "score": float}`. `detect_active_meeting_id` uses
    this when callers pass a `freshness_floor_secs` to filter out meetings
    whose data is entirely older than the floor.

    Implementation reuses `score_meeting_ids` to avoid duplicating the WAL
    scan; the extra dict-building is cheap relative to the strings(1) call.
    """
    user_name = os.environ.get("ZOOM_NOTES_USER_NAME", "").strip().lower()
    lines = read_wal_strings(wal_path)
    meeting_data: dict[str, dict] = {}
    _RECENCY_WINDOW_SECS = 30 * 60
    now_hms = datetime.now().strftime("%H:%M:%S")
    try:
        now_secs = sum(int(x) * m for x, m in zip(now_hms.split(":"), [3600, 60, 1]))
    except ValueError:
        now_secs = 0

    i = 0
    while i < len(lines):
        if lines[i] == "meetingId" and i + 1 < len(lines):
            mid = lines[i + 1]
            if mid and len(mid) > 8:
                if mid not in meeting_data:
                    meeting_data[mid] = {
                        "count": 0, "has_recent": False, "has_user": False,
                        "latest_ts_secs": -1,
                    }
                meeting_data[mid]["count"] += 1
                for back in range(i - 1, max(i - 60, -1), -1):
                    if lines[back] == "timeStampContent" and back + 1 < len(lines):
                        try:
                            ts_secs = sum(int(x) * m for x, m in zip(lines[back + 1].split(":"), [3600, 60, 1]))
                            if ts_secs > meeting_data[mid]["latest_ts_secs"]:
                                meeting_data[mid]["latest_ts_secs"] = ts_secs
                            if abs(now_secs - ts_secs) <= _RECENCY_WINDOW_SECS:
                                meeting_data[mid]["has_recent"] = True
                        except (ValueError, AttributeError):
                            pass
                    elif lines[back] == "username" and back + 1 < len(lines):
                        if user_name and lines[back + 1].strip().lower() == user_name:
                            meeting_data[mid]["has_user"] = True
        i += 1

    for mid, data in meeting_data.items():
        score = float(data["count"])
        if data["has_recent"]:
            score += 100_000
        if data["has_user"]:
            score += 1_000_000
        data["score"] = score
    return meeting_data


def detect_active_meeting_id(
    wal_path: Path,
    *,
    exclude_meeting_id: str | None = None,
    freshness_floor_secs: int | None = None,
) -> str | None:
    """Return the best-scoring meetingId from the WAL.

    Uses score_meeting_ids() which factors in entry count, timestamp recency,
    and whether ZOOM_NOTES_USER_NAME appears as a speaker — preventing a
    ghost/double-booked meeting from crowding out the one you actually attended.

    Boundary-aware filtering (the 2026-04-30 lesson):
      `exclude_meeting_id` drops a specific id from contention unless its
      latest WAL timestamp exceeds `freshness_floor_secs`.  The engine passes
      the just-completed meeting here so its still-resident WAL data can't
      trap detection on the next IDLE -> ACTIVE transition.

      The freshness exception is essential for meetings with silent
      collaboration periods (FigJam, whiteboard, screen-share reading) that
      are long enough to trip the 90-second idle threshold mid-meeting.  Once
      speech resumes the WAL gets new entries with timestamps beyond the
      floor, and detection must be allowed to re-latch onto the same meeting
      ID rather than orphaning the remaining transcript.

      `freshness_floor_secs` additionally drops any meeting whose latest WAL
      timestamp is <= this floor (HH:MM:SS converted to seconds-since-
      midnight). When the engine knows a session ended at 11:21, a meeting
      whose newest entry is 11:21 or older can't be the *new* active meeting
      — it's by definition from a session that finished.  Without this, the
      just-ended meeting's huge entry count + recency bonus dominates the
      freshly-starting one's few-entry count, and the engine locks onto the
      wrong id.
    """
    if exclude_meeting_id is None and freshness_floor_secs is None:
        scores = score_meeting_ids(wal_path)
        return max(scores, key=lambda k: scores[k]) if scores else None

    detailed = score_meeting_ids_detailed(wal_path)
    eligible: dict[str, float] = {}
    for mid, data in detailed.items():
        if exclude_meeting_id and mid == exclude_meeting_id:
            # Block checkpoint replays of the just-processed session, but
            # allow the *same* meeting to be re-detected if it has genuinely
            # new entries beyond the freshness floor.  This handles meetings
            # that have silent collaboration periods (FigJam, whiteboard,
            # screen-share reading) long enough to trigger premature idle:
            # once speech resumes the timestamps exceed the floor and the
            # meeting can be tracked again instead of being locked out.
            has_new_content = (
                freshness_floor_secs is not None
                and data["latest_ts_secs"] > freshness_floor_secs
            )
            if not has_new_content:
                continue
        if freshness_floor_secs is not None and data["latest_ts_secs"] >= 0 \
                and data["latest_ts_secs"] <= freshness_floor_secs:
            continue
        eligible[mid] = data["score"]
    return max(eligible, key=lambda k: eligible[k]) if eligible else None


# ── Transcript persistence ─────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / ".cache" / "zoom-notes"
# Confirmed-failed snapshots get moved here from the root cache. Purge
# window is much wider (30 days vs 24h for the root) because the user
# may need a long time to notice / fix the underlying cause (API down,
# quota exceeded, on vacation, etc.).
_FAILED_SUBDIR = "failed"
_FAILED_SIDECAR_NAME = "failed.json"
_FAILED_PURGE_SECS = 30 * 24 * 3600


def _atomic_write_text(path: Path, data: str) -> None:
    """Write text to `path` atomically: write to .tmp, fsync, rename.

    Prevents partial reads if the engine crashes mid-write or another
    process happens to read the file during a refresh.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _safe_meeting_id_slug(meeting_id: str) -> str:
    """Make a meeting ID safe for use as a filename component.

    Zoom meeting IDs are base64-ish and may contain `/`, `+`, `=` — none of
    which we want in filenames. Replace any non-alphanumeric character with
    `_` so the cache file is always a valid POSIX filename.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", meeting_id)


def persist_accumulator(meeting_id: str, entries: dict) -> None:
    """Write the current accumulator snapshot atomically.

    Writes two files to ~/.cache/zoom-notes/:
      - in-progress-{slug}.json  — full structured entries for retry/replay
      - in-progress-{slug}.md    — human-readable transcript for crash recovery

    Both files are written atomically (write-tmp, fsync, rename) so a crash
    during the write can never leave a half-written file on disk.

    Cross-meeting contamination guard: the in-memory accumulator in
    zoom_engine is intentionally permissive (it captures everything
    parse_transcript yields, even when current_meeting_id might be wrong,
    so a stale meeting ID can never silently drop new utterances). At the
    persistence boundary we tighten that contract — the on-disk snapshot
    keyed under `meeting_id` must only contain entries whose own
    meeting_id matches (or is missing, since early WAL pages may not yet
    carry the meetingId field). Without this filter, when two meetings'
    entries co-exist in the WAL — which is normal during the first few
    seconds of a back-to-back meeting — the snapshot under meeting B's
    slug ends up holding entries from meeting A, and that file then
    surfaces forever as a "Recover unfinished meeting" ghost on every
    engine startup.
    """
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        slug = _safe_meeting_id_slug(meeting_id)
        filtered_values = [
            e for e in entries.values()
            if not e.get("meeting_id") or e.get("meeting_id") == meeting_id
        ]
        sorted_entries = sorted(
            filtered_values, key=lambda e: e.get("timestamp") or ""
        )
        json_path = _CACHE_DIR / f"in-progress-{slug}.json"
        _atomic_write_text(
            json_path, json.dumps(sorted_entries, ensure_ascii=False)
        )
        md_path = _CACHE_DIR / f"in-progress-{slug}.md"
        header = (
            f"# Live transcript (in progress)\n\n"
            f"Meeting ID: `{meeting_id}`  \n"
            f"Last updated: {datetime.now().isoformat(timespec='seconds')}  \n"
            f"Entries: {len(sorted_entries)}\n\n"
            f"---\n\n"
        )
        _atomic_write_text(md_path, header + format_transcript(sorted_entries) + "\n")
    except OSError:
        pass


def _failed_dir() -> Path:
    return _CACHE_DIR / _FAILED_SUBDIR


def _persisted_paths(meeting_id: str) -> tuple[Path | None, str]:
    """Resolve the on-disk snapshot for `meeting_id`, checking root first
    then `failed/`. Returns (path_or_none, location) where location is
    'root', 'failed', or '' if not found. Used by retry/recover to find
    the snapshot regardless of which lifecycle bucket it currently sits in.
    """
    slug = _safe_meeting_id_slug(meeting_id)
    root_path = _CACHE_DIR / f"in-progress-{slug}.json"
    if root_path.exists():
        return root_path, "root"
    failed_path = _failed_dir() / f"in-progress-{slug}.json"
    if failed_path.exists():
        return failed_path, "failed"
    return None, ""


def load_persisted_accumulator(meeting_id: str) -> dict | None:
    """Load a previously persisted accumulator snapshot keyed by msg_id.

    Searches the root cache first (live + recently-failed meetings), then
    falls back to `failed/` (confirmed-failed meetings inside their 30-day
    retention window). This dual-lookup is what lets retry / recover keep
    working even after a failed meeting has been promoted out of root.
    """
    path, _location = _persisted_paths(meeting_id)
    if path is None:
        return None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        return {e["msg_id"]: e for e in entries if "msg_id" in e}
    except Exception:
        return None


def load_failed_sidecar(meeting_id: str) -> dict | None:
    """Return the sidecar metadata dict for a failed meeting, or None if absent."""
    slug = _safe_meeting_id_slug(meeting_id)
    sidecar_path = _failed_dir() / f"{slug}.{_FAILED_SIDECAR_NAME}"
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_persisted_accumulator(meeting_id: str) -> None:
    """Remove the in-progress snapshot after successful note generation.

    Clears BOTH the root and the `failed/` location. The retry success path
    is the one common entry point that should clean up regardless of which
    bucket the snapshot was sitting in — without this, a successfully
    retried meeting would linger in `failed/` for 30 days and keep showing
    up as a recoverable item in the menu.
    """
    slug = _safe_meeting_id_slug(meeting_id)
    targets = [
        _CACHE_DIR / f"in-progress-{slug}.json",
        _CACHE_DIR / f"in-progress-{slug}.md",
        _failed_dir() / f"in-progress-{slug}.json",
        _failed_dir() / f"in-progress-{slug}.md",
        _failed_dir() / f"{slug}.{_FAILED_SIDECAR_NAME}",
    ]
    for path in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def mark_meeting_failed(meeting_id: str, metadata: dict | None = None) -> None:
    """Promote a snapshot from the root cache into `failed/`.

    Called from the engine's note_failed branch after the placeholder note
    is written. Moves both the .json and .md files atomically and writes
    a sidecar with the metadata the menu bar needs to label the recovery
    item ("Sales Sync — failed 3 days ago", etc.).

    Idempotent: if the snapshot is already in `failed/` (e.g. retry
    failed again), the .json/.md moves are no-ops and the sidecar is
    rewritten with fresh metadata + a refreshed `failed_at` timestamp.

    `metadata` is a dict written verbatim to the sidecar. Recommended keys:
      title, attendees, transcript_path, note_path, message, date_str.
    `failed_at` is always added/refreshed by this function.
    """
    slug = _safe_meeting_id_slug(meeting_id)
    failed_dir = _failed_dir()
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    for ext in (".json", ".md"):
        src = _CACHE_DIR / f"in-progress-{slug}{ext}"
        dst = failed_dir / f"in-progress-{slug}{ext}"
        if src.exists():
            try:
                os.replace(src, dst)
            except OSError:
                pass

    sidecar = dict(metadata or {})
    sidecar["meeting_id"] = meeting_id
    sidecar["failed_at"] = datetime.now().isoformat(timespec="seconds")
    sidecar_path = failed_dir / f"{slug}.{_FAILED_SIDECAR_NAME}"
    try:
        _atomic_write_text(
            sidecar_path,
            json.dumps(sidecar, ensure_ascii=False),
        )
    except OSError:
        pass


def clear_failed_meeting(meeting_id: str) -> None:
    """Remove the `failed/` snapshot + sidecar after a successful retry.

    Distinct entry point from `delete_persisted_accumulator` so callers
    that have already cleaned up root can target only the failed bucket
    without re-touching root paths. In practice the retry path uses
    `delete_persisted_accumulator` (clears both); this is here for tests
    and for any future caller that wants surgical cleanup.
    """
    slug = _safe_meeting_id_slug(meeting_id)
    failed_dir = _failed_dir()
    targets = [
        failed_dir / f"in-progress-{slug}.json",
        failed_dir / f"in-progress-{slug}.md",
        failed_dir / f"{slug}.{_FAILED_SIDECAR_NAME}",
    ]
    for path in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _scan_recoverable_in_dir(
    directory: Path, min_entries: int, location: str
) -> list[dict]:
    """Scan one cache directory for in-progress accumulator files.

    Helper for `list_recoverable_meetings` — extracted so we can scan the
    root and `failed/` buckets with the same logic but tag the results
    with their location and (for failed/) merge sidecar metadata.
    """
    out: list[dict] = []
    if not directory.exists():
        return out

    for json_path in directory.glob("in-progress-*.json"):
        try:
            entries = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list) or len(entries) < min_entries:
            continue

        meeting_ids = {
            e.get("meeting_id") for e in entries
            if isinstance(e, dict) and e.get("meeting_id")
        }
        if not meeting_ids:
            continue
        if len(meeting_ids) > 1:
            from collections import Counter
            counter = Counter(
                e.get("meeting_id") for e in entries
                if isinstance(e, dict) and e.get("meeting_id")
            )
            meeting_id = counter.most_common(1)[0][0]
        else:
            meeting_id = next(iter(meeting_ids))

        try:
            mtime = json_path.stat().st_mtime
        except OSError:
            continue

        first_speaker = next(
            (e.get("speaker") for e in entries
             if isinstance(e, dict) and e.get("speaker") and e.get("speaker") != "Unknown"),
            None,
        )
        timestamps = sorted(
            e.get("timestamp") for e in entries
            if isinstance(e, dict) and e.get("timestamp")
        )
        earliest_ts = timestamps[0] if timestamps else None

        slug_hint_parts = []
        if first_speaker:
            slug_hint_parts.append(first_speaker)
        if earliest_ts:
            slug_hint_parts.append(earliest_ts)
        slug_hint = " — ".join(slug_hint_parts) if slug_hint_parts else "Recovered meeting"

        record = {
            "meeting_id": meeting_id,
            "entry_count": len(entries),
            "last_updated": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "last_updated_ts": mtime,
            "slug_hint": slug_hint,
            "path": str(json_path),
            "location": location,
        }

        if location == "failed":
            slug = _safe_meeting_id_slug(meeting_id)
            sidecar_path = directory / f"{slug}.{_FAILED_SIDECAR_NAME}"
            if sidecar_path.exists():
                try:
                    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    if isinstance(sidecar, dict):
                        if sidecar.get("title"):
                            record["title"] = sidecar["title"]
                            record["slug_hint"] = sidecar["title"]
                        if sidecar.get("failed_at"):
                            record["failed_at"] = sidecar["failed_at"]
                        if sidecar.get("message"):
                            record["last_error"] = sidecar["message"]
                except (OSError, json.JSONDecodeError):
                    pass

        out.append(record)

    return out


def list_recoverable_meetings(min_entries: int = 1) -> list[dict]:
    """Scan the cache dir for in-progress accumulator files left from prior runs.

    Returns a list of dicts, one per recoverable meeting, sorted by
    last-updated time descending (newest first). Each dict has:
      - meeting_id     : the original Zoom meeting ID (extracted from inside
                         the JSON, since the on-disk slug is lossy)
      - entry_count    : number of unique transcript entries on disk
      - last_updated   : ISO8601 timestamp of the snapshot's mtime
      - last_updated_ts: float unix timestamp (for sorting / age comparisons)
      - slug_hint      : a best-effort human-readable label derived from the
                         first speaker + earliest timestamp (or the title
                         from the failed/ sidecar when present)
      - path           : absolute path to the JSON snapshot
      - location       : 'root' (live or just-failed) or 'failed' (confirmed-
                         failed, sitting in `failed/` inside its 30-day window)

    For `location == 'failed'` records, additional fields populated from the
    sidecar when present:
      - title          : the human meeting title at the time of failure
      - failed_at      : ISO8601 timestamp the snapshot was promoted to failed/
      - last_error     : the LLM error message that triggered the failure

    Files with fewer than `min_entries` entries are skipped — they're either
    empty placeholders from a meeting that ended before any speech, or
    corrupt JSON. The caller (engine startup) uses this to decide whether to
    emit a recovery_available event.

    De-duplication: if the same meeting_id appears in BOTH root and failed/
    (briefly possible during the move, or after a snapshot was re-persisted
    after a failed retry started), the failed/ entry wins — its sidecar has
    the title metadata that produces a better menu bar label.

    Critical: this function reads the meeting_id from inside the JSON rather
    than parsing it out of the filename. `_safe_meeting_id_slug` replaces
    non-alphanumeric chars with `_` for filesystem safety, which is lossy —
    `abc+/=` and `abc___` would both produce the same filename. The original
    ID is only recoverable from the entries themselves.
    """
    if not _CACHE_DIR.exists():
        return []
    root = _scan_recoverable_in_dir(_CACHE_DIR, min_entries, "root")
    failed = _scan_recoverable_in_dir(_failed_dir(), min_entries, "failed")

    by_id: dict[str, dict] = {}
    for record in root:
        by_id[record["meeting_id"]] = record
    for record in failed:
        by_id[record["meeting_id"]] = record

    out = list(by_id.values())
    out.sort(key=lambda d: d["last_updated_ts"], reverse=True)
    return out


def _demote_snapshot_to_failed(json_path: Path) -> None:
    """Move a prior-day root snapshot to failed/ so it surfaces for manual recovery.

    Called by purge_stale_accumulators for root .json files whose mtime is
    from a prior calendar day.  The snapshot must not auto-load into a live
    session (that causes Case-B abandoned-meeting logic to generate notes for
    the wrong meeting), but deleting it silently would destroy unrecovered
    transcripts.  Moving it to failed/ is the right middle ground: the engine
    ignores it for live detection, and the menu bar surfaces it as a
    recoverable meeting.

    Idempotent: if either destination file already exists it is left in place.
    """
    slug = json_path.stem[len("in-progress-"):]
    failed_dir = _failed_dir()
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    for ext in (".json", ".md"):
        src = json_path.parent / f"in-progress-{slug}{ext}"
        dst = failed_dir / f"in-progress-{slug}{ext}"
        if src.exists() and not dst.exists():
            try:
                os.replace(src, dst)
            except OSError:
                pass

    # Write a minimal sidecar only if one doesn't already exist (a real LLM
    # failure sidecar written earlier takes precedence).
    sidecar_path = failed_dir / f"{slug}.{_FAILED_SIDECAR_NAME}"
    if not sidecar_path.exists():
        try:
            _atomic_write_text(
                sidecar_path,
                json.dumps({
                    "demoted_at": datetime.now().isoformat(timespec="seconds"),
                    "message": "Notes were not generated (auto-archived from a prior day).",
                }, ensure_ascii=False),
            )
        except OSError:
            pass


def purge_stale_accumulators(
    failed_max_age_secs: int = _FAILED_PURGE_SECS,
) -> None:
    """Demote prior-day root snapshots to failed/ and purge aged-out failed/ entries.

    ROOT (`~/.cache/zoom-notes/in-progress-*`)
        Any snapshot whose file mtime is from a prior calendar day is moved
        to failed/ rather than deleted, preserving it for manual recovery
        through the menu bar.  Snapshots from today stay in root for same-day
        retry and the normal live-session seed path.  Stale .md orphans (whose
        .json companion was already removed) and partial .tmp writes older than
        1 hour are deleted outright since they carry no recoverable content.

    FAILED (`~/.cache/zoom-notes/failed/in-progress-*` + sidecars)
        Purged after `failed_max_age_secs` (default 30 days).  Confirmed
        failures need a wide window because the user may not notice or fix the
        underlying cause (API quota, vacation, broken model config) for days.
    """
    if not _CACHE_DIR.exists():
        return

    today = datetime.now().date()
    now = time.time()

    # Demote prior-day root .json snapshots to failed/.
    for json_path in _CACHE_DIR.glob("in-progress-*.json"):
        try:
            mtime = json_path.stat().st_mtime
            if datetime.fromtimestamp(mtime).date() < today:
                _demote_snapshot_to_failed(json_path)
        except OSError:
            pass

    # Clean up orphaned .md files (their .json was already demoted or deleted).
    for md_path in _CACHE_DIR.glob("in-progress-*.md"):
        try:
            if not md_path.with_suffix(".json").exists():
                md_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Delete partial .tmp writes older than 1 hour (crashed mid-write).
    for tmp_path in _CACHE_DIR.glob("in-progress-*.tmp"):
        try:
            if now - tmp_path.stat().st_mtime > 3600:
                tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    failed_dir = _failed_dir()
    if not failed_dir.exists():
        return
    for pattern in (
        "in-progress-*.json",
        "in-progress-*.md",
        "in-progress-*.tmp",
        f"*.{_FAILED_SIDECAR_NAME}",
    ):
        for f in failed_dir.glob(pattern):
            try:
                if now - f.stat().st_mtime > failed_max_age_secs:
                    f.unlink(missing_ok=True)
            except OSError:
                pass


def _sanitize_speaker(name: str) -> str:
    """Strip control chars and clip to 80 chars.

    Speaker names are user-controlled (Zoom self-rename / external attendees)
    and end up both in the rendered Markdown transcript AND in the LLM prompt
    context, so they're a small prompt-injection vector. Stripping ASCII
    control codes and clipping to a reasonable length keeps things clean
    without blocking legitimate emoji or non-Latin scripts.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()
    return cleaned[:80] if cleaned else ""


def parse_transcript(wal_path: Path, meeting_id_filter: str | None = None) -> list[dict]:
    """
    Parse transcript entries from WAL strings output.

    Each entry in the WAL looks like:
      messageId
      <id>          ← e.g. "16:0:16787456:0:217494649"
      message
      <text>
      timeStampContent
      <HH:MM:SS>
      ...
      speaker
      speakerId
      username
      <name>
      ...
      meetingId
      <id>

    The WAL stores multiple pages so entries repeat. We deduplicate
    first by messageId (exact copies), then by rolling-update logic.

    If meeting_id_filter is provided, only entries from that meeting are kept.
    """
    lines = read_wal_strings(wal_path)

    # Pass 1: collect all raw entries keyed by messageId
    # messageId → best (longest text) entry
    by_id: dict[str, dict] = {}
    id_order: list[str] = []  # preserve first-seen order

    _JUNK_EXACT = {
        "timeStampContent", "timeStampSeconds", "textLanguage",
        "startTimeMsec", "endTimeMsec", "messageId", "uniqueUserId",
        "meetingId", "speaker", "speakerId", "username", "userId",
        "originalName", "avatarUrl", "avatarName", "message",
    }
    # Strict HH:MM:SS form. Rejects partial values like "16:33:" that occur
    # when `strings -n 2` truncates the seconds digits — those would
    # otherwise sort to the wrong place and corrupt title resolution.
    _TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")

    i = 0
    while i < len(lines):
        if lines[i] == "messageId" and i + 1 < len(lines):
            msg_id = lines[i + 1]
            # Expect "message" two lines later
            if i + 2 < len(lines) and lines[i + 2] == "message" and i + 3 < len(lines):
                text = lines[i + 3]
                timestamp = None
                speaker = None
                meeting_id = None

                # Forward-walk for this entry's metadata. Start AFTER the
                # entry's own message text (i + 4) and stop the moment we
                # cross into the next entry — `messageId` or `message` are
                # the boundary markers. The 2026-05-04 Quick-Chat-on-PnP
                # incident: when `strings -n 2` ate the leading bytes of
                # the next entry's `messageId` line down to a fragment,
                # the old loop continued past the boundary and slurped the
                # next entry's username / timestamp / meetingId into this
                # one, producing a Nick utterance attributed to Michael
                # Huard from a totally different (Brand MLT) meeting.
                for j in range(i + 4, min(i + 60, len(lines))):
                    line = lines[j]
                    if line == "messageId" or line == "message":
                        break
                    if line == "timeStampContent" and j + 1 < len(lines):
                        cand = lines[j + 1]
                        if _TS_RE.match(cand):
                            timestamp = cand
                    elif line == "username" and j + 1 < len(lines):
                        speaker = _sanitize_speaker(lines[j + 1])
                    elif line == "meetingId" and j + 1 < len(lines):
                        meeting_id = lines[j + 1]
                        break

                # Length floor lowered from >3 to >=2 in tandem with the
                # `strings -n 2` change in read_wal_strings — without this,
                # the short utterances that strings now emits would be
                # filtered right back out here. The other validators (junk
                # word list, prefix bans, must-contain-letter) carry the
                # weight that the length check used to.
                is_real_text = (
                    len(text) >= 2
                    and text not in _JUNK_EXACT
                    and not text.startswith(("{", "http", "BLOCK_", "PRODUCT_", "16:0:"))
                    and not text.isdigit()
                    and not all(c in "0123456789TZ:.-+=" for c in text)
                    and any(c.isalpha() for c in text)
                )

                if is_real_text:
                    if meeting_id_filter and meeting_id and meeting_id != meeting_id_filter:
                        i += 4
                        continue
                    if msg_id not in by_id:
                        id_order.append(msg_id)
                        by_id[msg_id] = {
                            "speaker": speaker or "Unknown",
                            "text": text,
                            "timestamp": timestamp,
                            "msg_id": msg_id,
                            "meeting_id": meeting_id,
                        }
                    else:
                        # Keep longest text for this message (rolling word updates)
                        existing = by_id[msg_id]
                        if len(text) > len(existing["text"]):
                            existing["text"] = text
                        if speaker:
                            existing["speaker"] = speaker
                        if timestamp:
                            existing["timestamp"] = timestamp
                        if meeting_id:
                            existing["meeting_id"] = meeting_id
                i += 4
                continue
        i += 1

    raw = [by_id[mid] for mid in id_order]

    # Pass 2: sort by timestamp so entries are chronological
    def ts_sort_key(e: dict) -> tuple:
        ts = e.get("timestamp") or ""
        # timestamps are HH:MM:SS — sort lexicographically works fine
        return ts

    raw.sort(key=ts_sort_key)

    # Pass 3: merge consecutive same-speaker entries that are sub-strings
    return deduplicate(raw)


def deduplicate(entries: list[dict]) -> list[dict]:
    """
    Remove rolling word-by-word partial updates — keep the longest
    (most complete) version of each utterance from the same speaker.
    """
    result = []
    for entry in entries:
        if result and result[-1]["speaker"] == entry["speaker"]:
            prev_text = result[-1]["text"]
            curr_text = entry["text"]
            if curr_text.startswith(prev_text) or prev_text.startswith(curr_text):
                result[-1]["text"] = max(prev_text, curr_text, key=len)
                # Keep the later (more complete) timestamp
                if entry["timestamp"]:
                    result[-1]["timestamp"] = entry["timestamp"]
                continue
        result.append(dict(entry))
    return result


_TITLE_JUNK = {
    "fileId", "rootBlockId", "parentId", "nextBlockId", "updatedBy",
    "createdBy", "version", "type", "content", "title", "style",
    "productId", "pageIcon", "pageIconUpdateAt", "docTitleUpdateAt",
    "editorsVisible", "lastUpdatedVisible", "readTimeVisible",
    "visitsVisible", "updatedAt", "createdAt", "lastUsed", "pageId",
    "Zoom Meeting",
    # Zoom UI / calendar-sync status strings that appear in the blocks WAL
    # when calendar integration is misconfigured or disabled.
    "Google Calendar not synced",
    "Calendar is not synced",
    "No calendar connected",
    "Connect your calendar",
    "Calendar not connected",
}

# Matches a space-separated token that looks like a base64/hex meeting-ID fragment
# (≥16 chars, only alphanumeric + base64 padding — no human punctuation).
# Zoom stores user-authored note text adjacent to meeting-ID hashes in the
# blocks WAL; when strings(1) extracts them the hash fuses to the last word of
# the note.  Meeting titles never contain such tokens.
_HASH_TOKEN_RE = re.compile(r'(?<!\S)[A-Za-z0-9+/=]{16,}(?!\S)')


def _title_has_hash_token(text: str) -> bool:
    """Return True if any fragment of text looks like a raw ID/hash.

    Splits on any non-alphanumeric character so that hashes fused to
    punctuation (e.g. "one?26oit2v1HSQSi5kic4VLE7kQ") are still caught.
    """
    fragments = re.split(r'[^A-Za-z0-9+/=]+', text)
    return any(_HASH_TOKEN_RE.fullmatch(f) for f in fragments if f)


_CALENDAR_EVENTS_PATH = Path.home() / ".local" / "share" / "zoom-notes" / "calendar_events.json"


def read_calendar_title(transcript_entries: list[dict]) -> str | None:
    """Return the Apple Calendar event title whose window covers this transcript.

    Reads the sidecar written by CalendarService.swift every 5 minutes.
    The sidecar contains today's multi-attendee events with HH:MM start/end
    times. Finds the event whose start <= earliest_transcript_time <= end.
    When multiple events overlap, prefers the one closest to starting.
    Returns None if the file is absent or no event matches.
    """
    try:
        raw = json.loads(_CALENDAR_EVENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    ts_strings = sorted(e.get("timestamp") for e in transcript_entries if e.get("timestamp"))
    if not ts_strings:
        return None

    # Earliest transcript entry as HH:MM for comparison against sidecar HH:MM times
    ref_hhmm = ts_strings[0][:5]  # "HH:MM"

    best_title: str | None = None
    best_delta: int | None = None  # in minutes, smaller = closer to start of event

    for event in raw:
        title = event.get("title", "").strip()
        start = event.get("startDate", "")
        end = event.get("endDate", "")
        if not title or len(start) != 5 or len(end) != 5:
            continue
        if not (start <= ref_hhmm <= end):
            continue
        # delta in minutes from event start to first transcript entry
        try:
            sh, sm = int(start[:2]), int(start[3:])
            rh, rm = int(ref_hhmm[:2]), int(ref_hhmm[3:])
            delta = (rh * 60 + rm) - (sh * 60 + sm)
        except ValueError:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_title = title

    return best_title


def parse_meeting_title(blocks_wal: Path, transcript_entries: list[dict] | None = None) -> str | None:
    """Extract the meeting title from the blocks WAL.

    When transcript_entries is provided, matches the title whose embedded
    Zoom start-time (YYYY-MM-DD HH:MM) is closest to the transcript's own
    timestamps, so we pick the right meeting when the WAL holds multiple sessions.
    Falls back to the last title found if no time-based match is possible.

    Tight matching window (the 2026-04-30 lesson):
      Previously this used a 2-hour window and grabbed the closest title
      within it. When two unrelated meetings ran back-to-back on the same
      morning, and the second meeting's AI Notetaker never wrote a fresh
      title to the blocks WAL (e.g. Notetaker was off, or hadn't reached
      block-creation yet), the parser cheerfully matched the FIRST
      meeting's stale title (31 minutes off) and the second meeting got
      saved as "Daily Standup", overwriting the actual standup's note.

      Now: the window is 10 minutes, AND a candidate title is only
      eligible if its embedded start time is at or before the transcript's
      earliest entry by no more than that window. A title can't post-date
      the meeting it's supposed to label. If nothing matches, the caller
      gets None and falls back to the generic "Zoom Meeting <date> <time>"
      label — better a generic name than a wrong-meeting name.
    """
    lines = read_wal_strings(blocks_wal)
    titles = []
    for i, line in enumerate(lines):
        if line == "title" and i + 1 < len(lines):
            candidate = lines[i + 1]
            if (
                len(candidate) > 5
                and candidate not in _TITLE_JUNK
                and not candidate.startswith(("http", "{", "[", "BLOCK_", "PRODUCT_"))
                and " " in candidate
                and any(c.isalpha() for c in candidate)
                and not _title_has_hash_token(candidate)
            ):
                titles.append(candidate)

    if not titles:
        return None

    if not transcript_entries:
        return titles[-1]

    # Build a reference time from the transcript: use the earliest HH:MM:SS timestamp
    # combined with today's date to get a comparable datetime.
    ts_strings = [e.get("timestamp") for e in transcript_entries if e.get("timestamp")]
    if not ts_strings:
        return titles[-1]

    ts_strings.sort()
    earliest_ts = ts_strings[0]  # HH:MM:SS
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        ref_dt = datetime.strptime(f"{today} {earliest_ts}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return titles[-1]

    # Parse the date+time embedded in each Zoom title: "Name YYYY-MM-DD HH:MM(GMT...)"
    # Only accept titles whose embedded start time is within 10 minutes BEFORE
    # the transcript's earliest entry (with 60s slack on the future side for
    # clock skew between Zoom's title timestamp and the first parsed utterance).
    _zoom_time_re = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")
    _TITLE_LATE_TOLERANCE_SECS = 10 * 60
    _TITLE_FUTURE_SLACK_SECS = 60
    best_title = None
    best_delta = None
    for t in titles:
        m = _zoom_time_re.search(t)
        if not m:
            continue
        try:
            title_dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        # Signed delta: positive when title is BEFORE the transcript (the
        # normal case — Zoom stamps the meeting start, which precedes the
        # first utterance). Negative would mean the title is from a meeting
        # that started AFTER the transcript's first entry, which is
        # physically impossible for the active meeting.
        delta = (ref_dt - title_dt).total_seconds()
        if delta < -_TITLE_FUTURE_SLACK_SECS:
            continue
        if delta > _TITLE_LATE_TOLERANCE_SECS:
            continue
        abs_delta = abs(delta)
        if best_delta is None or abs_delta < best_delta:
            best_delta = abs_delta
            best_title = t

    # If no title matched within the tight window, fall back to the last title
    # only if it has no parseable timestamp (i.e. a custom/renamed meeting title).
    #
    # Guard: if the WAL already has at least one today-dated title for a
    # *different* meeting, Zoom's AI Notetaker was active today and would
    # have written a fresh dated entry for the current meeting too — unless
    # the Notetaker simply wasn't enabled for it. In that case a stale
    # custom-named entry (e.g. "Daily Standup" from a past session) is
    # indistinguishable from a legitimate custom title, so we return None
    # and let the caller fall back to the generic "Zoom Meeting" label.
    if best_title is None:
        has_same_day_timestamped = any(
            _zoom_time_re.search(t) and today in t for t in titles
        )
        if not has_same_day_timestamped:
            for t in reversed(titles):
                if not _zoom_time_re.search(t):
                    return t
        return None

    return best_title


# ── Transcript Formatting ──────────────────────────────────────────────────────

def format_transcript(entries: list[dict]) -> str:
    lines = []
    prev_speaker = None
    for e in entries:
        if e["speaker"] != prev_speaker:
            ts = f" [{e['timestamp']}]" if e["timestamp"] else ""
            lines.append(f"\n**{e['speaker']}**{ts}")
            prev_speaker = e["speaker"]
        lines.append(f"  {e['text']}")
    return "\n".join(lines).strip()


# ── LLM Summarization ──────────────────────────────────────────────────────────

def summarize_with_claude(
    transcript: str,
    meeting_title: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    api_url: str = "https://api.anthropic.com/v1/messages",
    system_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Call Claude (Anthropic) API to summarize the transcript."""
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    payload = {
        "model": model,
        "max_tokens": 64000,
        "system": prompt,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Meeting: {meeting_title}\n\n"
                    f"Transcript:\n\n{transcript}"
                ),
            }
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    return _http_retry(req, lambda body: body["content"][0]["text"], cancel_event=cancel_event)


def summarize_with_openai(
    transcript: str,
    meeting_title: str,
    api_key: str,
    model: str = "gpt-4o",
    api_url: str = "https://api.openai.com/v1/chat/completions",
    system_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Call OpenAI Chat Completions API to summarize the transcript."""
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Meeting: {meeting_title}\n\n"
                    f"Transcript:\n\n{transcript}"
                ),
            },
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    return _http_retry(
        req,
        lambda body: body["choices"][0]["message"]["content"],
        cancel_event=cancel_event,
    )


def summarize_with_gemini(
    transcript: str,
    meeting_title: str,
    api_key: str,
    model: str = "gemini-2.0-flash",
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models",
    system_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Call Google Gemini generateContent API to summarize the transcript."""
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    url = f"{base_url}/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            f"Meeting: {meeting_title}\n\n"
                            f"Transcript:\n\n{transcript}"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 8192},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    return _http_retry(
        req,
        lambda body: body["candidates"][0]["content"]["parts"][0]["text"],
        cancel_event=cancel_event,
    )


def summarize_with_ollama(
    transcript: str,
    meeting_title: str,
    model: str = "llama3.2",
    base_url: str = "http://localhost:11434",
    system_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Call Ollama /api/chat to summarize the transcript (no API key needed)."""
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Meeting: {meeting_title}\n\n"
                    f"Transcript:\n\n{transcript}"
                ),
            },
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    return _http_retry(req, lambda body: body["message"]["content"], cancel_event=cancel_event)


class CancelledError(RuntimeError):
    """Raised when a cancel_event fires mid-generation. Distinct type so callers
    can disambiguate cancellation from genuine LLM errors when surfacing
    messages to the user (cancellation is silent — a placeholder note for
    cancelled work is just noise)."""


def _cancel_sleep(seconds: float, cancel_event: threading.Event | None) -> None:
    """Sleep for `seconds` total, but abort early when `cancel_event` fires.

    Plain `time.sleep` blocks until the timer expires; the backoff path used
    to sleep up to 60s, which dominated cancellation latency. Splitting into
    1s chunks via `Event.wait` cuts the worst-case to ~1s while keeping the
    same overall pacing on the happy path.
    """
    if cancel_event is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event.wait(min(1.0, remaining)):
            raise CancelledError("LLM call cancelled")


# Per-attempt urllib timeout. 90s is comfortably above the longest legitimate
# LLM call we observe in the wild while keeping cancellation latency bounded
# (cancel_event set during an in-flight HTTP request can only be observed
# when urlopen returns or times out — so this number IS the cancel ceiling).
_HTTP_TIMEOUT_SECS = 90


def _http_retry(
    req: urllib.request.Request,
    extract_fn,
    retries: int = 4,
    cancel_event: threading.Event | None = None,
) -> str:
    """Shared retry loop for all LLM HTTP calls.

    `cancel_event`, when set, aborts the loop at the next checkpoint:
      - between attempts (before sleep)
      - during the inter-attempt backoff sleep
      - just before the next urlopen
    Already in-flight HTTP requests still run to their `_HTTP_TIMEOUT_SECS`
    ceiling because urllib doesn't expose a cooperative interrupt.
    """
    _RETRYABLE = {429, 500, 502, 503, 504}
    last_exc: Exception | None = None
    for attempt in range(retries):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("LLM call cancelled")
        if attempt:
            _cancel_sleep(15 * (2 ** (attempt - 1)), cancel_event)  # 15s, 30s, 60s
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("LLM call cancelled")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECS) as resp:
                body = json.loads(resp.read())
            try:
                return extract_fn(body)
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"Unexpected LLM response shape: {e}") from e
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            last_exc = RuntimeError(f"LLM API error {e.code}: {error_body}")
            if e.code not in _RETRYABLE:
                raise last_exc from e
        except OSError as e:
            last_exc = RuntimeError(f"LLM network error: {e}")
    raise last_exc or RuntimeError("LLM call failed after retries")


def summarize(
    transcript: str,
    meeting_title: str,
    cfg: ZoomNotesConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """
    Dispatch to the correct LLM backend based on cfg.llm_provider.
    cfg defaults to get_config() if not provided.

    `cancel_event`, when set, aborts retries / backoff sleeps with
    `CancelledError`. Already in-flight HTTP calls run to the per-attempt
    `_HTTP_TIMEOUT_SECS` ceiling (urllib has no cooperative interrupt).
    """
    if cfg is None:
        cfg = get_config()

    provider = cfg.llm_provider
    model = cfg.llm_model
    system_prompt = cfg.effective_system_prompt

    if provider == "claude":
        api_key = get_api_key("claude")
        if not api_key:
            raise RuntimeError(
                "No Anthropic API key found. Set it in Settings or ANTHROPIC_API_KEY env var."
            )
        api_url = os.environ.get("ZOOM_NOTES_API_URL", "https://api.anthropic.com/v1/messages")
        return summarize_with_claude(
            transcript, meeting_title, api_key, model, api_url, system_prompt,
            cancel_event=cancel_event,
        )

    if provider == "openai":
        api_key = get_api_key("openai")
        if not api_key:
            raise RuntimeError(
                "No OpenAI API key found. Set it in Settings or OPENAI_API_KEY env var."
            )
        return summarize_with_openai(
            transcript, meeting_title, api_key, model, cfg.openai_base_url, system_prompt,
            cancel_event=cancel_event,
        )

    if provider == "gemini":
        api_key = get_api_key("gemini")
        if not api_key:
            raise RuntimeError(
                "No Gemini API key found. Set it in Settings or GEMINI_API_KEY env var."
            )
        return summarize_with_gemini(
            transcript, meeting_title, api_key, model, cfg.gemini_base_url, system_prompt,
            cancel_event=cancel_event,
        )

    if provider == "ollama":
        return summarize_with_ollama(
            transcript, meeting_title, model, cfg.ollama_base_url, system_prompt,
            cancel_event=cancel_event,
        )

    raise RuntimeError(f"Unknown LLM provider: {provider!r}")


# ── Note Writing ───────────────────────────────────────────────────────────────

def slugify_title(title: str, fallback_date: str | None = None) -> str:
    """Turn a meeting title into a safe filename fragment.

    Strips Zoom's trailing "YYYY-MM-DD HH:MM" timestamp, removes filesystem-
    unsafe chars, collapses whitespace, and enforces a non-empty 1..100 char
    result that can't be a hidden file or reserved name.
    """
    clean = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}.*$", "", title or "").strip()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Strip leading dot/dash so we never produce a hidden file or option-shaped name
    clean = clean.lstrip(".-").strip()
    # Truncate to 100 chars (filesystem-friendly) at a word boundary if possible
    if len(clean) > 100:
        clean = clean[:100].rsplit(" ", 1)[0] or clean[:100]
    if not clean or clean in {".", "..", "-"}:
        clean = f"Meeting {fallback_date}" if fallback_date else "Meeting"
    return clean


_RECENT_OVERWRITE_WINDOW_SECS = 60

# Read at most this many bytes when sniffing an existing file's frontmatter.
# Frontmatter blocks are always at the top; a few KB is more than enough and
# bounds memory if a user points us at a large file.
_FRONTMATTER_SNIFF_BYTES = 8192


def _read_existing_meeting_id(path: Path) -> str | None:
    """Extract `meeting_id` from a file's YAML frontmatter, if present.

    Returns:
      - the meeting id string (possibly empty if the field exists but is blank),
      - None if the file doesn't exist, isn't readable, has no frontmatter,
        or has no `meeting_id` key.

    Implementation note: we don't pull in PyYAML — the engine intentionally
    stays stdlib-only — so this is a tiny line-oriented scan of the leading
    `---` block. Frontmatter values are emitted by `_yaml_quote`, so the
    serialization is either a plain scalar or a JSON-quoted string. Both are
    handled. Anything past the closing `---` is ignored.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(_FRONTMATTER_SNIFF_BYTES)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    # Walk lines after the opening fence until the closing one.
    body = head.split("\n", 1)[1] if "\n" in head else ""
    for line in body.splitlines():
        if line.strip() == "---":
            return None
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() != "meeting_id":
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value[1:-1]
        return value
    return None


def _resolve_save_path(target: Path, meeting_id: str | None) -> Path:
    """Return the path to save to, never silently overwriting a different meeting.

    Decision tree:
      1. Target doesn't exist → use it.
      2. Target exists AND BOTH the new and existing meeting_ids are
         non-empty AND equal → use it (same meeting refreshing itself,
         e.g. retry-after-LLM-failure).
      3. Target exists AND new meeting_id is non-empty AND existing file's
         meeting_id is non-empty but different → search siblings.
      4. Target exists AND either side has an empty/missing meeting_id →
         we cannot prove same-meeting; ALWAYS create a sibling.

         The only carve-out for the 60-second legacy window is callers
         passing `meeting_id=None` (CLI `--notes`, manual recovery). For
         engine-driven saves with empty `meeting_id` (=""), we treat the
         empty values as "unprovable" and create a sibling — that closes
         the 2026-04-30 PM regression where two engine-generated files
         both had `meeting_id: ""` (because Zoom hadn't written its
         meetingId field at IDLE->ACTIVE) and the second silently
         overwrote the first.

    The distinction matters: `meeting_id=None` is "the caller doesn't have
    one to give" (CLI tool); `meeting_id=""` is "the engine had one but it
    was empty at the time of save" (bug we're guarding against).
    """
    if not target.exists():
        return target

    new_is_empty = meeting_id is None or meeting_id == ""
    existing_id = _read_existing_meeting_id(target)
    existing_is_empty = existing_id is None or existing_id == ""

    if not new_is_empty and not existing_is_empty and existing_id == meeting_id:
        # Same meeting refreshing itself. Overwrite is the right thing.
        return target

    if meeting_id is None and existing_is_empty:
        # CLI / recovery path AND existing file also has no meeting_id
        # frontmatter (e.g. a prior CLI run): preserve the 60-second
        # window so a quick rerun (`python zoom_notes.py --notes` twice
        # while iterating on a prompt) replaces rather than piles up
        # siblings. After 60s, we assume the user moved on and create
        # a sibling instead of silently overwriting older work.
        age = time.time() - target.stat().st_mtime
        if age < _RECENT_OVERWRITE_WINDOW_SECS:
            return target

    # Every other case → create a sibling. We can't prove same-meeting:
    #   - new id is empty but engine-driven (treat as different);
    #   - existing file has no meeting_id (legacy / hand-authored / engine-
    #     bug-era) and we must never clobber it;
    #   - both ids are non-empty but different (the original Daily-Standup
    #     overwrite scenario).
    stem, suffix = target.stem, target.suffix
    parent = target.parent
    for n in range(2, 1000):
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        if not new_is_empty:
            cand_id = _read_existing_meeting_id(candidate)
            if cand_id and cand_id == meeting_id:
                return candidate
    return target  # give up; overwrite rather than fail


# Back-compat shim. Old callers (and tests that mock this name) still work,
# but new code should use `_resolve_save_path` so the meeting_id check applies.
def _next_available_path(target: Path) -> Path:
    return _resolve_save_path(target, meeting_id=None)


def save_transcript_only(
    transcript_content: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    meeting_id: str | None = None,
) -> Path:
    """Save just the transcript to the user's Transcripts/ folder.

    This is the durability boundary: once this returns, the meeting's
    content is safe on disk regardless of whether note generation succeeds.

    `meeting_id` is used to make path resolution collision-safe: a second
    meeting that happens to slugify to the same filename on the same day
    (different Zoom meeting, same derived title) will land in a `-2`
    sibling instead of silently overwriting the first. Callers that don't
    have a meeting_id (CLI `--notes`) pass None and fall back to the
    legacy 60-second overwrite window.
    """
    if cfg is None:
        cfg = get_config()
    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    transcripts_dir = cfg.transcripts_path / subfolder if subfolder else cfg.transcripts_path
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str) + ".md"
    transcript_path = _resolve_save_path(
        transcripts_dir / transcript_filename, meeting_id=meeting_id
    )
    _atomic_write_text(transcript_path, transcript_content)
    return transcript_path


def save_note_only(
    note_content: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    meeting_id: str | None = None,
) -> Path:
    """Save just the note to the user's Notes/ folder.

    Used both for the final LLM-generated note and for the placeholder note
    written when LLM generation fails.

    See `save_transcript_only` for `meeting_id` semantics — it's the same
    collision-safety contract.
    """
    if cfg is None:
        cfg = get_config()
    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    notes_dir = cfg.notes_path / subfolder if subfolder else cfg.notes_path
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_filename = resolve_filename(cfg.filename_pattern, slug, date_str) + ".md"
    note_path = _resolve_save_path(
        notes_dir / note_filename, meeting_id=meeting_id
    )
    _atomic_write_text(note_path, note_content)
    return note_path


def overwrite_note(
    note_path: Path,
    note_content: str,
) -> None:
    """Overwrite an existing note file in place (used by retry to replace placeholder)."""
    _atomic_write_text(note_path, note_content)


def save_note(
    note_content: str,
    transcript_content: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    meeting_id: str | None = None,
) -> Path:
    """Write note and transcript to their respective dated vault subfolders.

    Convenience wrapper for callers that already have both pieces ready.
    Engine code should prefer save_transcript_only + save_note_only so a
    transcript is preserved even when note generation fails.
    """
    save_transcript_only(transcript_content, meeting_title, date_str, cfg, meeting_id=meeting_id)
    return save_note_only(note_content, meeting_title, date_str, cfg, meeting_id=meeting_id)


def _path_to_vault_link(actual_path: Path, cfg: "ZoomNotesConfig", kind: str, date_str: str) -> str:
    """Build an Obsidian wikilink from an actual saved file path.

    Uses the stem of the file (no .md extension) as the link target, so
    collision-suffixed paths like "Daily Standup — transcript-2.md" produce
    the correct link "[[Meetings/Transcripts/2026-05-11/Daily Standup — transcript-2]]"
    rather than the base slug that `resolve_filename` would compute.
    """
    subfolder = resolve_subfolder(cfg, date_str)
    stem = actual_path.stem
    if kind == "transcript":
        if subfolder:
            return f"[[Meetings/Transcripts/{subfolder}/{stem}]]"
        return f"[[Meetings/Transcripts/{stem}]]"
    else:
        if subfolder:
            return f"[[Meetings/Notes/{subfolder}/{stem}]]"
        return f"[[Meetings/Notes/{stem}]]"


_YAML_UNSAFE_CHARS = set(":#[]{},&*?|>\"'%@`")


def _yaml_quote(value: str) -> str:
    """Return a YAML-safe scalar representation of `value`.

    Plain scalars are valid YAML when they contain none of the indicator chars
    above and don't start with whitespace, `-`, `?`, or `:`. Anything else is
    emitted as a JSON string, which is also valid YAML and round-trips safely.
    """
    if value == "":
        return '""'
    if value[0] in " -?:" or value[-1] == " ":
        return json.dumps(value, ensure_ascii=False)
    if any(c in _YAML_UNSAFE_CHARS for c in value):
        return json.dumps(value, ensure_ascii=False)
    if "\n" in value:
        return json.dumps(value, ensure_ascii=False)
    return value


def _build_custom_frontmatter(cfg: "ZoomNotesConfig", title: str, date_str: str) -> str:
    """Return custom frontmatter lines (with trailing newline) from config."""
    lines = []
    for prop in (cfg.custom_frontmatter_properties or []):
        key = prop.get("key", "").strip()
        value = prop.get("value", "").strip()
        if not key:
            continue
        # Sanitize the key to a YAML-safe identifier so users can't break the
        # frontmatter by typing `:` in the key field.
        key = re.sub(r"[^A-Za-z0-9_\-]", "_", key)
        value = value.replace("{title}", title).replace("{date}", date_str)
        lines.append(f"{key}: {_yaml_quote(value)}")
    extra = (cfg.extra_frontmatter_yaml or "").strip()
    if extra:
        extra = extra.replace("{title}", title).replace("{date}", date_str)
        # Raw YAML is intentionally not quoted — power users opt in by enabling
        # the "raw YAML block" toggle and accept responsibility for syntax.
        lines.append(extra)
    return ("\n".join(lines) + "\n") if lines else ""


def build_note_content(
    summary: str,
    meeting_title: str,
    date_str: str,
    attendees: list[str],
    created_iso: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    meeting_id: str | None = None,
    transcript_link_override: str | None = None,
) -> str:
    if cfg is None:
        cfg = get_config()

    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)

    if transcript_link_override:
        transcript_link = transcript_link_override
    else:
        transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str)
        transcript_link = (
            f"[[Meetings/Transcripts/{date_str}/{transcript_filename}]]"
            if subfolder
            else f"[[Meetings/Transcripts/{transcript_filename}]]"
        )
    daily_link = f"[[Daily/{date_str}]]"
    attendees_yaml = "\n".join(f"  - {_yaml_quote(a)}" for a in attendees)

    custom_lines = _build_custom_frontmatter(cfg, slug, date_str)

    # `meeting_id` is the canonical identity of this Zoom session and is
    # what `_resolve_save_path` reads back to decide whether a same-day
    # filename collision is the same meeting refreshing itself (overwrite
    # OK) or a different meeting that must not be clobbered (write a -2
    # sibling). Always emit the field, even when empty, so the absence of
    # a Zoom-Notes-owned file is unambiguous.
    meeting_id_line = f"meeting_id: {_yaml_quote(meeting_id or '')}\n"

    return f"""---
title: {_yaml_quote(slug)}
type: meeting
source: zoom-notes
date: {date_str}
created: {created_iso}
{meeting_id_line}attendees:
{attendees_yaml}
transcript: {_yaml_quote(transcript_link)}
daily_note: {_yaml_quote(daily_link)}
{custom_lines}---

# {slug}

{summary}
"""


def build_placeholder_note(
    meeting_title: str,
    date_str: str,
    attendees: list[str],
    created_iso: str,
    error_message: str,
    meeting_id: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    transcript_link_override: str | None = None,
) -> str:
    """Build a placeholder note used when LLM generation fails.

    Includes machine-readable retry metadata in the frontmatter and a
    human-readable error message in the body so the user can either click
    Retry in the menu bar or invoke the CLI manually.
    """
    if cfg is None:
        cfg = get_config()

    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)

    if transcript_link_override:
        transcript_link = transcript_link_override
    else:
        transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str)
        transcript_link = (
            f"[[Meetings/Transcripts/{date_str}/{transcript_filename}]]"
            if subfolder
            else f"[[Meetings/Transcripts/{transcript_filename}]]"
        )
    daily_link = f"[[Daily/{date_str}]]"
    attendees_yaml = "\n".join(f"  - {_yaml_quote(a)}" for a in attendees)
    cache_slug = _safe_meeting_id_slug(meeting_id)
    cache_path = f"~/.cache/zoom-notes/in-progress-{cache_slug}.json"

    custom_lines = _build_custom_frontmatter(cfg, slug, date_str)

    body = f"""# {slug}

> **Note generation failed.** The transcript was saved successfully, but the LLM call did not complete.

**Error:** {error_message}

## Retry options

- **Recommended:** Open the Zoom Notes menu bar item and click **Retry note generation**.
- **Manual CLI:** Run `python zoom_notes.py --notes` (uses the latest WAL) or restore the cached transcript from `{cache_path}` and re-summarize.

The full transcript is preserved at the linked file above and at the cache path so nothing is lost.
"""

    return f"""---
title: {_yaml_quote(slug)}
type: meeting
source: zoom-notes
date: {date_str}
created: {created_iso}
meeting_id: {_yaml_quote(meeting_id)}
status: note-generation-failed
attendees:
{attendees_yaml}
transcript: {_yaml_quote(transcript_link)}
daily_note: {_yaml_quote(daily_link)}
retry_meeting_id: {_yaml_quote(meeting_id)}
retry_cache_path: {_yaml_quote(cache_path)}
{custom_lines}---

{body}"""


def build_transcript_content(
    transcript: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
    *,
    meeting_id: str | None = None,
    note_link_override: str | None = None,
) -> str:
    if cfg is None:
        cfg = get_config()

    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)

    if note_link_override:
        note_link = note_link_override
    else:
        note_filename = resolve_filename(cfg.filename_pattern, slug, date_str)
        note_link = (
            f"[[Meetings/Notes/{date_str}/{note_filename}]]"
            if subfolder
            else f"[[Meetings/Notes/{note_filename}]]"
        )
    transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str)
    meeting_id_line = f"meeting_id: {_yaml_quote(meeting_id or '')}\n"
    return f"""---
title: {_yaml_quote(transcript_filename)}
type: transcript
source: zoom-notes
date: {date_str}
{meeting_id_line}note: {_yaml_quote(note_link)}
---

# {transcript_filename}

{transcript}
"""


# ── CLI Commands ───────────────────────────────────────────────────────────────

def cmd_list(origin: Path) -> None:
    cfg = get_config()
    blocks_wal = find_wal(origin, cfg.blocks_db_prefix, kind="blocks")
    transcript_wal = find_wal(origin, cfg.transcript_db_prefix, kind="transcript")

    print("Zoom Meeting Notes — Recent Meetings\n")
    if blocks_wal:
        entries_for_title = parse_transcript(transcript_wal) if transcript_wal else None
        title = parse_meeting_title(blocks_wal, entries_for_title)
        mtime = datetime.fromtimestamp(blocks_wal.stat().st_mtime)
        print(f"  Meeting title : {title or '(unknown)'}")
        print(f"  Blocks WAL    : {blocks_wal}")
        print(f"  Last modified : {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    if transcript_wal:
        mtime = datetime.fromtimestamp(transcript_wal.stat().st_mtime)
        entries = parse_transcript(transcript_wal)
        speakers = sorted({e["speaker"] for e in entries})
        print(f"\n  Transcript WAL: {transcript_wal}")
        print(f"  Last modified : {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Utterances    : {len(entries)}")
        print(f"  Speakers      : {', '.join(speakers)}")


def cmd_dump(origin: Path) -> None:
    cfg = get_config()
    wal = find_wal(origin, cfg.transcript_db_prefix, kind="transcript")
    if not wal:
        print("No transcript WAL found. Is a meeting with My Notes active?", file=sys.stderr)
        sys.exit(1)
    entries = parse_transcript(wal)
    if not entries:
        print("No transcript entries found.", file=sys.stderr)
        sys.exit(1)
    print(format_transcript(entries))


def cmd_watch(origin: Path) -> None:
    cfg = get_config()
    wal = find_wal(origin, cfg.transcript_db_prefix, kind="transcript")
    if not wal:
        print("No transcript WAL found. Start a Zoom meeting with My Notes enabled.", file=sys.stderr)
        sys.exit(1)

    print(f"Watching: {wal.name}")
    print("─" * 60)

    last_count = 0
    try:
        while True:
            entries = parse_transcript(wal)
            new_entries = entries[last_count:]
            for e in new_entries:
                ts = f"[{e['timestamp']}] " if e["timestamp"] else ""
                print(f"{ts}{e['speaker']}: {e['text']}")
            last_count = len(entries)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_notes(origin: Path, dry_run: bool = False) -> None:
    cfg = get_config()

    transcript_wal = find_wal(origin, cfg.transcript_db_prefix, kind="transcript")
    blocks_wal = find_wal(origin, cfg.blocks_db_prefix, kind="blocks")

    if not transcript_wal:
        print("No transcript WAL found.", file=sys.stderr)
        sys.exit(1)

    active_meeting_id = detect_active_meeting_id(transcript_wal)
    entries = parse_transcript(transcript_wal, meeting_id_filter=active_meeting_id)
    if not entries:
        print("No transcript entries found.", file=sys.stderr)
        sys.exit(1)

    # Determine meeting title and date
    meeting_title = None
    if blocks_wal:
        meeting_title = parse_meeting_title(blocks_wal, entries)

    if not meeting_title:
        mtime = transcript_wal.stat().st_mtime
        meeting_title = f"Zoom Meeting {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", meeting_title)
    date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")

    # Extract unique speakers in order of first appearance (exclude Unknown)
    seen: dict[str, None] = {}
    for e in entries:
        s = e["speaker"]
        if s and s != "Unknown":
            seen[s] = None
    attendees = list(seen.keys())

    created_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    transcript_text = format_transcript(entries)

    print(f"Meeting  : {meeting_title}")
    print(f"Date     : {date_str}")
    print(f"Speakers : {', '.join(attendees)}")
    print(f"Lines    : {len(entries)}")
    print(f"Provider : {cfg.llm_provider} / {cfg.llm_model}")

    print("\nSummarizing...")
    summary = summarize(transcript_text, meeting_title, cfg)

    note_content = build_note_content(
        summary, meeting_title, date_str, attendees, created_iso, cfg
    )
    transcript_content = build_transcript_content(transcript_text, meeting_title, date_str, cfg)

    if dry_run:
        print("\n" + "─" * 60)
        print("── NOTE ──")
        print(note_content)
        print("\n── TRANSCRIPT ──")
        print(transcript_content[:800] + "\n... (truncated)")
        print("─" * 60)
        print("\n(Dry run — files not saved)")
    else:
        note_path = save_note(note_content, transcript_content, meeting_title, date_str, cfg)
        slug = slugify_title(meeting_title, fallback_date=date_str)
        subfolder = resolve_subfolder(cfg, date_str)
        transcripts_dir = cfg.transcripts_path / subfolder if subfolder else cfg.transcripts_path
        transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str) + ".md"
        print(f"\nNote saved      : {note_path}")
        print(f"Transcript saved: {transcripts_dir / transcript_filename}")


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Zoom Notes — extract and summarize Zoom transcripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List recent meetings found in WAL")
    group.add_argument("--dump", action="store_true", help="Print current transcript to stdout")
    group.add_argument("--watch", action="store_true", help="Live-follow transcript during a meeting")
    group.add_argument("--notes", action="store_true", help="Generate and save meeting notes")
    parser.add_argument("--dry-run", action="store_true", help="Preview notes without saving (use with --notes)")

    args = parser.parse_args()

    origin = find_origin_dir()
    if not origin:
        print(
            "Error: Zoom MyNotes directory not found.\n"
            f"Expected: {MY_NOTES_ORIGINS}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.list:
        cmd_list(origin)
    elif args.dump:
        cmd_dump(origin)
    elif args.watch:
        cmd_watch(origin)
    elif args.notes:
        cmd_notes(origin, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
