"""L1 — Bootstrap wizard REST endpoints.

Exposes the admin-only finalize transition that closes the first-install
wizard. Finalize is guarded by the same four-gate contract driven by
:func:`backend.bootstrap.get_bootstrap_status`: if any gate is still red
OR any required step is missing from ``bootstrap_state``, the call
returns HTTP 409 with the offending signal so the wizard can surface
which step the operator still owes.

The route lives under ``/bootstrap/*`` so the global bootstrap gate
middleware in :mod:`backend.main` lets it through before the app is
finalized — otherwise finalize itself would be redirected to the wizard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend import auth as _au
from backend import audit
from backend import bootstrap as _boot
from backend import llm_secrets as _secrets
from backend.config import settings as _settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L2 — Step 1 (force admin password rotation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AdminPasswordRequest(BaseModel):
    """Request body for the wizard's Step 1 password rotation.

    ``current_password`` is verified against the default admin row (the
    one still flagged ``must_change_password=1``). ``new_password`` is
    re-validated server-side using :func:`auth.validate_password_strength`
    — the 12-char + zxcvbn ≥ 3 bar owned by K7/K1.
    """

    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class AdminPasswordResponse(BaseModel):
    status: str
    admin_password_default: bool
    user_id: str


@router.post("/admin-password", response_model=AdminPasswordResponse)
async def bootstrap_admin_password(req: AdminPasswordRequest) -> AdminPasswordResponse:
    """Rotate the shipping default admin credential during the wizard.

    This endpoint is intentionally unauthenticated — during L2 Step 1 no
    admin is logged in yet. It identifies the target user as the single
    admin row carrying ``must_change_password=1`` (i.e. the one
    :func:`auth.ensure_default_admin` created with the bundled
    ``omnisight-admin`` fallback). The operator's ``current_password``
    must still verify against that row, so an attacker without access
    to the default password cannot trigger this flow.

    On success:
      * rotates the password (clears ``must_change_password``)
      * records ``bootstrap_state.admin_password_set`` with the admin's
        user id as actor
      * writes audit action ``bootstrap.admin_password_set``

    Error contract:
      * 409 if no admin still requires a password change (already done)
      * 401 if current_password is wrong
      * 422 if new_password fails the strength check
    """
    target = await _au.find_admin_requiring_password_change()
    if target is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={
                "detail": "No admin currently requires a password change — "
                          "default credential has already been rotated.",
                "admin_password_default": False,
            },
        )

    verified = await _au.authenticate_password(target.email, req.current_password)
    if verified is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=401,
            content={"detail": "current password is incorrect"},
        )

    strength_err = _au.validate_password_strength(req.new_password)
    if strength_err:
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={"detail": strength_err},
        )

    # Rotate (clears must_change_password atomically).
    await _au.change_password(target.id, req.new_password)

    # Record the wizard step — drives the L1 finalize gate.
    await _boot.record_bootstrap_step(
        _boot.STEP_ADMIN_PASSWORD,
        actor_user_id=target.id,
        metadata={"email": target.email, "source": "wizard"},
    )

    try:
        await audit.log(
            action="bootstrap.admin_password_set",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_ADMIN_PASSWORD,
            before={"must_change_password": True},
            after={"must_change_password": False, "user_id": target.id},
            actor=target.email,
        )
    except Exception as exc:
        logger.debug("bootstrap.admin_password_set audit emit failed: %s", exc)

    logger.info(
        "bootstrap: admin password rotated for user=%s via wizard Step 1",
        target.email,
    )
    return AdminPasswordResponse(
        status="password_changed",
        admin_password_default=False,
        user_id=target.id,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3 — Step 2 (LLM provider + API key provisioning)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LlmProvisionRequest(BaseModel):
    """Request body for the wizard's L3 Step 2 provider provisioning.

    ``api_key`` is required for every hosted provider (anthropic / openai
    / azure); ``ollama`` is local and authenticates by reachability only.
    ``base_url`` is mandatory for Azure (the resource endpoint) and
    optional for Ollama (defaults to ``http://localhost:11434``). A
    caller-supplied ``model`` is echoed back into
    :attr:`settings.llm_model` so the agent factory picks it up.
    """

    provider: str = Field(min_length=1, max_length=32)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=128)
    base_url: str = Field(default="", max_length=512)
    azure_deployment: str = Field(default="", max_length=128)


class LlmProvisionResponse(BaseModel):
    status: str
    provider: str
    model: str
    fingerprint: str
    latency_ms: int
    models: list[str] = Field(default_factory=list)


_PING_KIND_TO_STATUS: dict[str, int] = {
    "key_invalid": 401,
    "quota_exceeded": 429,
    "network_unreachable": 504,
    "bad_request": 400,
    "provider_error": 502,
}


@router.post("/llm-provision", response_model=LlmProvisionResponse)
async def bootstrap_llm_provision(req: LlmProvisionRequest) -> LlmProvisionResponse:
    """Verify + persist an LLM provider credential during wizard Step L3.

    Flow:
      1. ``provider.ping()`` — a single REST probe against the hosted
         provider that classifies the failure into ``key_invalid``,
         ``quota_exceeded``, ``network_unreachable``, or ``bad_request``.
         Ollama uses the local ``/api/tags`` probe so the same path also
         satisfies the "Ollama reachability" bullet of L3 Step 2.
      2. On success, persist the credential encrypted-at-rest via
         :mod:`backend.llm_secrets` (Fernet; key from ``OMNISIGHT_SECRET_KEY``
         or ``data/.secret_key``).
      3. Mirror the active provider into ``settings.llm_provider`` and
         clear the LLM factory cache so the next ``get_llm()`` call uses
         the fresh credential without an env reload.
      4. Record ``bootstrap_state.llm_provider_configured`` and emit an
         audit row — mirrors the admin-password step's contract.

    Intentionally unauthenticated: during the wizard the operator has
    no admin session yet. The global bootstrap-gate middleware
    (:mod:`backend.main`) only permits ``/bootstrap/*`` until the wizard
    finalizes, so the endpoint cannot be reached after install.

    Error codes mirror ``_PING_KIND_TO_STATUS``:
      * 401 — key was rejected (invalid / expired)
      * 429 — provider returned quota exhausted
      * 504 — network unreachable / timeout
      * 400 — bad request shape (e.g. Azure w/o base_url)
      * 502 — provider 5xx
    """
    provider = req.provider.strip().lower()
    if provider not in _secrets.SUPPORTED_PROVIDERS:
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={
                "detail": _secrets.clear_message(
                    "bad_request",
                    req.provider or "<empty>",
                    f"unsupported provider — valid: {list(_secrets.SUPPORTED_PROVIDERS)}",
                ),
                "kind": "bad_request",
            },
        )

    api_key = req.api_key.strip()
    if provider != "ollama" and not api_key:
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={
                "detail": _secrets.clear_message(
                    "key_invalid",
                    provider,
                    "no API key provided — paste the key from the provider dashboard",
                ),
                "kind": "key_invalid",
            },
        )
    if provider == "azure" and not req.base_url.strip():
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={
                "detail": _secrets.clear_message(
                    "bad_request",
                    "Azure OpenAI",
                    "endpoint (base_url) is required — e.g. "
                    "https://<resource>.openai.azure.com",
                ),
                "kind": "bad_request",
            },
        )

    try:
        ping = await _secrets.ping_provider(
            provider,
            api_key=api_key,
            base_url=req.base_url,
            azure_deployment=req.azure_deployment,
        )
    except _secrets.ProviderPingError as exc:
        status_code = _PING_KIND_TO_STATUS.get(exc.kind, 502)
        logger.info(
            "bootstrap: llm-provision ping failed for provider=%s kind=%s (%s)",
            provider, exc.kind, exc.message,
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": exc.message, "kind": exc.kind},
        )

    # Ping succeeded — persist the credential and flip settings.
    record = _secrets.set_provider_credentials(
        provider,
        api_key=api_key,
        model=req.model,
        base_url=req.base_url,
        azure_deployment=req.azure_deployment,
    )
    _settings.llm_provider = provider
    if req.model.strip():
        _settings.llm_model = req.model.strip()

    # Mark the wizard step + emit an audit row. Actor is anonymous
    # (wizard runs pre-login); the audit row still captures the event.
    try:
        await _boot.record_bootstrap_step(
            _boot.STEP_LLM_PROVIDER,
            actor_user_id=None,
            metadata={
                "provider": provider,
                "model": record["model"] or _settings.get_model_name(),
                "fingerprint": record["fingerprint"],
                "base_url": record["base_url"],
                "latency_ms": ping["latency_ms"],
            },
        )
    except Exception as exc:
        logger.warning("bootstrap: record_bootstrap_step(llm_provider) failed: %s", exc)

    try:
        await audit.log(
            action="bootstrap.llm_provisioned",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_LLM_PROVIDER,
            before=None,
            after={
                "provider": provider,
                "model": record["model"],
                "fingerprint": record["fingerprint"],
                "base_url": record["base_url"],
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.llm_provisioned audit emit failed: %s", exc)

    logger.info(
        "bootstrap: LLM provider provisioned — provider=%s model=%s fp=%s latency=%dms",
        provider,
        record["model"] or _settings.get_model_name(),
        record["fingerprint"],
        ping["latency_ms"],
    )

    return LlmProvisionResponse(
        status="provisioned",
        provider=provider,
        model=record["model"] or _settings.get_model_name(),
        fingerprint=record["fingerprint"],
        latency_ms=ping["latency_ms"],
        models=list(ping.get("models", []) or []),
    )


class OllamaDetectResponse(BaseModel):
    """Response for read-only Ollama reachability probe.

    ``reachable`` is true iff ``GET {base_url}/api/tags`` returned 200.
    ``models`` carries the names Ollama reported (may be empty if the
    host has no models pulled yet). ``kind`` is one of the
    :class:`backend.llm_secrets.ProviderPingError` classifications on
    failure, or empty string on success.
    """

    reachable: bool
    base_url: str
    latency_ms: int
    models: list[str] = Field(default_factory=list)
    kind: str = ""
    detail: str = ""


@router.get("/ollama-detect", response_model=OllamaDetectResponse)
async def bootstrap_ollama_detect(base_url: str = "") -> OllamaDetectResponse:
    """Probe a local Ollama daemon before the operator commits.

    Read-only companion to :func:`bootstrap_llm_provision` — used by the
    wizard's L3 Step 2 when the operator picks "Ollama (local)". Hits
    ``GET {base_url}/api/tags`` (default ``http://localhost:11434``) and
    reports reachability + available models so the UI can render a
    model dropdown without writing any state.

    This endpoint never persists credentials, never touches
    ``bootstrap_state``, and never emits an audit row — it is a pure
    probe. The ``provision`` call is still the single writer.

    Response is always 200; the ``reachable`` boolean + ``kind`` field
    carry the outcome so the UI does not have to parse HTTP status codes
    for a UX affordance.
    """
    target = (base_url or "").strip() or "http://localhost:11434"
    try:
        info = await _secrets.ping_provider("ollama", base_url=target)
    except _secrets.ProviderPingError as exc:
        logger.info(
            "bootstrap: ollama-detect probe failed base_url=%s kind=%s (%s)",
            target, exc.kind, exc.message,
        )
        return OllamaDetectResponse(
            reachable=False,
            base_url=target,
            latency_ms=0,
            models=[],
            kind=exc.kind,
            detail=exc.message,
        )

    return OllamaDetectResponse(
        reachable=True,
        base_url=target,
        latency_ms=int(info.get("latency_ms", 0)),
        models=list(info.get("models", []) or []),
        kind="",
        detail="",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L4 — Step 3 (Cloudflare Tunnel: skip / LAN-only audit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CfTunnelSkipRequest(BaseModel):
    """Request body for the wizard's Step 3 ``skip tunnel`` transition.

    The operator is explicitly asserting "this install is LAN-only / I
    don't want remote access right now" — the wizard records the skip
    so the finalize gate can go green, but audit captures the operator
    intent with warning severity so it never looks like a silent bypass.
    """

    reason: str = Field(
        default="",
        max_length=500,
        description="Optional free-text note stored with the audit row.",
    )


class CfTunnelSkipResponse(BaseModel):
    status: str
    cf_tunnel_configured: bool


@router.post("/cf-tunnel-skip", response_model=CfTunnelSkipResponse)
async def bootstrap_cf_tunnel_skip(req: CfTunnelSkipRequest) -> CfTunnelSkipResponse:
    """Mark CF tunnel as intentionally skipped (LAN-only deployment).

    Unauthenticated like the other wizard steps. Two side-effects:

      1. Writes ``cf_tunnel_skipped=true`` to the bootstrap marker and
         records ``STEP_CF_TUNNEL`` in ``bootstrap_state`` with
         ``metadata.skipped=true`` so :func:`missing_required_steps`
         clears the step for finalize.
      2. Emits an audit row ``bootstrap.cf_tunnel_skipped`` with warning
         severity — the operator chose LAN-only on purpose, but the
         trail must show who took that call and when.
    """
    reason = (req.reason or "").strip()
    _boot.mark_cf_tunnel(skipped=True)
    try:
        await _boot.record_bootstrap_step(
            _boot.STEP_CF_TUNNEL,
            actor_user_id=None,
            metadata={"skipped": True, "reason": reason, "source": "wizard"},
        )
    except Exception as exc:
        logger.warning("bootstrap: record_bootstrap_step(cf_tunnel skip) failed: %s", exc)

    try:
        await audit.log(
            action="bootstrap.cf_tunnel_skipped",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_CF_TUNNEL,
            before=None,
            after={
                "skipped": True,
                "reason": reason,
                "severity": "warning",
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.cf_tunnel_skipped audit emit failed: %s", exc)

    logger.warning(
        "bootstrap: cf_tunnel step SKIPPED (LAN-only) via wizard — reason=%r",
        reason or "<none>",
    )
    return CfTunnelSkipResponse(status="skipped", cf_tunnel_configured=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L5 — Step 4 (service start — systemd / docker compose / dev)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


DeployMode = Literal["systemd", "docker-compose", "dev"]

_SYSTEMD_UNITS: tuple[str, ...] = (
    "omnisight-backend.service",
    "omnisight-frontend.service",
)

# Compose file the docker-compose mode starts. Overridable via env so
# deployments that ship a different filename don't have to patch source.
_DEFAULT_COMPOSE_FILE = "docker-compose.prod.yml"
_START_TIMEOUT_SECS = 120


def _detect_deploy_mode() -> DeployMode:
    """Pick the launch strategy based on what's available on the host.

    Delegates to :func:`backend.deploy_mode.detect_deploy_mode` — the
    L7 richer probe inspects ``/.dockerenv``, ``/proc/1/cgroup``,
    ``/run/systemd/system``, and ``/var/run/docker.sock`` in addition
    to the PATH lookups the skeleton used. This wrapper exists because
    the Step 4 launcher only needs the mode string; the full detection
    record (with ``reason`` + per-probe signals) is exposed via
    :mod:`backend.deploy_mode` for the wizard UI / audit trail.
    """
    from backend.deploy_mode import detect_deploy_mode as _detect_full

    return _detect_full().mode


class StartServicesRequest(BaseModel):
    """Body for ``POST /bootstrap/start-services``.

    ``mode`` overrides the auto-detection when the operator needs to pin
    a specific launch strategy (e.g. CI forcing ``dev``). ``compose_file``
    is passed through to ``docker compose -f`` in docker-compose mode.
    """

    mode: str = Field(
        default="",
        max_length=32,
        description="Deploy mode override: systemd / docker-compose / dev. "
                    "Empty → auto-detect.",
    )
    compose_file: str = Field(
        default="",
        max_length=256,
        description="Override path for docker compose file "
                    "(defaults to docker-compose.prod.yml).",
    )


class StartServicesResponse(BaseModel):
    status: str
    mode: DeployMode
    command: list[str]
    returncode: int
    stdout_tail: str
    stderr_tail: str


def _tail(text: str, limit: int = 4000) -> str:
    """Return the last ``limit`` chars of *text* (for the HTTP response)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _start_command(mode: DeployMode, compose_file: str) -> list[str]:
    """Build the argv for the chosen deploy mode.

    Kept separate so tests can assert command shape without having to
    actually exec the subprocess. ``dev`` returns an empty list — the
    endpoint short-circuits and never exec's anything.
    """
    if mode == "systemd":
        return ["systemctl", "start", *_SYSTEMD_UNITS]
    if mode == "docker-compose":
        cf = (compose_file or "").strip() or _DEFAULT_COMPOSE_FILE
        return ["docker", "compose", "-f", cf, "up", "-d"]
    return []


@router.post("/start-services", response_model=StartServicesResponse)
async def bootstrap_start_services(
    req: StartServicesRequest | None = None,
) -> StartServicesResponse:
    """Launch the OmniSight services for the wizard's Step 4.

    Dispatches by deploy mode:
      * ``systemd`` → ``systemctl start omnisight-backend omnisight-frontend``
      * ``docker-compose`` → ``docker compose -f <file> up -d``
      * ``dev`` → no-op (processes are already running under uvicorn /
        next-dev); the endpoint returns ``status="already_running"``.

    Unauthenticated like the other wizard endpoints — during the wizard
    there is no admin session yet and the bootstrap-gate middleware
    limits who can reach ``/bootstrap/*`` before finalize.

    Side-effects:
      * ``logger.info`` with the full argv
      * audit row ``bootstrap.start_services`` capturing mode + return
        code so a failed start has a traceable fingerprint.

    HTTP contract:
      * 200 on success (returncode==0, or dev no-op)
      * 502 when the launcher exited non-zero — stdout/stderr tails are
        echoed back in the body so the SSE-log follow-up (next checkbox)
        has a first point of reference.
      * 504 on timeout after ``_START_TIMEOUT_SECS``.
    """
    body = req or StartServicesRequest()
    override_mode = body.mode.strip().lower()
    if override_mode:
        if override_mode not in ("systemd", "docker-compose", "dev"):
            return JSONResponse(  # type: ignore[return-value]
                status_code=422,
                content={
                    "detail": (
                        "mode must be one of: systemd, docker-compose, dev — "
                        f"got {override_mode!r}"
                    ),
                },
            )
        mode: DeployMode = override_mode  # type: ignore[assignment]
    else:
        mode = _detect_deploy_mode()

    command = _start_command(mode, body.compose_file)

    if mode == "dev":
        logger.info(
            "bootstrap: start-services skipped — mode=dev "
            "(uvicorn / next dev already running)"
        )
        try:
            await audit.log(
                action="bootstrap.start_services",
                entity_kind="bootstrap",
                entity_id="start_services",
                before=None,
                after={"mode": mode, "command": [], "returncode": 0,
                       "status": "already_running"},
                actor="wizard",
            )
        except Exception as exc:
            logger.debug("bootstrap.start_services audit emit failed: %s", exc)
        return StartServicesResponse(
            status="already_running",
            mode=mode,
            command=[],
            returncode=0,
            stdout_tail="",
            stderr_tail="",
        )

    logger.info("bootstrap: start-services mode=%s cmd=%s", mode, command)

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_START_TIMEOUT_SECS,
        )
        returncode = proc.returncode or 0
    except asyncio.TimeoutError:
        logger.error(
            "bootstrap: start-services TIMEOUT mode=%s after %ds",
            mode, _START_TIMEOUT_SECS,
        )
        try:
            await audit.log(
                action="bootstrap.start_services",
                entity_kind="bootstrap",
                entity_id="start_services",
                before=None,
                after={"mode": mode, "command": command,
                       "status": "timeout",
                       "timeout_secs": _START_TIMEOUT_SECS},
                actor="wizard",
            )
        except Exception as exc:
            logger.debug("bootstrap.start_services audit emit failed: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=504,
            content={
                "detail": (
                    f"launcher did not finish within {_START_TIMEOUT_SECS}s — "
                    "check host for stuck systemctl / docker-compose"
                ),
                "mode": mode,
                "command": command,
            },
        )
    except FileNotFoundError as exc:
        logger.error("bootstrap: start-services binary missing: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=502,
            content={
                "detail": (
                    f"launcher binary not found: {exc} — expected {command[0]!r} "
                    f"on PATH for mode={mode}"
                ),
                "mode": mode,
                "command": command,
            },
        )

    stdout_tail = _tail(stdout_b.decode(errors="replace"))
    stderr_tail = _tail(stderr_b.decode(errors="replace"))

    try:
        await audit.log(
            action="bootstrap.start_services",
            entity_kind="bootstrap",
            entity_id="start_services",
            before=None,
            after={
                "mode": mode,
                "command": command,
                "returncode": returncode,
                "status": "started" if returncode == 0 else "failed",
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.start_services audit emit failed: %s", exc)

    if returncode != 0:
        logger.error(
            "bootstrap: start-services FAILED mode=%s rc=%d stderr=%r",
            mode, returncode, stderr_tail[-400:],
        )
        return JSONResponse(  # type: ignore[return-value]
            status_code=502,
            content={
                "detail": (
                    f"launcher exited with code {returncode} — "
                    "see stderr_tail for the failure reason"
                ),
                "mode": mode,
                "command": command,
                "returncode": returncode,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
        )

    logger.info(
        "bootstrap: start-services OK mode=%s rc=%d",
        mode, returncode,
    )
    return StartServicesResponse(
        status="started",
        mode=mode,
        command=command,
        returncode=returncode,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L5 — Step 4 (SSE stream — tail systemd / docker logs into the UI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# How long the SSE stream will tail logs before closing cleanly. The
# wizard UI only needs to watch services start (G1 /readyz polling caps
# at 180s), so a little headroom keeps idle connections from piling up
# without cutting off the operator mid-boot.
_TICK_STREAM_MAX_SECS = 300
# Heartbeat sent when there is no log activity so the EventSource
# connection stays alive behind any intermediary proxies.
_TICK_HEARTBEAT_SECS = 10.0


def _tick_command(mode: DeployMode, compose_file: str, tail: int) -> list[str]:
    """Build the argv used to tail service logs for the wizard.

    ``systemd``      → ``journalctl -u <unit>... --follow --lines <tail>
                         --output short-iso --no-pager``
    ``docker-compose`` → ``docker compose -f <file> logs --follow --tail <tail>``
    ``dev``          → empty list (the endpoint does not exec anything;
                         dev already runs under uvicorn / next dev and has
                         no managed units to tail).
    """
    lines = max(int(tail or 0), 0)
    if mode == "systemd":
        cmd = ["journalctl"]
        for unit in _SYSTEMD_UNITS:
            cmd.extend(["-u", unit])
        cmd.extend([
            "--follow",
            "--lines", str(lines),
            "--output", "short-iso",
            "--no-pager",
        ])
        return cmd
    if mode == "docker-compose":
        cf = (compose_file or "").strip() or _DEFAULT_COMPOSE_FILE
        return [
            "docker", "compose", "-f", cf, "logs",
            "--follow", "--tail", str(lines), "--no-color",
        ]
    return []


def _pack_tick(line: str, stream: str, seq: int) -> dict:
    """Shape a single log line as an SSE ``bootstrap.service.tick`` event."""
    return {
        "event": "bootstrap.service.tick",
        "data": json.dumps({
            "line": line,
            "stream": stream,
            "seq": seq,
            "ts": time.time(),
        }),
    }


async def _drain_stream(
    stream,
    kind: str,
    queue: "asyncio.Queue[tuple[str, str]]",
) -> None:
    """Copy each decoded line from *stream* into *queue* as ``(kind, line)``.

    Exits cleanly on EOF (process closed its pipe) so the generator
    downstream can distinguish "no more output" from "heartbeat timeout".
    """
    if stream is None:
        return
    try:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            await queue.put((kind, raw.decode(errors="replace").rstrip("\n")))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover — defensive, readline rarely raises
        logger.debug("bootstrap: tick drain(%s) failed: %s", kind, exc)


@router.get("/service-tick")
async def bootstrap_service_tick(
    request: Request,
    mode: str = "",
    compose_file: str = "",
    tail: int = 50,
    max_seconds: int = _TICK_STREAM_MAX_SECS,
):
    """SSE stream: pipe live service logs into the wizard's Step 4 UI.

    Counterpart to ``POST /bootstrap/start-services``. Once the launcher
    returns, the UI opens this EventSource to watch the services come
    up in real time. Each decoded log line becomes a
    ``bootstrap.service.tick`` event carrying ``{line, stream, seq, ts}``.

    Mode dispatch mirrors ``_detect_deploy_mode`` (systemd / docker-compose
    / dev). ``dev`` emits a single informational tick and closes so the
    UI doesn't hang waiting for output on a dev box with no managed
    units.

    Lifecycle events:
      * ``start`` — mode + command + pid (once, at the top)
      * ``bootstrap.service.tick`` — one per log line (stdout + stderr
        are interleaved in source order with a monotonically increasing
        ``seq`` so the UI can render a stable transcript)
      * ``heartbeat`` — every ~10s of silence to keep the connection
        alive through proxies
      * ``done`` — when the tailer exits OR ``max_seconds`` elapses OR
        the client disconnects

    Query parameters:
      * ``mode`` — systemd / docker-compose / dev (empty → auto-detect)
      * ``compose_file`` — override for docker compose mode
      * ``tail`` — historical lines to replay before following (default 50)
      * ``max_seconds`` — upper bound on stream duration (default 300s)

    Kept unauthenticated like the rest of ``/bootstrap/*`` so the wizard
    can stream without an admin session. The bootstrap-gate middleware
    still blocks access after finalize.
    """
    override_mode = (mode or "").strip().lower()
    if override_mode:
        if override_mode not in ("systemd", "docker-compose", "dev"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        "mode must be one of: systemd, docker-compose, dev — "
                        f"got {override_mode!r}"
                    ),
                },
            )
        active_mode: DeployMode = override_mode  # type: ignore[assignment]
    else:
        active_mode = _detect_deploy_mode()

    command = _tick_command(active_mode, compose_file, tail)
    deadline = time.monotonic() + max(int(max_seconds or 0), 1)

    async def event_generator():
        seq = 0
        # ── dev mode: surface one informational tick and close ──────
        if active_mode == "dev":
            yield {
                "event": "start",
                "data": json.dumps({
                    "mode": active_mode,
                    "command": [],
                    "pid": None,
                    "tail": int(tail or 0),
                }),
            }
            yield _pack_tick(
                "dev mode — services run under uvicorn / next dev, "
                "no managed units to tail",
                "info",
                0,
            )
            yield {
                "event": "done",
                "data": json.dumps({
                    "mode": active_mode,
                    "reason": "dev_noop",
                    "returncode": 0,
                }),
            }
            return

        # ── systemd / docker-compose: exec the tailer and stream ───
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            logger.error("bootstrap: service-tick launcher missing: %s", exc)
            yield {
                "event": "error",
                "data": json.dumps({
                    "detail": (
                        f"tailer binary not found: {exc} — expected "
                        f"{command[0]!r} on PATH for mode={active_mode}"
                    ),
                    "mode": active_mode,
                    "command": command,
                }),
            }
            yield {
                "event": "done",
                "data": json.dumps({
                    "mode": active_mode,
                    "reason": "launcher_missing",
                    "returncode": None,
                }),
            }
            return

        yield {
            "event": "start",
            "data": json.dumps({
                "mode": active_mode,
                "command": command,
                "pid": proc.pid,
                "tail": int(tail or 0),
            }),
        }
        logger.info(
            "bootstrap: service-tick streaming mode=%s pid=%s cmd=%s",
            active_mode, proc.pid, command,
        )

        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=512)
        drain_out = asyncio.create_task(
            _drain_stream(proc.stdout, "stdout", queue)
        )
        drain_err = asyncio.create_task(
            _drain_stream(proc.stderr, "stderr", queue)
        )
        drains = {drain_out, drain_err}

        reason = "eof"
        returncode: int | None = None
        try:
            while True:
                if await request.is_disconnected():
                    reason = "client_disconnect"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "max_seconds"
                    break
                timeout = min(_TICK_HEARTBEAT_SECS, remaining)
                try:
                    kind, line = await asyncio.wait_for(
                        queue.get(), timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    # No output — if both drains ended AND queue is empty
                    # the process is done, so break; otherwise heartbeat.
                    if all(d.done() for d in drains) and queue.empty():
                        reason = "eof"
                        break
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"ts": time.time()}),
                    }
                    continue
                seq += 1
                yield _pack_tick(line, kind, seq)
        finally:
            for d in drains:
                d.cancel()
            for d in drains:
                try:
                    await d
                except (asyncio.CancelledError, Exception):
                    pass
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    returncode = await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        returncode = await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        returncode = None
            else:
                returncode = proc.returncode

        logger.info(
            "bootstrap: service-tick closed mode=%s reason=%s rc=%s seq=%d",
            active_mode, reason, returncode, seq,
        )
        yield {
            "event": "done",
            "data": json.dumps({
                "mode": active_mode,
                "reason": reason,
                "returncode": returncode,
                "lines": seq,
            }),
        }

    return EventSourceResponse(event_generator())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L5 — Step 4 (poll G1 /readyz until green or 180s elapsed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# After ``/bootstrap/start-services`` exec's systemctl / docker-compose
# and the SSE tick stream replays the log lines, the wizard needs a
# deterministic "it's up" signal so Step 4 can flip green and move on
# to the smoke-test step. The readiness probe is owned by G1
# (``/healthz`` = liveness, ``/readyz`` = readiness — DB + migration +
# provider chain). This endpoint is the UI-facing side of that wait:
# it polls ``GET /readyz`` every ``interval_secs`` for up to
# ``timeout_secs`` and reports a single structured outcome.
#
# While G1 is still rolling out the dedicated ``/readyz`` route, the
# probe transparently falls back to the already-shipped ``/healthz``
# endpoint on a 404 so wizards on older backends still converge.


_WAIT_READY_DEFAULT_TIMEOUT_SECS = 180.0
_WAIT_READY_DEFAULT_INTERVAL_SECS = 2.0
_WAIT_READY_MAX_TIMEOUT_SECS = 600.0
# Individual probe timeout — a hung /readyz shouldn't starve the
# overall wait. Kept short so 180s still yields ~90 probe opportunities
# even when every request hits the ceiling.
_WAIT_READY_PROBE_TIMEOUT_SECS = 3.0


def _default_readyz_url() -> str:
    """Pick the URL the wizard should poll for readiness.

    Resolution order:
      1. ``OMNISIGHT_READYZ_URL`` env var (explicit operator override).
      2. ``http://127.0.0.1:<OMNISIGHT_PORT or 8000>{api_prefix}/readyz``.

    ``api_prefix`` comes from :mod:`backend.config` so the URL matches
    whatever the backend is mounted at (``/api/v1`` by default).
    """
    explicit = (os.environ.get("OMNISIGHT_READYZ_URL") or "").strip()
    if explicit:
        return explicit
    port = (os.environ.get("OMNISIGHT_PORT") or "").strip() or "8000"
    prefix = (_settings.api_prefix or "").rstrip("/")
    return f"http://127.0.0.1:{port}{prefix}/readyz"


class WaitReadyRequest(BaseModel):
    """Body for ``POST /bootstrap/wait-ready``.

    ``timeout_secs`` caps the total wait; ``interval_secs`` is the gap
    between probes. ``url`` overrides the default readyz target (useful
    when the wizard runs behind a reverse proxy or on a non-standard
    port). ``fallback_healthz`` keeps older backends (no ``/readyz`` yet)
    working: on a 404 the probe swaps the suffix to ``/healthz`` once.
    """

    timeout_secs: float = Field(
        default=_WAIT_READY_DEFAULT_TIMEOUT_SECS,
        ge=0.05, le=_WAIT_READY_MAX_TIMEOUT_SECS,
        description="Upper bound on total wait time (default 180s).",
    )
    interval_secs: float = Field(
        default=_WAIT_READY_DEFAULT_INTERVAL_SECS,
        ge=0.05, le=30.0,
        description="Seconds to sleep between probes.",
    )
    url: str = Field(
        default="", max_length=1024,
        description="Optional override for the readyz URL.",
    )
    fallback_healthz: bool = Field(
        default=True,
        description=(
            "If the readyz URL 404s, swap the suffix to /healthz and keep "
            "polling. Lets wizards on pre-G1 backends still converge."
        ),
    )


WaitReadyReason = Literal["ready", "timeout", "connection_error"]


class WaitReadyResponse(BaseModel):
    ready: bool
    url: str
    attempts: int
    elapsed_ms: int
    last_status_code: int | None = None
    last_error: str | None = None
    reason: WaitReadyReason
    fallback_applied: bool = False


async def _probe_ready_once(
    url: str,
    *,
    timeout_secs: float = _WAIT_READY_PROBE_TIMEOUT_SECS,
) -> tuple[int | None, str | None]:
    """Single GET probe — returns ``(status_code, error)``.

    On transport failure (connect refused, DNS, timeout) ``status_code``
    is None and ``error`` carries a short ``<ExcName>: <msg>`` string.
    Kept as a module-level coroutine so tests can monkeypatch it to
    sequence probe outcomes without spinning up a real server.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            resp = await client.get(url)
        return resp.status_code, None
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    except Exception as exc:  # pragma: no cover — defensive
        return None, f"{type(exc).__name__}: {exc}"[:200]


@router.post("/wait-ready", response_model=WaitReadyResponse)
async def bootstrap_wait_ready(
    req: WaitReadyRequest | None = None,
) -> WaitReadyResponse:
    """Block until the backend's readyz probe goes green or 180s elapses.

    The wizard's Step 4 kicks services via ``start-services``, streams
    their logs via ``service-tick``, and finally calls this endpoint to
    get one deterministic boolean: did the stack actually come up? We
    poll ``GET {url}`` every ``interval_secs`` and return as soon as any
    probe reports a 2xx. If no probe succeeds within ``timeout_secs`` we
    return ``ready=false`` with ``reason=timeout`` (or
    ``connection_error`` if every probe failed at the transport layer —
    the distinction matters for UX: a misconfigured URL vs. services
    that are still booting).

    Lives under ``/bootstrap/*`` so the gate middleware lets it through
    before finalize. Unauthenticated like every other wizard step.

    Response is always HTTP 200 — the polling itself completed, the
    ``ready`` boolean carries the outcome so the UI can render a
    green/red check without parsing error bodies.
    """
    body = req or WaitReadyRequest()
    url = (body.url or "").strip() or _default_readyz_url()
    started = time.monotonic()
    deadline = started + body.timeout_secs

    attempts = 0
    last_status: int | None = None
    last_error: str | None = None
    fallback_applied = False

    while True:
        attempts += 1
        status, err = await _probe_ready_once(url)
        last_status = status
        last_error = err

        if status is not None and 200 <= status < 300:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "bootstrap: wait-ready GREEN url=%s attempts=%d elapsed=%dms",
                url, attempts, elapsed_ms,
            )
            try:
                await audit.log(
                    action="bootstrap.wait_ready",
                    entity_kind="bootstrap",
                    entity_id="wait_ready",
                    before=None,
                    after={
                        "url": url,
                        "attempts": attempts,
                        "elapsed_ms": elapsed_ms,
                        "reason": "ready",
                        "last_status_code": status,
                        "fallback_applied": fallback_applied,
                    },
                    actor="wizard",
                )
            except Exception as exc:
                logger.debug("bootstrap.wait_ready audit emit failed: %s", exc)
            return WaitReadyResponse(
                ready=True,
                url=url,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
                last_status_code=status,
                last_error=None,
                reason="ready",
                fallback_applied=fallback_applied,
            )

        # G1 is still landing /readyz on some backends. If we get a 404
        # on a /readyz suffix, retry on /healthz (same probe shape,
        # already shipped) exactly once so older stacks still converge.
        if (
            status == 404
            and body.fallback_healthz
            and not fallback_applied
            and url.endswith("/readyz")
        ):
            new_url = url[: -len("/readyz")] + "/healthz"
            logger.info(
                "bootstrap: wait-ready got 404 on %s — falling back to %s",
                url, new_url,
            )
            url = new_url
            fallback_applied = True
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(body.interval_secs, remaining))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    reason: WaitReadyReason = (
        "connection_error"
        if (last_status is None and last_error is not None)
        else "timeout"
    )
    logger.warning(
        "bootstrap: wait-ready NOT READY url=%s attempts=%d elapsed=%dms "
        "reason=%s last_status=%s err=%s",
        url, attempts, elapsed_ms, reason, last_status, last_error,
    )
    try:
        await audit.log(
            action="bootstrap.wait_ready",
            entity_kind="bootstrap",
            entity_id="wait_ready",
            before=None,
            after={
                "url": url,
                "attempts": attempts,
                "elapsed_ms": elapsed_ms,
                "reason": reason,
                "last_status_code": last_status,
                "last_error": last_error,
                "fallback_applied": fallback_applied,
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.wait_ready audit emit failed: %s", exc)

    return WaitReadyResponse(
        ready=False,
        url=url,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        last_status_code=last_status,
        last_error=last_error,
        reason=reason,
        fallback_applied=fallback_applied,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L5 — Step 4 (parallel health check: 4 gates in a single response)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The wizard's Step 4 shows four checkboxes that must all flip green
# before the operator can move on: backend ready / frontend ready /
# DB migration up-to-date / CF tunnel connector online. ``wait-ready``
# already answers the "is backend up" question, but Step 4 needs all
# four signals in one deterministic call so the UI can light each tick
# from the same server observation — separate endpoints would race each
# other and produce split-brain states in the UI.
#
# The CF tunnel check is conditional: if the operator explicitly skipped
# Step 3 (LAN-only deployment), ``cf_tunnel`` reports ``skipped`` rather
# than red — skipped still counts as a green tick from the wizard's POV
# because nothing is broken, the operator just doesn't want a tunnel.

_PARALLEL_CHECK_DEFAULT_TIMEOUT_SECS = 5.0
_PARALLEL_CHECK_MAX_TIMEOUT_SECS = 30.0
_PARALLEL_CHECK_PROBE_TIMEOUT_SECS = 3.0


def _default_healthz_url() -> str:
    """Pick the URL for the backend /healthz probe.

    Mirrors :func:`_default_readyz_url` but suffixes ``/healthz`` so
    the backend-ready check doesn't depend on the newer ``/readyz``
    route (which is still landing on some builds). ``/healthz`` is
    already wired up in ``backend.routers.observability``.
    """
    explicit = (os.environ.get("OMNISIGHT_HEALTHZ_URL") or "").strip()
    if explicit:
        return explicit
    port = (os.environ.get("OMNISIGHT_PORT") or "").strip() or "8000"
    prefix = (_settings.api_prefix or "").rstrip("/")
    return f"http://127.0.0.1:{port}{prefix}/healthz"


def _default_frontend_url() -> str:
    """Pick the URL for the frontend readiness probe.

    Resolution order:
      1. ``OMNISIGHT_FRONTEND_URL`` env var (explicit operator override).
      2. ``settings.frontend_origin`` (the same origin CORS trusts).
    Any trailing slash is stripped so the probe hits the bare root URL
    — the Next.js server responds with 200 on ``/`` once it's ready.
    """
    explicit = (os.environ.get("OMNISIGHT_FRONTEND_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    origin = (getattr(_settings, "frontend_origin", "") or "").strip()
    return (origin or "http://localhost:3000").rstrip("/")


CheckStatus = Literal["green", "red", "skipped"]


class CheckResult(BaseModel):
    """One of the four parallel probes' outcome.

    ``status`` is tri-state: ``skipped`` means the probe was not
    applicable (e.g. CF tunnel when the operator chose LAN-only). The
    wizard UI treats ``green`` and ``skipped`` both as a green check;
    only ``red`` keeps the step gated.
    """

    ok: bool
    status: CheckStatus
    detail: str | None = None
    latency_ms: int | None = None


class ParallelHealthCheckRequest(BaseModel):
    """Body for ``POST /bootstrap/parallel-health-check``.

    Every field is optional so the wizard can fire a bare POST on
    default installations — operator overrides are only needed when
    running behind a reverse proxy or in docker-compose with non-default
    service names.
    """

    timeout_secs: float = Field(
        default=_PARALLEL_CHECK_DEFAULT_TIMEOUT_SECS,
        ge=0.1, le=_PARALLEL_CHECK_MAX_TIMEOUT_SECS,
        description="Per-probe hard timeout (default 5s).",
    )
    backend_url: str = Field(
        default="", max_length=1024,
        description="Optional override for the backend /healthz URL.",
    )
    frontend_url: str = Field(
        default="", max_length=1024,
        description="Optional override for the frontend readiness URL.",
    )


class ParallelHealthCheckResponse(BaseModel):
    """Aggregated result of the four Step-4 probes.

    ``all_green`` is True iff none of the four checks reports ``red``
    (``skipped`` counts as green because nothing is actually broken).
    ``elapsed_ms`` is the wall-clock cost of the slowest probe, not the
    sum — the four checks fan out in parallel.
    """

    all_green: bool
    elapsed_ms: int
    backend: CheckResult
    frontend: CheckResult
    db_migration: CheckResult
    cf_tunnel: CheckResult


async def _check_backend_ready(url: str, timeout: float) -> CheckResult:
    """HTTP GET the backend /healthz and grade the response.

    Green on 2xx. Observability's /healthz already wraps DB ping +
    watchdog + SSE + sandbox counters, so a 2xx here means the backend
    process is fully wired up — not just listening.
    """
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        elapsed = int((time.monotonic() - started) * 1000)
        if 200 <= resp.status_code < 300:
            return CheckResult(ok=True, status="green", latency_ms=elapsed)
        return CheckResult(
            ok=False, status="red",
            detail=f"HTTP {resp.status_code}",
            latency_ms=elapsed,
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"{type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # pragma: no cover — defensive
        return CheckResult(
            ok=False, status="red",
            detail=f"{type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )


async def _check_frontend_ready(url: str, timeout: float) -> CheckResult:
    """Probe the Next.js server root.

    A Next.js dev/prod server answers ``GET /`` with 200 once compiled.
    We accept anything <500 as "reachable" so redirect-based login gates
    (e.g. 302 → /login) still count as ready — the test is "is the
    frontend process serving responses", not "is the landing page
    fully rendered".
    """
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            resp = await client.get(url)
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code < 500:
            return CheckResult(ok=True, status="green", latency_ms=elapsed)
        return CheckResult(
            ok=False, status="red",
            detail=f"HTTP {resp.status_code}",
            latency_ms=elapsed,
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"{type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # pragma: no cover — defensive
        return CheckResult(
            ok=False, status="red",
            detail=f"{type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )


# Columns backend/db.py:_migrate treats as load-bearing invariants
# (see the ``REQUIRED`` set in db._migrate). If any of these is missing
# the schema is out of date and the rest of the app will IntegrityError
# on the first insert.
_DB_MIGRATION_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tasks", "npi_phase_id"),
    ("agents", "sub_type"),
    ("users", "must_change_password"),
    ("users", "tenant_id"),
    ("bootstrap_state", "step"),
)


async def _check_db_migration() -> CheckResult:
    """Verify the load-bearing migration invariants against PRAGMA.

    We don't use Alembic for application schema (``backend.db._migrate``
    runs at startup with ALTER/CREATE IF NOT EXISTS). The ready signal
    is therefore "do the columns that the runtime hard-depends on
    exist" — lifted from ``db._migrate``'s own ``REQUIRED`` set plus a
    couple of columns every wizard step touches.
    """
    started = time.monotonic()
    try:
        from backend import db as _db

        conn = _db._conn()
    except Exception as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"db not ready: {type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    missing: list[str] = []
    for table, column in _DB_MIGRATION_REQUIRED_COLUMNS:
        try:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
        except Exception as exc:
            return CheckResult(
                ok=False, status="red",
                detail=f"PRAGMA {table} failed: {type(exc).__name__}: {exc}"[:200],
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        if column not in cols:
            missing.append(f"{table}.{column}")

    elapsed = int((time.monotonic() - started) * 1000)
    if missing:
        return CheckResult(
            ok=False, status="red",
            detail="missing columns: " + ", ".join(missing),
            latency_ms=elapsed,
        )
    return CheckResult(
        ok=True, status="green",
        detail=f"{len(_DB_MIGRATION_REQUIRED_COLUMNS)} invariants present",
        latency_ms=elapsed,
    )


async def _check_cf_tunnel(timeout: float) -> CheckResult:
    """Check Cloudflare tunnel connector status — or report skipped.

    Three outcomes:
      * ``skipped`` — operator explicitly skipped Step 3 (LAN-only).
        Counts as a green tick since nothing is broken.
      * ``green`` — tunnel is provisioned AND at least one connector
        reports ``is_pending_reconnect=False`` against the CF API.
      * ``red`` — tunnel expected but connector offline / CF API error.

    Also returns ``skipped`` if Step 3 has not run at all yet — the
    parallel-health-check is a probe, not a gate, and missing-step
    state is already surfaced by ``GET /bootstrap/status``.
    """
    started = time.monotonic()
    marker = _boot._read_marker()
    if marker.get("cf_tunnel_skipped") is True:
        return CheckResult(
            ok=True, status="skipped",
            detail="operator skipped Step 3 (LAN-only)",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        from backend.routers import cloudflare_tunnel as _cft
    except Exception as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"cf-tunnel module unavailable: {type(exc).__name__}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    state = _cft._get_state()
    tunnel_id = state.get("tunnel_id")
    if not tunnel_id:
        if marker.get("cf_tunnel_configured") is True:
            return CheckResult(
                ok=False, status="red",
                detail="marker says configured but router has no tunnel_id",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return CheckResult(
            ok=True, status="skipped",
            detail="Step 3 not yet run",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    account_id = state.get("account_id", "")
    tunnel_name = state.get("tunnel_name")
    try:
        client = _cft._client_from_stored()
    except Exception as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"cf client init failed: {type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        tunnels = await asyncio.wait_for(
            client.list_tunnels(account_id, name=tunnel_name),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            ok=False, status="red",
            detail=f"cf list_tunnels timed out after {timeout}s",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        return CheckResult(
            ok=False, status="red",
            detail=f"cf list_tunnels failed: {type(exc).__name__}: {exc}"[:200],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    if not tunnels:
        return CheckResult(
            ok=False, status="red",
            detail="tunnel_id stored but not found on Cloudflare account",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    tunnel = tunnels[0]
    connections = getattr(tunnel, "connections", None) or []
    connector_online = any(
        (c.get("is_pending_reconnect") is False) for c in connections
    ) if connections else False

    elapsed = int((time.monotonic() - started) * 1000)
    if connector_online:
        return CheckResult(
            ok=True, status="green",
            detail=f"{len(connections)} connection(s), at least one online",
            latency_ms=elapsed,
        )
    return CheckResult(
        ok=False, status="red",
        detail="no cloudflared connector online (is_pending_reconnect)",
        latency_ms=elapsed,
    )


@router.post(
    "/parallel-health-check",
    response_model=ParallelHealthCheckResponse,
)
async def bootstrap_parallel_health_check(
    req: ParallelHealthCheckRequest | None = None,
) -> ParallelHealthCheckResponse:
    """Run the four Step-4 readiness probes in parallel.

    The wizard's Step 4 UI shows four checkboxes (backend / frontend /
    DB migration / CF tunnel). A single call to this endpoint returns
    the state of all four so the UI lights them from one server
    observation — no racing between independent polls. ``cf_tunnel``
    is tri-state: ``skipped`` when the operator chose LAN-only at
    Step 3, otherwise ``green`` / ``red`` against the live CF API.

    Like every other wizard endpoint this route is unauthenticated
    (the admin hasn't logged in yet) and lives under ``/bootstrap/*``
    so the gate middleware lets it through before finalize. Response
    is always HTTP 200 — per-probe status lives in the body.
    """
    body = req or ParallelHealthCheckRequest()
    timeout = min(body.timeout_secs, _PARALLEL_CHECK_PROBE_TIMEOUT_SECS * 2)
    backend_url = (body.backend_url or "").strip() or _default_healthz_url()
    frontend_url = (body.frontend_url or "").strip() or _default_frontend_url()

    overall_started = time.monotonic()

    results = await asyncio.gather(
        _check_backend_ready(backend_url, timeout),
        _check_frontend_ready(frontend_url, timeout),
        _check_db_migration(),
        _check_cf_tunnel(timeout),
        return_exceptions=True,
    )

    def _coerce(result, label: str) -> CheckResult:
        if isinstance(result, CheckResult):
            return result
        return CheckResult(
            ok=False, status="red",
            detail=f"{label} probe raised: {type(result).__name__}: {result}"[:200],
            latency_ms=None,
        )

    backend_r = _coerce(results[0], "backend")
    frontend_r = _coerce(results[1], "frontend")
    db_r = _coerce(results[2], "db_migration")
    cf_r = _coerce(results[3], "cf_tunnel")

    elapsed_ms = int((time.monotonic() - overall_started) * 1000)

    all_green = all(
        r.status != "red" for r in (backend_r, frontend_r, db_r, cf_r)
    )

    logger.info(
        "bootstrap: parallel-health-check all_green=%s backend=%s frontend=%s "
        "db_migration=%s cf_tunnel=%s elapsed=%dms",
        all_green, backend_r.status, frontend_r.status,
        db_r.status, cf_r.status, elapsed_ms,
    )

    try:
        await audit.log(
            action="bootstrap.parallel_health_check",
            entity_kind="bootstrap",
            entity_id="parallel_health_check",
            before=None,
            after={
                "all_green": all_green,
                "elapsed_ms": elapsed_ms,
                "backend_url": backend_url,
                "frontend_url": frontend_url,
                "backend": backend_r.model_dump(),
                "frontend": frontend_r.model_dump(),
                "db_migration": db_r.model_dump(),
                "cf_tunnel": cf_r.model_dump(),
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.parallel_health_check audit emit failed: %s", exc)

    return ParallelHealthCheckResponse(
        all_green=all_green,
        elapsed_ms=elapsed_ms,
        backend=backend_r,
        frontend=frontend_r,
        db_migration=db_r,
        cf_tunnel=cf_r,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L6 — Step 5 (run scripts/prod_smoke_test.py --subset dag1 in-process)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The wizard's Step 5 proves the install actually works end-to-end by
# running the compile-flash host_native DAG from
# ``scripts/prod_smoke_test.py`` (DAG #1, the ~60s subset picked so a
# fresh install doesn't also pay the aarch64 cross-compile cost). On
# top of the DAG result we verify the audit-log hash chain — if the
# chain is already corrupted this early, bootstrap is lying and the
# operator must know before finalize.
#
# Two outcomes the wizard UI needs:
#   * green — both the DAG validates + submits AND ``audit.verify_chain``
#             is intact → mark ``smoke_passed=true`` + record ``STEP_SMOKE``
#             so the fifth gate flips and finalize becomes reachable.
#   * red   — return the validator errors / audit first-bad-id so the UI
#             can render a diagnosable banner and send the operator back
#             to the appropriate earlier step.
#
# Implementation notes:
#   * The DAG payload is the SAME ``DAG_1_COMPILE_FLASH_HOST_NATIVE``
#     shape shipped in ``scripts/prod_smoke_test.py`` — single source of
#     truth lives in the script so prod-UI smoke and wizard smoke can't
#     drift.
#   * We do NOT shell out to the script. During bootstrap the admin has
#     not logged in yet, so the script's operator-gated ``POST /dag``
#     would 401. Instead we import the validator + workflow machinery
#     directly and let the bootstrap-gate middleware keep the route
#     wizard-only.

_SMOKE_DAG_ID = "smoke-compile-flash-host-native"
_SMOKE_DAG_ID_AARCH64 = "smoke-cross-compile-aarch64"


class SmokeSubsetRequest(BaseModel):
    """Body for ``POST /bootstrap/smoke-subset``.

    ``subset`` selects which DAG(s) to invoke:
      * ``dag1`` (default) — the compile-flash host_native DAG, ~60s;
        matches what L6 Step 5 locked in for the fast-path wizard run.
      * ``dag2`` — the aarch64 cross-compile DAG, validation + plan
        persistence only (no physical cross-compile runs in-process).
      * ``both`` — run ``dag1`` + ``dag2`` sequentially so the Step-5 UI
        can render a run summary for each DAG shipped in
        ``scripts/prod_smoke_test.py``.

    The wizard drives ``subset="both"`` so the operator sees the
    "兩個 DAG 的 run summary" (Step 5 TODO); external callers / CLI
    users can still pin to ``dag1`` for the 60-second fast path.
    """

    subset: Literal["dag1", "dag2", "both"] = Field(
        default="dag1",
        description=(
            "Which DAG subset to run — ``dag1`` (compile-flash "
            "host_native, ~60s fast path), ``dag2`` (aarch64 "
            "cross-compile), or ``both`` (wizard default on Step 5)."
        ),
    )


class SmokeRunSummary(BaseModel):
    key: str = ""
    label: str
    dag_id: str
    ok: bool
    validation_errors: list[dict] = Field(default_factory=list)
    run_id: str | None = None
    plan_id: int | None = None
    plan_status: str | None = None
    task_count: int = 0
    t3_runner: str | None = None
    target_platform: str | None = None


class AuditChainSummary(BaseModel):
    ok: bool
    first_bad_id: int | None = None
    detail: str = ""
    tenant_count: int = 0
    bad_tenants: list[str] = Field(default_factory=list)


class SmokeSubsetResponse(BaseModel):
    smoke_passed: bool
    subset: str
    elapsed_ms: int
    runs: list[SmokeRunSummary]
    audit_chain: AuditChainSummary


def _dag1_payload() -> dict:
    """Return the compile-flash host_native DAG payload verbatim.

    Mirrors ``DAG_1_COMPILE_FLASH_HOST_NATIVE`` in ``scripts/prod_smoke_test.py``
    so the two smoke invocations test the same artefact. Kept as a
    function (not a module-level dict) so a test can swap in a smaller
    stub without mutating global state.
    """
    return {
        "dag": {
            "schema_version": 1,
            "dag_id": _SMOKE_DAG_ID,
            "tasks": [
                {
                    "task_id": "compile",
                    "description": (
                        "Build firmware image (host-native, no cross-compile)"
                    ),
                    "required_tier": "t1",
                    "toolchain": "cmake",
                    "inputs": [],
                    "expected_output": "build/firmware.bin",
                    "depends_on": [],
                },
                {
                    # On host_native the T3 resolver picks LOCAL and the
                    # validator swaps the effective tier to t1. python3 is
                    # a t1-legal toolchain so the symbolic "flash" step
                    # passes validation — there's no physical board to
                    # flash when target arch == host arch.
                    "task_id": "flash",
                    "description": (
                        "Flash built image (T3 resolves to LOCAL on host_native)"
                    ),
                    "required_tier": "t3",
                    "toolchain": "python3",
                    "inputs": ["build/firmware.bin"],
                    "expected_output": "logs/flash.log",
                    "depends_on": ["compile"],
                },
            ],
        },
        "target_platform": "host_native",
        "metadata": {"source": "bootstrap:smoke-subset", "test_run": True},
    }


def _dag2_payload() -> dict:
    """Return the cross-compile aarch64 DAG payload verbatim.

    Mirrors ``DAG_2_CROSS_COMPILE_AARCH64`` in ``scripts/prod_smoke_test.py``.
    During bootstrap we validate + persist the plan only — the actual
    aarch64 cross-compile toolchain is not invoked in-process, so the
    DAG completes the ``workflow.start`` handshake almost instantly on
    any host regardless of whether ``aarch64-linux-gnu-gcc`` is present.
    """
    return {
        "dag": {
            "schema_version": 1,
            "dag_id": _SMOKE_DAG_ID_AARCH64,
            "tasks": [
                {
                    "task_id": "cross-compile",
                    "description": (
                        "Cross-compile firmware for AArch64 target"
                    ),
                    "required_tier": "t1",
                    "toolchain": "cmake",
                    "inputs": [],
                    "expected_output": "build/firmware-aarch64.bin",
                    "depends_on": [],
                },
                {
                    "task_id": "package",
                    "description": (
                        "Package cross-compiled artifact for deployment"
                    ),
                    "required_tier": "t1",
                    "toolchain": "make",
                    "inputs": ["build/firmware-aarch64.bin"],
                    "expected_output": "dist/firmware-aarch64.tar.gz",
                    "depends_on": ["cross-compile"],
                },
            ],
        },
        "target_platform": "aarch64",
        "metadata": {"source": "bootstrap:smoke-subset", "test_run": True},
    }


# Single source of truth for the bootstrap smoke DAG catalogue. Matches
# the (key, label, payload) tuple shape used by
# ``scripts/prod_smoke_test.py`` so the two surfaces cannot drift.
def _smoke_dag_catalogue() -> list[tuple[str, str, str, dict]]:
    return [
        (
            "dag1",
            "DAG #1: compile-flash (host_native)",
            _SMOKE_DAG_ID,
            _dag1_payload(),
        ),
        (
            "dag2",
            "DAG #2: cross-compile (aarch64)",
            _SMOKE_DAG_ID_AARCH64,
            _dag2_payload(),
        ),
    ]


def _select_smoke_dags(subset: str) -> list[tuple[str, str, str, dict]]:
    """Filter the catalogue by subset keyword (mirrors the CLI helper)."""
    catalogue = _smoke_dag_catalogue()
    if subset == "both":
        return catalogue
    return [entry for entry in catalogue if entry[0] == subset]


async def _run_smoke_dag(
    key: str, label: str, dag_id: str, payload: dict,
) -> SmokeRunSummary:
    """Validate + open a workflow run for one smoke DAG payload.

    Generic replacement for the old ``_run_dag1_smoke`` helper — the
    shape is identical so the wizard's Step-5 pane can iterate over
    ``runs[]`` without special-casing any individual DAG.
    """
    from backend.dag_schema import DAG
    from backend import dag_storage as _ds
    from backend import dag_validator as _dv
    from backend import workflow as wf
    from backend.routers import dag as _dag_router

    target_name = payload.get("target_platform") or "host_native"

    try:
        dag = DAG.model_validate(payload["dag"])
    except Exception as exc:
        return SmokeRunSummary(
            key=key,
            label=label,
            dag_id=dag_id,
            ok=False,
            validation_errors=[{
                "rule": "schema",
                "task_id": None,
                "message": str(exc),
            }],
            target_platform=target_name,
        )

    target_profile = _dag_router._resolve_target_profile(target_name)
    result = _dv.validate(dag, target_profile=target_profile)

    try:
        from backend.t3_resolver import resolve_from_profile

        t3_resolution = resolve_from_profile(target_profile).kind.value
    except Exception as exc:
        logger.debug("bootstrap: smoke t3 resolver probe failed: %s", exc)
        t3_resolution = None

    if not result.ok:
        return SmokeRunSummary(
            key=key,
            label=label,
            dag_id=dag_id,
            ok=False,
            validation_errors=[e.to_dict() for e in result.errors],
            plan_status="failed",
            task_count=len(dag.tasks),
            t3_runner=t3_resolution,
            target_platform=target_name,
        )

    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("source", "bootstrap:smoke-subset")
    metadata.setdefault("target_platform", target_name)

    try:
        run = await wf.start(
            "invoke",
            dag=dag,
            metadata=metadata,
            target_profile=target_profile,
        )
    except Exception as exc:
        logger.exception("bootstrap: smoke workflow.start failed")
        return SmokeRunSummary(
            key=key,
            label=label,
            dag_id=dag_id,
            ok=False,
            validation_errors=[{
                "rule": "workflow_start",
                "task_id": None,
                "message": f"{type(exc).__name__}: {exc}"[:400],
            }],
            task_count=len(dag.tasks),
            t3_runner=t3_resolution,
            target_platform=target_name,
        )

    plan = await _ds.get_plan_by_run(run.id)
    plan_status = plan.status if plan else None
    plan_id = plan.id if plan else None
    smoke_ok = bool(plan and plan_status in ("validated", "executing"))

    return SmokeRunSummary(
        key=key,
        label=label,
        dag_id=dag_id,
        ok=smoke_ok,
        validation_errors=(plan.errors() if plan and not smoke_ok else []),
        run_id=run.id,
        plan_id=plan_id,
        plan_status=plan_status,
        task_count=len(dag.tasks),
        t3_runner=t3_resolution,
        target_platform=target_name,
    )


async def _verify_audit_chain() -> AuditChainSummary:
    """Verify every tenant's audit hash chain and aggregate the result.

    Matches the ``/audit/verify-all`` semantics the prod smoke script
    reaches for, but called in-process so it works during bootstrap
    before any admin session exists.
    """
    try:
        from backend import audit as _audit

        results = await _audit.verify_all_chains()
    except Exception as exc:
        logger.exception("bootstrap: audit verify_all_chains failed")
        return AuditChainSummary(
            ok=False,
            first_bad_id=None,
            detail=f"verify_all_chains raised: {type(exc).__name__}: {exc}"[:300],
            tenant_count=0,
            bad_tenants=[],
        )

    first_bad: int | None = None
    bad_tenants: list[str] = []
    for tid, (ok, bad) in sorted(results.items()):
        if not ok:
            bad_tenants.append(tid)
            if first_bad is None:
                first_bad = bad
    all_ok = not bad_tenants
    detail = (
        f"{len(results)} tenant(s) verified"
        if all_ok
        else f"broken chain in tenant(s): {', '.join(bad_tenants)}"
    )
    return AuditChainSummary(
        ok=all_ok,
        first_bad_id=first_bad,
        detail=detail,
        tenant_count=len(results),
        bad_tenants=bad_tenants,
    )


@router.post("/smoke-subset", response_model=SmokeSubsetResponse)
async def bootstrap_smoke_subset(
    req: SmokeSubsetRequest | None = None,
) -> SmokeSubsetResponse:
    """Run the wizard's Step-5 smoke subset (compile-flash host_native and/or
    cross-compile aarch64).

    Sequence:
      1. Validate + submit each selected DAG via ``workflow.start`` —
         same artefact shape the prod-UI smoke uses. ``subset=both``
         yields one run summary per DAG so Step 5 can render both.
      2. Verify the audit hash chain for every tenant (catches
         pre-finalize tampering).
      3. On every selected DAG green AND audit chain intact, flip the
         ``smoke_passed`` bootstrap marker + record ``STEP_SMOKE`` so
         the finalize gate can pass.

    Unauthenticated like every other wizard endpoint — the bootstrap-
    gate middleware keeps ``/bootstrap/*`` wizard-scoped until finalize.
    Response is always HTTP 200 — ``smoke_passed`` in the body carries
    the outcome so the UI can render the result pane without parsing
    error bodies.
    """
    body = req or SmokeSubsetRequest()
    started = time.monotonic()

    selected = _select_smoke_dags(body.subset)
    run_summaries: list[SmokeRunSummary] = []
    for key, label, dag_id, payload in selected:
        run_summaries.append(await _run_smoke_dag(key, label, dag_id, payload))

    audit_summary = await _verify_audit_chain()
    elapsed_ms = int((time.monotonic() - started) * 1000)

    runs_ok = bool(run_summaries) and all(r.ok for r in run_summaries)
    smoke_passed = bool(runs_ok and audit_summary.ok)

    if smoke_passed:
        try:
            _boot.mark_smoke_passed(True)
        except Exception as exc:
            logger.warning(
                "bootstrap: mark_smoke_passed after smoke-subset failed: %s", exc,
            )
        try:
            await _boot.record_bootstrap_step(
                _boot.STEP_SMOKE,
                actor_user_id=None,
                metadata={
                    "subset": body.subset,
                    # Keep legacy single-run fields populated with the
                    # first (primary) run so anything already querying
                    # the marker keeps working.
                    "run_id": run_summaries[0].run_id,
                    "plan_id": run_summaries[0].plan_id,
                    "dag_id": run_summaries[0].dag_id,
                    "dag_runs": [
                        {
                            "key": r.key,
                            "dag_id": r.dag_id,
                            "run_id": r.run_id,
                            "plan_id": r.plan_id,
                            "plan_status": r.plan_status,
                            "task_count": r.task_count,
                            "target_platform": r.target_platform,
                        }
                        for r in run_summaries
                    ],
                    "audit_tenant_count": audit_summary.tenant_count,
                    "elapsed_ms": elapsed_ms,
                },
            )
        except Exception as exc:
            logger.warning(
                "bootstrap: record_bootstrap_step(smoke) failed: %s", exc,
            )

    logger.info(
        "bootstrap: smoke-subset subset=%s smoke_passed=%s runs=%s "
        "audit_ok=%s audit_tenants=%d elapsed=%dms",
        body.subset, smoke_passed,
        [(r.dag_id, r.ok, r.plan_status) for r in run_summaries],
        audit_summary.ok, audit_summary.tenant_count, elapsed_ms,
    )

    try:
        await audit.log(
            action="bootstrap.smoke_subset",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_SMOKE,
            before=None,
            after={
                "subset": body.subset,
                "smoke_passed": smoke_passed,
                "elapsed_ms": elapsed_ms,
                "dag_runs": [r.model_dump() for r in run_summaries],
                "audit_chain": audit_summary.model_dump(),
            },
            actor="wizard",
        )
    except Exception as exc:
        logger.debug("bootstrap.smoke_subset audit emit failed: %s", exc)

    return SmokeSubsetResponse(
        smoke_passed=smoke_passed,
        subset=body.subset,
        elapsed_ms=elapsed_ms,
        runs=run_summaries,
        audit_chain=audit_summary,
    )


class FinalizeRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional free-text note persisted with the finalize row.",
        max_length=500,
    )


class FinalizeResponse(BaseModel):
    finalized: bool
    status: dict
    actor_user_id: str


@router.get("/status")
async def bootstrap_status() -> dict:
    """Public read of the four-gate status + finalized flag.

    Exempt from auth so the wizard UI can poll it during install before
    the admin has even logged in. No secrets leak — each field is a
    boolean derived from already-public server state.
    """
    status = await _boot.get_bootstrap_status()
    missing = await _boot.missing_required_steps()
    return {
        "status": status.to_dict(),
        "all_green": status.all_green,
        "finalized": _boot.is_bootstrap_finalized_flag(),
        "missing_steps": missing,
    }


@router.post("/finalize", response_model=FinalizeResponse)
async def bootstrap_finalize(
    req: FinalizeRequest | None = None,
    admin: _au.User = Depends(_au.require_admin),
):
    """Close out the wizard — admin only, requires every gate green.

    409 conditions (the wizard should keep the operator on the current
    step):
      * any live gate is still red (password default, no LLM key,
        CF tunnel unprovisioned, smoke not green)
      * any required step row is missing from ``bootstrap_state``
    On success, writes a ``finalized`` audit row into
    ``bootstrap_state`` and flips the persisted
    ``bootstrap_finalized=true`` app-setting flag.
    """
    metadata: dict = {"reason": (req.reason if req else None) or ""}

    try:
        status = await _boot.mark_bootstrap_finalized(
            actor_user_id=admin.id,
            metadata=metadata,
        )
    except RuntimeError as exc:
        live_status = await _boot.get_bootstrap_status()
        missing = await _boot.missing_required_steps()
        logger.warning(
            "bootstrap: finalize refused for admin=%s: %s", admin.email, exc,
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "status": live_status.to_dict(),
                "missing_steps": missing,
            },
        )

    try:
        await audit.log(
            action="bootstrap_finalized",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_FINALIZED,
            before=None,
            after={"status": status.to_dict(), **metadata},
            actor=admin.email,
        )
    except Exception as exc:
        logger.debug("bootstrap: audit log failed (non-fatal): %s", exc)

    return FinalizeResponse(
        finalized=True,
        status=status.to_dict(),
        actor_user_id=admin.id,
    )
