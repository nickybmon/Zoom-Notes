# Zoom Notes

A macOS menu bar app that watches Zoom's AI Notetaker in real time, detects when a meeting ends, and automatically generates structured meeting notes via Claude, OpenAI, Gemini, or Ollama — saved directly to your configured output directory (Obsidian vault, Desktop folder, or anywhere you like).

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
  → Menu bar icon switches to "In Meeting"

Meeting ends
  → WAL stops changing
  → After idle threshold: icon switches to "Generating"
  → Transcript is parsed, deduplicated, sent to your configured LLM
  → Structured notes saved to configured output directory
  → macOS notification fires
  → Icon returns to "Idle"
```

---

## Architecture

A native Swift/SwiftUI menu bar app spawns a headless Python engine as a child process. The Python engine watches the WAL and emits newline-delimited JSON events; the Swift app renders state and handles user-facing concerns.

```
ZoomNotesApp (Swift)         zoom_engine.py (Python)
┌──────────────────┐         ┌─────────────────────┐
│ Menu bar         │ stdout  │ WAL poll loop       │
│ Settings UI      │◄────────│ Idle detection      │
│ Notifications    │ stdin   │ LLM summarization   │
│ Keychain         │────────►│ Note writing        │
└──────────────────┘         └─────────────────────┘
```

API keys live exclusively in the macOS Keychain (service `zoom-notes-assistant`). The Swift app reads them and injects them into the Python engine's environment at launch — no key files on disk.

---

## Output

Two files are written per meeting to the configured output directory:

```
Meeting Notes/             (default: ~/Desktop/Meeting Notes/)
  Notes/
    YYYY-MM-DD/
      Meeting Title.md          ← structured notes
  Transcripts/
    YYYY-MM-DD/
      Meeting Title — transcript.md  ← raw deduplicated transcript
```

**Notes file frontmatter:**
```yaml
title: "Team Sync"
type: meeting
source: zoom-notes
date: 2026-04-21
created: 2026-04-21T19:01:36
attendees:
  - "Alice Smith"
  - "Bob Jones"
transcript: "[[Meetings/Transcripts/2026-04-21/Team Sync — transcript.md]]"
daily_note: "[[Daily/2026-04-21]]"
```

**Note body sections:**
- **Overview** — 2-4 sentence purpose and outcome
- **Attendees** — speaker names from transcript
- **Topics Discussed** — sequenced, specific bullets per topic
- **Key Decisions** — explicit decisions made (or "No explicit decisions recorded")
- **Action Items** — Owner / Task / Due Date table
- **Open Questions** — deferred topics (omitted if none)
- **Notes** — additional context (omitted if none)

### Obsidian integration

If you use Obsidian, point the Notes and Transcripts folders at your vault's meetings folder via Settings → Output. The frontmatter format (`source: zoom-notes`, wikilink-style `transcript` and `daily_note` fields) is compatible with Obsidian companion plugins for attendee resolution and daily note breadcrumbs.

A common setup: clone this repo into `YourVault/Scripts/zoom-notes/` and configure the output dirs to `YourVault/Meetings/Notes` and `YourVault/Meetings/Transcripts`.

---

## Privacy

This app sends full meeting transcripts (including any sensitive personnel, legal, or commercial content) to whatever LLM provider you configure. By default that's Anthropic.

If you need local-only processing, configure **Ollama** as the provider in Settings → API / LLM. Ollama runs entirely on your machine and never sends transcripts to a third party.

---

## Requirements

- macOS 13 (Ventura) or later
- Zoom desktop app with **My Notes** (AI Companion / AI Notetaker) enabled
- API key for your chosen LLM provider (or Ollama installed locally)

No Python install required — Python 3.12 is bundled inside the app.

---

## Installation

1. **[Download the latest release](https://github.com/nickybmon/Zoom-Notes/releases/latest)** (`Zoom Notes-<version>.dmg`)
2. Open the DMG and drag **Zoom Notes** into your Applications folder
3. Launch the app — a menu bar icon appears
4. On first launch, an onboarding window walks you through setup:
   - Click **Open Settings** to configure your API key
   - Pick your LLM provider in the **API / LLM** tab
   - Paste your API key — stored in macOS Keychain, never written to disk
   - Adjust output paths in the **Output** tab if needed (default: `~/Desktop/Meeting Notes/`)
5. Start a Zoom meeting with My Notes enabled — the icon changes to "In Meeting" automatically

### Building from source

```bash
git clone https://github.com/nickybmon/Zoom-Notes.git
cd Zoom-Notes

