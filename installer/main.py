"""BS.4.2 — omnisight-installer sidecar long-poll loop entrypoint.

Replaces the BS.4.1 stub with the real worker loop against the backend
``GET /api/v1/installer/jobs/poll`` endpoint, including the ADR §4.3
protocol-version handshake on first connect.

Wire-protocol summary (mirrors ``backend/routers/installer.py`` +
``docs/design/bs-bootstrap-vertical-aware.md`` §4.2-§4.4)
─────────────────────────────────────────────────────────
Sidecar long-polls one URL:

    GET {backend_url}/api/v1/installer/jobs/poll
        ?sidecar_id={sid}
        &protocol_version={ver}
        &timeout_s={tout}
    Authorization: Bearer {token}

Response classification:

* ``200`` — JSON body of the *claimed* install_jobs row (already flipped
  to ``state='running'`` server-side via ``FOR UPDATE SKIP LOCKED``;
  exactly one sidecar wins). Sidecar must dispatch to the install method
  (BS.4.3) and report progress/result (BS.4.4). This row only ships the
  poll loop — claimed jobs are logged at INFO and left for BS.4.3 to
  pick up; the row stays in ``running`` until BS.4.3 ships method
  dispatch + ``POST .../result``. That is intentional: BS.4 epic is
  inert until BS.4.6 wires it into compose, so a stranded ``running``
  row is impossible in any operator-facing deployment during the
  BS.4.2-only window.
* ``204`` — long-poll window expired with no claim. Re-poll immediately
  (the timeout already absorbed the wait — no client-side sleep).
* ``426`` — protocol_version unsupported. Backend sends
  ``{client_protocol_version, supported, min_version, max_version}``;
  sidecar logs the gap loudly and **sleeps with backoff** instead of
  exiting so docker-compose ``restart: unless-stopped`` doesn't busy-
  loop hammering a guaranteed-fail endpoint. Operator pulls the right
  ``omnisight-installer:bs-vN`` tag and the next poll succeeds.
* ``401 / 403`` — auth misconfig (admin token missing / wrong / sidecar
  token rotation pending — see BS-future row). Same backoff treatment
  as 426; loud log, no exit.
* ``5xx`` / connection errors — backend transient (restart, DB blip,
  network partition). Exponential backoff capped at 30 s.

Configuration (env vars)
────────────────────────
The compose service block (BS.4.6) wires these. Defaults are picked so
``docker run --rm omnisight-installer:tag`` smoke-runs as far as the
first connection attempt before failing loudly.

* ``OMNISIGHT_INSTALLER_BACKEND_URL``  default ``http://backend-a:8000``
  Base URL for the backend the sidecar polls. Trailing slash optional.
* ``OMNISIGHT_INSTALLER_TOKEN``        default ``""`` (no auth)
  Bearer token for ``Authorization: Bearer …`` — until BS-future ships
  per-sidecar service tokens, this reuses the legacy
  ``OMNISIGHT_DECISION_BEARER`` value the backend already honours
  (``backend/auth.py::_legacy_bearer_matches``). Empty means the
  backend must be in ``OMNISIGHT_AUTH_MODE=open`` or the poll will
  401 — sidecar reports the misconfig loudly.
* ``OMNISIGHT_INSTALLER_SIDECAR_ID``   default ``$HOSTNAME`` or
  ``omnisight-installer-1``. Self-identifier the backend writes into
  ``install_jobs.sidecar_id`` on claim. Must match
  ``backend/routers/installer.py::SIDECAR_ID_PATTERN`` — chars beyond
  ``[A-Za-z0-9_.\\-:]`` get replaced with ``-`` to keep the poll
  query-string valid (a malformed sidecar_id would 422 on every poll).
* ``OMNISIGHT_INSTALLER_PROTOCOL_VERSION``  default ``1``
  Protocol version this sidecar speaks. Backend supports N and N-1
  per ADR §4.3; today only v1 ships. When v2 lands, the
  ``omnisight-installer:bs-v2`` image bumps this to ``2`` and v1
  sidecars start getting 426 → operator triggers ``docker-compose
  pull`` (image tag pinned per ADR §4.3 rule 3, so this is a deliberate
  upgrade, not silent drift).
* ``OMNISIGHT_INSTALLER_POLL_TIMEOUT_S``  default ``30``
  Long-poll window the backend waits before returning 204. Capped
  server-side at 60 s (``POLL_TIMEOUT_S_MAX``); we ask for 30 s by
  default, matching ADR §4.4 "long-poll default 30 s".
* ``OMNISIGHT_INSTALLER_AIRGAP``        default ``0``
  Threat model §6 air-gap mode. BS.4.2 only logs the flag for
  visibility; the actual ``--network=none`` enforcement and
  ``file://`` URL coercion land alongside ``installer/methods/`` in
  BS.4.3 (and the ``test_airgap_violation.py`` hook listed in the
  threat model §11 CI table.)
* ``OMNISIGHT_INSTALLER_LOG_LEVEL``     default ``INFO``
  Standard logging level name (``DEBUG``/``INFO``/``WARNING``/``ERROR``).

Module-global / cross-worker state audit (per implement_phase_step.md
Step 1)
────────────────────────────────────────────────────────────────────────
This module reads env vars inside ``_load_config()`` (called from
``main()``) and threads the resulting ``Config`` dataclass through every
helper — no module-level mutable singletons / caches / counters. The
sidecar runs as a single in-container process (``ENTRYPOINT python3 -m
installer.main``) under docker-compose ``restart: unless-stopped``;
"multi-worker" is not a concern in the same sense uvicorn workers are.
If the operator scales to ``--scale omnisight-installer=N`` (BS.4
epic does not currently recommend this, but it is permitted), each
container is its own OS process with its own env-derived config; the
cross-instance coordination is enforced *server-side* by ``SELECT …
FOR UPDATE SKIP LOCKED`` in ``backend/routers/installer.py::poll_for_job``
— a queued job is delivered to exactly one sidecar regardless of
sidecar replica count. Answer #1 from the SOP rubric: "every worker
derives the same value from the same source" — sidecar replicas are
stateless.

Read-after-write timing (per SOP Step 1)
────────────────────────────────────────
N/A — sidecar is a pure HTTP client. The PG transactional ordering is
owned by the backend handler; sidecar observes it through HTTP
responses.

Pre-commit fingerprint grep
───────────────────────────
``_conn() / await conn.commit() / datetime('now') / VALUES (?, ?…)``
all 0 hits — sidecar makes no DB calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger("omnisight.installer")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — keep in sync with backend/routers/installer.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_BACKEND_URL = "http://backend-a:8000"
_DEFAULT_PROTOCOL_VERSION = 1
_DEFAULT_POLL_TIMEOUT_S = 30
_POLL_TIMEOUT_S_HARD_MAX = 60  # backend's POLL_TIMEOUT_S_MAX

# ``backend/routers/installer.py::SIDECAR_ID_PATTERN``
_SIDECAR_ID_VALID_CHARS = re.compile(r"[A-Za-z0-9_.\-:]")
_SIDECAR_ID_MAX_LEN = 128

# Backoff bounds. After 426/auth/5xx/network errors we sleep before
# retrying so docker-compose ``restart: unless-stopped`` doesn't watch
# us busy-loop. Caps at 30 s — long enough that an operator pushing a
# fix will see the next attempt promptly, short enough that recovery is
# perceived as immediate after the fix.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
_BACKOFF_FACTOR = 2.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config dataclass (env-derived, threaded — no module globals)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class Config:
    backend_url: str
    token: str
    sidecar_id: str
    protocol_version: int
    poll_timeout_s: int
    airgap: bool
    log_level: str
    extra_headers: dict[str, str] = field(default_factory=dict)


def _sanitize_sidecar_id(raw: str) -> str:
    cleaned = "".join(
        ch if _SIDECAR_ID_VALID_CHARS.match(ch) else "-" for ch in raw
    )
    cleaned = cleaned.strip("-") or "omnisight-installer"
    return cleaned[:_SIDECAR_ID_MAX_LEN]


def _load_config() -> Config:
    backend_url = (
        os.environ.get("OMNISIGHT_INSTALLER_BACKEND_URL")
        or _DEFAULT_BACKEND_URL
    ).rstrip("/")
    token = os.environ.get("OMNISIGHT_INSTALLER_TOKEN", "").strip()

    raw_sid = (
        os.environ.get("OMNISIGHT_INSTALLER_SIDECAR_ID")
        or os.environ.get("HOSTNAME")
        or "omnisight-installer-1"
    )
    sidecar_id = _sanitize_sidecar_id(raw_sid)

    try:
        protocol_version = int(
            os.environ.get("OMNISIGHT_INSTALLER_PROTOCOL_VERSION")
            or _DEFAULT_PROTOCOL_VERSION
        )
    except ValueError:
        protocol_version = _DEFAULT_PROTOCOL_VERSION

    try:
        poll_timeout_s = int(
            os.environ.get("OMNISIGHT_INSTALLER_POLL_TIMEOUT_S")
            or _DEFAULT_POLL_TIMEOUT_S
        )
    except ValueError:
        poll_timeout_s = _DEFAULT_POLL_TIMEOUT_S
    poll_timeout_s = max(0, min(poll_timeout_s, _POLL_TIMEOUT_S_HARD_MAX))

    airgap = (os.environ.get("OMNISIGHT_INSTALLER_AIRGAP") or "0") == "1"
    log_level = (
        os.environ.get("OMNISIGHT_INSTALLER_LOG_LEVEL") or "INFO"
    ).upper()

    return Config(
        backend_url=backend_url,
        token=token,
        sidecar_id=sidecar_id,
        protocol_version=protocol_version,
        poll_timeout_s=poll_timeout_s,
        airgap=airgap,
        log_level=log_level,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signal handling — SIGTERM/SIGINT must drop us out of the loop cleanly
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _ShutdownFlag:
    """Tiny mutable holder so signal handlers and the main loop share
    state without resorting to a module-level global. The instance is
    constructed inside ``main()`` and threaded into the loop."""

    def __init__(self) -> None:
        self.requested = False
        self.signal: int | None = None

    def request(self, signum: int, _frame: Any) -> None:
        self.requested = True
        self.signal = signum
        logger.info(
            "shutdown signal received (signum=%d); exiting after current poll",
            signum,
        )


def _install_signal_handlers(flag: _ShutdownFlag) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, flag.request)
        except (OSError, ValueError):  # pragma: no cover — non-main-thread
            logger.warning(
                "could not install handler for signal %s; relying on default",
                sig,
            )


def _interruptible_sleep(seconds: float, flag: _ShutdownFlag) -> None:
    """Sleep up to ``seconds`` but bail early if shutdown is requested.
    250 ms tick mirrors the backend's ``_POLL_TICK_S``."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while not flag.requested and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP — long-poll request
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PollOutcome:
    """Result of one ``GET /installer/jobs/poll`` invocation.

    Exactly one of ``job`` / ``protocol_error`` / ``auth_error`` /
    ``transient_error`` is set; ``no_content`` is True iff the server
    returned 204.
    """

    job: dict[str, Any] | None = None
    no_content: bool = False
    protocol_error: dict[str, Any] | None = None
    auth_error: int | None = None
    transient_error: str | None = None


