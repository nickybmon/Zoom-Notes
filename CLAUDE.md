# CLAUDE.md — Zoom Meeting Notes Assistant

## What this project is

A macOS menu bar app that reads Zoom's AI Notetaker WAL file in real time, detects meeting end via idle detection, and auto-generates structured meeting notes via Claude API. Notes are saved to `~/Documents/Vault Mind/Meetings/`.

Two files do everything:
- `zoom_notes.py` — all core logic: WAL discovery, transcript parsing, Claude API call, note writing
- `zoom_menu_bar.py` — menu bar UI only; imports all logic from zoom_notes.py, adds state machine + rumps timer

---

## Architecture

```
WAL file (Zoom writes this)
  → find_origin_dir() discovers the hash path
  → find_wal() locates the transcript and blocks WAL
  → parse_transcript() extracts entries via strings(1), deduplicates by messageId
  → parse_meeting_title() reads meeting name from blocks WAL
  → format_transcript() formats speaker-attributed output
  → summarize_with_claude() calls Claude API (claude-opus-4-5, max_tokens 4096)
  → build_note_content() assembles frontmatter (source: local-app) + summary
  → build_transcript_content() assembles transcript file with backlink frontmatter
  → save_note() writes note → VAULT_NOTES/YYYY-MM-DD/Title.md
                          and transcript → VAULT_TRANSCRIPTS/YYYY-MM-DD/Title — transcript.md
```

The `source: local-app` frontmatter field causes the Obsidian companion plugin to pick up the note and run its post-processing pipeline (attendee wikilink resolution, People file updates, daily note breadcrumb).

The menu bar app (`zoom_menu_bar.py`) wraps this in a `rumps.App` subclass with a 5-second `rumps.Timer`. State machine: IDLE → ACTIVE → GENERATING → IDLE.

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
The menu bar poller compares `st_mtime` and `st_size` on each 5-second tick. If both are unchanged for `IDLE_THRESHOLD_SECS` (30s) after a period of activity, it triggers note generation. This fires before Zoom checkpoints the WAL into the main DB.

### Thread safety
Note generation runs in a `threading.Thread`. A `threading.Lock` (`_generating_lock`) prevents double-triggering. The rumps run loop stays unblocked. After generation completes (success or error), the lock is released and state resets to IDLE.

---

## Constants to know

| File | Name | Default | Purpose |
|------|------|---------|---------|
| `zoom_notes.py` | `VAULT_NOTES` | `~/Documents/Vault Mind/Meetings/Notes` | Notes output directory |
| `zoom_notes.py` | `VAULT_TRANSCRIPTS` | `~/Documents/Vault Mind/Meetings/Transcripts` | Transcripts output directory |
| `zoom_notes.py` | `TRANSCRIPT_DB_PREFIX` | `1CB477F679D6` | IndexedDB folder prefix |
| `zoom_notes.py` | `BLOCKS_DB_PREFIX` | `DDEC8414E29A` | Title/blocks DB prefix |
| `zoom_menu_bar.py` | `POLL_INTERVAL_SECS` | `5` | WAL poll frequency |
| `zoom_menu_bar.py` | `IDLE_THRESHOLD_SECS` | `30` | Seconds idle before triggering |

---

## Running

```bash
# One-time setup
python3 -m venv venv
./venv/bin/pip install rumps

# Run the menu bar app
./venv/bin/python3 zoom_menu_bar.py

# Auto-launch at login
./venv/bin/python3 zoom_menu_bar.py --install-login-item
launchctl load ~/Library/LaunchAgents/com.nickblackmon.zoom-notes.plist
```

`ANTHROPIC_API_KEY` must be set in the environment. The `--install-login-item` command bakes the current value of the key into the launchd plist.

---

## What to do if the WAL path breaks

Zoom updates may change the DB prefix hashes. If detection stops working:

1. Run `python3 zoom_notes.py --list` — if it outputs nothing, the path changed
2. Check the current WAL with:
   ```bash
   ls ~/Library/Application\ Support/zoom.us/data/UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins/<hash>/<hash>/IndexedDB/
   ```
3. During an active meeting, the transcript WAL will be the largest recently-modified `.sqlite3-wal` file
4. Update `TRANSCRIPT_DB_PREFIX` and `BLOCKS_DB_PREFIX` in `zoom_notes.py` to match the new folder prefix

---

## Extending this

**Change the output format:** Edit `SYSTEM_PROMPT` in `zoom_notes.py` — it's the full Claude instruction for note structure.

**Change the output location:** Change `VAULT_NOTES` and `VAULT_TRANSCRIPTS` in `zoom_notes.py`.

**Change the Claude model:** Edit `"model"` in `summarize_with_claude()`.

**Add a second trigger condition:** Modify `_poll()` in `zoom_menu_bar.py` — all state logic lives there.

**Add a notification action (e.g., open in Obsidian):** Modify the `rumps.notification()` call in `_generate_notes_worker()`. rumps supports `action_button` parameter for clickable notifications.

---

## Dependencies

- `rumps` — macOS menu bar app framework (PyObjC wrapper). Install: `pip install rumps`
- stdlib only otherwise: `subprocess`, `threading`, `urllib.request`, `pathlib`, `shutil`, `tempfile`
- macOS system `strings` binary (pre-installed) for WAL text extraction

No LangChain, no heavy dependencies, no API wrappers — direct `urllib.request` to Anthropic's REST API.
