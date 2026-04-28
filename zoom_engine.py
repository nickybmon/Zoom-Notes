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
  {"event": "done", "title": "...", "path": "...", "transcript_path": "...", "attendees": [...], "meeting_id": "..."}
  {"event": "recovery_available", "meeting_id": "...", "entry_count": N, "last_updated": "...", "slug_hint": "...", "location": "root|failed", "title": "...?", "failed_at": "...?", "last_error": "...?"}
  {"event": "error", "message": "..."}

stdin commands accepted (one JSON object per line):
  {"cmd": "generate"}                       — manual trigger
  {"cmd": "reload"}                         — reload settings (also triggered by SIGHUP)
  {"cmd": "retry", "meeting_id": "..."}     — retry a meeting whose LLM call just failed
  {"cmd": "recover", "meeting_id": "..."}   — recover a meeting from a prior crash
"""

import concurrent.futures
import json
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


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
    CancelledError,
    find_origin_dir,
    find_wal,
    parse_transcript,
    parse_meeting_title,
    format_transcript,
    summarize,
    build_note_content,
    build_placeholder_note,
    build_transcript_content,
    save_transcript_only,
    save_note_only,
    overwrite_note,
    slugify_title,
    detect_active_meeting_id,
    persist_accumulator,
    load_persisted_accumulator,
    delete_persisted_accumulator,
    purge_stale_accumulators,
    list_recoverable_meetings,
    mark_meeting_failed,
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

# Force a fresh on-disk snapshot every N ACTIVE ticks regardless of whether
# the in-memory accumulator changed. Belt-and-suspenders against a SQLite WAL
# checkpoint that resets the file without the parser yielding new entries —
# the in-RAM accumulator stays correct, but the on-disk copy could otherwise
# go stale right when a crash would leave it as the only surviving copy.
# At the default 5s poll interval, 6 ticks ≈ 30s.
_PERIODIC_PERSIST_TICKS = 6

# A WAL "shrink" is suspicious only when the new size is well below the
# previous size — normal frame rotation can drop a few bytes between ticks
# without truncating the log. Treat <50% of last_size as a truncate signal.
_TRUNCATE_RATIO = 0.5


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
        # WAL mtime captured when the current ACTIVE session first started
        # (the IDLE→ACTIVE transition tick). Combined with `_active_meeting_id`
        # it forms a session fingerprint that distinguishes recurring Zoom
        # meetings (same meeting_id, different start time) from a checkpoint
        # mutation of the same meeting we just generated for.
        self._active_session_mtime: float | None = None

        # Fingerprint of the last successfully-generated meeting session.
        # Tuple of (meeting_id, session_start_mtime). Guards against:
        #   1. Zoom's post-meeting WAL checkpoint replaying the same meeting.
        #   2. False suppression of recurring meetings that reuse the same
        #      Zoom meeting_id — a new IDLE→ACTIVE produces a different
        #      session_start_mtime so the guard correctly lets it through.
        self._last_generated_session: tuple[str, float] | None = None

        # Set by _generate_notes when it writes a placeholder note instead of
        # a final note. Read by the worker so it knows to keep the persisted
        # accumulator (for retry) rather than deleting it.
        self._last_run_note_failed: bool = False

        # Cooperative cancellation signal for in-flight note generation.
        # Cleared at the start of every _trigger_generate / _trigger_retry
        # and set when the outer 5-min wall-clock timeout fires (so the
        # still-running summarize() thread can abort at its next checkpoint
        # instead of zombie-running until the urllib per-call timeout).
        self._cancel_event = threading.Event()

        # Most recent failed-note metadata, used to drive a retry without
        # asking the user to remember the meeting ID. Cleared on retry success.
        self._last_failed_meeting: dict | None = None

        # Accumulated transcript entries keyed by msg_id. Populated on every
        # poll tick so a WAL checkpoint can't lose data already read.
        self._accumulated: dict[str, dict] = {}
        self._accumulated_lock = threading.Lock()

        # Monotonic timestamp of when the WAL was last seen while ACTIVE.
        # Set when the WAL disappears mid-meeting (Zoom checkpoint) so we can
        # fire generation after idle_threshold even without the WAL present.
        self._wal_gone_since: float | None = None

        # Counter of ACTIVE ticks since the last accumulator persist call. Used
        # to force a fresh disk snapshot every _PERIODIC_PERSIST_TICKS even when
        # nothing changed in the parser output — guards against the gap where a
        # SQLite WAL checkpoint truncates the file without producing parser
        # output that flips `changed_in_acc` true.
        self._ticks_since_persist: int = 0

        # Streak counter for change-ticks while ACTIVE that yielded zero
        # new accumulator entries. A growing streak strongly suggests a
        # parser regression vs. a Zoom WAL format change — the WAL is
        # being written (mtime/size moved) but our parser sees nothing.
        # Emitted via diag at threshold so a degraded session is visible
        # in logs without re-creating the issue.
        self._empty_parse_streak: int = 0

        # Purge stale in-progress cache files left over from prior crashes.
        # Order matters: purge first so the recovery scan that follows only
        # surfaces snapshots inside the live retention window.
        purge_stale_accumulators()

        # Snapshot any in-progress accumulators that survived purge so run()
        # can emit `recovery_available` events on startup. We capture this in
        # __init__ rather than at run() time so a crash between those two
        # phases doesn't lose the list. The Swift menu bar uses these events
        # to surface a "Recover unfinished meeting" item — without this,
        # cached transcripts from a prior crash are unreachable through the
        # UI even though they're sitting on disk.
        try:
            self._recoverable_at_startup: list[dict] = list_recoverable_meetings()
        except Exception:
            self._recoverable_at_startup = []

        # Config (reloaded on SIGHUP or "reload" command)
        self._cfg_lock = threading.Lock()
        self._reload_requested = False
        # Cleared together with config so a settings change picks up new WAL
        # prefixes / a Zoom install relocation on the next tick.
        self._origin_invalidated = False

        # Resolved WAL paths, keyed by (origin_str, kind). Populated lazily on
        # first successful resolution and reused on every subsequent poll —
        # the underlying IndexedDB folder structure is stable for the life of
        # an engine session. Cleared whenever origin is invalidated (settings
        # reload, Zoom relocation) so a config change picks up fresh paths.
        # Only positive results are cached — `None` is intentionally NOT
        # cached so we keep retrying when the user enables Notetaker / starts
        # their first meeting after launching the app.
        self._wal_cache: dict[tuple[str, str], Path] = {}

        # One-shot guard for the "Zoom installed but no transcript WAL"
        # setup error. Without this, a misconfigured Zoom would re-emit the
        # error every 5-second poll and spam the user. Reset on origin
        # invalidation so settings changes give us a fresh chance to retry.
        self._setup_error_emitted = False

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

    def _read_session_mtime(self) -> float | None:
        with self._tracking_lock:
            return self._active_session_mtime

    def _write_tracking(self, *, mtime=..., size=..., active_ts=..., meeting_id=..., session_mtime=...):
        with self._tracking_lock:
            if mtime is not ...:
                self._last_mtime = mtime
            if size is not ...:
                self._last_size = size
            if active_ts is not ...:
                self._last_active_ts = active_ts
            if meeting_id is not ...:
                self._active_meeting_id = meeting_id
            if session_mtime is not ...:
                self._active_session_mtime = session_mtime

    def _reset_tracking(self) -> None:
        with self._tracking_lock:
            self._last_mtime = None
            self._last_size = None
            self._last_active_ts = None
            self._active_meeting_id = None
            self._active_session_mtime = None
        # Reset the periodic-persist counter alongside tracking — its meaning
        # is "ticks since last persist while ACTIVE", and a tracking reset
        # always implies we're leaving ACTIVE. Same reasoning for the
        # empty-parse streak.
        self._ticks_since_persist = 0
        self._empty_parse_streak = 0

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

    # ── Diagnostics ─────────────────────────────────────────────────────────

    def _emit_diag(self, kind: str, **fields) -> None:
        """Emit a structured diagnostic event when diagnostics is enabled.

        These show up in the engine's stdout JSON stream and are mirrored to
        the Swift app's log file by EngineManager. Useful for post-mortem
        investigation of "why was my meeting skipped" without code reading.
        """
        try:
            if not self._diagnostics_enabled():
                return
        except Exception:
            return
        payload = {"event": "diag", "kind": kind}
        payload.update(fields)
        emit(payload)

    def _diagnostics_enabled(self) -> bool:
        try:
            cfg = self._get_cfg()
            return bool(getattr(cfg, "diagnostics", False))
        except Exception:
            return False

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
        if was:
            # Drop cached WAL paths and re-arm the one-shot setup error so a
            # settings change (or a Zoom reinstall mid-session) gets a fresh
            # resolution and a fresh chance to surface a real diagnostic.
            self._wal_cache.clear()
            self._setup_error_emitted = False
        return was

    # ── WAL resolution (with cache and setup-error fallback) ────────────────

    def _resolve_wal(self, origin, cfg, kind: str) -> Path | None:
        """Resolve the transcript or blocks WAL, caching positive results.

        `kind` is `"transcript"` or `"blocks"`. Calls into `find_wal()` —
        which tries the configured prefix first and falls back to scanning
        WAL contents — and caches the resolved Path in memory. Subsequent
        polls re-use the cached path until the file disappears or origin is
        invalidated. Negative results (None) are NOT cached: that's the
        common cold-start state for users who haven't yet run a meeting
        with AI Notetaker, and we want every poll to keep trying until the
        WAL appears.
        """
        if origin is None:
            return None
        cache_key = (str(origin), kind)
        cached = self._wal_cache.get(cache_key)
        if cached is not None:
            try:
                if cached.exists() and cached.stat().st_size > 256:
                    return cached
            except OSError:
                pass
            # Stale cache — Zoom rotated the WAL or the user reinstalled.
            self._wal_cache.pop(cache_key, None)

        prefix = (
            cfg.transcript_db_prefix
            if kind == "transcript"
            else cfg.blocks_db_prefix
        )
        wal = find_wal(origin, prefix, kind=kind)
        if wal is not None:
            self._wal_cache[cache_key] = wal
            via = "prefix" if wal.parent.name.startswith(prefix) else "content"
            self._emit_diag(
                "wal_resolved",
                wal_kind=kind,
                path=str(wal),
                via=via,
            )
        return wal

    def _maybe_emit_setup_error(self, origin) -> None:
        """Emit a one-shot UI error when origin is found but no transcript
        WAL exists.

        This is the failure mode that previously left the engine silently
        stuck in IDLE forever: Zoom is installed (we have an origin), but
        none of its IndexedDB stores contain transcript data — typically
        because AI Companion / Notetaker is disabled or has never been
        used on this account. Surfacing this in the UI tells the user
        what to fix instead of leaving them staring at "Waiting for
        meeting..." through an entire call.
        """
        if origin is None or self._setup_error_emitted:
            return
        self._setup_error_emitted = True
        emit({
            "event": "error",
            "message": (
                "Couldn't find Zoom's transcript database. Make sure "
                "AI Companion / Notetaker is enabled in your Zoom client "
                "and has been used at least once on this account."
            ),
        })

    # ── Persistence helpers ─────────────────────────────────────────────────

    def _persist_accumulator_now(self, meeting_id: str, reason: str) -> None:
        """Snapshot and write the in-memory accumulator to disk, atomically.

        Resets the periodic-persist tick counter so a same-tick persist can't
        be immediately followed by another forced one. `reason` is included in
        the diag event for post-mortem analysis ("changed", "truncated",
        "periodic"). Safe to call even with an empty accumulator — it just
        writes an empty snapshot, which is a valid resume state.
        """
        if not meeting_id:
            return
        with self._accumulated_lock:
            snapshot = dict(self._accumulated)
        try:
            persist_accumulator(meeting_id, snapshot)
        except Exception:
            return
        self._ticks_since_persist = 0
        self._emit_diag(
            "accumulator_persisted",
            count=len(snapshot),
            meeting_id=meeting_id,
            reason=reason,
        )

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

        # Surface any in-progress accumulators left over from a prior crash.
        # Emitted between `ready` and the first `state` event so the UI can
        # render a "Recover unfinished meeting" menu before the engine starts
        # actively polling. The UI is responsible for suppressing recovery
        # entries whose meeting_id later matches an `active` state event —
        # those will be auto-resumed via the existing IDLE→ACTIVE seed path.
        for rec in self._recoverable_at_startup:
            evt = {
                "event": "recovery_available",
                "meeting_id": rec["meeting_id"],
                "entry_count": rec["entry_count"],
                "last_updated": rec["last_updated"],
                "slug_hint": rec["slug_hint"],
                "location": rec.get("location", "root"),
            }
            # Confirmed-failed snapshots (in `failed/`) carry sidecar metadata
            # — pass through what we have so the menu bar can render a richer
            # label like "Sales Sync — failed 3 days ago" instead of the
            # speaker-name slug hint.
            if rec.get("title"):
                evt["title"] = rec["title"]
            if rec.get("failed_at"):
                evt["failed_at"] = rec["failed_at"]
            if rec.get("last_error"):
                evt["last_error"] = rec["last_error"]
            emit(evt)

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

        wal = self._resolve_wal(origin, cfg, "transcript")
        if not wal:
            # Zoom is installed but we can't find a transcript-shaped WAL.
            # Surface the diagnostic the first time we hit this so the user
            # sees a real error instead of silently sitting in IDLE. We
            # only emit when we've never been ACTIVE — once a meeting has
            # been seen, a transient WAL-gone window is the existing
            # "checkpoint mid-meeting" path below and shouldn't trigger a
            # setup-error message.
            if self._get_state() == EngineState.IDLE:
                self._maybe_emit_setup_error(origin)
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

        # Truncate detection: the WAL shrunk well below its previous size,
        # which on SQLite means a checkpoint truncated the journal. The new
        # entries we already accumulated are still valid in RAM, but the
        # parser will yield nothing new from this point until fresh writes
        # land — so this is a critical moment to force a persist.
        truncated = (
            last_size is not None
            and size < last_size
            and size < last_size * _TRUNCATE_RATIO
        )

        self._write_tracking(mtime=mtime, size=size)

        state = self._get_state()

        # Force a fresh disk snapshot when the WAL just got truncated (a
        # SQLite checkpoint cleared the journal) or when we've gone too many
        # ACTIVE ticks without persisting. Run this BEFORE the change-handling
        # branch so a tick that sees a truncate ALSO benefits from the regular
        # accumulator-update logic below if there happens to be fresh data.
        if state == EngineState.ACTIVE:
            self._ticks_since_persist += 1
            _, _, _, current_meeting_id = self._read_tracking()
            if current_meeting_id and (
                truncated or self._ticks_since_persist >= _PERIODIC_PERSIST_TICKS
            ):
                if truncated:
                    self._emit_diag(
                        "wal_truncated",
                        meeting_id=current_meeting_id,
                        old_size=last_size, new_size=size,
                    )
                self._persist_accumulator_now(
                    current_meeting_id,
                    reason="truncated" if truncated else "periodic",
                )

        if changed:
            self._write_tracking(active_ts=now)
            try:
                meeting_id = detect_active_meeting_id(wal)
            except Exception:
                meeting_id = None

            if state == EngineState.IDLE:
                # Stamp the session fingerprint as we enter ACTIVE. `mtime`
                # is the current WAL mtime; combined with meeting_id it lets
                # the post-success dedupe distinguish a recurring meeting's
                # new session from a checkpoint of the one we just finished.
                self._write_tracking(meeting_id=meeting_id, session_mtime=mtime)
                # Fresh ACTIVE period — restart the periodic-persist clock so
                # we don't immediately force a snapshot on the very first
                # change tick before the accumulator has any new content.
                self._ticks_since_persist = 0
                with self._accumulated_lock:
                    # Seed from any persisted snapshot so a mid-meeting engine
                    # restart doesn't lose entries collected before the restart.
                    # Validate every entry's meeting_id matches — guards against
                    # a mismatched snapshot ever poisoning the accumulator.
                    self._accumulated = {}
                    if meeting_id:
                        persisted = load_persisted_accumulator(meeting_id)
                        if persisted:
                            self._accumulated = {
                                k: v for k, v in persisted.items()
                                if not v.get("meeting_id") or v.get("meeting_id") == meeting_id
                            }
                    acc_size = len(self._accumulated)
                self._emit_diag(
                    "meeting_id_changed",
                    from_id=None, to_id=meeting_id or "",
                    reason="idle_to_active", seeded_from_snapshot=acc_size > 0,
                )
                self._set_state(
                    EngineState.ACTIVE,
                    meeting_id=meeting_id or "",
                    accumulator_size=acc_size,
                )
            elif state == EngineState.ACTIVE and meeting_id:
                # Re-evaluate the active meeting ID on every tick. If the WAL
                # now scores a different meeting as best (new meeting started
                # while old meeting's data was still in the WAL), switch to it
                # and clear the accumulator so we don't mix the two meetings.
                _, _, _, current_tracking_id = self._read_tracking()
                if current_tracking_id and meeting_id != current_tracking_id:
                    # Delete the OLD meeting's on-disk snapshot. By definition
                    # we now know that meeting was a misidentification — the
                    # scoring just promoted a different meeting to active —
                    # so the old slug's cache file is at best stale and at
                    # worst contaminated with entries that actually belong to
                    # the new meeting (since `_persist_accumulator_now` writes
                    # whatever's in the in-memory accumulator under the old
                    # slug). Leaving it on disk causes it to perpetually
                    # resurface as a "Recover unfinished meeting" item on
                    # every engine startup until the 24h purge kicks in
                    # (which it often doesn't, because the file's mtime gets
                    # bumped by every subsequent persist).
                    try:
                        delete_persisted_accumulator(current_tracking_id)
                    except Exception:
                        pass
                    # New active meeting → new session fingerprint.
                    self._write_tracking(meeting_id=meeting_id, session_mtime=mtime)
                    with self._accumulated_lock:
                        self._accumulated = {}
                    self._emit_diag(
                        "meeting_id_changed",
                        from_id=current_tracking_id, to_id=meeting_id,
                        reason="active_reevaluation",
                    )
                    self._set_state(
                        EngineState.ACTIVE,
                        meeting_id=meeting_id,
                        accumulator_size=0,
                    )

            # Snapshot new entries into the accumulator on every change tick.
            # No meeting_id_filter here — we accumulate everything and filter
            # at generation time. This prevents a stale meeting ID (detected
            # at IDLE→ACTIVE when old data was still in the WAL) from causing
            # new utterances to be silently dropped.
            try:
                _, _, _, current_meeting_id = self._read_tracking()
                fresh = parse_transcript(wal)
                changed_in_acc = False
                with self._accumulated_lock:
                    for entry in fresh:
                        mid_key = entry["msg_id"]
                        if mid_key not in self._accumulated:
                            self._accumulated[mid_key] = entry
                            changed_in_acc = True
                        else:
                            existing = self._accumulated[mid_key]
                            if len(entry.get("text", "")) > len(existing.get("text", "")):
                                existing["text"] = entry["text"]
                                changed_in_acc = True
                            if entry.get("speaker") and entry["speaker"] != "Unknown" \
                               and existing.get("speaker") != entry["speaker"]:
                                existing["speaker"] = entry["speaker"]
                                changed_in_acc = True
                            if entry.get("timestamp") and existing.get("timestamp") != entry["timestamp"]:
                                existing["timestamp"] = entry["timestamp"]
                                changed_in_acc = True
                            if entry.get("meeting_id") and not existing.get("meeting_id"):
                                existing["meeting_id"] = entry["meeting_id"]
                                changed_in_acc = True
                # Persist on any change (new entry, longer text, speaker fix,
                # timestamp update). Speaker corrections and timestamp fills
                # matter for retry just as much as new utterances.
                if changed_in_acc and current_meeting_id:
                    self._persist_accumulator_now(current_meeting_id, reason="changed")

                # Empty-parse streak: WAL moved (mtime/size) but the parser
                # found nothing new. Reset on real changes; emit a diag at
                # threshold so a degraded session shows up in logs.
                if changed_in_acc:
                    self._empty_parse_streak = 0
                else:
                    self._empty_parse_streak += 1
                    if self._empty_parse_streak in (5, 20, 60):
                        self._emit_diag(
                            "empty_parse_streak",
                            count=self._empty_parse_streak,
                            meeting_id=current_meeting_id or "",
                        )
            except Exception as exc:
                # Previously a silent `pass`. Surface as a diag event so
                # parser regressions are visible in logs without users
                # having to recreate the issue.
                self._emit_diag(
                    "parse_error",
                    error=str(exc)[:200],
                    meeting_id=(self._read_tracking()[3] or ""),
                )
        else:
            if state == EngineState.ACTIVE and last_active_ts is not None:
                idle_secs = now - last_active_ts
                if idle_secs >= idle_threshold:
                    # Belt-and-suspenders: don't re-summarize a meeting we
                    # already finished. Zoom checkpoints the WAL after the
                    # meeting ends, which mutates mtime/size and would
                    # otherwise look like a new active period.
                    #
                    # The fingerprint is (meeting_id, session_start_mtime).
                    # Using just meeting_id wrongly suppresses recurring
                    # meetings that reuse the same Zoom ID across days —
                    # those re-enter IDLE→ACTIVE with a new mtime and so
                    # produce a different fingerprint. A checkpoint-only
                    # mutation keeps the same session_start_mtime (which
                    # was captured at IDLE→ACTIVE, not on every tick) and
                    # so still matches.
                    _, _, _, current_meeting_id = self._read_tracking()
                    current_session_mtime = self._read_session_mtime()
                    if current_meeting_id is not None and \
                       current_session_mtime is not None and \
                       self._last_generated_session is not None and \
                       (current_meeting_id, current_session_mtime) == self._last_generated_session:
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

        # Snapshot the meeting_id and session-start mtime we're processing
        # so the worker can stamp the success fingerprint even after
        # _reset_tracking has cleared the live tracking state.
        _, _, _, processing_meeting_id = self._read_tracking()
        processing_session_mtime = self._read_session_mtime()

        # Reset the per-run note_failed flag so the worker can detect whether
        # _generate_notes wrote a placeholder (LLM failure) or a final note.
        self._last_run_note_failed = False

        # Reset cancellation so this run starts clean. Any cancel signal
        # raised below targets THIS generation only.
        self._cancel_event.clear()

        def worker():
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self._generate_notes, origin, cfg)
            try:
                future.result(timeout=_GENERATE_TIMEOUT_SECS)
                # Remember which session we just processed so we don't
                # re-trigger if Zoom mutates the WAL on checkpoint. We do
                # this even on note_failed — the user retries via the menu
                # bar, not by waiting for another idle period.
                if processing_meeting_id and processing_session_mtime is not None:
                    self._last_generated_session = (
                        processing_meeting_id, processing_session_mtime
                    )
                # Clean up the in-memory accumulator. Keep the persisted
                # disk snapshot if note generation failed — it's the source
                # of truth for the retry flow.
                with self._accumulated_lock:
                    self._accumulated = {}
                if processing_meeting_id and not self._last_run_note_failed:
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
                    wal = self._resolve_wal(origin, cfg, "transcript")
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
                # future.cancel() is a no-op once the work has started, so
                # signal cooperative cancellation. The summarize() thread
                # will observe this between retries / during backoff sleep
                # and bail out with CancelledError. Worst-case latency is
                # bounded by the urllib per-call timeout (_HTTP_TIMEOUT_SECS).
                self._cancel_event.set()
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
        """Three-stage finalization: build transcript, save transcript, generate note.

        Save-transcript is the durability boundary — once it succeeds, the
        meeting's content is safe on disk regardless of what happens to the
        LLM call. LLM failures produce a placeholder note (saved alongside
        the transcript) and a retryable `note_failed` event, NOT a hard error.
        """
        gen_start = time.monotonic()
        transcript_wal = self._resolve_wal(origin, cfg, "transcript")
        blocks_wal = self._resolve_wal(origin, cfg, "blocks")

        _, _, _, active_meeting_id = self._read_tracking()

        entries = self._collect_entries_for_generation(transcript_wal, active_meeting_id)

        if not transcript_wal and not entries:
            raise RuntimeError("Transcript WAL not found during generation.")

        if not entries:
            raise RuntimeError("No transcript entries found.")

        meeting_title = self._derive_meeting_title(blocks_wal, transcript_wal, entries)
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

        cfg = self._get_cfg()

        # ── Stage 1: save transcript (durability boundary) ─────────────────
        transcript_content = build_transcript_content(transcript_text, meeting_title, date_str, cfg)
        transcript_path = save_transcript_only(transcript_content, meeting_title, date_str, cfg)
        slug = slugify_title(meeting_title, fallback_date=date_str)

        # ── Stage 2: generate note via LLM ─────────────────────────────────
        try:
            summary = summarize(
                transcript_text, meeting_title, cfg, cancel_event=self._cancel_event
            )
        except CancelledError:
            # A cancelled run leaves the transcript on disk (Stage 1 already
            # ran) and the persisted accumulator intact for retry. We do NOT
            # write a placeholder — a cancellation note in the user's vault
            # would be confusing noise; the menu bar already surfaces the
            # timeout via the `error` event.
            self._last_run_note_failed = True
            self._last_failed_meeting = {
                "meeting_id": active_meeting_id or "",
                "title": slugify_title(meeting_title, fallback_date=date_str),
                "note_path": "",
                "transcript_path": str(transcript_path),
                "message": "Note generation cancelled (timed out)",
                "date_str": date_str,
                "attendees": attendees,
            }
            if active_meeting_id:
                mark_meeting_failed(active_meeting_id, metadata=self._last_failed_meeting)
            return
        except Exception as exc:
            error_msg = _friendly_error(exc)
            placeholder = build_placeholder_note(
                meeting_title=meeting_title,
                date_str=date_str,
                attendees=attendees,
                created_iso=created_iso,
                error_message=error_msg,
                meeting_id=active_meeting_id or "",
                cfg=cfg,
            )
            placeholder_path = save_note_only(placeholder, meeting_title, date_str, cfg)
            self._last_run_note_failed = True
            failed_record = {
                "meeting_id": active_meeting_id or "",
                "title": slug,
                "note_path": str(placeholder_path),
                "transcript_path": str(transcript_path),
                "message": error_msg,
                "date_str": date_str,
                "attendees": attendees,
            }
            self._last_failed_meeting = failed_record
            # Promote the snapshot from root cache into `failed/` so the
            # 30-day purge applies (instead of 24h) and the menu bar can
            # surface a labeled recovery item even after a long delay.
            # This MUST run before the worker's post-_generate_notes
            # cleanup — that block keeps the root snapshot intact only
            # while `_last_run_note_failed` is True; promoting it here
            # means root is empty afterward and `failed/` owns the data.
            if active_meeting_id:
                mark_meeting_failed(active_meeting_id, metadata=failed_record)
            emit({
                "event": "note_failed",
                "title": slug,
                "note_path": str(placeholder_path),
                "transcript_path": str(transcript_path),
                "meeting_id": active_meeting_id or "",
                "attendees": attendees,
                "message": error_msg,
            })
            return

        # ── Stage 3: save the final note ───────────────────────────────────
        note_content = build_note_content(
            summary, meeting_title, date_str, attendees, created_iso, cfg
        )
        note_path = save_note_only(note_content, meeting_title, date_str, cfg)

        emit({
            "event": "done",
            "title": slug,
            "path": str(note_path),
            "transcript_path": str(transcript_path),
            "attendees": attendees,
            "meeting_id": active_meeting_id or "",
        })

        # Diag: how long did this take, how big was the transcript, which
        # provider/model. Useful for spotting regressions ("LLM is slow this
        # week") without instrumenting the user's machine further.
        self._emit_diag(
            "generation_completed",
            duration_ms=int((time.monotonic() - gen_start) * 1000),
            entry_count=len(entries),
            attendee_count=len(attendees),
            provider=cfg.llm_provider,
            model=cfg.llm_model,
            meeting_id=active_meeting_id or "",
        )

    def _collect_entries_for_generation(self, transcript_wal, active_meeting_id):
        """Pick the best entries source: in-memory accumulator > WAL > disk snapshot.

        Filter by active_meeting_id at this stage (not at poll time) so a stale
        ID at IDLE→ACTIVE transition never silently drops new utterances. Falls
        back to unfiltered if the filter would wipe everything (e.g. entries
        captured before the meetingId field appeared in the WAL).
        """
        with self._accumulated_lock:
            acc_snapshot = dict(self._accumulated)

        if acc_snapshot:
            if active_meeting_id:
                acc_entries = [
                    e for e in acc_snapshot.values()
                    if e.get("meeting_id") == active_meeting_id
                ]
                if not acc_entries:
                    acc_entries = list(acc_snapshot.values())
            else:
                acc_entries = list(acc_snapshot.values())
            return sorted(acc_entries, key=lambda e: e.get("timestamp") or "")

        if transcript_wal:
            return parse_transcript(transcript_wal, meeting_id_filter=active_meeting_id)

        if active_meeting_id:
            persisted = load_persisted_accumulator(active_meeting_id)
            if persisted:
                return sorted(
                    persisted.values(), key=lambda e: e.get("timestamp") or ""
                )

        return []

    def _derive_meeting_title(self, blocks_wal, transcript_wal, entries) -> str:
        meeting_title = None
        if blocks_wal:
            try:
                meeting_title = parse_meeting_title(blocks_wal, entries)
            except Exception:
                pass
        if not meeting_title:
            mtime = transcript_wal.stat().st_mtime if transcript_wal else time.time()
            meeting_title = f"Zoom Meeting {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"
        return meeting_title

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
        elif action == "retry":
            meeting_id = cmd.get("meeting_id") or ""
            self._trigger_retry(meeting_id)
        elif action == "recover":
            # Recovery is mechanically identical to retry — load the persisted
            # accumulator, re-derive title from blocks WAL, run the LLM,
            # write the note. The distinction is purely intent-level: retry
            # operates on a meeting whose failure was just observed in this
            # engine session; recover operates on one whose failure happened
            # in a prior session and was found at startup.
            meeting_id = cmd.get("meeting_id") or ""
            if not meeting_id:
                emit({"event": "error", "message": "recover command requires meeting_id."})
                return
            self._trigger_retry(meeting_id)

    # ── Retry flow ──────────────────────────────────────────────────────────

    def _trigger_retry(self, meeting_id: str) -> None:
        """Re-run note generation for a previously-failed meeting.

        Loads the persisted accumulator from disk, re-runs the LLM stage,
        and overwrites the placeholder note on success. The transcript on
        disk is left as-is. Emits `done` on success or `error` on failure.
        """
        if not self._generating_lock.acquire(blocking=False):
            emit({"event": "error", "message": "Generation already in progress."})
            return

        # Resolve which failed meeting to retry. If the caller didn't pass a
        # meeting_id, use the most recent failure (driven from the menu bar).
        failed = self._last_failed_meeting
        if not meeting_id and failed:
            meeting_id = failed.get("meeting_id") or ""
        if failed and failed.get("meeting_id") != meeting_id:
            failed = None  # caller is retrying a different meeting

        if not meeting_id:
            self._generating_lock.release()
            emit({"event": "error", "message": "No failed meeting to retry."})
            return

        cfg = self._get_cfg()
        # Reset cancellation for this retry — a prior cancelled run must
        # not cause the retry to abort instantly.
        self._cancel_event.clear()

        def worker():
            self._set_state(EngineState.GENERATING)
            try:
                persisted = load_persisted_accumulator(meeting_id)
                if not persisted:
                    emit({
                        "event": "error",
                        "message": "Cached transcript no longer available for retry.",
                    })
                    return
                entries = sorted(
                    persisted.values(), key=lambda e: e.get("timestamp") or ""
                )
                if not entries:
                    emit({"event": "error", "message": "No transcript entries to retry."})
                    return

                # Reuse failure metadata when available; otherwise re-derive.
                if failed:
                    meeting_title = failed.get("title") or ""
                    date_str = failed.get("date_str") or datetime.now().strftime("%Y-%m-%d")
                    attendees = list(failed.get("attendees") or [])
                    placeholder_path_str = failed.get("note_path") or ""
                else:
                    origin = find_origin_dir()
                    blocks_wal = self._resolve_wal(origin, cfg, "blocks") if origin else None
                    transcript_wal = self._resolve_wal(origin, cfg, "transcript") if origin else None
                    meeting_title = self._derive_meeting_title(blocks_wal, transcript_wal, entries)
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", meeting_title)
                    date_str = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
                    seen: dict[str, None] = {}
                    for e in entries:
                        s = e.get("speaker")
                        if s and s != "Unknown":
                            seen[s] = None
                    attendees = list(seen.keys())
                    placeholder_path_str = ""

                created_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                transcript_text = format_transcript(entries)
                slug = slugify_title(meeting_title, fallback_date=date_str)

                try:
                    summary = summarize(
                        transcript_text, meeting_title, cfg,
                        cancel_event=self._cancel_event,
                    )
                except CancelledError:
                    # Retry was cancelled (e.g. user quit the app). Leave
                    # the failed/ snapshot intact so the next launch still
                    # surfaces the recovery item.
                    emit({"event": "error", "message": "Retry cancelled"})
                    return
                except Exception as exc:
                    err_msg = _friendly_error(exc)
                    # Refresh the failed/ sidecar so the menu bar shows the
                    # latest error and a fresh `failed_at` (the file's mtime
                    # also gets bumped, which keeps it inside the 30-day
                    # purge window for another month). The .json/.md moves
                    # are no-ops when already in failed/, so this is safe
                    # to call repeatedly.
                    mark_meeting_failed(meeting_id, metadata={
                        "meeting_id": meeting_id,
                        "title": slug,
                        "note_path": placeholder_path_str,
                        "transcript_path": (failed or {}).get("transcript_path", ""),
                        "message": err_msg,
                        "date_str": date_str,
                        "attendees": attendees,
                    })
                    emit({
                        "event": "note_failed",
                        "title": slug,
                        "note_path": placeholder_path_str,
                        "transcript_path": (failed or {}).get("transcript_path", ""),
                        "meeting_id": meeting_id,
                        "attendees": attendees,
                        "message": err_msg,
                    })
                    return

                note_content = build_note_content(
                    summary, meeting_title, date_str, attendees, created_iso, cfg
                )

                # Overwrite the placeholder note in place if we know its path,
                # otherwise write a fresh note (which save_note_only will
                # disambiguate via _next_available_path).
                from pathlib import Path as _Path
                if placeholder_path_str and _Path(placeholder_path_str).exists():
                    overwrite_note(_Path(placeholder_path_str), note_content)
                    note_path = _Path(placeholder_path_str)
                else:
                    note_path = save_note_only(note_content, meeting_title, date_str, cfg)

                # Cleanup: drop the persisted accumulator and forget the
                # last-failed meeting so the menu bar Retry item disappears.
                delete_persisted_accumulator(meeting_id)
                self._last_failed_meeting = None
                self._last_run_note_failed = False

                emit({
                    "event": "done",
                    "title": slug,
                    "path": str(note_path),
                    "transcript_path": (failed or {}).get("transcript_path", ""),
                    "attendees": attendees,
                    "meeting_id": meeting_id,
                })
            except Exception as exc:
                emit({"event": "error", "message": _friendly_error(exc)})
            finally:
                self._generating_lock.release()
                self._set_state(EngineState.IDLE)

        threading.Thread(target=worker, daemon=True).start()

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
