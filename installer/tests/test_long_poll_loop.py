"""BS.4.7 — drift guard for ``installer.main`` long-poll worker loop.

Locks the wire-protocol contract BS.4.2 / BS.4.4 / BS.4.5 established
between the sidecar and ``backend/routers/installer.py::poll_for_job``.
Each test runs against a real ``http.server.ThreadingHTTPServer`` on
``127.0.0.1`` so we exercise the actual ``urllib`` request path the
container ships with — no httpx / requests mocking layer to drift
from.

Coverage axes (5 PollOutcome paths + 1 retry/cancel loop integration):

1. **200 + valid job dict** → ``PollOutcome.job`` populated, ``id``
   round-trips. The first-connect handshake-OK log line is the
   sidecar's only init signal.
2. **204 (long-poll expired)** → ``PollOutcome.no_content`` set,
   ``backoff`` resets to initial (BS.4.2 spec). Re-poll without sleep.
3. **426 protocol mismatch** → ``PollOutcome.protocol_error`` carries
   the JSON body so the loop can log ``client/min/max`` versions
   loudly. Loop applies backoff + retry rather than ``sys.exit(1)``.
4. **401 auth misconfig** → ``PollOutcome.auth_error`` set; loop
   backs off + retries (operator must fix env var, no exit).
5. **5xx transient** → ``PollOutcome.transient_error`` populated;
   loop backs off + retries (network/DB blip).
6. **SIGTERM during retry/cancel** → ``run_loop`` exits 0 cleanly
   (no compose-side restart loop on a fail-loud retryable error).

Module-global state audit (per implement_phase_step.md Step 1)
──────────────────────────────────────────────────────────────
The sidecar's ``installer.main`` is module-global-free (Config dataclass
is threaded; signal handler holds onto a ``_ShutdownFlag`` instance
constructed inside ``main()``). Tests construct fresh ``Config`` /
``_ShutdownFlag`` per case. Each test starts/stops its own HTTP server
to keep the port-binding ephemeral and parallel-safe.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from installer.main import (
    Config,
    _ShutdownFlag,
    _poll_once,
    run_loop,
)


# ────────────────────────────────────────────────────────────────────
#  Test HTTP server harness
# ────────────────────────────────────────────────────────────────────


def _make_handler(behaviour: Callable[[Any], None]) -> type:
    """Build an http.server handler that delegates GET requests to
    *behaviour* — a callable receiving the handler instance and
    writing the response inline. We construct one class per test so
    each test owns its own behaviour without sharing module state."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any, **_kwargs: Any) -> None:
            # Suppress the noisy stderr log from the test server.
            return

        def do_GET(self) -> None:  # noqa: N802
            behaviour(self)

    return _Handler