def _build_poll_url(cfg: Config) -> str:
    qs = urlencode({
        "sidecar_id": cfg.sidecar_id,
        "protocol_version": cfg.protocol_version,
        "timeout_s": cfg.poll_timeout_s,
    })
    return f"{cfg.backend_url}/api/v1/installer/jobs/poll?{qs}"


def _build_request(cfg: Config) -> urllib.request.Request:
    req = urllib.request.Request(
        _build_poll_url(cfg), method="GET",
    )
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"omnisight-installer/{cfg.protocol_version}")
    if cfg.token:
        req.add_header("Authorization", f"Bearer {cfg.token}")
    for k, v in cfg.extra_headers.items():
        req.add_header(k, v)
    return req


def _poll_once(cfg: Config) -> PollOutcome:
    """Issue one long-poll. Network/HTTP-layer failures are caught and
    returned as ``transient_error`` so the main loop owns the backoff
    decision."""
    req = _build_request(cfg)
    # Server may hold the conn for ``cfg.poll_timeout_s`` before
    # returning 204; allow a small slack so we close cleanly instead
    # of racing the server-side deadline.
    socket_timeout = float(cfg.poll_timeout_s) + 10.0
    try:
        with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
            status = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body_bytes = exc.read()
        except Exception:  # noqa: BLE001
            body_bytes = b""
    except urllib.error.URLError as exc:
        return PollOutcome(transient_error=f"url_error:{exc.reason!r}")
    except (TimeoutError, OSError) as exc:
        return PollOutcome(transient_error=f"network:{exc.__class__.__name__}:{exc}")

    if status == 200:
        try:
            payload = json.loads(body_bytes.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return PollOutcome(
                transient_error=f"bad_200_body:{exc.__class__.__name__}",
            )
        if not isinstance(payload, dict) or "id" not in payload:
            return PollOutcome(
                transient_error=f"bad_200_shape:missing_id_field",
            )
        return PollOutcome(job=payload)

    if status == 204:
        return PollOutcome(no_content=True)

    if status == 426:
        try:
            payload = json.loads(body_bytes.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return PollOutcome(protocol_error=payload if isinstance(payload, dict) else {})

    if status in (401, 403):
        return PollOutcome(auth_error=status)

    return PollOutcome(transient_error=f"http_{status}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _handle_claimed_job(job: dict[str, Any]) -> None:
    """BS.4.2 placeholder: log the claim + leave dispatch to BS.4.3.

    BS.4.3 will replace this with method dispatch
    (``installer/methods/{noop,docker_pull,shell_script,vendor_installer}.py``)
    + ``POST /installer/jobs/{id}/result`` on terminal state. The job
    already lives in ``state='running'`` server-side (claim happened in
    the same transaction as the SELECT FOR UPDATE) — it stays there
    until BS.4.3 reports a terminal state. That is acceptable for the
    BS.4.2-only window because BS.4.6 has not wired the sidecar into
    compose; no operator-facing deployment can reach this code path
    until BS.4 epic 7/7 is green.
    """
    logger.info(
        "claimed install job id=%s entry_id=%s tenant=%s state=%s "
        "(BS.4.2: dispatch lands in BS.4.3 — job will sit in 'running' "
        "until then; this is expected during BS.4 epic mid-rollout)",
        job.get("id"),
        job.get("entry_id"),
        job.get("tenant_id"),
        job.get("state"),
    )


def _log_protocol_handshake_failure(cfg: Config, payload: dict[str, Any]) -> None:
    """Single source of truth for the 426 log line — both first-connect
    handshake and any later 426 (e.g. backend rolled back to a tighter
    range mid-flight) get the same loud message."""
    logger.error(
        "protocol_version_unsupported: client=%d backend_supports=%s "
        "(min=%s max=%s). Sidecar image mismatch — operator must pull "
        "a compatible omnisight-installer:bs-vN tag. Will keep retrying "
        "with backoff; not exiting (compose restart would just hammer "
        "this same fail).",
        cfg.protocol_version,
        payload.get("supported"),
        payload.get("min_version"),
        payload.get("max_version"),
    )


def run_loop(cfg: Config, flag: _ShutdownFlag) -> int:
    """Long-poll loop. Returns process exit code.

    Exit conditions:

    * ``flag.requested`` (SIGTERM/SIGINT) → return 0 cleanly. compose
      ``restart: unless-stopped`` will not restart on exit 0.
    * Anything else → loop forever. Backoff caps prevent CPU spin.
    """
    logger.info(
        "omnisight-installer starting: backend=%s sidecar_id=%s "
        "protocol_version=%d timeout_s=%d airgap=%s token_set=%s",
        cfg.backend_url,
        cfg.sidecar_id,
        cfg.protocol_version,
        cfg.poll_timeout_s,
        cfg.airgap,
        bool(cfg.token),
    )
    if not cfg.token:
        logger.warning(
            "OMNISIGHT_INSTALLER_TOKEN not set — backend must be in "
            "OMNISIGHT_AUTH_MODE=open or every poll will return 401. "
            "Set the env var to the legacy OMNISIGHT_DECISION_BEARER "
            "value (per backend/auth.py::_legacy_bearer_matches) until "
            "BS-future ships per-sidecar service tokens."
        )

    is_first_connect = True
    backoff = _BACKOFF_INITIAL_S

    while not flag.requested:
        if is_first_connect:
            logger.info(
                "first connect — performing protocol_version=%d handshake "
                "against %s",
                cfg.protocol_version, cfg.backend_url,
            )

        outcome = _poll_once(cfg)

        if outcome.job is not None:
            if is_first_connect:
                logger.info("handshake OK (got 200 + claimed job on first poll)")
                is_first_connect = False
            backoff = _BACKOFF_INITIAL_S
            _handle_claimed_job(outcome.job)
            continue

        if outcome.no_content:
            if is_first_connect:
                logger.info("handshake OK (got 204 — backend speaks v%d)",
                            cfg.protocol_version)
                is_first_connect = False
            backoff = _BACKOFF_INITIAL_S
            continue  # immediate re-poll; long-poll already absorbed wait

        if outcome.protocol_error is not None:
            _log_protocol_handshake_failure(cfg, outcome.protocol_error)
            _interruptible_sleep(backoff, flag)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)
            continue

        if outcome.auth_error is not None:
            logger.error(
                "auth failed (HTTP %d): backend rejected our credentials. "
                "Check OMNISIGHT_INSTALLER_TOKEN matches the backend's "
                "OMNISIGHT_DECISION_BEARER, or set OMNISIGHT_AUTH_MODE=open "
                "for dev. Will retry with backoff.",
                outcome.auth_error,
            )
            _interruptible_sleep(backoff, flag)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)
            continue

        if outcome.transient_error is not None:
            logger.warning(
                "poll failed transiently (%s); sleeping %.1fs before retry",
                outcome.transient_error, backoff,
            )
            _interruptible_sleep(backoff, flag)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)
            continue

        # Defensive: PollOutcome with all fields unset shouldn't happen.
        logger.error("unexpected empty PollOutcome; sleeping before retry")
        _interruptible_sleep(backoff, flag)
        backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)

    logger.info(
        "shutdown complete (signal=%s); exiting 0",
        flag.signal,
    )
    return 0


def main() -> int:
    cfg = _load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    flag = _ShutdownFlag()
    _install_signal_handlers(flag)
    try:
        return run_loop(cfg, flag)
    except Exception as exc:  # noqa: BLE001 — last-ditch logging
        logger.exception("uncaught exception in run_loop: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
