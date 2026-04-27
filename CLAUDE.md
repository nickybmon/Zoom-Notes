# CLAUDE.md — Zoom Meeting Notes Assistant

## What this project is

A macOS menu bar app that reads Zoom's AI Notetaker WAL file in real time, detects meeting end via idle detection, and auto-generates structured meeting notes via Claude, OpenAI, Gemini, or Ollama. Notes are saved to the configured output directory (default: `~/Desktop/Meeting Notes/`).

**Architecture:** Swift/SwiftUI menu bar app (`ZoomNotesApp/`) + headless Python engine (`zoom_engine.py`).

---

## Project structure

```
zoom_notes.py       — core logic: WAL discovery, transcript parsing, LLM calls, note writing
zoom_config.py      — settings.json + Keychain config (used by Python engine and Swift Settings UI)
zoom_engine.py      — headless WAL poller; emits JSON events to stdout; no rumps
ZoomNotesApp/       — Swift/SwiftUI menu bar application (Xcode project)
  ZoomNotesApp.xcodeproj
  ZoomNotesApp/
    ZoomNotesApp.swift          — @main entry point
    AppDelegate.swift           — NSStatusItem, menu, windows, state machine
    Models/
      EngineEvent.swift         — Codable structs for stdout JSON events
      ZoomNotesConfig.swift     — Swift mirror of ZoomNotesConfig + Keychain helpers
    Services/
      EngineManager.swift       — Process lifecycle, stdout pipe, restart on crash
    ViewModels/
      AppState.swift            — @Published engine state
      SettingsViewModel.swift   — config load/save, Keychain, Ollama model fetch
    Views/
      SettingsView.swift        — sidebar + 4 tabs: API/LLM, Output, Prompt, Advanced
    Utilities/
      ConsoleLogger.swift       — log to ~/Library/Logs/zoom-notes/
      Permissions.swift         — notification permission helpers
    Resources/
      Assets.xcassets
      Info.plist
    ZoomNotesApp.entitlements
```

---

## Architecture

```
Swift ZoomNotesApp (menu bar)
  └─ AppDelegate → AppState → EngineManager
                                  │ spawn
                                  ▼
                            zoom_engine.py  (headless Python)
                              │  WAL poll loop (5s)
                              │  idle detection (30s)
                              │  → parse_transcript()
                              │  → summarize()
                              │  → save_note()
                              │
                              │  stdout: newline-delimited JSON events
                              │  {"event":"state","value":"idle|active|generating"}
                              │  {"event":"done","title":"...","path":"...","attendees":[...]}
                              │  {"event":"error","message":"..."}
                              │
                              │  stdin: JSON commands (optional)
                              │  {"cmd":"generate"}  — manual trigger
                              │  {"cmd":"reload"}    — reload settings
                              ▼
                  ~/Library/Application Support/zoom-notes/settings.json
                  macOS Keychain (service: zoom-notes-assistant)
                  ~/Desktop/Meeting Notes/  (or configured output dir)
```

---

## Key implementation details

### WAL path
Zoom's transcript WAL lives at:
```
~/Library/Application Support/zoom.us/data/
  UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins/
    <hash>/<hash>/IndexedDB/
      1CB477F679D6.../IndexedDB.sqlite3-wal   ← transcript store
      DDEC8414E29A.../IndexedDB.sqlite3-wal   ← blocks/title store
```
The `<hash>` is stable per Zoom account. `find_origin_dir()` discovers it dynamically — never hardcode it.

### WAL parsing
The WAL stores V8-serialized blobs but WAL pages contain raw UTF-8. `strings(1)` extracts them. Each transcript entry follows this pattern in the strings output:
```
messageId
16:0:16787456:0:217494649   ← unique ID
message
<spoken text>
timeStampContent
HH:MM:SS
...
speaker
speakerId
username
<speaker name>
```

Zoom streams word-by-word, so the same `messageId` appears many times with progressively longer text. The parser deduplicates by `messageId` (keeping the longest text), then sorts by timestamp, then merges consecutive same-speaker sub-string entries.

### Idle detection
`zoom_engine.py` compares `st_mtime` and `st_size` on each 5-second tick. If both are unchanged for `idle_threshold_secs` (default 30s) after a period of activity, it triggers note generation.

### Thread safety
Note generation runs in a background `threading.Thread`. A `threading.Lock` prevents double-triggering. After generation completes (success or error), the lock is released and state resets to idle.

### Incremental transcript persistence
The polling loop accumulates entries in memory (`_accumulated`, keyed by `msg_id`) and atomically persists a snapshot to `~/.cache/zoom-notes/in-progress-{meeting_id}.json` (plus a human-readable `.md` mirror) on every change tick. This guarantees the transcript survives a Zoom WAL checkpoint, an engine crash, or a settings-driven engine restart — the next `IDLE → ACTIVE` transition seeds the accumulator from the persisted snapshot if one exists for the active meeting.

### Note generation pipeline (3 stages)
1. **Build transcript** from the accumulator (filtered by active `meeting_id` with safe fallback).
2. **Save transcript** to the user's `Transcripts/` folder. This is the durability boundary — once it lands, the meeting's content is safe regardless of LLM outcome.
3. **Generate note** via LLM. On success: write the final note. On failure: write a placeholder note (with retry metadata in YAML frontmatter) and emit `note_failed`. The cached accumulator is preserved so the menu bar's "Retry note generation" item can re-run only the LLM stage.

