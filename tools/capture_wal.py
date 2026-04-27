#!/usr/bin/env python3
"""
capture_wal.py — Snapshot the current Zoom transcript + blocks WAL into a fixture.

Usage:
    python3 tools/capture_wal.py <fixture-name>

Examples:
    python3 tools/capture_wal.py single_meeting
    python3 tools/capture_wal.py multi_meeting_wal

The fixture lands at tests/fixtures/<fixture-name>/ and contains:
    transcript.sqlite3-wal
    blocks.sqlite3-wal
    meta.json   — capture timestamp, prefixes used, file sizes

Run during a real meeting (or shortly after) to seed the test harness with a
realistic WAL. Fixtures are committed so anyone can run the test suite without
needing Zoom installed.

Why this exists: the bug we hit on 2026-04-27 (stale meeting ID in WAL causing
silent drop of new utterances) required a captured WAL to reproduce reliably.
Live WALs change too quickly to debug interactively.
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from zoom_config import get_config  # noqa: E402
from zoom_notes import find_origin_dir, find_wal  # noqa: E402


def capture(fixture_name: str) -> int:
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / fixture_name
    if fixture_dir.exists():
        print(f"Fixture already exists: {fixture_dir}", file=sys.stderr)
        print("Delete it first if you want to recapture.", file=sys.stderr)
        return 1

    cfg = get_config()
    origin = find_origin_dir()
    if not origin:
        print("Error: Zoom MyNotes directory not found.", file=sys.stderr)
        return 1

    transcript_wal = find_wal(origin, cfg.transcript_db_prefix)
    blocks_wal = find_wal(origin, cfg.blocks_db_prefix)

    if not transcript_wal:
        print("Error: no transcript WAL found.", file=sys.stderr)
        return 1

    fixture_dir.mkdir(parents=True)

    shutil.copy2(transcript_wal, fixture_dir / "transcript.sqlite3-wal")
    if blocks_wal:
        shutil.copy2(blocks_wal, fixture_dir / "blocks.sqlite3-wal")

    meta = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "transcript_db_prefix": cfg.transcript_db_prefix,
        "blocks_db_prefix": cfg.blocks_db_prefix,
        "transcript_wal_size": transcript_wal.stat().st_size,
        "blocks_wal_size": blocks_wal.stat().st_size if blocks_wal else 0,
        "transcript_wal_mtime": transcript_wal.stat().st_mtime,
    }
    (fixture_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"Captured fixture: {fixture_dir}")
    print(f"  transcript WAL: {meta['transcript_wal_size']} bytes")
    print(f"  blocks WAL:     {meta['blocks_wal_size']} bytes")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(capture(sys.argv[1]))


if __name__ == "__main__":
    main()
