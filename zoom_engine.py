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

import concurrent.futures
import json
import re
import signal
import sys
import threading
import time
from datetime import datetime


def _friendly_error(exc: Exception) -> str:
    """Convert a raw exception into a short, human-readable menu bar message."""
    msg = str(exc)
    # HTTP status codes from LLM providers
    for code, label in [
        ("429", "LLM quota exceeded — try again later"),
        ("401", "LLM authentication failed — check your API key in Settings"),
        ("403", "LLM access denied — check your API key in Settings"),
        ("500", "LLM server error — try again later"),
        ("503", "LLM service unavailable — try again later"),
    ]:
        if code in msg:
            return label
    # Truncate anything else to a reasonable length
    first_line = msg.splitlines()[0] if msg else "Unknown error"
    return first_line[:80] + ("…" if len(first_line) > 80 else "")


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
    persist_accumulator,
    load_persisted_accumulator,
    delete_persisted_accumulator,
    purge_stale_accumulators,
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


# Hard ceiling for one note-generation pass (parse + LLM + write).
# urllib's per-call timeout is 180s and _http_retry sleeps 15+30+60 between
# attempts, so the worst-case for one summarize() call is ~13 minutes. We cap
# the whole pipeline at 5 minutes — anything longer is almost certainly stuck.
_GENERATE_TIMEOUT_SECS = 5 * 60


