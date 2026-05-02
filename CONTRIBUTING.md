# Contributing to Zoom Notes

Thanks for your interest in contributing. This is a small, focused tool — contributions that fix bugs, improve reliability, or add support for new LLM providers are most welcome.

## Before you start

- Open an issue first for anything non-trivial so we can align on scope before you invest time writing code.
- Bug fixes and small improvements can go straight to a PR.

## Setup

```bash
git clone https://github.com/nickybmon/Zoom-Notes.git
cd Zoom-Notes

# Install git hooks (blocks accidental commits of WAL files and secrets)
make install-hooks

# Python environment (for running tests)
python3 -m venv venv
./venv/bin/pip install pytest

# Run the test suite
make test
```

## Pull request checklist

- [ ] `make test` passes with no failures
- [ ] No WAL files, `settings.json`, API keys, or real meeting content committed (the pre-commit hook enforces this)
- [ ] Swift changes compile in Xcode without warnings
- [ ] New behaviour is covered by a test in `tests/`

## Project structure

```
zoom_notes.py       — WAL discovery, transcript parsing, LLM calls, note writing
zoom_config.py      — Settings and Keychain helpers
zoom_engine.py      — Headless poller spawned by the Swift app
ZoomNotesApp/       — Swift/SwiftUI menu bar app
tests/              — pytest suite
```

See `CLAUDE.md` for a detailed architecture walkthrough.

## Code style

- Python: standard library only (no third-party runtime dependencies). PEP 8, type hints where practical.
- Swift: system frameworks only (no SPM packages). Standard SwiftUI/AppKit patterns.
- No heavy abstractions — prefer direct, readable code over clever generalization.

## Reporting issues

Please include:
- macOS version
- Zoom version
- Which LLM provider you're using
- Relevant output from `python3 zoom_notes.py --list` or Console.app logs under `zoom-notes`