# Fetch the bundled Python runtime (arm64 + x86_64 universal)
./scripts/fetch-python-runtime.sh

# Open in Xcode and run, or build a release DMG:
./scripts/release.sh
```

`release.sh` requires Xcode command-line tools, a Developer ID Application certificate, and a stored `notarytool` keychain profile named `zoom-notes-notarytool`.

### Getting an API key

| Provider | Where |
|---|---|
| Claude (Anthropic) | [console.anthropic.com](https://console.anthropic.com/) |
| OpenAI | [platform.openai.com](https://platform.openai.com/) |
| Gemini (Google) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Ollama (local) | [ollama.com](https://ollama.com/) — no key required |

A spend limit is recommended for paid providers.

---

## Configuration

All settings are managed through the in-app Settings window. They persist to:

- **`~/Library/Application Support/zoom-notes/settings.json`** — non-sensitive preferences
- **macOS Keychain** (service `zoom-notes-assistant`) — API keys

Tabs:
- **API / LLM** — provider, model, API key, connection test
- **Output** — notes/transcripts directories, subfolder pattern, filename pattern, custom frontmatter
- **Prompt** — system prompt customization
- **Advanced** — poll interval, idle threshold, WAL DB prefixes, base URL overrides

### Environment variable overrides (optional)

If you launch the engine outside the Swift app (e.g. for CLI debugging), the engine still reads these env vars as fallbacks if Keychain is empty:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude key |
| `OPENAI_API_KEY` | OpenAI key |
| `GEMINI_API_KEY` | Gemini key |
| `ZOOM_NOTES_OUTPUT_DIR` | Override notes output directory |
| `ZOOM_NOTES_TRANSCRIPTS_DIR` | Override transcripts output directory |
| `ZOOM_NOTES_USER_NAME` | Your Zoom display name (exactly as it appears in meetings) — boosts the correct meeting when you're double-booked or in back-to-back meetings |

---

## CLI tools (debugging)

`zoom_notes.py` is also runnable directly for inspecting WAL state:

```bash
./venv/bin/python3 zoom_notes.py --list                # Detect meetings in current WAL
./venv/bin/python3 zoom_notes.py --dump                # Print full transcript to stdout
./venv/bin/python3 zoom_notes.py --watch               # Live-follow transcript during a meeting
./venv/bin/python3 zoom_notes.py --notes               # Generate and save notes now
./venv/bin/python3 zoom_notes.py --notes --dry-run     # Preview without saving
```

These commands read the same `settings.json` and Keychain entries the menu bar app uses.

---

## Caveats

- **Zoom updates may break this.** The WAL path and DB structure are internal implementation details — no stability guarantee. If detection stops working, update the WAL prefixes in Settings → Advanced.
- After a meeting ends, Zoom checkpoints the WAL (deleting or shrinking it). The engine handles this by persisting accumulated transcript entries to `~/.cache/zoom-notes/` on every tick — notes are not lost even if the WAL disappears before the idle threshold fires.
- Zoom stores notes under a per-profile WebView *bucket* (`UnSigned` when signed out, a per-account hash like `oit2v1HSQSi5kic4VLE7kQ` once you sign in) plus per-account `<hash>` folders inside it. Both change on sign-in/sign-out and app updates. The engine scans **every** bucket and picks the one Zoom is actively writing, so this is handled automatically — but if detection ever stops, run `python3 zoom_notes.py --list` to verify the resolved path.
- Transcription accuracy depends on Zoom's server-side ASR, not local processing.

---

## File structure

```
ZoomNotesApp/                          # Swift/SwiftUI menu bar app (Xcode project)
zoom_notes.py                          # Core: WAL discovery, transcript parsing, LLM calls, note writing
zoom_engine.py                         # Headless WAL poller spawned by Swift; emits JSON events
zoom_config.py                         # Settings + Keychain helpers (Python side)
scripts/
  release.sh                           # Full release pipeline: archive → sign → notarize → DMG
  fetch-python-runtime.sh              # Downloads universal Python 3.12 into python-runtime/
  dmg-assets/                          # DMG background image and appdmg config
zoom-transcript-extraction.md          # Research: how the WAL was discovered and mapped
CLAUDE.md                              # Project overview for AI coding assistants
```
