"""OP-947 H2 — SLO breach handler for the L3 conductor (ADR-0018 row 8).

When D11's ``slo.breach`` lands the L3 contract is:

* **Halt** the L3 advance loop for the active release — block new
  child pick-ups until the operator clears the breach.
* **Surface** the breach to the operator dashboard via the existing
  events bus (already wired by D11; we re-publish a `release.halt`
  frame so the React dashboard knows to render the halt state).
* **Do NOT** auto-rollback — D11 already calls its own
  ``RollbackTrigger`` (per ``slo_monitor.py:445``); L3's only job is
  to stop *forward* progress so the rollback can complete cleanly.

The actual halt-state lives in the in-process ``_HALT_STATE`` registry
below — keyed by ``release_id`` (when known) or ``__global__`` for a
breach without a release context. The runner pickup gate consults
:func:`is_halted` before claiming a release child.
"""
from __future__ import annotations

import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)


_HALT_STATE: dict[str, dict[str, Any]] = {}
_HALT_LOCK = threading.Lock()


def is_halted(release_id: str | None = None) -> bool:
    """Return True if forward progress is currently halted.

    A release-scoped halt blocks only its own release; a global halt
    blocks every release. Both are cleared by :func:`clear_halt`.
    """
    with _HALT_LOCK:
        if "__global__" in _HALT_STATE:
            return True
        if release_id and release_id in _HALT_STATE:
            return True
    return False


def clear_halt(release_id: str | None = None) -> None:
    """Operator action — clear the halt. Exposed so the H4 web UI can
    wire a "resume" button; tests use it for cleanup between cases."""
    with _HALT_LOCK:
        if release_id is None:
            _HALT_STATE.clear()
        else:
            _HALT_STATE.pop(release_id, None)


def reset_for_tests() -> None:
    """Test-only — wipe the halt state between cases."""
    with _HALT_LOCK:
        _HALT_STATE.clear()


def on_slo_breach(event: dict[str, Any]) -> dict[str, Any]:
    """Record the halt + return a classified hint.

    The breach payload (from ``backend.orchestrator.slo_monitor``)
    carries ``error_rate`` / ``p95`` / ``breach_window_start`` and
    optionally a ``release_id``. Per ADR-0018 row 8 the handler does
    NOT mutate the JIRA graph and does NOT trigger a rollback — those
    side effects are already owned by D11.
    """
    release_id = str(event.get("release_id") or "") or None
    halt_key = release_id or "__global__"

    breach_meta = {
        "error_rate": event.get("error_rate"),
        "p95": event.get("p95"),
        "breach_window_start": event.get("breach_window_start"),
        "breach_id": event.get("breach_id"),
    }
    with _HALT_LOCK:
        _HALT_STATE[halt_key] = breach_meta

    logger.warning(
        "release_conductor.slo_breach halt_key=%s breach=%s",
        halt_key,
        breach_meta,
    )
    return {
        "outcome": "halt_recorded",
        "halt_key": halt_key,
        "release_id": release_id,
        "breach": breach_meta,
    }
