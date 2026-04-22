# Zoom AI Notetaker — Local Transcript Extraction

## What We Found

Zoom's AI Notetaker ("My Notes") writes the live transcript to the local filesystem
in real time, inside a WebKit IndexedDB used by the Zoom Docs web view.

No network interception, no screen scraping, no special permissions beyond
normal file read access.

---

## Where the Data Lives

```
~/Library/Application Support/zoom.us/data/
  UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/
    Origins/<hash>/<hash>/
      IndexedDB/
        1CB477F679D6.../ ← transcript store (notes:transcript:v1)
          IndexedDB.sqlite3
          IndexedDB.sqlite3-wal  ← actively written during meetings
        DDEC8414E2.../           ← blocks/speakers store
          IndexedDB.sqlite3-wal
      LocalStorage/
        localstorage.sqlite3     ← session/user metadata
```

The `<hash>` folder corresponds to `https://docs.zoom.us` — this is the
WebKit origin for the Zoom Docs web app embedded in the client.

The `-wal` (write-ahead log) file is updated as each transcript chunk arrives,
making it the real-time source.

---

## Data Structure

Each transcript entry in the WAL contains:

| Field            | Example                        |
|------------------|--------------------------------|
| `message`        | `"Does that work for you?"`    |
| `speaker`        | (object)                       |
| `username`       | `"Grace Hayes"`                |
| `userId`         | `"oit2v1HSQSi5kic4VLE7kQ"`     |
| `startTimeMsec`  | epoch ms                       |
| `endTimeMsec`    | epoch ms                       |
| `messageId`      | UUID                           |
| `uniqueUserId`   | internal ID                    |
| `textLanguage`   | `"en"`                         |
| `meetingId`      | base64 meeting ID              |

Zoom streams word-by-word: the same utterance appears many times in the WAL
as each word is appended. The final (longest) version of each utterance is
the complete one.

---

## How to Read It

The values in the IndexedDB `Records` table are V8-serialized blobs (not
readable directly). However the WAL file stores raw page data where strings
are in plain UTF-8 and extractable with `strings(1)`.

```bash
# Dump all readable strings from the active transcript WAL
strings ~/Library/Application\ Support/zoom.us/data/\
UnifyWebView_Cache/WebKit/UnSigned/Default/MyNotes/Origins/\
<hash>/<hash>/IndexedDB/1CB477F679D6.../IndexedDB.sqlite3-wal
```

The active WAL is always the most recently modified one ≥ a few KB.

---

## Parsing Algorithm

1. Find lines where `message` is followed by a non-empty string — that string is the spoken text
2. Look ahead ~25 lines for `username` followed by the speaker's name
3. Deduplicate: when entry B from the same speaker *starts with* entry A's text, keep only B (the longer/complete version)

In Python (~30 lines):

```python
import subprocess, shutil, tempfile
from pathlib import Path

def parse_wal(wal_path):
    tmp = Path(tempfile.mktemp(suffix=".wal"))
    shutil.copy2(wal_path, tmp)
    result = subprocess.run(["strings", str(tmp)], capture_output=True,
                            text=True, errors="replace")
    tmp.unlink(missing_ok=True)
    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]

    raw = []
    i = 0
    while i < len(lines):
        if lines[i] == "message" and i + 1 < len(lines):
            text = lines[i + 1]
            speaker = None
            for j in range(i + 1, min(i + 30, len(lines))):
                if lines[j] == "username" and j + 1 < len(lines):
                    speaker = lines[j + 1]
                    break
            if len(text) > 3 and not text.startswith(("{", "http")):
                raw.append({"speaker": speaker or "Unknown", "text": text})
            i += 2
        else:
            i += 1

    # Deduplicate rolling updates — keep longest version of each utterance
    result = []
    for entry in raw:
        if result and result[-1]["speaker"] == entry["speaker"]:
            prev = result[-1]["text"]
            if entry["text"].startswith(prev) or prev.startswith(entry["text"]):
                result[-1]["text"] = max(prev, entry["text"], key=len)
                continue
        result.append(entry)
    return result
```

---

## Watching for New Entries

Poll `wal.stat().st_size` every 2 seconds. When it changes, re-parse.
New entries appear at the end of the deduplicated list.

```python
import time
last_count = 0
while True:
    entries = parse_wal(wal)
    for e in entries[last_count:]:
        print(f"[{e['speaker']}]: {e['text']}")
    last_count = len(entries)
    time.sleep(2)
```

---

## Meeting Title Detection

The meeting title lives in a second IndexedDB in the same origin directory
(the `blocks` store, which has `BLOCK_TYPE_PAGE` records with a `title` field).
Readable via `strings` on that WAL too — look for lines that follow
`title` and contain meeting-name patterns (` / `, `Sync`, `Standup`, etc.).

---

## Implementation Options

### Option A — Obsidian Plugin (TypeScript)
- Use Node's `fs.watch` on the WAL file path
- Parse with a Buffer → UTF-8 string scan (same algorithm as above)
- On meeting end (idle detection), call Claude API and write a note via
  Obsidian's `vault.create()` API
- Benefit: lives entirely inside Obsidian, no separate process needed

### Option B — Lightweight Menu Bar App (Swift/SwiftUI)
- `DispatchSource.makeFileSystemObjectSource` for efficient WAL watching
- Parse the WAL in-process (no `strings` subprocess needed — just scan
  the file for UTF-8 sequences between null bytes)
- Show live transcript in a floating HUD
- On end: POST to Claude API, write `.md` to vault folder
- Benefit: native, zero dependencies, menu bar icon shows recording state

### Option C — Standalone Python Script
- Single file, no dependencies beyond stdlib
- Run manually after a meeting or via `--watch` mode
- Uses `strings` subprocess + `urllib` for Claude API
- Benefit: simplest to inspect, modify, and understand

---

## Caveats

- **Zoom updates may break this.** The path and DB structure are internal
  implementation details with no stability guarantee.
- The WAL is only present during an active meeting where My Notes is enabled.
  After the meeting ends, WebKit checkpoints the WAL into the main DB and
  may delete it.
- The `<hash>` directory name appears stable per Zoom account (it's derived
  from the `https://docs.zoom.us` origin + user ID), but could change on
  re-login or app update.
- Entries reflect what Zoom's ASR captures — accuracy depends on Zoom's
  server-side transcription, not local processing.
