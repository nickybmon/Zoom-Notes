#!/usr/bin/env python3
"""
Zoom Meeting Notes — Menu Bar App

Sits in the macOS menu bar and watches for active Zoom meetings via the
AI Notetaker WAL file. When a meeting ends (WAL goes idle for 30s),
automatically generates meeting notes via Claude and saves them to Vault Mind.

Run:
  ./venv/bin/python3 zoom_menu_bar.py

To launch at login, run this once:
  python3 zoom_menu_bar.py --install-login-item
"""

import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import rumps

# Log to the same file launchd uses, so it's visible whether running manually or at login
_log_path = Path.home() / "Library/Logs/zoom-notes.log"
logging.basicConfig(
    filename=_log_path,
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zoom-notes")

# Ensure zoom_notes.py is importable from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from zoom_notes import (
    BLOCKS_DB_PREFIX,
    TRANSCRIPT_DB_PREFIX,
    VAULT_NOTES,
    build_note_content,
    build_transcript_content,
    count_meeting_ids,
    deduplicate,
    find_origin_dir,
    find_wal,
    format_transcript,
    parse_meeting_title,
    parse_transcript,
    save_note,
    slugify_title,
    summarize_with_claude,
)


# ── Constants ──────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECS = 5
IDLE_THRESHOLD_SECS = 30

# Menu bar title strings per state
ICON_IDLE = "▶"       # play — ready, waiting for a meeting
ICON_ACTIVE = "‖"     # pause — meeting in progress / recording
ICON_GENERATING = "↻" # clockwise arrow — generating notes


# ── State ──────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE = auto()
    ACTIVE = auto()
    GENERATING = auto()


# ── App ────────────────────────────────────────────────────────────────────────

class ZoomNotesApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="Zoom Notes",
            title=ICON_IDLE,
            quit_button=None,  # we add our own so it's always last
        )

        # State
        self._state = State.IDLE
        self._last_wal_mtime: float | None = None
        self._last_wal_size: int | None = None
        self._last_active_time: float | None = None
        self._processed_meeting_ids: set[str] = set()  # prevent re-trigger on same meeting
        self._generating_lock = threading.Lock()

        # Active meeting tracking
        self._active_meeting_id: str | None = None  # meetingId we're recording
        self._meeting_start_time: float | None = None  # monotonic time when meeting activity first seen
        # Accumulated transcript: messageId → best entry dict.
        # Populated every poll tick so a WAL checkpoint can't lose data.
        self._accumulated: dict[str, dict] = {}

        # Cached origin dir (stable for the process lifetime)
        self._origin = find_origin_dir()

        log.info("App started. Origin: %s", self._origin or "(not found)")

        # Seed WAL state from disk only if the WAL was recently modified,
        # indicating an in-progress meeting. A stale WAL (older than 90 seconds)
        # is left alone — the poller will detect new activity naturally when the
        # next meeting starts writing to the same file. We must NOT mark stale
        # WALs as processed here, because Zoom reuses the same WAL path across
        # meetings and we'd silently skip the next meeting.
        if self._origin:
            _wal = find_wal(self._origin, TRANSCRIPT_DB_PREFIX)
            if _wal:
                try:
                    _stat = _wal.stat()
                    age_secs = time.time() - _stat.st_mtime
                    if age_secs < 90:
                        # WAL is fresh — we may be mid-meeting; arm idle detection
                        self._last_wal_mtime = _stat.st_mtime
                        self._last_wal_size = _stat.st_size
                        self._last_active_time = time.monotonic()
                        log.info(
                            "Seeded WAL state on startup (fresh, age=%.0fs): size=%d mtime=%.0f",
                            age_secs, _stat.st_size, _stat.st_mtime,
                        )
                    else:
                        # Stale WAL — seed mtime/size so the poller can detect
                        # when a new meeting starts changing this file, but do NOT
                        # mark it processed (that would block the next meeting).
                        self._last_wal_mtime = _stat.st_mtime
                        self._last_wal_size = _stat.st_size
                        log.info(
                            "Stale WAL on startup (age=%.0fs) — seeded size/mtime, not marking processed.",
                            age_secs,
                        )
                except OSError:
                    pass

        # Menu items
        self._status_item = rumps.MenuItem("Status: Idle", callback=None)
        self._status_item.set_callback(None)  # non-clickable label

        self._generate_item = rumps.MenuItem(
            "Generate Notes Now", callback=self._on_generate_now
        )

        self.menu = [
            self._status_item,
            None,  # separator
            self._generate_item,
            None,  # separator
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]

        # Start polling timer
        self._timer = rumps.Timer(self._poll, POLL_INTERVAL_SECS)
        self._timer.start()

    # ── Polling ────────────────────────────────────────────────────────────────

    def _poll(self, _sender):
        """Called every POLL_INTERVAL_SECS by rumps.Timer."""
        if self._state == State.GENERATING:
            return  # don't touch WAL state while generating

        if not self._origin:
            self._origin = find_origin_dir()
            if not self._origin:
                self._set_state(State.IDLE)
                return

        wal = find_wal(self._origin, TRANSCRIPT_DB_PREFIX)
        if not wal:
            self._set_state(State.IDLE)
            self._last_wal_mtime = None
            self._last_wal_size = None
            self._last_active_time = None
            return

        try:
            stat = wal.stat()
        except OSError:
            self._set_state(State.IDLE)
            return

        mtime = stat.st_mtime
        size = stat.st_size

        wal_changed = (
            mtime != self._last_wal_mtime or size != self._last_wal_size
        )

        if wal_changed:
            self._last_wal_mtime = mtime
            self._last_wal_size = size
            self._last_active_time = time.monotonic()

            # Re-evaluate the dominant meeting ID for the first 60s of activity.
            # Locking too early risks picking a ghost meeting (e.g. a double-booked
            # calendar invite already in the WAL) before enough transcript entries
            # from the real meeting have accumulated to outvote it.
            meeting_lock_age = (
                time.monotonic() - self._meeting_start_time
                if self._meeting_start_time is not None
                else 0
            )
            if self._meeting_start_time is None:
                self._meeting_start_time = time.monotonic()

            if self._active_meeting_id is None or meeting_lock_age < 60:
                counts = count_meeting_ids(wal)
                new_id = max(counts, key=lambda k: counts[k]) if counts else None
                if new_id != self._active_meeting_id:
                    log.info(
                        "Meeting ID %s -> %s (lock_age=%.0fs, all counts: %s)",
                        self._active_meeting_id, new_id, meeting_lock_age,
                        {k: v for k, v in sorted(counts.items(), key=lambda x: -x[1])},
                    )
                    self._active_meeting_id = new_id
                elif self._active_meeting_id and meeting_lock_age >= 60:
                    log.info(
                        "Locked onto meeting_id=%s after %.0fs",
                        self._active_meeting_id, meeting_lock_age,
                    )

            # Never re-trigger on a meeting we already processed this session.
            # Check AFTER resolving the meeting ID so we don't block on stale state.
            if self._active_meeting_id and self._active_meeting_id in self._processed_meeting_ids:
                return

            # Snapshot new entries into accumulator so WAL checkpoints can't lose them
            before = len(self._accumulated)
            self._snapshot_wal(wal)
            after = len(self._accumulated)
            if after > before:
                log.info("Snapshot: +%d entries (total %d)", after - before, after)

            self._set_state(State.ACTIVE)
            return

        # WAL unchanged — check idle threshold
        if self._state == State.ACTIVE and self._last_active_time is not None:
            idle_secs = time.monotonic() - self._last_active_time
            if idle_secs >= IDLE_THRESHOLD_SECS:
                self._trigger_notes()

    # ── WAL snapshot ───────────────────────────────────────────────────────────

    def _snapshot_wal(self, wal: Path):
        """Parse WAL and merge new entries into the in-memory accumulator.

        Runs on every poll tick where the WAL changed, so even if Zoom
        checkpoints (shrinks) the WAL at meeting end, we retain everything
        we've seen. Filtered to self._active_meeting_id if set.
        """
        try:
            fresh = parse_transcript(wal, meeting_id_filter=self._active_meeting_id)
        except Exception:
            return
        for entry in fresh:
            mid = entry["msg_id"]
            if mid not in self._accumulated:
                self._accumulated[mid] = entry
            else:
                existing = self._accumulated[mid]
                if len(entry["text"]) > len(existing["text"]):
                    existing["text"] = entry["text"]
                if entry.get("speaker"):
                    existing["speaker"] = entry["speaker"]
                if entry.get("timestamp"):
                    existing["timestamp"] = entry["timestamp"]

    # ── State transitions ──────────────────────────────────────────────────────

    def _set_state(self, new_state: State):
        if self._state == new_state:
            return
        self._state = new_state
        if new_state == State.IDLE:
            self.title = ICON_IDLE
            self._status_item.title = "Status: Idle"
            self._generate_item.set_callback(self._on_generate_now)
        elif new_state == State.ACTIVE:
            self.title = ICON_ACTIVE
            self._status_item.title = "Status: In Meeting"
            self._generate_item.set_callback(self._on_generate_now)
        elif new_state == State.GENERATING:
            self.title = ICON_GENERATING
            self._status_item.title = "Status: Generating notes..."
            self._generate_item.set_callback(None)  # disable during generation

    # ── Note generation ────────────────────────────────────────────────────────

    def _trigger_notes(self):
        """Transition to GENERATING and spawn background thread."""
        if not self._generating_lock.acquire(blocking=False):
            return  # already generating
        self._set_state(State.GENERATING)
        t = threading.Thread(target=self._generate_notes_worker, daemon=True)
        t.start()

    def _generate_notes_worker(self):
        """Background thread: parse, summarize, save, notify."""
        log.info("Generation started. accumulated=%d meeting_id=%s", len(self._accumulated), self._active_meeting_id)
        try:
            gemini_key = os.environ.get("ANTHROPIC_API_KEY")
            if not gemini_key:
                self._notify_error("No API key found. Set ANTHROPIC_API_KEY in .env.")
                return

            origin = self._origin
            if not origin:
                self._notify_error("Zoom MyNotes directory not found.")
                return

            blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)
            transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)

            # Prefer the in-memory accumulator (immune to WAL checkpointing).
            # Do a final WAL snapshot first to catch any last words.
            if transcript_wal:
                # If we have no active meeting ID yet (e.g. manual trigger right
                # after startup), detect it from the WAL before snapshotting so
                # the filter is applied correctly.
                if self._active_meeting_id is None:
                    counts = count_meeting_ids(transcript_wal)
                    if counts:
                        self._active_meeting_id = max(counts, key=lambda k: counts[k])
                        log.info("Detected meeting_id from WAL at generation time: %s", self._active_meeting_id)
                self._snapshot_wal(transcript_wal)

            if self._accumulated:
                # Reconstruct sorted, deduplicated entries from accumulator
                raw = sorted(self._accumulated.values(), key=lambda e: e.get("timestamp") or "")
                entries = deduplicate(raw)
            elif transcript_wal:
                entries = parse_transcript(transcript_wal, meeting_id_filter=self._active_meeting_id)
            else:
                entries = []

            if not entries:
                self._notify_error("Transcript appears empty.")
                return

            # Resolve meeting title
            meeting_title = None
            if blocks_wal:
                meeting_title = parse_meeting_title(blocks_wal, entries)
            if not meeting_title:
                mtime = transcript_wal.stat().st_mtime
                meeting_title = f"Zoom Meeting {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"

            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", meeting_title)
            date_str = (
                date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
            )

            # Extract unique speakers in order of first appearance
            seen: dict[str, None] = {}
            for e in entries:
                s = e["speaker"]
                if s and s != "Unknown":
                    seen[s] = None
            attendees = list(seen.keys())

            created_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            transcript_text = format_transcript(entries)
            summary = summarize_with_claude(transcript_text, meeting_title, gemini_key)
            note_content = build_note_content(
                summary, meeting_title, date_str, attendees, created_iso
            )
            transcript_content = build_transcript_content(
                transcript_text, meeting_title, date_str
            )
            note_path = save_note(note_content, transcript_content, meeting_title, date_str)

            short_name = slugify_title(meeting_title)
            notes_dir = VAULT_NOTES / date_str
            rel_path = str(notes_dir / note_path.name)
            log.info("Notes saved: %s  (%d entries, %d attendees)", note_path.name, len(entries), len(attendees))
            rumps.notification(
                title="Meeting Notes Saved",
                subtitle=short_name,
                message=f"Saved to {rel_path}",
                sound=True,
            )

        except Exception as exc:
            log.error("Generation failed: %s", exc, exc_info=True)
            self._notify_error(str(exc))

        finally:
            self._generating_lock.release()
            # Mark this meeting as processed so we never trigger on it again
            if self._active_meeting_id:
                self._processed_meeting_ids.add(self._active_meeting_id)
            self._last_wal_mtime = None
            self._last_wal_size = None
            self._last_active_time = None
            self._active_meeting_id = None
            self._meeting_start_time = None
            self._accumulated = {}
            self._set_state(State.IDLE)

    def _notify_error(self, message: str):
        rumps.notification(
            title="Meeting Notes Failed",
            subtitle="",
            message=message,
            sound=True,
        )

    # ── Menu callbacks ─────────────────────────────────────────────────────────

    def _on_generate_now(self, _sender):
        """Manual trigger from menu."""
        if self._state == State.GENERATING:
            return
        self._trigger_notes()

    def _on_quit(self, _sender):
        lock_path = Path(tempfile.gettempdir()) / "zoom-notes.lock"
        lock_path.unlink(missing_ok=True)
        rumps.quit_application()