@contextmanager
def _serve(behaviour: Callable[[Any], None]) -> Iterator[str]:
    """Start a ThreadingHTTPServer bound to 127.0.0.1:0 (kernel-picked
    free port), yield its base URL, and tear down on exit. The
    behaviour callable can read ``self.path`` to inspect query string
    if needed."""
    handler_cls = _make_handler(behaviour)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _make_cfg(backend_url: str, **overrides: Any) -> Config:
    """Construct a Config dataclass with sane defaults for the loop
    tests. ``poll_timeout_s=1`` keeps long-poll tests fast — the loop
    waits up to 1s + 10s slack for 204s; we always return immediately
    in our test handler."""
    base = {
        "backend_url": backend_url,
        "token": "",
        "sidecar_id": "test-sidecar-1",
        "protocol_version": 1,
        "poll_timeout_s": 1,
        "airgap": False,
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
#  PollOutcome contract — one test per HTTP outcome
# ────────────────────────────────────────────────────────────────────


def test_poll_200_returns_claimed_job() -> None:
    """Backend returns 200 + JSON job row → ``PollOutcome.job`` set
    with the parsed dict. The ``id`` field is the only hard wire-
    protocol invariant the poller checks."""
    job_payload = {
        "id": "ij-aaaaaaaaaaaa",
        "entry_id": "entry-1",
        "tenant_id": "tenant-x",
        "state": "running",
        "install_method": "noop",
    }

    def behaviour(h: Any) -> None:
        body = json.dumps(job_payload).encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        outcome = _poll_once(cfg)

    assert outcome.job == job_payload
    assert outcome.no_content is False
    assert outcome.protocol_error is None
    assert outcome.auth_error is None
    assert outcome.transient_error is None


def test_poll_204_returns_no_content() -> None:
    """Backend returns 204 (long-poll window expired with no claim)
    → ``PollOutcome.no_content`` set. Loop must re-poll without
    client-side sleep — the server already absorbed the wait."""

    def behaviour(h: Any) -> None:
        h.send_response(204)
        h.end_headers()

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        outcome = _poll_once(cfg)

    assert outcome.no_content is True
    assert outcome.job is None
    assert outcome.transient_error is None


def test_poll_426_returns_protocol_error() -> None:
    """Backend returns 426 + ``{client_protocol_version, supported,
    min_version, max_version}`` → ``PollOutcome.protocol_error``
    carries the JSON body so the loop's loud log can name the gap.
    Operator must pull a compatible image tag (per ADR §4.3 rule 3)."""
    body = {
        "client_protocol_version": 1,
        "supported": [2, 3],
        "min_version": 2,
        "max_version": 3,
    }

    def behaviour(h: Any) -> None:
        raw = json.dumps(body).encode("utf-8")
        h.send_response(426)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        outcome = _poll_once(cfg)

    assert outcome.protocol_error == body
    assert outcome.job is None
    assert outcome.no_content is False
    assert outcome.auth_error is None


def test_poll_401_returns_auth_error() -> None:
    """Backend returns 401 (token missing / wrong / sidecar token
    rotation pending) → ``PollOutcome.auth_error == 401``. Loop
    backs off + retries; does NOT ``sys.exit(1)`` (compose
    ``restart: unless-stopped`` would just hammer the same fail)."""

    def behaviour(h: Any) -> None:
        h.send_response(401)
        h.send_header("Content-Length", "0")
        h.end_headers()

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        outcome = _poll_once(cfg)

    assert outcome.auth_error == 401
    assert outcome.job is None
    assert outcome.transient_error is None


def test_poll_5xx_returns_transient_error() -> None:
    """Backend returns 500 (DB blip / restart) → ``PollOutcome.transient_error``
    contains the structured ``http_500`` token. Loop applies
    exponential backoff capped at 30s."""

    def behaviour(h: Any) -> None:
        h.send_response(500)
        h.send_header("Content-Length", "0")
        h.end_headers()

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        outcome = _poll_once(cfg)

    assert outcome.transient_error is not None
    assert "500" in outcome.transient_error
    assert outcome.job is None
    assert outcome.no_content is False


def test_run_loop_retries_5xx_then_exits_on_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: backend returns 500 several times (loop retries
    with backoff), then a SIGTERM-equivalent flag.requested = True
    drops the loop out cleanly with exit code 0.

    Locks the BS.4.2 invariant that retryable errors NEVER cause
    process exit — only an explicit shutdown signal does. Otherwise
    compose's ``restart: unless-stopped`` would busy-loop on a
    guaranteed-fail backend (e.g. 426 + wrong image tag).

    We monkeypatch ``_BACKOFF_INITIAL_S`` / ``_BACKOFF_MAX_S`` to
    sub-second values so the test runs in <2s wall-clock instead of
    waiting on the production backoff cap."""
    monkeypatch.setattr("installer.main._BACKOFF_INITIAL_S", 0.05)
    monkeypatch.setattr("installer.main._BACKOFF_MAX_S", 0.1)

    call_count = {"n": 0}

    def behaviour(h: Any) -> None:
        call_count["n"] += 1
        h.send_response(500)
        h.send_header("Content-Length", "0")
        h.end_headers()

    flag = _ShutdownFlag()

    def signal_after_delay() -> None:
        # Let the loop iterate a few times before requesting shutdown.
        time.sleep(0.5)
        flag.requested = True
        flag.signal = 15  # SIGTERM

    canceller = threading.Thread(target=signal_after_delay, daemon=True)

    with _serve(behaviour) as url:
        cfg = _make_cfg(url)
        canceller.start()
        rc = run_loop(cfg, flag, health=None)
        canceller.join(timeout=2.0)

    assert rc == 0, "shutdown signal must yield clean exit 0"
    # Sanity: the loop did at least one real poll attempt before the
    # cancel arrived (otherwise the test isn't actually exercising
    # the retry path).
    assert call_count["n"] >= 1, f"expected ≥1 poll, got {call_count['n']}"
