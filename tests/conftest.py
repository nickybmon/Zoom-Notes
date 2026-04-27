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