class ZoomEngine:
    def __init__(self):
        self._state = EngineState.IDLE
        self._state_lock = threading.Lock()
        self._generating_lock = threading.Lock()

        # All WAL-tracking variables share a single lock. Read and write only
        # via the helpers below; never touch the underscored fields directly
        # from outside the lock.
        self._tracking_lock = threading.Lock()
        self._last_mtime: float | None = None
        self._last_size: int | None = None
        self._last_active_ts: float | None = None
        self._active_meeting_id: str | None = None

        # Last meeting_id we successfully generated notes for. Guards against
        # duplicate generation when Zoom checkpoints/mutates the WAL after a
        # meeting ends — the post-meeting WAL still contains the same entries,
        # so we'd otherwise re-summarize the same conversation.
        self._last_generated_meeting_id: str | None = None

        # Accumulated transcript entries keyed by msg_id. Populated on every
        # poll tick so a WAL checkpoint can't lose data already read.
        self._accumulated: dict[str, dict] = {}
        self._accumulated_lock = threading.Lock()

        # Monotonic timestamp of when the WAL was last seen while ACTIVE.
        # Set when the WAL disappears mid-meeting (Zoom checkpoint) so we can
        # fire generation after idle_threshold even without the WAL present.
        self._wal_gone_since: float | None = None

        # Purge stale in-progress cache files left over from prior crashes.
        purge_stale_accumulators()

        # Config (reloaded on SIGHUP or "reload" command)
        self._cfg_lock = threading.Lock()
        self._reload_requested = False
        # Cleared together with config so a settings change picks up new WAL
        # prefixes / a Zoom install relocation on the next tick.
        self._origin_invalidated = False

        signal.signal(signal.SIGHUP, self._on_sighup)

    def _on_sighup(self, signum, frame):
        """SIGHUP triggers a config reload on the next poll tick."""
        self._reload_requested = True

    # ── Tracking helpers (always held under _tracking_lock) ─────────────────

    def _read_tracking(self):
        with self._tracking_lock:
            return (
                self._last_mtime,
                self._last_size,
                self._last_active_ts,
                self._active_meeting_id,
            )

    def _write_tracking(self, *, mtime=..., size=..., active_ts=..., meeting_id=...):
        with self._tracking_lock:
            if mtime is not ...:
                self._last_mtime = mtime
            if size is not ...:
                self._last_size = size
            if active_ts is not ...:
                self._last_active_ts = active_ts
            if meeting_id is not ...:
                self._active_meeting_id = meeting_id

    def _reset_tracking(self) -> None:
        with self._tracking_lock:
            self._last_mtime = None
            self._last_size = None
            self._last_active_ts = None
            self._active_meeting_id = None

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
                self._origin_invalidated = True
        return get_config()

    def _consume_origin_invalidated(self) -> bool:
        with self._cfg_lock:
            was = self._origin_invalidated
            self._origin_invalidated = False
            return was

    # ── Polling loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        # Emit a one-shot readiness event with Zoom-detection status before
        # the regular state stream, so the UI can show a setup error if
        # Zoom isn't installed / WAL paths don't resolve.
        initial_origin = find_origin_dir()
        emit({
            "event": "ready",
            "zoom_installed": initial_origin is not None,
            "wal_path": str(initial_origin) if initial_origin else None,
        })
        emit({"event": "state", "value": EngineState.IDLE})

        threading.Thread(target=self._stdin_reader, daemon=True).start()

        origin = initial_origin
        while True:
            try:
                cfg = self._get_cfg()
                poll_interval = cfg.poll_interval_secs
                idle_threshold = cfg.idle_threshold_secs

                # Reset cached origin whenever config was reloaded OR we never
                # found one — a re-poll is cheap (single directory iter) and
                # lets us recover when Zoom is installed/relocated mid-session.
                if origin is None or self._consume_origin_invalidated():
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
            state = self._get_state()
            if state == EngineState.ACTIVE:
                with self._accumulated_lock:
                    has_entries = bool(self._accumulated)
                if has_entries:
                    # WAL disappeared mid-meeting (Zoom checkpoint). Keep ACTIVE
                    # and fire generation once the idle threshold elapses.
                    if self._wal_gone_since is None:
                        self._wal_gone_since = time.monotonic()
                    idle_secs = time.monotonic() - self._wal_gone_since
                    if idle_secs >= idle_threshold:
                        self._wal_gone_since = None
                        self._trigger_generate(origin, cfg)
                    return
                # No entries yet — WAL gone before we accumulated anything
                self._wal_gone_since = None
                self._set_state(EngineState.IDLE)
                self._reset_tracking()
            return
        # WAL present — reset the gone timer
        self._wal_gone_since = None

        try:
            stat = wal.stat()
        except OSError:
            return

        mtime = stat.st_mtime
        size = stat.st_size
        now = time.time()

        last_mtime, last_size, last_active_ts, _ = self._read_tracking()

        # First poll after startup (or after _reset_tracking): we don't know
        # whether the WAL is currently changing or just has stale state from a
        # past meeting. Anchor to the observed values WITHOUT marking it as
        # "changed" — otherwise a stale WAL from a meeting that ended hours
        # ago would immediately flip us to ACTIVE on engine restart and
        # re-trigger generation 90 seconds later.
        if last_mtime is None or last_size is None:
            self._write_tracking(mtime=mtime, size=size)
            return

        changed = (mtime != last_mtime) or (size != last_size)
        self._write_tracking(mtime=mtime, size=size)

        state = self._get_state()

        if changed:
            self._write_tracking(active_ts=now)
            try:
                meeting_id = detect_active_meeting_id(wal)
            except Exception:
                meeting_id = None

            if state == EngineState.IDLE:
                self._write_tracking(meeting_id=meeting_id)
                with self._accumulated_lock:
                    self._accumulated = {}
                self._set_state(EngineState.ACTIVE, meeting_id=meeting_id or "")

            # Snapshot new entries into the accumulator on every change tick
            try:
                _, _, _, current_meeting_id = self._read_tracking()
                fresh = parse_transcript(wal, meeting_id_filter=current_meeting_id)
                with self._accumulated_lock:
                    before = len(self._accumulated)
                    for entry in fresh:
                        mid_key = entry["msg_id"]
                        if mid_key not in self._accumulated:
                            self._accumulated[mid_key] = entry
                        else:
                            existing = self._accumulated[mid_key]
                            if len(entry.get("message", "")) > len(existing.get("message", "")):
                                existing["message"] = entry["message"]
                            if entry.get("speaker") and entry["speaker"] != "Unknown":
                                existing["speaker"] = entry["speaker"]
                            if entry.get("timestamp"):
                                existing["timestamp"] = entry["timestamp"]
                    added = len(self._accumulated) - before
                if added > 0 and current_meeting_id:
                    with self._accumulated_lock:
                        snapshot = dict(self._accumulated)
                    persist_accumulator(current_meeting_id, snapshot)
            except Exception:
                pass
        else:
            if state == EngineState.ACTIVE and last_active_ts is not None:
                idle_secs = now - last_active_ts
                if idle_secs >= idle_threshold:
                    # Belt-and-suspenders: don't re-summarize a meeting we
                    # already finished. Zoom checkpoints the WAL after the
                    # meeting ends, which mutates mtime/size and would
                    # otherwise look like a new active period.
                    _, _, _, current_meeting_id = self._read_tracking()
                    if current_meeting_id is not None and \
                       current_meeting_id == self._last_generated_meeting_id:
                        self._write_tracking(active_ts=None)
                        self._set_state(EngineState.IDLE)
                        return

                    provider = cfg.llm_provider
                    if provider != "ollama":
                        from zoom_config import get_api_key as _get_key
                        if not _get_key(provider):
                            emit({"event": "error", "message": f"No API key set for {provider}. Open Settings to configure."})
                            self._write_tracking(active_ts=None)  # disarm until WAL changes again
                            return
                    self._trigger_generate(origin, cfg)

    # ── Note generation ─────────────────────────────────────────────────────

    def _trigger_generate(self, origin, cfg) -> None:
        """Start note generation in a background thread (non-blocking).

        Wraps the work in a ThreadPoolExecutor so we can cap wall-clock time at
        _GENERATE_TIMEOUT_SECS — a stuck LLM call must not pin the engine to
        the GENERATING state forever.
        """
        if not self._generating_lock.acquire(blocking=False):
            return

        self._set_state(EngineState.GENERATING)

        # Snapshot tracking state under-lock so a restore-on-error can put us
        # back in a coherent ACTIVE-but-disarmed state.
        saved_mtime, saved_size, _, _ = self._read_tracking()

        # Snapshot the meeting_id we're processing so the worker can record
        # it on success even after _reset_tracking would have cleared it.
        _, _, _, processing_meeting_id = self._read_tracking()

        def worker():
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self._generate_notes, origin, cfg)
            try:
                future.result(timeout=_GENERATE_TIMEOUT_SECS)
                # Remember which meeting we just summarized so we don't
                # re-summarize it if Zoom mutates the WAL on checkpoint.
                self._last_generated_meeting_id = processing_meeting_id
                # Clean up the in-memory accumulator and persisted disk snapshot.
                with self._accumulated_lock:
                    self._accumulated = {}
                if processing_meeting_id:
                    delete_persisted_accumulator(processing_meeting_id)
                self._wal_gone_since = None
                # Anchor tracking to the WAL state we just consumed. We must
                # NOT reset to None — that would make the next poll see the
                # unchanged WAL as "new", flip back to ACTIVE, idle out, and
                # regenerate the same notes in a loop.
                #
                # Re-stat now so we capture any final writes Zoom did during
                # the generation window; if the meeting really is over the
                # next poll's mtime/size will match these and `changed`
                # stays False until a NEW meeting starts.
                try:
                    wal = find_wal(origin, cfg.transcript_db_prefix)
                    if wal is not None:
                        s = wal.stat()
                        self._write_tracking(
                            mtime=s.st_mtime, size=s.st_size,
                            active_ts=None, meeting_id=None,
                        )
                    else:
                        # WAL disappeared entirely — meeting fully checkpointed.
                        # Safe to reset; there's nothing to re-trigger on.
                        self._reset_tracking()
                except OSError:
                    self._reset_tracking()
            except concurrent.futures.TimeoutError:
                emit({"event": "error", "message": "Note generation timed out — try again"})
                future.cancel()
                self._write_tracking(
                    mtime=saved_mtime, size=saved_size,
                    active_ts=None, meeting_id=None,
                )
                self._wal_gone_since = None
            except Exception as exc:
                emit({"event": "error", "message": _friendly_error(exc)})
                self._write_tracking(
                    mtime=saved_mtime, size=saved_size,
                    active_ts=None, meeting_id=None,
                )
                self._wal_gone_since = None
            finally:
                executor.shutdown(wait=False)
                self._generating_lock.release()
                self._set_state(EngineState.IDLE)

        threading.Thread(target=worker, daemon=True).start()

    def _generate_notes(self, origin, cfg) -> None:
        transcript_wal = find_wal(origin, cfg.transcript_db_prefix)
        blocks_wal = find_wal(origin, cfg.blocks_db_prefix)

        _, _, _, active_meeting_id = self._read_tracking()

        # Try the in-memory accumulator first (most up-to-date).
        with self._accumulated_lock:
            acc_snapshot = dict(self._accumulated)

        if acc_snapshot:
            entries = sorted(acc_snapshot.values(), key=lambda e: e.get("timestamp") or "")
        elif transcript_wal:
            entries = parse_transcript(transcript_wal, meeting_id_filter=active_meeting_id)
        elif active_meeting_id:
            # Process was restarted mid-meeting — try the persisted disk snapshot.
            persisted = load_persisted_accumulator(active_meeting_id)
            entries = sorted(persisted.values(), key=lambda e: e.get("timestamp") or "") if persisted else []
        else:
            entries = []

        if not transcript_wal and not entries:
            raise RuntimeError("Transcript WAL not found during generation.")

        if not entries:
            raise RuntimeError("No transcript entries found.")

        # Minimum-meeting-length gate: skip false-positive triggers from
        # mid-meeting silences or test calls. Heuristic: at least 10 utterances
        # AND at least 120 seconds spanned by their timestamps.
        if len(entries) < 10:
            raise RuntimeError("Meeting too short — skipped (fewer than 10 utterances).")
        ts_strings = sorted(e.get("timestamp") or "" for e in entries if e.get("timestamp"))
        if len(ts_strings) >= 2:
            try:
                first = datetime.strptime(ts_strings[0], "%H:%M:%S")
                last = datetime.strptime(ts_strings[-1], "%H:%M:%S")
                span = (last - first).total_seconds()
                if span < 0:  # crossed midnight; treat as long enough
                    span = 120
                if span < 120:
                    raise RuntimeError(
                        f"Meeting too short — skipped (transcript spans only {int(span)}s)."
                    )
            except ValueError:
                pass  # malformed timestamps — fall through and let summarization run

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
        slug = slugify_title(meeting_title, fallback_date=date_str)
        subfolder = resolve_subfolder(cfg, date_str)
        transcripts_dir = cfg.transcripts_path / subfolder if subfolder else cfg.transcripts_path
        transcript_filename = resolve_filename(cfg.transcript_filename_pattern, slug, date_str) + ".md"
        transcript_path = transcripts_dir / transcript_filename

        emit({
            "event": "done",
            "title": slug,
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
