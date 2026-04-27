"""Phase 10 — short utterances and short speaker names round-trip.

The previous parser ran `strings(1)` with its 4-byte minimum default,
which silently dropped short utterances ("OK", "Hi", "No", "Hmm") and
short speaker names ("Li", "An", "Bo"). That made transcripts blind to
one-word answers and labeled their speakers as "Unknown".

These tests don't depend on a captured WAL — they bypass the strings(1)
call entirely by patching `read_wal_strings` to return synthetic token
streams that mirror the WAL's `messageId / <id> / message / <text> /
... / username / <name> / meetingId / <id>` layout. We then exercise
`parse_transcript` and assert that:

  - 2-char and 3-char utterances make it through the validator
  - 2-char speaker names round-trip into entries
  - the long-text case still works (regression guard)
  - obvious junk (digits-only, prefix-banned strings) is still rejected
"""
from pathlib import Path
from unittest.mock import patch

import pytest

import zoom_notes
from zoom_notes import parse_transcript


def _wal_lines(*entries: dict) -> list[str]:
    """Build a synthetic strings() output from a sequence of entry dicts.

    Each entry dict supplies: msg_id, text, ts, speaker, meeting_id.
    The on-disk byte layout is faithfully replicated; padding tokens
    appear in the middle to mimic the noise that real WAL pages carry.
    """
    out: list[str] = []
    for e in entries:
        out += [
            "messageId",
            e["msg_id"],
            "message",
            e["text"],
            "timeStampContent",
            e["ts"],
            "speaker",
            "speakerId",
            "noise_pad",
            "username",
            e["speaker"],
            "uniqueUserId",
            "noise_pad_2",
            "meetingId",
            e["meeting_id"],
        ]
    return out


# ── short utterances ─────────────────────────────────────────────────────────


def test_two_character_utterance_is_kept():
    """'OK' as a one-word answer should survive parsing."""
    lines = _wal_lines({
        "msg_id": "16:0:1:0:1",
        "text": "OK",
        "ts": "00:00:01",
        "speaker": "Alice",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert len(entries) == 1
    assert entries[0]["text"] == "OK"
    assert entries[0]["speaker"] == "Alice"


def test_three_character_utterance_is_kept():
    """'Hmm' was previously silently dropped (3 chars, below strings -n 4)."""
    lines = _wal_lines({
        "msg_id": "16:0:2:0:2",
        "text": "Hmm",
        "ts": "00:00:02",
        "speaker": "Alice",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert len(entries) == 1
    assert entries[0]["text"] == "Hmm"


def test_yes_no_yeah_all_kept():
    """One-word answers are the heart of why this change matters."""
    lines = _wal_lines(
        {"msg_id": "1", "text": "Yes", "ts": "00:00:01",
         "speaker": "Alice", "meeting_id": "M-1"},
        {"msg_id": "2", "text": "No", "ts": "00:00:02",
         "speaker": "Bob", "meeting_id": "M-1"},
        {"msg_id": "3", "text": "Yeah", "ts": "00:00:03",
         "speaker": "Alice", "meeting_id": "M-1"},
    )
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    texts = [e["text"] for e in entries]
    assert "Yes" in texts
    assert "No" in texts
    assert "Yeah" in texts


# ── short speakers ──────────────────────────────────────────────────────────


def test_two_character_speaker_name_is_kept():
    """'Li' / 'An' / 'Bo' should land in the speaker field, not 'Unknown'."""
    lines = _wal_lines({
        "msg_id": "16:0:3:0:3",
        "text": "Hello everyone, welcome",
        "ts": "00:00:01",
        "speaker": "Li",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert len(entries) == 1
    assert entries[0]["speaker"] == "Li"


def test_three_character_speaker_name_is_kept():
    lines = _wal_lines({
        "msg_id": "1",
        "text": "Sounds good to me",
        "ts": "00:00:01",
        "speaker": "Joe",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert entries[0]["speaker"] == "Joe"


# ── regression guards ───────────────────────────────────────────────────────


def test_long_text_still_works():
    """Regression: lowering the floor should not break the common case."""
    text = "This is a perfectly normal long utterance with many words."
    lines = _wal_lines({
        "msg_id": "1",
        "text": text,
        "ts": "00:00:01",
        "speaker": "Alice Williams",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert len(entries) == 1
    assert entries[0]["text"] == text


def test_pure_digit_text_still_rejected():
    """The new floor is text length, but `not text.isdigit()` and the
    must-contain-letter rule should still kick out things like '42'."""
    lines = _wal_lines({
        "msg_id": "1",
        "text": "42",
        "ts": "00:00:01",
        "speaker": "Alice",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert entries == []


def test_junk_token_text_still_rejected():
    """A two-char string that happens to match junk-prefix rules — '{x' for
    the JSON prefix guard — must still be rejected."""
    lines = _wal_lines({
        "msg_id": "1",
        "text": "{a",
        "ts": "00:00:01",
        "speaker": "Alice",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert entries == []


def test_single_character_text_still_rejected():
    """Floor is `>=2`, so single-char text shouldn't slip through."""
    lines = _wal_lines({
        "msg_id": "1",
        "text": "a",
        "ts": "00:00:01",
        "speaker": "Alice",
        "meeting_id": "M-1",
    })
    with patch.object(zoom_notes, "read_wal_strings", return_value=lines):
        entries = parse_transcript(Path("/dev/null"))
    assert entries == []


# ── strings command flag ────────────────────────────────────────────────────


def test_read_wal_strings_invokes_strings_with_n2(tmp_path):
    """Lock in the `-n 2` argument — drifting back to the default would
    silently re-introduce the short-utterance loss."""
    wal = tmp_path / "fake.wal"
    wal.write_bytes(b"\x00" * 32)

    captured: dict = {}

    class FakeResult:
        stdout = ""
        returncode = 0

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeResult()

    with patch.object(zoom_notes.subprocess, "run", side_effect=fake_run):
        zoom_notes.read_wal_strings(wal)

    assert captured["cmd"][:3] == ["/usr/bin/strings", "-n", "2"]
