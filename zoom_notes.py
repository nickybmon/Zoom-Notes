#!/usr/bin/env python3
"""
Zoom Meeting Notes Assistant
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


def find_wal(origin: Path, db_prefix: str) -> Path | None:
    """Find the WAL file for a given IndexedDB prefix."""
    idb_dir = origin / "IndexedDB"
    if not idb_dir.exists():
        return None
    candidates = []
    for db_dir in idb_dir.iterdir():
        if db_dir.name.startswith(db_prefix):
            wal = db_dir / "IndexedDB.sqlite3-wal"
            if wal.exists() and wal.stat().st_size > 256:
                candidates.append(wal)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


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
    - Recency bonus: +50 if any entry has a timestamp within 2 hours of now
    - Speaker bonus: +1000 if ZOOM_NOTES_USER_NAME matches a speaker in the meeting

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
                    meeting_data[mid] = {"count": 0, "has_recent": False, "has_user": False}
                meeting_data[mid]["count"] += 1
                for back in range(i - 1, max(i - 60, -1), -1):
                    if lines[back] == "timeStampContent" and back + 1 < len(lines):
                        try:
                            ts_secs = sum(int(x) * m for x, m in zip(lines[back + 1].split(":"), [3600, 60, 1]))
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


def detect_active_meeting_id(wal_path: Path) -> str | None:
    """Return the best-scoring meetingId from the WAL.

    Uses score_meeting_ids() which factors in entry count, timestamp recency,
    and whether ZOOM_NOTES_USER_NAME appears as a speaker — preventing a
    ghost/double-booked meeting from crowding out the one you actually attended.
    """
    scores = score_meeting_ids(wal_path)
    return max(scores, key=lambda k: scores[k]) if scores else None


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


def purge_stale_accumulators(
    max_age_secs: int = 24 * 3600,
    failed_max_age_secs: int = _FAILED_PURGE_SECS,
) -> None:
    """Delete in-progress cache files older than the appropriate retention window.

    Two separate windows govern the two cache buckets:

      ROOT (`~/.cache/zoom-notes/in-progress-*`)
        Default: 24h. Wide enough that a same-day retry always has the cache
        available, while still cleaning up forgotten snapshots from prior
        days. Live meetings that just ended sit here briefly before being
        either deleted (success) or promoted to failed/ (LLM error).

      FAILED (`~/.cache/zoom-notes/failed/in-progress-*` + sidecars)
        Default: 30 days. Confirmed failures need a much wider window
        because the user may not notice / fix the underlying cause for
        days (API quota, vacation, broken model config). The transcript
        is already on disk in the user's Notes/Transcripts folder, but
        the cache snapshot is the only way to retry note generation
        without re-parsing the WAL — we can't reconstruct it later.
    """
    if not _CACHE_DIR.exists():
        return
    now = time.time()
    for pattern in ("in-progress-*.json", "in-progress-*.md", "in-progress-*.tmp"):
        for f in _CACHE_DIR.glob(pattern):
            try:
                if now - f.stat().st_mtime > max_age_secs:
                    f.unlink(missing_ok=True)
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

                for j in range(i + 3, min(i + 60, len(lines))):
                    if lines[j] == "timeStampContent" and j + 1 < len(lines):
                        timestamp = lines[j + 1]
                    if lines[j] == "username" and j + 1 < len(lines):
                        speaker = _sanitize_speaker(lines[j + 1])
                    if lines[j] == "meetingId" and j + 1 < len(lines):
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
}

def parse_meeting_title(blocks_wal: Path, transcript_entries: list[dict] | None = None) -> str | None:
    """Extract the meeting title from the blocks WAL.

    When transcript_entries is provided, matches the title whose embedded
    Zoom start-time (YYYY-MM-DD HH:MM) is closest to the transcript's own
    timestamps, so we pick the right meeting when the WAL holds multiple sessions.
    Falls back to the last title found if no time-based match is possible.
    """
    lines = read_wal_strings(blocks_wal)
    titles = []
    for i, line in enumerate(lines):
        if line == "title" and i + 1 < len(lines):
            candidate = lines[i + 1]
            if (
                len(candidate) > 5
                and candidate not in _TITLE_JUNK
                and not candidate.startswith(("http", "{", "BLOCK_", "PRODUCT_"))
                and " " in candidate
                and any(c.isalpha() for c in candidate)
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
    # Only accept titles whose embedded start time is within 2 hours of the transcript.
    _zoom_time_re = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")
    _MAX_TITLE_DELTA_SECS = 2 * 3600
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
        delta = abs((ref_dt - title_dt).total_seconds())
        if delta <= _MAX_TITLE_DELTA_SECS and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_title = t

    # If no title matched within the time window, fall back to the last title
    # only if it has no parseable timestamp (i.e. a custom/renamed meeting title).
    if best_title is None:
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


def _next_available_path(target: Path) -> Path:
    """If target was modified within the last `_RECENT_OVERWRITE_WINDOW_SECS`,
    return a `-2`, `-3`, ... suffixed sibling so we don't silently overwrite a
    freshly-saved note.

    The window is intentionally short (60s): legitimate same-second reruns from
    a manual "Generate Notes Now" should disambiguate, but a runaway engine
    loop must NOT be allowed to produce dozens of `-N.md` siblings — the
    real fix for that lives in zoom_engine._trigger_generate (last-meeting-id
    guard + tracking anchor on success).

    Older files are allowed to be overwritten — repeat invocations of the same
    recurring meeting on different days land in different dated subfolders, so
    a same-day path collision usually means the user wants to refresh.
    """
    if not target.exists():
        return target
    age = time.time() - target.stat().st_mtime
    if age >= _RECENT_OVERWRITE_WINDOW_SECS:
        return target
    stem, suffix = target.stem, target.suffix
    parent = target.parent
    for n in range(2, 100):
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    return target  # give up; overwrite rather than fail


def save_transcript_only(
    transcript_content: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
) -> Path:
    """Save just the transcript to the user's Transcripts/ folder.

    This is the durability boundary: once this returns, the meeting's
    content is safe on disk regardless of whether note generation succeeds.
    """
    if cfg is None:
        cfg = get_config()
    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    transcripts_dir = cfg.transcripts_path / subfolder if subfolder else cfg.transcripts_path
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str) + ".md"
    transcript_path = _next_available_path(transcripts_dir / transcript_filename)
    _atomic_write_text(transcript_path, transcript_content)
    return transcript_path


def save_note_only(
    note_content: str,
    meeting_title: str,
    date_str: str,
    cfg: ZoomNotesConfig | None = None,
) -> Path:
    """Save just the note to the user's Notes/ folder.

    Used both for the final LLM-generated note and for the placeholder note
    written when LLM generation fails.
    """
    if cfg is None:
        cfg = get_config()
    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    notes_dir = cfg.notes_path / subfolder if subfolder else cfg.notes_path
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_filename = resolve_filename(cfg.filename_pattern, slug, date_str) + ".md"
    note_path = _next_available_path(notes_dir / note_filename)
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
) -> Path:
    """Write note and transcript to their respective dated vault subfolders.

    Convenience wrapper for callers that already have both pieces ready.
    Engine code should prefer save_transcript_only + save_note_only so a
    transcript is preserved even when note generation fails.
    """
    save_transcript_only(transcript_content, meeting_title, date_str, cfg)
    return save_note_only(note_content, meeting_title, date_str, cfg)


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
) -> str:
    if cfg is None:
        cfg = get_config()

    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str)

    transcript_link = (
        f"[[Meetings/Transcripts/{date_str}/{transcript_filename}]]"
        if subfolder
        else f"[[Meetings/Transcripts/{transcript_filename}]]"
    )
    daily_link = f"[[Daily/{date_str}]]"
    attendees_yaml = "\n".join(f"  - {_yaml_quote(a)}" for a in attendees)

    custom_lines = _build_custom_frontmatter(cfg, slug, date_str)

    return f"""---
