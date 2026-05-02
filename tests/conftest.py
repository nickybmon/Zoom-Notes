"""Test configuration: make the project root importable for tests."""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def multi_meeting_wal() -> Path:
    """Path to the captured multi-meeting WAL fixture.

    This fixture contains two meetings' worth of entries in a single WAL —
    the exact scenario that broke the engine on 2026-04-27. Tests that
    reference this fixture are guarding against regressions of that bug.
    """
    path = FIXTURES_DIR / "multi_meeting_wal" / "transcript.sqlite3-wal"
    if not path.exists():
        pytest.skip(
            "multi_meeting_wal fixture not present — run "
            "`python3 tools/capture_wal.py multi_meeting_wal` during a meeting "
            "to capture one"
        )
    return path


@pytest.fixture
def multi_meeting_blocks() -> Path:
    path = FIXTURES_DIR / "multi_meeting_wal" / "blocks.sqlite3-wal"
    if not path.exists():
        pytest.skip("multi_meeting_wal blocks WAL not captured")
    return path


@pytest.fixture
def multi_meeting_meta() -> dict:
    path = FIXTURES_DIR / "multi_meeting_wal" / "meta.json"
    if not path.exists():
        pytest.skip("multi_meeting_wal meta not captured")
    return json.loads(path.read_text())


@pytest.fixture
def back_to_back_marc_anna_wal() -> Path:
    """Real WAL captured 2026-04-30 12:30, ~25 min after the back-to-back
    meeting overwrite incident. The Marc 1:1 entries had already been
    checkpointed by Zoom by capture time; what remains is the Anna stale
    fragment from the prior day. Used to assert that the title parser
    refuses to attach a stale title to such a fragment."""
    path = FIXTURES_DIR / "back_to_back_marc_anna" / "transcript.sqlite3-wal"
    if not path.exists():
        pytest.skip("back_to_back_marc_anna fixture not present")
    return path


@pytest.fixture
def back_to_back_marc_anna_blocks() -> Path:
    path = FIXTURES_DIR / "back_to_back_marc_anna" / "blocks.sqlite3-wal"
    if not path.exists():
        pytest.skip("back_to_back_marc_anna blocks WAL not captured")
    return path
