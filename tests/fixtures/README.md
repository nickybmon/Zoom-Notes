# Test fixtures

This directory holds captured Zoom WAL files used by the pytest suite.

**WAL files are deliberately not committed** — they contain real meeting
content (speaker names, spoken text) and are inherently personal. The
`.gitignore` rule `tests/fixtures/*/` prevents them from ever being
checked in.

## Capturing a fixture locally

Run during (or just after) a real Zoom meeting:

```bash
make capture-fixture NAME=single_meeting       # any meeting
make capture-fixture NAME=multi_meeting_wal    # right after a new meeting starts while old data is still in WAL
```

This produces `tests/fixtures/<name>/` with `transcript.sqlite3-wal`,
`blocks.sqlite3-wal`, and `meta.json`.

## Running tests

Tests that depend on a fixture call `pytest.skip(...)` when it isn't
present, so the suite still runs cleanly on a fresh clone — you'll just
see those tests skipped:

```bash
make test
```

To exercise the regression-guard tests for the 2026-04-27 multi-meeting
bug, capture `multi_meeting_wal` first.