### Settings reload
When the Swift settings window closes, `EngineManager.reloadSettings()` sends `SIGHUP` to the Python process. `zoom_engine.py` catches `SIGHUP` and sets a flag to call `invalidate_config_cache()` on the next poll tick.

---

## Configuration

Settings are stored in two places (shared between Swift and Python):
- **`~/Library/Application Support/zoom-notes/settings.json`** — non-sensitive prefs
- **macOS Keychain** (service `zoom-notes-assistant`) — API keys per provider

| Setting | Default | Purpose |
|---------|---------|---------|
| `llm_provider` | `claude` | `claude` \| `openai` \| `gemini` \| `ollama` |
| `llm_model` | `claude-sonnet-4-6` | Model name |
| `notes_dir` | `~/Desktop/Meeting Notes/Notes` | Notes output directory |
| `transcripts_dir` | `~/Desktop/Meeting Notes/Transcripts` | Transcripts output directory |
| `subfolder_pattern` | `day` | `none` \| `day` \| `month` |
| `filename_pattern` | `{title}` | Note filename template |
| `transcript_filename_pattern` | `{title} — transcript` | Transcript filename template |
| `system_prompt` | `null` (use default) | Custom LLM instruction |
| `poll_interval_secs` | `5` | WAL poll frequency |
| `idle_threshold_secs` | `30` | Seconds idle before triggering |
| `transcript_db_prefix` | `1CB477F679D6` | IndexedDB folder prefix |
| `blocks_db_prefix` | `DDEC8414E29A` | Title/blocks DB prefix |

---

## Building and running

```bash
# ── Python setup (one-time) ───────────────────────────────────────
python3 -m venv venv
./venv/bin/pip install -r requirements.txt   # (only stdlib needed for engine)

# ── Swift app (Xcode) ─────────────────────────────────────────────
open ZoomNotesApp/ZoomNotesApp.xcodeproj
# Build & run in Xcode, or:
xcodebuild -project ZoomNotesApp/ZoomNotesApp.xcodeproj \
           -scheme ZoomNotesApp -configuration Release build

# ── CLI tools (still work independently) ──────────────────────────
./venv/bin/python3 zoom_notes.py --list    # list meetings found in WAL
./venv/bin/python3 zoom_notes.py --dump    # print current transcript
./venv/bin/python3 zoom_notes.py --notes   # generate notes immediately
```

The Swift app automatically finds `zoom_engine.py` by walking up from the app bundle path. API keys are set in the Settings window (stored in Keychain) or via `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` env vars.

---

## What to do if the WAL path breaks

Zoom updates may change the DB prefix hashes. If detection stops working:

1. Run `python3 zoom_notes.py --list` — if it outputs nothing, the path changed
2. Check the current WAL with:
   ```bash
   ls ~/Library/Application\ Support/zoom.us/data/UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins/<hash>/<hash>/IndexedDB/
   ```
3. During an active meeting, the transcript WAL will be the largest recently-modified `.sqlite3-wal` file
4. Update `transcript_db_prefix` / `blocks_db_prefix` in Settings → Advanced (or in `settings.json`)

---

## Extending this

**Change the output format:** Open Settings → Prompt, edit the system prompt.

**Change the output location:** Open Settings → Output.

**Change the LLM:** Open Settings → API / LLM.

**Add a trigger condition:** Modify `_poll_once()` in `zoom_engine.py`.

**Add a notification action:** Modify `sendNoteMadeNotification()` in `AppState.swift`.

---

## Dependencies

- **Swift:** `AppKit`, `SwiftUI`, `UserNotifications`, `Security` — all system frameworks, no SPM dependencies
- **Python:** stdlib only — `subprocess`, `threading`, `urllib.request`, `pathlib`, `shutil`, `tempfile`, `signal`
- macOS system `strings` binary (pre-installed) for WAL text extraction
- **Test-only:** `pytest` (in venv) — production code stays stdlib-only

No rumps, no LangChain, no heavy dependencies.

---

## Testing

The Python engine and parser are covered by a pytest suite at `tests/`. Run before any release:

```bash
make test        # verbose
make test-quick  # quiet
```

### Test layout

- `tests/test_parser.py` — `parse_transcript`, deduplication, meeting-ID detection, slugify edge cases.
- `tests/test_engine_state_machine.py` — drives `_poll_once` through fixture WALs with synthetic mtime/size, asserts state transitions and accumulator contents. Includes `test_no_silent_drop_when_meeting_id_is_stale` — the regression guard for the 2026-04-27 bug where a stale meeting ID caused silent drop of new utterances.
- `tests/test_replay.py` — full IDLE → ACTIVE → GENERATING cycle against a fixture WAL with the LLM stubbed. Confirms the transcript is saved to disk before the LLM runs (durability boundary) and that LLM failures produce a placeholder note plus retryable failure event.

### WAL fixtures

Real Zoom WAL captures live under `tests/fixtures/`. They're committed so anyone can run the suite without Zoom installed. To capture a fresh fixture during a live meeting:

```bash
make capture-fixture NAME=single_meeting     # mid-meeting
make capture-fixture NAME=multi_meeting_wal  # right after a new meeting starts (stale + new in same WAL)
```

The `multi_meeting_wal` fixture is the most important — recapturing it requires being in a Zoom meeting that just started while a previous meeting's data is still in the WAL.