title: {_yaml_quote(slug)}
type: meeting
source: zoom-notes
date: {date_str}
created: {created_iso}
attendees:
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
) -> str:
    if cfg is None:
        cfg = get_config()

    slug = slugify_title(meeting_title, fallback_date=date_str)
    subfolder = resolve_subfolder(cfg, date_str)
    note_filename = resolve_filename(cfg.filename_pattern, slug, date_str)

    note_link = (
        f"[[Meetings/Notes/{date_str}/{note_filename}]]"
        if subfolder
        else f"[[Meetings/Notes/{note_filename}]]"
    )
    transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str)
    return f"""---
title: {_yaml_quote(transcript_filename)}
type: transcript
source: zoom-notes
date: {date_str}
note: {_yaml_quote(note_link)}
---

# {transcript_filename}

{transcript}
"""


# ── CLI Commands ───────────────────────────────────────────────────────────────

def cmd_list(origin: Path) -> None:
    cfg = get_config()
    blocks_wal = find_wal(origin, cfg.blocks_db_prefix)
    transcript_wal = find_wal(origin, cfg.transcript_db_prefix)

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
    wal = find_wal(origin, cfg.transcript_db_prefix)
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
    wal = find_wal(origin, cfg.transcript_db_prefix)
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

    transcript_wal = find_wal(origin, cfg.transcript_db_prefix)
    blocks_wal = find_wal(origin, cfg.blocks_db_prefix)

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
        description="Zoom Meeting Notes Assistant — extract and summarize Zoom transcripts",
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
