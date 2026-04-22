#!/usr/bin/env python3
"""
Zoom Meeting Notes Assistant
Reads Zoom's live AI Notetaker WAL files to extract transcripts,
then summarizes them via Claude API and saves to Vault Mind.

Usage:
  python zoom_notes.py --list          # List recent meetings found in WAL
  python zoom_notes.py --dump          # Print current transcript to stdout
  python zoom_notes.py --watch         # Live-follow transcript during a meeting
  python zoom_notes.py --notes         # Generate and save Claude meeting notes
  python zoom_notes.py --notes --dry-run  # Preview notes without saving
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

ZOOM_BASE = Path.home() / "Library/Application Support/zoom.us/data"
MY_NOTES_ORIGINS = (
    ZOOM_BASE
    / "UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins"
)
VAULT_MEETINGS = Path.home() / "Documents/Vault Mind/Meetings"

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


def parse_transcript(wal_path: Path) -> list[dict]:
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

    The WAL stores multiple pages so entries repeat. We deduplicate
    first by messageId (exact copies), then by rolling-update logic.
    """
    lines = read_wal_strings(wal_path)

    # Pass 1: collect all raw entries keyed by messageId
    # messageId → best (longest text) entry
    by_id: dict[str, dict] = {}
    id_order: list[str] = []  # preserve first-seen order

    i = 0
    while i < len(lines):
        if lines[i] == "messageId" and i + 1 < len(lines):
            msg_id = lines[i + 1]
            # Expect "message" two lines later
            if i + 2 < len(lines) and lines[i + 2] == "message" and i + 3 < len(lines):
                text = lines[i + 3]
                timestamp = None
                speaker = None

                for j in range(i + 3, min(i + 50, len(lines))):
                    if lines[j] == "timeStampContent" and j + 1 < len(lines):
                        timestamp = lines[j + 1]
                    if lines[j] == "username" and j + 1 < len(lines):
                        speaker = lines[j + 1]
                        break

                _JUNK_EXACT = {
                    "timeStampContent", "timeStampSeconds", "textLanguage",
                    "startTimeMsec", "endTimeMsec", "messageId", "uniqueUserId",
                    "meetingId", "speaker", "speakerId", "username", "userId",
                    "originalName", "avatarUrl", "avatarName", "message",
                }
                is_real_text = (
                    len(text) > 3
                    and text not in _JUNK_EXACT
                    and not text.startswith(("{", "http", "BLOCK_", "PRODUCT_", "16:0:"))
                    and not text.isdigit()
                    and not all(c in "0123456789TZ:.-+=" for c in text)
                    and any(c.isalpha() for c in text)
                )

                if is_real_text:
                    if msg_id not in by_id:
                        id_order.append(msg_id)
                        by_id[msg_id] = {
                            "speaker": speaker or "Unknown",
                            "text": text,
                            "timestamp": timestamp,
                            "msg_id": msg_id,
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


def parse_meeting_title(blocks_wal: Path) -> str | None:
    """Extract the most recent meeting title from the blocks WAL."""
    lines = read_wal_strings(blocks_wal)
    titles = []
    for i, line in enumerate(lines):
        if line == "title" and i + 1 < len(lines):
            candidate = lines[i + 1]
            # Meeting titles have patterns like "Name YYYY-MM-DD HH:MM"
            if (
                len(candidate) > 5
                and not candidate.startswith(("http", "{", "BLOCK_", "PRODUCT_"))
                and not candidate in ("Zoom Meeting",)
                and any(c.isalpha() for c in candidate)
            ):
                titles.append(candidate)
    # Return the last real title (most recent meeting)
    return titles[-1] if titles else None


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


# ── Claude Summarization ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional meeting notes assistant. Given a Zoom meeting transcript, produce structured meeting notes in Markdown.

Output format:
## Summary
2-3 sentence overview of what the meeting was about.

## Attendees
- Name (role/context if clear from transcript)

## Key Discussion Points
- Bullet points of the main topics discussed

## Decisions Made
- Any explicit decisions or conclusions reached (skip section if none)

## Action Items
- [ ] Action item — **Owner** (if named)

## Notable Quotes
1-2 direct quotes that capture the essence of the meeting (optional, only if truly insightful)

Be concise. Do not pad with filler. Use the speaker names from the transcript."""


def summarize_with_claude(
    transcript: str,
    meeting_title: str,
    api_key: str,
) -> str:
    """Call Claude API to summarize the transcript."""
    payload = {
        "model": "claude-opus-4-5",
        "max_tokens": 1500,
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
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
            return body["content"][0]["text"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API error {e.code}: {error_body}") from e


# ── Note Writing ───────────────────────────────────────────────────────────────

def slugify_title(title: str) -> str:
    """Turn a meeting title into a safe filename fragment."""
    # Strip the Zoom-appended date/time from titles like "WFC Sync 2026-04-21 15:01(GMT-4:00)"
    import re
    clean = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}.*$", "", title).strip()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def save_note(content: str, meeting_title: str, date_str: str) -> Path:
    """Save the generated note to Vault Mind/Meetings."""
    VAULT_MEETINGS.mkdir(parents=True, exist_ok=True)
    slug = slugify_title(meeting_title)
    filename = f"{date_str} {slug}.md"
    note_path = VAULT_MEETINGS / filename
    note_path.write_text(content, encoding="utf-8")
    return note_path


def build_note_content(
    summary: str,
    transcript: str,
    meeting_title: str,
    date_str: str,
) -> str:
    return f"""---
date: {date_str}
meeting: {meeting_title}
tags: [meeting-notes, zoom]
---

# {slugify_title(meeting_title)}

{summary}

---

## Full Transcript

{transcript}
"""


# ── CLI Commands ───────────────────────────────────────────────────────────────

def cmd_list(origin: Path) -> None:
    blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)
    transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)

    print("Zoom Meeting Notes — Recent Meetings\n")
    if blocks_wal:
        title = parse_meeting_title(blocks_wal)
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)
    blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)

    if not transcript_wal:
        print("No transcript WAL found.", file=sys.stderr)
        sys.exit(1)

    entries = parse_transcript(transcript_wal)
    if not entries:
        print("No transcript entries found.", file=sys.stderr)
        sys.exit(1)

    # Determine meeting title and date
    meeting_title = None
    if blocks_wal:
        meeting_title = parse_meeting_title(blocks_wal)

    if not meeting_title:
        # Fall back to WAL modification time
        mtime = transcript_wal.stat().st_mtime
        meeting_title = f"Zoom Meeting {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"

    # Extract date from title or fall back to today
    import re
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", meeting_title)
    date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")

    transcript_text = format_transcript(entries)

    print(f"Meeting  : {meeting_title}")
    print(f"Date     : {date_str}")
    print(f"Speakers : {len({e['speaker'] for e in entries})}")
    print(f"Lines    : {len(entries)}")
    print("\nSummarizing with Claude...")

    summary = summarize_with_claude(transcript_text, meeting_title, api_key)

    note_content = build_note_content(summary, transcript_text, meeting_title, date_str)

    if dry_run:
        print("\n" + "─" * 60)
        print(note_content)
        print("─" * 60)
        print("\n(Dry run — note not saved)")
    else:
        note_path = save_note(note_content, meeting_title, date_str)
        print(f"\nNote saved: {note_path}")


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
