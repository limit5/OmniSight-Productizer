r"""G1 #3 — systemd unit graceful-shutdown contract.

Pins the systemd-side half of the SIGTERM drain pipeline introduced in
G1 #1 (`backend/lifecycle.py`). The backend's drain coordinator stops
accepting new requests, flushes SSE, closes DB pools, and waits up to
30s for in-flight tasks. For that drain to actually run, systemd must:

  1. Send SIGTERM (not the default of whatever, which IS SIGTERM today
     but isn't contractually pinned — make it explicit so a future kid
     doesn't switch to SIGINT/SIGQUIT and silently break draining).

  2. Wait LONGER than the in-process drain budget before escalating to
     SIGKILL. Drain budget is 30s; we add a 10s buffer for FastAPI
     lifespan teardown + uvicorn worker exit, giving TimeoutStopSec=40.

If either is missing, systemd will SIGKILL the worker mid-flush, in-flight
HTTP requests get truncated, SSE clients see EOF, and the next process
inherits a half-closed DB pool. This file fails loudly at PR time so a
deploy regression can't ship that footgun.

Worker (`omnisight-worker@.service`) has its own contract (60s budget,
not 30s) because workers run docker-spawning agent tasks that can take
significantly longer than HTTP requests — verified separately.

Cost: <50ms, stdlib only, no systemd required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

BACKEND_UNIT = SYSTEMD_DIR / "omnisight-backend.service"
WORKER_UNIT = SYSTEMD_DIR / "omnisight-worker@.service"


def _service_block(unit_text: str) -> str:
    """Return the body of the [Service] section, excluding other sections."""
    m = re.search(r"^\[Service\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M)
    assert m, "unit file is missing a [Service] section"
    return m.group(1)


def _directive(block: str, key: str) -> str | None:
    """Return the value of `key=...` in a [Service] block, or None."""
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────────
# File presence
# ─────────────────────────────────────────────────────────────────────


def test_backend_unit_exists():
    assert BACKEND_UNIT.exists(), (
        f"missing {BACKEND_UNIT.relative_to(REPO_ROOT)} — required for production deploy"
    )


# ─────────────────────────────────────────────────────────────────────
# Backend SIGTERM + TimeoutStopSec contract
# ─────────────────────────────────────────────────────────────────────


def test_backend_kill_signal_is_sigterm():
    """KillSignal=SIGTERM is required so backend/lifecycle.py drain coordinator fires."""
    block = _service_block(BACKEND_UNIT.read_text())
    val = _directive(block, "KillSignal")
    assert val == "SIGTERM", (
        f"backend service must declare KillSignal=SIGTERM (drain coordinator triggers "
        f"on SIGTERM only); got {val!r}. Without this, systemd's default may change "
        f"and silently bypass backend/lifecycle.py."
    )


def test_backend_timeout_stop_sec_covers_drain_budget():
    """TimeoutStopSec=40 = 30s in-process drain budget + 10s lifespan teardown buffer."""
    block = _service_block(BACKEND_UNIT.read_text())
    val = _directive(block, "TimeoutStopSec")
    assert val is not None, "backend service must declare TimeoutStopSec"
    assert val == "40", (
        f"backend TimeoutStopSec must be 40 (drain budget 30s + 10s buffer); "
        f"got {val!r}. Lower values cause SIGKILL mid-drain, truncating in-flight "
        f"HTTP responses and corrupting SSE / DB pool state."
    )


def test_backend_drain_budget_smaller_than_timeout():
    """Sanity check: if either constant moves, ensure timeout still > drain budget."""
    block = _service_block(BACKEND_UNIT.read_text())
    timeout_val = _directive(block, "TimeoutStopSec")
    assert timeout_val is not None
    timeout_int = int(timeout_val)
    drain_budget = 30
    assert timeout_int > drain_budget, (
        f"TimeoutStopSec={timeout_int} must exceed in-process drain budget "
        f"({drain_budget}s). Otherwise systemd SIGKILLs before drain finishes."
    )


# ─────────────────────────────────────────────────────────────────────
# Worker template — already had both, regression-pin the contract
# ─────────────────────────────────────────────────────────────────────


def test_worker_unit_exists():
    assert WORKER_UNIT.exists()


def test_worker_kill_signal_is_sigterm():
    """Worker drains in-flight tasks + dist-locks on SIGTERM (backend/worker.py)."""
    block = _service_block(WORKER_UNIT.read_text())
    val = _directive(block, "KillSignal")
    assert val == "SIGTERM", (
        f"worker template must keep KillSignal=SIGTERM; got {val!r}"
    )


def test_worker_timeout_stop_sec_is_60():
    """Worker gets 60s (not 40s) — agent tasks can take longer than HTTP requests."""
    block = _service_block(WORKER_UNIT.read_text())
    val = _directive(block, "TimeoutStopSec")
    assert val == "60", (
        f"worker template must keep TimeoutStopSec=60 (longer drain for agent tasks); "
        f"got {val!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Documentation pin: comment must mention both directives so a future
# editor sees WHY they exist before bumping/removing them.
# ─────────────────────────────────────────────────────────────────────


def test_backend_unit_documents_drain_contract():
    text = BACKEND_UNIT.read_text()
    assert "KillSignal=SIGTERM" in text and "drain" in text.lower(), (
        "backend unit comment must reference KillSignal=SIGTERM + drain to explain "
        "why these knobs exist (so a future PR doesn't strip them as 'redundant')"
    )
    assert "TimeoutStopSec=40" in text, (
        "backend unit comment must reference TimeoutStopSec=40 with rationale"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
