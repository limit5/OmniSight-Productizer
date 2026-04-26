"""BS.4.4 — sidecar progress emitter (SSE via backend bus).

Replaces the BS.4.3 throttled-logger ``progress_cb`` with a real
backend-bound emitter. Each call POSTs the in-flight install state
to ``POST /installer/jobs/{job_id}/progress``; the backend handler
(see ``backend/routers/installer.py::report_progress``) updates
``install_jobs.{bytes_done,bytes_total,eta_seconds,log_tail}`` and
emits an SSE event the operator UI consumes for the live progress
bar / log tail panel.

Wire-protocol (mirrors backend handler, freeze for v1)
──────────────────────────────────────────────────────
``POST {backend_url}/api/v1/installer/jobs/{job_id}/progress``

Request body (JSON)::

    {
      "stage":         "downloading"|"verifying"|"running"|"promoting"|...,
      "bytes_done":    int >= 0,
      "bytes_total":   int >= 0  | null,
      "eta_seconds":   int >= 0  | null,
      "log_tail":      str (capped 4 KiB by base.LOG_TAIL_MAX_BYTES),
      "sidecar_id":    str  // for audit + cross-replica disambiguation
    }

Response (200)::

    { "state": "running"|"queued"|"completed"|"failed"|"cancelled",
      "id":    "ij-..." }

The response carries ``state`` so the emitter can detect operator
cancel: when the backend has flipped the install_jobs row to
``cancelled``, the emitter raises :class:`InstallCancelled` so the
in-flight install method's ``cancel_check`` / wrapping ``try`` lands
on the kill-and-reap path (threat model §4.8 — see
``installer/methods/base.py::run_in_process_group``).

Throttling
──────────
Background installs may emit thousands of progress ticks (every 1 MiB
chunk in :func:`installer.methods.base.fetch_url`). To keep the bus
load bounded we throttle by:

* ``min_interval_s`` (default ``1.0``) — minimum wall-clock between
  POSTs in the same stage. First call in a stage is always sent so
  the UI sees the stage transition immediately.
* ``min_byte_delta`` (default ``256 * 1024`` = 256 KiB) — also send
  if bytes_done has advanced this much since the last post, even
  inside the time window. This keeps the progress bar smooth on a
  fast download (1 MiB/s → ~4 posts/sec) without spamming on a slow
  one (10 KiB/s → 1 post/sec from time gate).
* Stage transitions ALWAYS send (otherwise the UI's stage label
  lags by ``min_interval_s``).
* Final ``flush()`` (called from the install method's terminal path)
  forces one trailing post.

HTTP failure handling
─────────────────────
A failed POST must NOT break the install. Three outcomes:

1. Network error / 5xx → log at WARNING, drop the tick, keep going.
   The next successful post covers the missed update; the UI tolerates
   gaps (it already does, see ``frontend/.../installer-progress.tsx``).
2. 404 (job vanished — extremely rare race after a hard DB wipe) →
   log ERROR, raise :class:`InstallCancelled` so the method aborts
   cleanly. There's no point computing more progress if the row
   doesn't exist.
3. 200 with ``state == 'cancelled'`` → raise :class:`InstallCancelled`.
4. 200 with terminal state ``completed/failed`` → raise
   :class:`InstallCancelled` (the operator hard-cancelled / a previous
   ``running`` row got force-failed; we should still stop).

Module-global state audit (per implement_phase_step.md Step 1)
──────────────────────────────────────────────────────────────
This module is pure factory + helper functions; no module-level
singletons / caches / counters. Each call to :func:`make_progress_cb`
returns a fresh closure with its own per-job state dict (last post
time, last bytes_done, last stage). The emitter is constructed inside
``main._handle_claimed_job`` per-job and disposed when the install
method returns — no long-lived state crosses jobs.

Read-after-write timing audit
─────────────────────────────
N/A — pure HTTP client; PG ordering is owned by the backend handler.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from installer.methods.base import (
    InstallCancelled,
    LOG_TAIL_MAX_BYTES,
    ProgressCallback,
)

logger = logging.getLogger("omnisight.installer.progress")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Throttle / cadence defaults
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_MIN_INTERVAL_S = 1.0
DEFAULT_MIN_BYTE_DELTA = 256 * 1024  # 256 KiB
DEFAULT_HTTP_TIMEOUT_S = 10.0
TERMINAL_REMOTE_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class ProgressEmitterConfig:
    """Subset of installer Config relevant to this emitter — keeps the
    factory call site narrow so it's clear what we depend on."""

    backend_url: str       # e.g. "http://backend-a:8000"
    token: str             # bearer; "" means open-mode backend
    sidecar_id: str        # mirrors install_jobs.sidecar_id
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    min_byte_delta: int = DEFAULT_MIN_BYTE_DELTA


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def make_progress_cb(
    cfg: ProgressEmitterConfig,
    job_id: str,
    *,
    now: "Any" = time.monotonic,
    post_fn: "Any" = None,
) -> ProgressCallback:
    """Build a :data:`ProgressCallback` for *job_id*.

    Every call to the returned ``cb(stage=..., bytes_done=..., ...)``:

    1. Decides whether to POST (stage change OR time-since-last >= min
       OR byte-delta >= min).
    2. POSTs to the backend; on 200 inspects ``state`` for cancel.
    3. Raises :class:`InstallCancelled` on remote-cancel / 404 / terminal.
    4. Eats network failures with a WARNING log (install must keep going).

    Parameters:

    * ``now`` — injectable monotonic clock (test seam).
    * ``post_fn`` — injectable transport (test seam). Signature matches
      :func:`_default_post`. Defaults to a stdlib-urllib POST that
      returns ``(status_code, parsed_body_or_None)``.
    """
    if not job_id:
        raise ValueError("make_progress_cb: job_id is required")

    transport = post_fn if post_fn is not None else _default_post
    state = {
        "last_post_at": 0.0,
        "last_bytes_done": -1,  # -1 forces first-tick post
        "last_stage": None,
    }

    def _cb(*, stage: str, bytes_done: int, bytes_total: int | None,
            eta_seconds: int | None, log_tail: str) -> None:
        ts = float(now())
        stage = str(stage or "running")
        bytes_done = max(0, int(bytes_done or 0))
        bytes_total = (
            None if bytes_total is None else max(0, int(bytes_total))
        )
        eta_seconds = (
            None if eta_seconds is None else max(0, int(eta_seconds))
        )
        log_tail = _truncate_log_tail(log_tail)

        first_in_stage = stage != state["last_stage"]
        time_elapsed = ts - state["last_post_at"]
        byte_delta = bytes_done - max(0, state["last_bytes_done"])

        if not first_in_stage:
            if (
                time_elapsed < cfg.min_interval_s
                and byte_delta < cfg.min_byte_delta
            ):
                # Throttled — neither time nor byte-delta gate tripped.
                return

        body = {
            "stage": stage,
            "bytes_done": bytes_done,
            "bytes_total": bytes_total,
            "eta_seconds": eta_seconds,
            "log_tail": log_tail,
            "sidecar_id": cfg.sidecar_id,
        }
        try:
            status, parsed = transport(
                url=_progress_url(cfg.backend_url, job_id),
                token=cfg.token,
                body=body,
                timeout_s=cfg.http_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — defensive; never break install
            logger.warning(
                "progress POST failed (transport): job=%s stage=%s err=%s",
                job_id, stage, exc,
            )
            # Update bookkeeping so we don't infinite-retry every tick;
            # the next gate-trip will try again.
            state["last_post_at"] = ts
            state["last_bytes_done"] = bytes_done
            state["last_stage"] = stage
            return

        # Bookkeeping AFTER the transport call so a transient miss
        # (caught above) doesn't reset the bytes_done watermark.
        state["last_post_at"] = ts
        state["last_bytes_done"] = bytes_done
        state["last_stage"] = stage

        if status == 404:
            logger.error(
                "progress POST returned 404 for job %s — install_jobs row "
                "missing (DB wipe? race with retry?). Aborting install.",
                job_id,
            )
            raise InstallCancelled(
                f"install_jobs row {job_id!r} missing on backend"
            )

        if status >= 400:
            logger.warning(
                "progress POST got HTTP %d for job %s stage=%s — keeping "
                "install going; next tick will retry.",
                status, job_id, stage,
            )
            return

        if not isinstance(parsed, dict):
            return  # 2xx without a JSON body — treat as success.
        remote_state = parsed.get("state")
        if remote_state in TERMINAL_REMOTE_STATES:
            logger.info(
                "backend reports job %s state=%s — aborting in-flight install",
                job_id, remote_state,
            )
            raise InstallCancelled(
                f"backend flipped job {job_id!r} to {remote_state!r}"
            )

    return _cb


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _progress_url(backend_url: str, job_id: str) -> str:
    return f"{backend_url.rstrip('/')}/api/v1/installer/jobs/{job_id}/progress"


def _truncate_log_tail(log_tail: str) -> str:
    """Mirror :data:`installer.methods.base.LOG_TAIL_MAX_BYTES` so the
    backend's payload validator never trips on an over-long tail."""
    if not log_tail:
        return ""
    encoded = log_tail.encode("utf-8", errors="replace")
    if len(encoded) <= LOG_TAIL_MAX_BYTES:
        return log_tail
    trimmed = encoded[-LOG_TAIL_MAX_BYTES:]
    return trimmed.decode("utf-8", errors="replace")


def _default_post(
    *, url: str, token: str, body: dict[str, Any], timeout_s: float,
) -> tuple[int, dict[str, Any] | None]:
    """Stdlib-urllib POST returning ``(status, parsed_body_or_None)``.

    Returns the HTTP status even for 4xx/5xx (we do NOT raise on those —
    the caller decides). Network errors propagate as exceptions and the
    factory's outer ``except Exception`` swallows them.
    """
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
    if not raw:
        return status, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None
    return status, parsed if isinstance(parsed, dict) else None


__all__ = [
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_MIN_BYTE_DELTA",
    "DEFAULT_MIN_INTERVAL_S",
    "ProgressEmitterConfig",
    "TERMINAL_REMOTE_STATES",
    "make_progress_cb",
]
