#!/usr/bin/env python3
"""
zoom_engine.py — Headless WAL poller for Zoom Meeting Notes Assistant.

Replaces zoom_menu_bar.py. No rumps, no UI — designed to run as a child process
of the Swift ZoomNotesApp. Emits newline-delimited JSON events to stdout and
accepts JSON commands on stdin (for future extensibility).

State machine: idle → active → generating → idle

JSON events emitted to stdout:
  {"event": "state", "value": "idle"}
  {"event": "state", "value": "active", "meeting_id": "<id>"}
  {"event": "state", "value": "generating"}
  {"event": "done", "title": "...", "path": "...", "transcript_path": "...", "attendees": [...]}
  {"event": "error", "message": "..."}

stdin commands accepted (one JSON object per line):
  {"cmd": "generate"}     — manual trigger
  {"cmd": "reload"}       — reload settings (also triggered by SIGHUP)
"""

import json
import os
import re
import select
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _load_dotenv():
    """Load .env file from the project directory (stdlib only)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

from zoom_config import (
    get_config,
    invalidate_config_cache,
    get_api_key,
    resolve_subfolder,
    resolve_filename,
)
from zoom_notes import (
    find_origin_dir,
    find_wal,
    parse_transcript,
    parse_meeting_title,
    format_transcript,
    summarize,
    build_note_content,
    build_transcript_content,
    save_note,
    slugify_title,
    detect_active_meeting_id,
)


# ── Event emission ────────────────────────────────────────────────────────────

_emit_lock = threading.Lock()


def emit(payload: dict) -> None:
    """Write a JSON event to stdout, thread-safe."""
    with _emit_lock:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ── WAL watcher ───────────────────────────────────────────────────────────────

class EngineState:
    IDLE = "idle"
    ACTIVE = "active"
    GENERATING = "generating"


class ZoomEngine:
    def __init__(self):
        self._state = EngineState.IDLE
        self._state_lock = threading.Lock()
        self._generating_lock = threading.Lock()

        # WAL tracking for idle detection
        self._last_mtime: float | None = None
        self._last_size: int | None = None
        self._last_active_ts: float | None = None
        self._active_meeting_id: str | None = None

        # Config (reloaded on SIGHUP or "reload" command)
        self._cfg_lock = threading.Lock()
        self._reload_requested = False

        # Register SIGHUP to reload settings
        signal.signal(signal.SIGHUP, self._on_sighup)

    def _on_sighup(self, signum, frame):
        """SIGHUP triggers a config reload on the next poll tick."""
        self._reload_requested = True

    # ── State helpers ───────────────────────────────────────────────────────

    def _set_state(self, new_state: str, **extra) -> None:
        with self._state_lock:
            self._state = new_state
        payload: dict = {"event": "state", "value": new_state}
        payload.update(extra)
        emit(payload)

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state

    # ── Config ─────────────────────────────────────────────────────────────

    def _get_cfg(self):
        with self._cfg_lock:
            if self._reload_requested:
                invalidate_config_cache()
                self._reload_requested = False
        return get_config()

    # ── Polling loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        emit({"event": "state", "value": EngineState.IDLE})

        # Start stdin reader thread
        threading.Thread(target=self._stdin_reader, daemon=True).start()

        origin = None
        while True:
            try:
                cfg = self._get_cfg()
                poll_interval = cfg.poll_interval_secs
                idle_threshold = cfg.idle_threshold_secs

                if origin is None:
                    origin = find_origin_dir()

                self._poll_once(origin, cfg, idle_threshold)

            except Exception as exc:
                emit({"event": "error", "message": f"Poll error: {exc}"})

            time.sleep(poll_interval)

    def _poll_once(self, origin, cfg, idle_threshold: int) -> None:
        if origin is None:
            return

        wal = find_wal(origin, cfg.transcript_db_prefix)
        if not wal:
            if self._get_state() == EngineState.ACTIVE:
                self._set_state(EngineState.IDLE)
                self._reset_tracking()
            return

        try:
            stat = wal.stat()
        except OSError:
            return

        mtime = stat.st_mtime
        size = stat.st_size
        now = time.time()

        changed = (mtime != self._last_mtime) or (size != self._last_size)
        self._last_mtime = mtime
        self._last_size = size

        state = self._get_state()

        if changed:
            self._last_active_ts = now
            # Try to detect the active meeting ID
            try:
                meeting_id = detect_active_meeting_id(wal)
            except Exception:
                meeting_id = None

            if state == EngineState.IDLE:
                self._active_meeting_id = meeting_id
                self._set_state(EngineState.ACTIVE, meeting_id=meeting_id or "")
        else:
            if state == EngineState.ACTIVE and self._last_active_ts is not None:
                idle_secs = now - self._last_active_ts
                if idle_secs >= idle_threshold:
                    self._trigger_generate(origin, cfg)

    # ── Note generation ─────────────────────────────────────────────────────

    def _trigger_generate(self, origin, cfg) -> None:
        """Start note generation in a background thread (non-blocking)."""
        if not self._generating_lock.acquire(blocking=False):
            return  # Already generating

        self._set_state(EngineState.GENERATING)

        def worker():
            try:
                self._generate_notes(origin, cfg)
            except Exception as exc:
                emit({"event": "error", "message": str(exc)})
            finally:
                self._generating_lock.release()
                self._reset_tracking()
                self._set_state(EngineState.IDLE)

        threading.Thread(target=worker, daemon=True).start()

    def _generate_notes(self, origin, cfg) -> None:
        transcript_wal = find_wal(origin, cfg.transcript_db_prefix)
        blocks_wal = find_wal(origin, cfg.blocks_db_prefix)

        if not transcript_wal:
            raise RuntimeError("Transcript WAL not found during generation.")

        # Filter to the active meeting's entries
        entries = parse_transcript(transcript_wal, meeting_id_filter=self._active_meeting_id)
        if not entries:
            raise RuntimeError("No transcript entries found.")

        # Meeting title
        meeting_title = None
        if blocks_wal:
            try:
                meeting_title = parse_meeting_title(blocks_wal, entries)
            except Exception:
                pass
        if not meeting_title:
            mtime = transcript_wal.stat().st_mtime
            meeting_title = f"Zoom Meeting {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", meeting_title)
        date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")

        seen: dict[str, None] = {}
        for e in entries:
            s = e["speaker"]
            if s and s != "Unknown":
                seen[s] = None
        attendees = list(seen.keys())

        created_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        transcript_text = format_transcript(entries)

        # Reload config in case settings changed while generating
        cfg = self._get_cfg()
        summary = summarize(transcript_text, meeting_title, cfg)

        note_content = build_note_content(
            summary, meeting_title, date_str, attendees, created_iso, cfg
        )
        transcript_content = build_transcript_content(transcript_text, meeting_title, date_str, cfg)

        note_path = save_note(note_content, transcript_content, meeting_title, date_str, cfg)

        # Derive transcript path for the "done" event
        slug = slugify_title(meeting_title)
        subfolder = resolve_subfolder(cfg, date_str)
        transcripts_dir = cfg.transcripts_path / subfolder if subfolder else cfg.transcripts_path
        transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str) + ".md"
        transcript_path = transcripts_dir / transcript_filename

        emit({
            "event": "done",
            "title": slugify_title(meeting_title),
            "path": str(note_path),
            "transcript_path": str(transcript_path),
            "attendees": attendees,
        })

    # ── Stdin reader ────────────────────────────────────────────────────────

    def _stdin_reader(self) -> None:
        """Read JSON commands from stdin line by line."""
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_command(cmd)

    def _handle_command(self, cmd: dict) -> None:
        action = cmd.get("cmd")
        if action == "reload":
            self._reload_requested = True
        elif action == "generate":
            cfg = self._get_cfg()
            origin = find_origin_dir()
            if origin:
                self._trigger_generate(origin, cfg)
            else:
                emit({"event": "error", "message": "Zoom WAL directory not found."})

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _reset_tracking(self) -> None:
        self._last_mtime = None
        self._last_size = None
        self._last_active_ts = None
        self._active_meeting_id = None


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Flush stdout immediately so the Swift parent reads events without buffering.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    engine = ZoomEngine()
    try:
        engine.run()
    except KeyboardInterrupt:
        emit({"event": "state", "value": "idle"})
        sys.exit(0)
