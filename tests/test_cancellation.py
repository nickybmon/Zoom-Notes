"""Cancellation tests for the LLM HTTP retry loop.

Covers the cooperative cancellation contract added in Phase 0 #1: when a
`threading.Event` cancel token is set, in-progress generation aborts at the
next checkpoint instead of running to completion (or to the urllib timeout).

These tests intentionally do not hit the network — they patch
`urllib.request.urlopen` to control timing.
"""
from __future__ import annotations

import threading
import time
from io import BytesIO
from unittest.mock import patch

import pytest

import zoom_notes
from zoom_notes import CancelledError, _http_retry


def _fake_resp(payload: bytes):
    """Build a minimal context-manager response object that urlopen returns."""
    class _R:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return payload
    return _R()


class TestCancelDuringBackoff:
    def test_cancel_during_inter_attempt_sleep_aborts_promptly(self):
        """A cancel set while the retry loop is sleeping between attempts
        should be observed within the 1-second chunked-wait window.

        Pre-fix this required the full backoff (15s on attempt 2) to elapse.
        """
        cancel = threading.Event()

        # Make every urlopen call raise a retryable error so the loop sleeps.
        import urllib.error
        def _always_503(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url if hasattr(req, "full_url") else "u",
                503, "Service Unavailable", hdrs=None, fp=BytesIO(b"down"),
            )

        # Set the cancel event 200ms after we start so it fires DURING the sleep.
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            start = time.monotonic()
            with patch("urllib.request.urlopen", side_effect=_always_503):
                with pytest.raises(CancelledError):
                    _http_retry(
                        zoom_notes.urllib.request.Request("http://invalid.test"),
                        lambda body: body["x"],
                        retries=3,
                        cancel_event=cancel,
                    )
            elapsed = time.monotonic() - start
        finally:
            timer.cancel()

        # Should bail out in well under the unmodified 15s first-backoff window.
        # 5s ceiling is generous on slow CI.
        assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s — backoff was not chunked"


class TestCancelBeforeAttempt:
    def test_pre_set_cancel_event_skips_first_attempt(self):
        """If the cancel event is already set when _http_retry starts, no
        HTTP call should happen at all."""
        cancel = threading.Event()
        cancel.set()
        called = {"n": 0}

        def _counted(req, timeout):
            called["n"] += 1
            return _fake_resp(b'{"x": "ok"}')

        with patch("urllib.request.urlopen", side_effect=_counted):
            with pytest.raises(CancelledError):
                _http_retry(
                    zoom_notes.urllib.request.Request("http://invalid.test"),
                    lambda body: body["x"],
                    retries=3,
                    cancel_event=cancel,
                )

        assert called["n"] == 0, "urlopen should not have been called when cancel was pre-set"


class TestCancelLetsHappyPathThrough:
    def test_unset_event_does_not_interfere(self):
        """Confirm the cancel-aware path doesn't break the normal happy
        flow — an unset event behaves identically to no event at all."""
        cancel = threading.Event()  # never set

        def _ok(req, timeout):
            return _fake_resp(b'{"text": "summary body"}')

        with patch("urllib.request.urlopen", side_effect=_ok):
            result = _http_retry(
                zoom_notes.urllib.request.Request("http://invalid.test"),
                lambda body: body["text"],
                retries=3,
                cancel_event=cancel,
            )

        assert result == "summary body"
