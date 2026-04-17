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
import logging
import os
import shutil
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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

    Order of precedence:
      1. ``OMNISIGHT_DEPLOY_MODE`` env var (explicit operator override).
      2. Running under ``systemctl`` with the units installed → ``systemd``.
      3. Docker compose binary available → ``docker-compose``.
      4. Fallback → ``dev`` (no-op; dev already runs uvicorn / next dev).

    Lives inside this module (rather than the L7 skeleton) so the Step 4
    endpoint is self-contained — L7's richer implementation can replace
    this when it lands without breaking the call site.
    """
    override = (os.environ.get("OMNISIGHT_DEPLOY_MODE") or "").strip().lower()
    if override in ("systemd", "docker-compose", "dev"):
        return override  # type: ignore[return-value]

    if shutil.which("systemctl") is not None:
        return "systemd"
    if shutil.which("docker") is not None:
        return "docker-compose"
    return "dev"


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
