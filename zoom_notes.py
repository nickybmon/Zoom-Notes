#!/usr/bin/env python3
"""
Zoom Meeting Notes Assistant
Reads Zoom's live AI Notetaker WAL files to extract transcripts,
then summarizes them via Claude API and saves to your configured output directory.

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
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


def _load_dotenv():
    """Load .env file from the project directory into os.environ (stdlib only)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()


# ── Paths ─────────────────────────────────────────────────────────────────────

ZOOM_BASE = Path.home() / "Library/Application Support/zoom.us/data"
MY_NOTES_ORIGINS = (
    ZOOM_BASE
    / "UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins"
)
VAULT_NOTES = Path(
    os.environ.get("ZOOM_NOTES_OUTPUT_DIR")
    or Path.home() / "Desktop/Meeting Notes/Notes"
)
VAULT_TRANSCRIPTS = Path(
    os.environ.get("ZOOM_NOTES_TRANSCRIPTS_DIR")
    or Path.home() / "Desktop/Meeting Notes/Transcripts"
)

# API endpoint — override with ZOOM_NOTES_API_URL to route through a proxy.
# When a proxy is available, set this to your internal endpoint and remove
# ANTHROPIC_API_KEY from .env (the proxy holds the key server-side).
CLAUDE_API_URL = os.environ.get(
    "ZOOM_NOTES_API_URL",
    "https://api.anthropic.com/v1/messages",
)

# Known transcript store prefix (from research)
TRANSCRIPT_DB_PREFIX = "1CB477F679D6"
BLOCKS_DB_PREFIX = "DDEC8414E29A"


# ── WAL Discovery ──────────────────────────────────────────────────────────────

def find_origin_dir() -> Path | None:
    """Find the docs.zoom.us origin directory (hash-named folder)."""
    if not MY_NOTES_ORIGINS.exists():
        return None
    for top in MY_NOTES_ORIGINS.iterdir():
        if top.is_dir():
            nested = top / top.name
            if (nested / "IndexedDB").exists():
                return nested
    return None


def find_wal(origin: Path, db_prefix: str) -> Path | None:
    """Find the WAL file for a given IndexedDB prefix."""
    idb_dir = origin / "IndexedDB"
    if not idb_dir.exists():
        return None
    candidates = []
    for db_dir in idb_dir.iterdir():
        if db_dir.name.startswith(db_prefix):
            wal = db_dir / "IndexedDB.sqlite3-wal"
            if wal.exists() and wal.stat().st_size > 1024:
                candidates.append(wal)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ── WAL Parsing ────────────────────────────────────────────────────────────────

