# Zoom Meeting Notes Assistant

A macOS menu bar app that watches Zoom's AI Notetaker in real time, detects when a meeting ends, and automatically generates structured meeting notes via Claude — saved directly to your Obsidian vault.

No screen scraping. No network interception. Reads a local file Zoom already writes to your disk.

---

## How it works

Zoom's AI Notetaker ("My Notes") writes a live transcript to a WebKit IndexedDB WAL file on your Mac as each word is spoken. This app watches that file:

```
~/Library/Application Support/zoom.us/data/
  UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins/
    <hash>/<hash>/IndexedDB/
      1CB477F679D6.../IndexedDB.sqlite3-wal  ← transcript (live during meeting)
      DDEC8414E29A.../IndexedDB.sqlite3-wal  ← blocks/title store
```

The WAL stores UTF-8 strings readable with `strings(1)` — no special permissions required beyond normal file read access.

**Flow:**

```
Zoom meeting starts
  → WAL file appears and grows
  → Menu bar icon switches to ⏺ (In Meeting)

Meeting ends
  → WAL stops changing
  → After 30 seconds idle: icon switches to ⟳ (Generating)
  → Transcript is parsed, deduplicated, sent to Claude
  → Structured notes saved to Vault Mind/Meetings/
  → macOS notification fires
  → Icon returns to ● (Idle)
```

---

## Menu bar states

| Icon | State | Meaning |
|------|-------|---------|
| `●` | Idle | No active meeting detected |
| `⏺` | In Meeting | WAL is actively changing |
| `⟳` | Generating | Claude summarization in flight |

The menu also has a **Generate Notes Now** item for manual trigger, and a **Quit** item.

---

## Output

Notes are saved to `~/Documents/Vault Mind/Meetings/YYYY-MM-DD Meeting Title.md` with:

- YAML frontmatter (date, meeting name, tags)
- **Summary** — 2-3 sentence overview
- **Attendees** — with role context where detectable
- **Key Discussion Points** — bulleted topics
- **Decisions Made** — explicit conclusions (omitted if none)
- **Action Items** — checkboxes with owner names
- **Notable Quotes** — 1-2 high-signal direct quotes (optional)
- **Full Transcript** — deduplicated, speaker-attributed, timestamped

---

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+
- Zoom desktop app with **My Notes** (AI Notetaker) enabled
- Anthropic API key

---

## Setup

```bash
# Clone
git clone https://github.com/nickybmon/Zoom-Meeting-Assistant.git
cd Zoom-Meeting-Assistant

# Create virtualenv and install the one dependency
python3 -m venv venv
./venv/bin/pip install rumps

# Set your Anthropic API key (add to ~/.zshrc to persist)
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Running

### Menu bar app

```bash
./venv/bin/python3 zoom_menu_bar.py
```

The `●` icon appears in your menu bar. Works automatically from there.

### Auto-launch at login

```bash
# Write the launchd plist and print activation instructions
./venv/bin/python3 zoom_menu_bar.py --install-login-item

# Activate immediately (no reboot needed)
launchctl load ~/Library/LaunchAgents/com.nickblackmon.zoom-notes.plist
```

Logs go to `~/Library/Logs/zoom-notes.log` and `zoom-notes-error.log`.

To remove:
```bash
launchctl unload ~/Library/LaunchAgents/com.nickblackmon.zoom-notes.plist
rm ~/Library/LaunchAgents/com.nickblackmon.zoom-notes.plist
```

### CLI tools (zoom_notes.py)

```bash
./venv/bin/python3 zoom_notes.py --list      # Detect meetings in current WAL
./venv/bin/python3 zoom_notes.py --dump      # Print full transcript to stdout
./venv/bin/python3 zoom_notes.py --watch     # Live-follow transcript during a meeting
./venv/bin/python3 zoom_notes.py --notes     # Generate and save notes now
./venv/bin/python3 zoom_notes.py --notes --dry-run  # Preview without saving
```

---

## Configuration

All tunable constants are at the top of each file:

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `zoom_menu_bar.py` | `POLL_INTERVAL_SECS` | `5` | How often to check the WAL |
| `zoom_menu_bar.py` | `IDLE_THRESHOLD_SECS` | `30` | Seconds of WAL inactivity before triggering |
| `zoom_notes.py` | `VAULT_MEETINGS` | `~/Documents/Vault Mind/Meetings` | Where notes are saved |
| `zoom_notes.py` | `TRANSCRIPT_DB_PREFIX` | `1CB477F679D6` | IndexedDB folder prefix for transcript store |
| `zoom_notes.py` | `BLOCKS_DB_PREFIX` | `DDEC8414E29A` | IndexedDB folder prefix for title/blocks store |

---

## Caveats

- **Zoom updates may break this.** The WAL path and DB structure are internal implementation details — no stability guarantee.
- The WAL is only present during an active meeting with My Notes enabled. After the meeting, Zoom checkpoints the WAL into the main DB and may delete it. The 30-second idle trigger is designed to capture it before that window closes.
- The `<hash>` folder name appears stable per Zoom account but could change on re-login or app update. If detection stops working, run `--list` to verify the path.
- Transcription accuracy depends on Zoom's server-side ASR, not local processing.

---

## File structure

```
zoom_notes.py           # Core: WAL discovery, transcript parsing, Claude API, note writing
zoom_menu_bar.py        # Menu bar app: state machine, WAL poller, rumps UI
zoom-transcript-extraction.md  # Original research: how the WAL was discovered and mapped
venv/                   # Python virtualenv (gitignored)
```