# ── Login item installer ───────────────────────────────────────────────────────

def install_login_item():
    """
    Add this script to macOS Login Items using a launchd user agent plist.
    Creates: ~/Library/LaunchAgents/com.zoom-notes-assistant.plist
    """
    # Use the venv Python directly (preserves venv site-packages with rumps)
    venv_python = Path(__file__).parent / "venv/bin/python3"
    python = str(venv_python if venv_python.exists() else Path(sys.executable))
    script = str(Path(__file__).resolve())
    plist_dir = Path.home() / "Library/LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.zoom-notes-assistant.plist"

    # Read current ANTHROPIC_API_KEY so launchd inherits it
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.webflow.zoom-notes-assistant</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
  </array>
    <key>EnvironmentVariables</key>
  <dict>
    <key>ANTHROPIC_API_KEY</key>
    <string>{api_key}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{Path.home()}/Library/Logs/zoom-notes.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/Library/Logs/zoom-notes-error.log</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    print(f"Plist written to: {plist_path}")
    print("To activate now (without rebooting), run:")
    print(f"  launchctl load {plist_path}")
    print("To remove the login item:")
    print(f"  launchctl unload {plist_path} && rm {plist_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    """Write a PID lockfile. Returns False if another instance is already running."""
    lock_path = Path(tempfile.gettempdir()) / "zoom-notes.lock"
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            # Check if that process is still alive
            os.kill(existing_pid, 0)
            return False  # process exists — another instance is running
        except (ValueError, OSError):
            pass  # stale lockfile — safe to overwrite
    lock_path.write_text(str(os.getpid()))
    return True


if __name__ == "__main__":
    if "--install-login-item" in sys.argv:
        install_login_item()
    else:
        if not _acquire_lock():
            print("zoom_menu_bar.py is already running. Exiting.")
            sys.exit(0)
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        ZoomNotesApp().run()
