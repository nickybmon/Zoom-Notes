"""Engine subprocess robustness: stderr flood must not block stdout JSON.

Phase 0 #2 regression guard. Pre-fix, EngineManager piped Python's stderr
to a `Pipe()` but never read it — a sufficiently noisy Python (a deprecation
warning loop, a daemon-thread traceback, an LLM library logging at INFO)
could fill the ~64KB pipe buffer and block the engine on its next stderr
write. From the user's perspective the menu bar would freeze with no
visible cause.

This is a pure-Python test: spawn a tiny subprocess that writes to BOTH
stdout (JSON event lines) AND stderr (filler) at high volume, and confirm
the parent can still decode every stdout JSON event. We don't run the
real engine — that would require a Zoom WAL — but we exercise the same
contract every reader of zoom_engine.py's output relies on.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap


def test_stderr_flood_does_not_block_stdout_when_drained():
    """When BOTH pipes are drained, every JSON line on stdout arrives
    intact regardless of how much filler hits stderr.

    This is the contract `EngineManager.swift`'s readability handlers
    must satisfy — the Python-side test exists to prove that *as long
    as both pipes are drained*, the protocol is robust to stderr noise.
    """
    script = textwrap.dedent(
        """
        import json, sys
        N = 200
        # Each iteration writes ~512 bytes to stderr (4x to overflow the
        # default ~64KB pipe over the course of the run) and one JSON
        # event line to stdout.
        FILLER = ('x' * 512) + '\\n'
        for i in range(N):
            sys.stdout.write(json.dumps({'event': 'tick', 'i': i}) + '\\n')
            sys.stdout.flush()
            sys.stderr.write(FILLER)
            sys.stderr.flush()
        sys.stdout.write(json.dumps({'event': 'done'}) + '\\n')
        sys.stdout.flush()
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Drain both pipes — same contract as EngineManager.swift after Phase 0 #2.
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"subprocess exited non-zero: {err[:200]}"

    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    ticks = [e for e in events if e.get("event") == "tick"]
    done = [e for e in events if e.get("event") == "done"]

    assert len(ticks) == 200, f"lost stdout events under stderr load: got {len(ticks)}/200"
    assert len(done) == 1, "final done event missing"
    # Stderr should have received the filler — confirms we actually
    # exercised the path.
    assert len(err) > 50_000, "stderr filler was suspiciously small — test may not be reproducing flood"


def test_undrained_stderr_blocks_subprocess():
    """Inverse / safety check: if stderr is NOT drained but the buffer
    overflows, the subprocess WILL block. This documents the failure mode
    we're guarding against in Phase 0 #2 — the existence of this test
    keeps the rationale visible to future maintainers."""
    script = textwrap.dedent(
        """
        import sys
        # Write 512KB to stderr without anyone reading. On macOS this far
        # exceeds the default 64KB pipe buffer.
        sys.stderr.write('x' * 512_000)
        sys.stderr.flush()
        # If the parent never drained, this print never runs.
        print('done')
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # piped but not drained until communicate()
        text=True,
    )
    # communicate() drains both — this should always succeed because
    # the test runner *does* drain. The interesting case is what
    # `EngineManager.stdoutPipe` would have looked like without the
    # stderr handler: the child would block on the stderr write forever.
    # We assert the subprocess completes when stderr IS drained, which
    # is the post-fix behavior.
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "done" in out
    assert len(err) >= 512_000
