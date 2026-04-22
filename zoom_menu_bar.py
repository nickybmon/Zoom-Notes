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

import os
import re
import sys
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import rumps

# Ensure zoom_notes.py is importable from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from zoom_notes import (
    BLOCKS_DB_PREFIX,
    TRANSCRIPT_DB_PREFIX,
    build_note_content,
    build_transcript_content,
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
ICON_IDLE = "●"       # neutral dot — no meeting
ICON_ACTIVE = "⏺"    # filled circle — meeting in progress
ICON_GENERATING = "⟳" # spinner — generating notes


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
        self._generating_lock = threading.Lock()

        # Cached origin dir (stable for the process lifetime)
        self._origin = find_origin_dir()

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
            self._set_state(State.ACTIVE)
            return

        # WAL unchanged — check idle threshold
        if self._state == State.ACTIVE and self._last_active_time is not None:
            idle_secs = time.monotonic() - self._last_active_time
            if idle_secs >= IDLE_THRESHOLD_SECS:
                self._trigger_notes()

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
        try:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                self._notify_error("ANTHROPIC_API_KEY not set in environment.")
                return

            origin = self._origin
            if not origin:
                self._notify_error("Zoom MyNotes directory not found.")
                return

            transcript_wal = find_wal(origin, TRANSCRIPT_DB_PREFIX)
            blocks_wal = find_wal(origin, BLOCKS_DB_PREFIX)

            if not transcript_wal:
                self._notify_error("No transcript WAL found.")
                return

            entries = parse_transcript(transcript_wal)
            if not entries:
                self._notify_error("Transcript appears empty.")
                return

            # Resolve meeting title
            meeting_title = None
            if blocks_wal:
                meeting_title = parse_meeting_title(blocks_wal)
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
            summary = summarize_with_claude(transcript_text, meeting_title, api_key)
            note_content = build_note_content(
                summary, meeting_title, date_str, attendees, created_iso
            )
            transcript_content = build_transcript_content(
                transcript_text, meeting_title, date_str
            )
            note_path = save_note(note_content, transcript_content, meeting_title, date_str)

            short_name = slugify_title(meeting_title)
            rel_path = f"Vault Mind/Meetings/Notes/{date_str}/{note_path.name}"
            rumps.notification(
                title="Meeting Notes Saved",
                subtitle=short_name,
                message=f"Saved to {rel_path}",
                sound=True,
            )

        except Exception as exc:
            self._notify_error(str(exc))

        finally:
            self._generating_lock.release()
            # Reset WAL tracking so we don't re-trigger on the same WAL
            self._last_wal_mtime = None
            self._last_wal_size = None
            self._last_active_time = None
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
        rumps.quit_application()


# ── Login item installer ───────────────────────────────────────────────────────

def install_login_item():
    """
    Add this script to macOS Login Items using a launchd user agent plist.
    Creates: ~/Library/LaunchAgents/com.nickblackmon.zoom-notes.plist
    """
    # Use the venv Python directly (preserves venv site-packages with rumps)
    venv_python = Path(__file__).parent / "venv/bin/python3"
    python = str(venv_python if venv_python.exists() else Path(sys.executable))
    script = str(Path(__file__).resolve())
    plist_dir = Path.home() / "Library/LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.nickblackmon.zoom-notes.plist"

    # Read current ANTHROPIC_API_KEY so launchd inherits it
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.nickblackmon.zoom-notes</string>
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
    print("To remove:")
    print(f"  launchctl unload {plist_path} && rm {plist_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--install-login-item" in sys.argv:
        install_login_item()
    else:
        ZoomNotesApp().run()