def read_wal_strings(wal_path: Path) -> list[str]:
    """Copy WAL to temp file and extract printable strings."""
    tmp = Path(tempfile.mktemp(suffix=".wal"))
    try:
        shutil.copy2(wal_path, tmp)
        result = subprocess.run(
            ["strings", str(tmp)],
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


def detect_active_meeting_id(wal_path: Path) -> str | None:
    """Return the meetingId that appears most frequently in the WAL.

    When multiple meetings are stored in the same WAL (e.g. a meeting you were
    invited to but didn't attend alongside one you did), the one you were
    actually in will have far more transcript entries.
    """
    counts = count_meeting_ids(wal_path)
    return max(counts, key=lambda k: counts[k]) if counts else None


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
                        speaker = lines[j + 1]
                    if lines[j] == "meetingId" and j + 1 < len(lines):
                        meeting_id = lines[j + 1]
                        break

                is_real_text = (
                    len(text) > 3
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


# ── Claude Summarization ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a meticulous meeting notetaker. Produce detailed, well-structured meeting notes from the transcript. Be thorough — a reader who wasn't in the meeting should come away with a complete picture of what was discussed, decided, and committed to.

Use this structure exactly. Include every section even if brief.

## Overview
2-4 sentences capturing the purpose and outcome of the meeting. Who was involved, what was the core focus, what was resolved or left open.

## Attendees
Bullet list of attendee names (use the speaker names from the transcript).

## Topics Discussed
A sequenced list of the main topics covered. For each topic, 1-3 sentences on what was said — include specific details, numbers, names, and context. Don't collapse important nuance into vague summaries.

Format:
- **[Topic name]** — [What was discussed. Be specific.]

## Key Decisions
Decisions that were explicitly made or agreed upon. If none, write "No explicit decisions recorded."

Format:
- [Decision] — [Who made it or who it affects, if clear]

## Action Items
A table of all commitments, tasks, and follow-ups. Include owner, task description, and due date if mentioned.

| Owner | Task | Due Date |
|-------|------|----------|
| [name] | [what they committed to] | [date or null] |

## Open Questions
Unresolved questions, decisions deferred, or topics that need follow-up. Omit this section entirely if none.

## Notes
Any additional context, background, or detail worth capturing that didn't fit above. Omit if nothing relevant.

---

Output only the meeting notes. No preamble, no explanation, no meta-commentary."""


def summarize_with_claude(
    transcript: str,
    meeting_title: str,
    api_key: str,
) -> str:
    """Call Claude API (or a configured proxy) to summarize the transcript."""
    payload = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
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
        CLAUDE_API_URL,
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    _RETRYABLE = {429, 500, 502, 503, 504}
    last_exc = None
    for attempt in range(4):
        if attempt:
            time.sleep(15 * (2 ** (attempt - 1)))  # 15s, 30s, 60s
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
                return body["content"][0]["text"]
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            last_exc = RuntimeError(f"Claude API error {e.code}: {error_body}")
            if e.code not in _RETRYABLE:
                raise last_exc from e
        except OSError as e:
            last_exc = RuntimeError(f"Claude network error: {e}")
    raise last_exc



# ── Note Writing ───────────────────────────────────────────────────────────────

def slugify_title(title: str) -> str:
    """Turn a meeting title into a safe filename fragment."""
    # Strip the Zoom-appended date/time from titles like "WFC Sync 2026-04-21 15:01(GMT-4:00)"
    import re
    clean = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}.*$", "", title).strip()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def save_note(
    note_content: str,
    transcript_content: str,
    meeting_title: str,
    date_str: str,
) -> Path:
    """Write note and transcript to their respective dated vault subfolders."""
    slug = slugify_title(meeting_title)

    notes_dir = VAULT_NOTES / date_str
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{slug}.md"
    note_path.write_text(note_content, encoding="utf-8")

    transcripts_dir = VAULT_TRANSCRIPTS / date_str
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcripts_dir / f"{slug} \u2014 transcript.md"
    transcript_path.write_text(transcript_content, encoding="utf-8")

    return note_path


def build_note_content(
    summary: str,
    meeting_title: str,
    date_str: str,
    attendees: list[str],
    created_iso: str,
) -> str:
    slug = slugify_title(meeting_title)
    transcript_link = (
        f"[[Meetings/Transcripts/{date_str}/{slug} \u2014 transcript.md]]"
    )
    daily_link = f"[[Daily/{date_str}]]"
    attendees_yaml = "\n".join(f'  - "{a}"' for a in attendees)
    return f"""---
title: "{slug}"
type: meeting
source: zoom-notes
date: {date_str}
created: {created_iso}
attendees:
{attendees_yaml}
transcript: "{transcript_link}"
daily_note: "{daily_link}"
---

# {slug}

{summary}
"""


def build_transcript_content(transcript: str, meeting_title: str, date_str: str) -> str:
    slug = slugify_title(meeting_title)
    note_link = f"[[Meetings/Notes/{date_str}/{slug}]]"
    return f"""---
title: "{slug} — transcript"
type: transcript
source: zoom-notes
date: {date_str}
note: "{note_link}"
---

# {slug} — Transcript

{transcript}
"""


# ── CLI Commands ───────────────────────────────────────────────────────────────

def cmd_list(origin: Path) -> None:
    blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)
    transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)

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
    wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)
    if not wal:
        print("No transcript WAL found. Is a meeting with My Notes active?", file=sys.stderr)
        sys.exit(1)
    entries = parse_transcript(wal)
    if not entries:
        print("No transcript entries found.", file=sys.stderr)
        sys.exit(1)
    print(format_transcript(entries))


def cmd_watch(origin: Path) -> None:
    wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)
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
    import re

    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        print(
            "Error: No API key found. Set ANTHROPIC_API_KEY in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)
    blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)

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

    print("\nSummarizing with Claude...")
    summary = summarize_with_claude(transcript_text, meeting_title, api_key)

    note_content = build_note_content(
        summary, meeting_title, date_str, attendees, created_iso
    )
    transcript_content = build_transcript_content(transcript_text, meeting_title, date_str)

    if dry_run:
        print("\n" + "─" * 60)
        print("── NOTE ──")
        print(note_content)
        print("\n── TRANSCRIPT ──")
        print(transcript_content[:800] + "\n... (truncated)")
        print("─" * 60)
        print("\n(Dry run — files not saved)")
    else:
        note_path = save_note(note_content, transcript_content, meeting_title, date_str)
        slug = slugify_title(meeting_title)
        print(f"\nNote saved      : {note_path}")
        print(f"Transcript saved: {VAULT_TRANSCRIPTS / date_str / f'{slug} \u2014 transcript.md'}")


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
    group.add_argument("--notes", action="store_true", help="Generate and save Claude meeting notes")
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
