"""W14.2 — REST endpoints for the live web-preview sidecar launcher.

Endpoints:

* ``POST /web-sandbox/preview`` — launch (or recover) a sidecar for a
  ``workspace_id``. Idempotent — calling it twice with the same
  ``workspace_id`` while the sidecar is up returns the existing
  instance with ``last_request_at`` bumped.

* ``GET /web-sandbox/preview/{workspace_id}`` — read the current
  instance snapshot.

* ``POST /web-sandbox/preview/{workspace_id}/touch`` — bump
  ``last_request_at`` so the (future) W14.5 idle reaper does not
  collect this sidecar mid-use.

* ``POST /web-sandbox/preview/{workspace_id}/ready`` — caller signals
  that the dev-server has reported ready (``mark_ready`` in the
  manager). Included so the future W14.6 frontend can flip its
  iframe indicator without polling docker logs from the browser.

* ``DELETE /web-sandbox/preview/{workspace_id}`` — stop + remove the
  sidecar. Optional ``?reason=`` query param feeds
  :attr:`WebSandboxInstance.killed_reason` so the future W14.10
  audit row can explain *why* the sandbox died.

* ``GET /web-sandbox/preview`` — list all live sidecars (operator UI
  / chatops triage; W14.6 panel will use this for the multi-workspace
  switcher).

Why this router is operator-gated
=================================

Web preview is an operator-tier feature: launching a sidecar consumes
docker resources (RAM, CPU, disk via ``pnpm install``), and the
W14.3 CF Tunnel ingress will assign a publicly-resolvable hostname
once it lands. We therefore reuse :func:`backend.auth.require_operator`
as the dependency for every write endpoint and ``require_viewer`` for
reads — the same RBAC contract every other workspace-touching router
uses (compare :mod:`backend.routers.workspaces`).

PEP HOLD (W14.8) gates this endpoint behind an operator-confirmation
flow because ``pnpm install`` can download 50–500 MB of node_modules
on a first-touch workspace and typically blocks 30–90s before the
dev server is reachable. The HOLD only fires on a *cold* launch
(no live instance for the workspace) — idempotent re-launches of an
already-running sandbox bypass the HOLD via
:func:`backend.web_sandbox_pep.requires_first_preview_hold`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import workspace as _ws
from backend.cf_access import (
    CFAccessConfig,
    CFAccessManager,
    CFAccessMisconfigured,
)
from backend.cf_ingress import (
    CFIngressConfig,
    CFIngressManager,
    CFIngressMisconfigured,
)
from backend.config import Settings
from backend.web_sandbox import (
    DEFAULT_DEV_COMMAND,
    DEFAULT_IMAGE_TAG,
    DEFAULT_INSTALL_COMMAND,
    WebSandboxAlreadyExists,
    WebSandboxConfig,
    WebSandboxError,
    WebSandboxInstance,
    WebSandboxManager,
    WebSandboxNotFound,
    load_image_manifest,
    validate_workspace_path,
)
from backend.web_sandbox_idle_reaper import (
    IdleReaperConfig,
    IdleReaperError,
    WebSandboxIdleReaper,
)
from backend.web_sandbox_resource_limits import (
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_STORAGE_LIMIT_BYTES,
    ResourceLimitsError,
    WebPreviewResourceLimits,
    parse_cpu_limit,
    parse_memory_bytes,
    parse_storage_bytes,
)
from backend.web_sandbox_pep import (
    WebPreviewPepResult,
    evaluate_first_preview_hold,
    requires_first_preview_hold,
)
from backend.web_sandbox_vite_errors import (
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    VITE_ERROR_ALLOWED_KINDS,
    VITE_ERROR_ALLOWED_PHASES,
    ViteBuildError,
    ViteBuildErrorValidationError,
    ViteErrorBuffer,
    get_default_buffer,
    validate_error_payload,
)
from backend.ui_sandbox import SubprocessDockerClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web-sandbox", tags=["web-sandbox"])


# ─────────────── Module-level manager (per-worker singleton) ───────────────
#
# One manager per uvicorn worker. The manager's in-memory dict is
# per-worker; cross-worker consistency is achieved through the docker
# daemon's deterministic container naming (see
# :mod:`backend.web_sandbox` SOP §1 audit). W14.10 will replace this
# with a PG-backed registry; until then the singleton is the right
# shape for the row's scope.

_manager: WebSandboxManager | None = None
# W14.5 — per-worker idle-timeout reaper. Constructed alongside the
# manager in :func:`get_manager` (lazy). Daemon thread, dies with the
# process. Tests inject via :func:`set_reaper_for_tests`.
_reaper: WebSandboxIdleReaper | None = None


def _build_cf_ingress_manager() -> CFIngressManager | None:
    """Construct a :class:`CFIngressManager` from current Settings,
    returning ``None`` when W14.3 env knobs are absent or invalid.

    The four ``OMNISIGHT_TUNNEL_HOST`` / ``OMNISIGHT_CF_API_TOKEN`` /
    ``OMNISIGHT_CF_ACCOUNT_ID`` / ``OMNISIGHT_CF_TUNNEL_ID`` env knobs
    are *all* required — partial config falls back to ``None`` so the
    W14.2 host-port preview path keeps working unchanged. *Malformed*
    values (e.g. token set but tunnel_id is not a UUID) also fall
    back, with a warning log so the operator can see what tripped.
    """

    try:
        settings = Settings()
        config = CFIngressConfig.from_settings(settings)
    except CFIngressMisconfigured as exc:
        logger.info(
            "web_sandbox: CF Tunnel ingress disabled — %s", exc,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "web_sandbox: CF Tunnel ingress disabled (Settings failed): %s",
            exc,
        )
        return None
    return CFIngressManager(config=config)


def _build_cf_access_manager() -> CFAccessManager | None:
    """Construct a :class:`CFAccessManager` from current Settings,
    returning ``None`` when W14.4 env knobs are absent or invalid.

    Required knobs (all four):
      - ``OMNISIGHT_TUNNEL_HOST`` (shared with W14.3)
      - ``OMNISIGHT_CF_API_TOKEN`` (shared with W14.3 — needs the
        ``Account:Cloudflare Access:Edit`` scope on top of the
        ``Account:Cloudflare Tunnel:Edit`` scope W14.3 needs)
      - ``OMNISIGHT_CF_ACCOUNT_ID`` (shared with W14.3)
      - ``OMNISIGHT_CF_ACCESS_TEAM_DOMAIN`` (W14.4-specific —
        ``<team>.cloudflareaccess.com``)

    Partial config falls back to ``None`` so the W14.3 ingress path
    keeps working unchanged with no SSO gate. Malformed values (e.g.
    team_domain set to a non-cloudflareaccess.com hostname) also fall
    back, with an info-level log so the operator can see what tripped.
    """

    try:
        settings = Settings()
        config = CFAccessConfig.from_settings(settings)
    except CFAccessMisconfigured as exc:
        logger.info(
            "web_sandbox: CF Access SSO disabled — %s", exc,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "web_sandbox: CF Access SSO disabled (Settings failed): %s",
            exc,
        )
        return None
    return CFAccessManager(config=config)


def _build_resource_limits() -> WebPreviewResourceLimits:
    """Construct a :class:`WebPreviewResourceLimits` from current
    Settings, falling back to row-spec defaults (2 GiB / 1 CPU /
    5 GiB) on any malformed env knob.

    The W14.9 row spec defaults work out of the box — operators don't
    need to set any env knob to get the documented behaviour. Three
    optional overrides:

      * ``OMNISIGHT_WEB_SANDBOX_MEMORY_LIMIT`` — docker-style size
        (``2g``, ``512m``, raw bytes).
      * ``OMNISIGHT_WEB_SANDBOX_CPU_LIMIT`` — fractional CPUs (``1``,
        ``0.5``, ``2``).
      * ``OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT`` — same syntax as
        memory; ``off`` / ``0`` / ``none`` disables the disk cap (for
        operators on overlay2-on-ext4 hosts where docker silently
        ignores ``--storage-opt size=``).

    On any parse failure we log a warning and fall through to the
    defaults; this preserves the W14.5 / W14.4 fallback shape
    ("malformed config falls back gracefully") so a single typo in
    ``.env`` cannot break every preview launch in the fleet.
    """

    try:
        settings = Settings()
        return WebPreviewResourceLimits.from_settings(settings)
    except ResourceLimitsError as exc:
        logger.warning(
            "web_sandbox: resource_limits env knobs malformed — falling "
            "back to row-spec defaults (2g / 1 cpu / 5g): %s", exc,
        )
        return WebPreviewResourceLimits.default()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "web_sandbox: resource_limits Settings load failed — falling "
            "back to row-spec defaults: %s", exc,
        )
        return WebPreviewResourceLimits.default()


def _build_idle_reaper(manager: WebSandboxManager) -> WebSandboxIdleReaper | None:
    """Construct a :class:`WebSandboxIdleReaper` from current Settings,
    returning ``None`` when the config is malformed (operator misset
    one of the two ``OMNISIGHT_WEB_SANDBOX_*`` knobs to a value the
    reaper module rejects — e.g. interval > timeout).

    A ``None`` return falls back to "no reaper" — the W14.2/W14.3/W14.4
    paths keep working without auto-kill, which matches the
    deployed-inactive shape every other W14.* row degrades to. The
    operator notices the missing reaper because their long-idle
    sandboxes never auto-clean and shows up as a Settings error in
    the startup log.
    """

    try:
        settings = Settings()
        config = IdleReaperConfig.from_settings(settings)
    except IdleReaperError as exc:
        logger.warning(
            "web_sandbox: idle reaper disabled — %s", exc,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "web_sandbox: idle reaper disabled (Settings failed): %s",
            exc,
        )
        return None
    return WebSandboxIdleReaper(manager=manager, config=config)


def get_manager() -> WebSandboxManager:
    """Return the per-worker :class:`WebSandboxManager`, lazy-creating
    one on first request.

    Lazy construction keeps unit tests that import the router (e.g.
    schema introspection) from triggering a docker-CLI subprocess at
    import time. The manager is overridable via
    :func:`set_manager_for_tests` so tests can inject a fake docker
    client.

    W14.3 wiring: when the four CF Tunnel env knobs are present and
    valid, this function constructs a :class:`CFIngressManager` and
    threads it into the launcher so launches automatically provision
    ``preview-{sandbox_id}.{tunnel_host}`` ingress rules. When any
    knob is missing the launcher falls back to the W14.2 host-port
    preview path — the same dev path :class:`WebSandboxManager` shipped
    in row W14.2.
    """

    global _manager, _reaper
    if _manager is None:
        try:
            manifest = load_image_manifest()
        except WebSandboxError as exc:
            logger.warning(
                "web_sandbox: image manifest missing (%s) — "
                "manager will run without manifest cross-checks", exc,
            )
            manifest = None
        cf_ingress = _build_cf_ingress_manager()
        cf_access = _build_cf_access_manager()
        resource_limits = _build_resource_limits()
        _manager = WebSandboxManager(
            docker_client=SubprocessDockerClient(),
            manifest=manifest,
            cf_ingress_manager=cf_ingress,
            cf_access_manager=cf_access,
            resource_limits=resource_limits,
        )
        # W14.5 — start the idle-timeout reaper daemon thread so any
        # sandbox sitting more than ``OMNISIGHT_WEB_SANDBOX_IDLE_TIMEOUT_S``
        # seconds without a touch/launch/ready bump is automatically
        # collected. ``manager.stop(reason="idle_timeout")`` cascades
        # the W14.3 ingress + W14.4 SSO cleanup — that is the "刪
        # ingress" half of the W14.5 row spec.
        if _reaper is None:
            _reaper = _build_idle_reaper(_manager)
            if _reaper is not None:
                _reaper.start()
    return _manager


def set_manager_for_tests(manager: WebSandboxManager | None) -> None:
    """Test-only injection point. ``None`` resets the singleton."""

    global _manager
    _manager = manager


def set_reaper_for_tests(reaper: WebSandboxIdleReaper | None) -> None:
    """Test-only injection point. ``None`` resets the singleton.

    Tests that swap in a fake :class:`WebSandboxIdleReaper` (or
    nullify it) should call :meth:`WebSandboxIdleReaper.stop` on the
    previous reaper themselves before swapping — the production
    :func:`get_manager` does that implicitly via process exit, but
    the unit-test :class:`fastapi.testclient.TestClient` lifetime is
    much shorter and a leaked daemon thread can leak across tests.
    """

    global _reaper
    if _reaper is not None and _reaper is not reaper:
        try:
            _reaper.stop(timeout_s=1.0)
        except Exception:  # pragma: no cover - defensive
            pass
    _reaper = reaper


def get_reaper() -> WebSandboxIdleReaper | None:
    """Return the per-worker reaper, or ``None`` when one has not
    been built. The W14.5 row constructs the reaper inside
    :func:`get_manager` so the first request that hits the manager
    also boots the daemon thread."""

    return _reaper


# ─────────────── W14.8 — PEP HOLD before first preview ───────────────
#
# Module-level handle to :func:`backend.web_sandbox_pep.evaluate_first_preview_hold`
# so tests can swap in a fake without monkey-patching imports. Calling
# this through the indirection (rather than importing the function name
# directly into ``launch_preview``) means a test that does
# ``set_pep_evaluator_for_tests(fake)`` is observed by the next request
# without rebinding ``backend.routers.web_sandbox`` symbols.
_pep_evaluator: Any = evaluate_first_preview_hold


def set_pep_evaluator_for_tests(evaluator: Any) -> None:
    """Test-only injection point. Pass ``None`` to reset to the default
    :func:`backend.web_sandbox_pep.evaluate_first_preview_hold`."""

    global _pep_evaluator
    _pep_evaluator = evaluator if evaluator is not None else evaluate_first_preview_hold


def get_pep_evaluator() -> Any:
    """Test helper — returns the active evaluator (default or injected)."""

    return _pep_evaluator


# ─────────────── Request / Response models ───────────────


class LaunchPreviewRequest(BaseModel):
    """Body for ``POST /web-sandbox/preview``.

    ``workspace_id`` is required. ``workspace_path`` is optional —
    when omitted, the launcher resolves it via
    :func:`backend.workspace.get_workspace` (which is keyed on
    ``agent_id`` today; until Y6 lands a true workspace_id index, the
    convention is "workspace_id == agent_id").
    """

    workspace_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Workspace identifier — also serves as the docker container name suffix.",
    )
    workspace_path: str | None = Field(
        None,
        description="Absolute host path to the workspace. When omitted, resolved from backend.workspace.get_workspace(workspace_id).",
    )
    image_tag: str = Field(
        DEFAULT_IMAGE_TAG,
        description="Sidecar image tag. Defaults to omnisight-web-preview:dev (the W14.1 image).",
    )
    git_ref: str | None = Field(
        None,
        description="Optional git ref to fetch + checkout before running pnpm install. Skips git steps when None.",
    )
    install_command: list[str] | None = Field(
        None,
        description="Override the default install command (pnpm install --frozen-lockfile).",
    )
    dev_command: list[str] | None = Field(
        None,
        description="Override the default dev command (pnpm dev --host 0.0.0.0). Use for Bun / Vite preview.",
    )
    container_port: int = Field(
        5173, ge=1, le=65535,
        description="In-container port the dev server binds to. 5173 = Vite default; 3000 = Nuxt SSR.",
    )
    env: dict[str, str] | None = Field(
        None,
        description="Extra environment variables to forward into the sidecar (e.g. NUXT_PUBLIC_API_URL).",
    )
    allowed_emails: list[str] | None = Field(
        None,
        description=(
            "W14.4 — additional emails to allow through the CF Access "
            "SSO gate. The launching operator's email is auto-prepended "
            "by the router; the operator-wide ``cf_access_default_emails`` "
            "admin allowlist is unioned in by the manager. None ⇒ rely "
            "on the operator's email plus the admin allowlist."
        ),
    )
    memory_limit: str | None = Field(
        None,
        description=(
            "W14.9 — per-launch override for the cgroup RAM cap. "
            "Docker-style size (``2g``, ``512m``, raw bytes). When "
            "omitted, the manager applies its operator-wide policy "
            "(default 2 GiB)."
        ),
    )
    cpu_limit: float | str | None = Field(
        None,
        description=(
            "W14.9 — per-launch override for the cgroup CPU cap. "
            "Fractional CPUs allowed (``1``, ``0.5``, ``2``). When "
            "omitted, the manager applies its operator-wide policy "
            "(default 1 CPU)."
        ),
    )
    storage_limit: str | None = Field(
        None,
        description=(
            "W14.9 — per-launch override for the writable-layer disk "
            "cap. Docker-style size (``5g``, ``10g``). ``off`` / ``0`` "
            "/ ``none`` disables. When omitted, the manager applies "
            "its operator-wide policy (default 5 GiB)."
        ),
    )


class WebSandboxInstanceResponse(BaseModel):
    """Wire shape for :class:`WebSandboxInstance`. Intentionally
    a thin pass-through of ``to_dict()`` so the wire format tracks
    :data:`WEB_SANDBOX_SCHEMA_VERSION` without duplicating field
    declarations.

    W14.8 — ``pep_decision_id`` is the optional id of the PEP
    proposal that gated the first-preview launch. Only present on
    the response shape (not on the snapshot/list endpoints) because
    it ties a specific HTTP request to the operator decision; idempotent
    re-launches keep it ``None``.
    """

    schema_version: str
    workspace_id: str
    sandbox_id: str
    container_name: str
    config: dict[str, Any]
    status: str
    container_id: str | None
    host_port: int | None
    preview_url: str | None
    ingress_url: str | None
    access_app_id: str | None
    created_at: float
    started_at: float | None
    ready_at: float | None
    stopped_at: float | None
    last_request_at: float
    error: str | None
    killed_reason: str | None
    warnings: list[str]
    pep_decision_id: str | None = None


def _resolve_workspace_path(workspace_id: str, override: str | None) -> str:
    """Resolve ``workspace_path`` from the request body or the
    workspace registry. Raises ``HTTPException(404)`` when neither
    is available — the caller has to provision the workspace first
    (Y6 #282) before launching a preview against it.
    """

    if override:
        try:
            validate_workspace_path(override)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return override

    info = _ws.get_workspace(workspace_id)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"workspace_id={workspace_id!r} is not registered and no "
                "workspace_path was supplied — provision the workspace "
                "via /workspaces/provision first."
            ),
        )
    return str(info.path)


def _instance_to_response(instance: WebSandboxInstance) -> dict[str, Any]:
    """Project the manager's frozen instance into the wire shape."""

    return instance.to_dict()


# ─────────────── Endpoints ───────────────


@router.post("/preview", status_code=200)
async def launch_preview(
    body: LaunchPreviewRequest,
    user: _au.User = Depends(_au.require_operator),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """Launch (or recover) a web-preview sidecar for ``workspace_id``.

    W14.8 — first launch (no live instance for the workspace) goes
    through a PEP HOLD before docker is touched. The operator approves
    via the standard PEP toast; the HOLD enforces that the operator
    consents to the 50–500 MB / 30–90s cold-install cost before the
    sidecar starts pulling tarballs. Idempotent re-launches of an
    already-running sandbox bypass the HOLD because the docker work
    has already been paid for.
    """

    workspace_path = _resolve_workspace_path(body.workspace_id, body.workspace_path)
    install_command = (
        tuple(body.install_command)
        if body.install_command
        else DEFAULT_INSTALL_COMMAND
    )
    dev_command = (
        tuple(body.dev_command)
        if body.dev_command
        else DEFAULT_DEV_COMMAND
    )
    # W14.4: prepend the launching operator's email to the requested
    # allowlist so the OIDC token CF Access issues for them lines up
    # 1-to-1 with the OmniSight session that called POST /preview.
    # Empty / missing email (rare — only when the auth-bypass dev
    # path runs without a user record) means the manager falls back
    # to the cf_access_default_emails admin allowlist, and surfaces a
    # warning if both are empty.
    auth_emails: list[str] = []
    operator_email = (getattr(user, "email", "") or "").strip()
    if operator_email:
        auth_emails.append(operator_email)
    if body.allowed_emails:
        auth_emails.extend(body.allowed_emails)
    # W14.9: per-launch resource-limit override. ``None`` everywhere
    # ⇒ defer to the manager's operator-wide policy (the env-knob /
    # row-spec default chain). Any subset triggers a per-launch
    # override that *fully* replaces the policy — partial overrides
    # would otherwise need the caller to know the manager's policy
    # to compose, which they typically don't.
    resource_override: WebPreviewResourceLimits | None = None
    if (
        body.memory_limit is not None
        or body.cpu_limit is not None
        or body.storage_limit is not None
    ):
        try:
            mem_bytes = (
                parse_memory_bytes(body.memory_limit)
                if body.memory_limit is not None
                else DEFAULT_MEMORY_LIMIT_BYTES
            )
            cpu_value = (
                parse_cpu_limit(body.cpu_limit)
                if body.cpu_limit is not None
                else DEFAULT_CPU_LIMIT
            )
            if body.storage_limit is None:
                storage_bytes: int | None = DEFAULT_STORAGE_LIMIT_BYTES
            else:
                from backend.web_sandbox_resource_limits import (
                    STORAGE_LIMIT_DISABLED_TOKENS,
                )
                if (
                    isinstance(body.storage_limit, str)
                    and body.storage_limit.strip().lower()
                    in STORAGE_LIMIT_DISABLED_TOKENS
                ):
                    storage_bytes = None
                else:
                    storage_bytes = parse_storage_bytes(body.storage_limit)
            resource_override = WebPreviewResourceLimits(
                memory_limit_bytes=mem_bytes,
                cpu_limit=cpu_value,
                storage_limit_bytes=storage_bytes,
            )
        except ResourceLimitsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        config = WebSandboxConfig(
            workspace_id=body.workspace_id,
            workspace_path=workspace_path,
            image_tag=body.image_tag,
            git_ref=body.git_ref,
            install_command=install_command,
            dev_command=dev_command,
            container_port=body.container_port,
            env=body.env or {},
            allowed_emails=tuple(auth_emails),
            resource_limits=resource_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # W14.8 — first-preview HOLD. Skipped for idempotent re-launches
    # of a non-terminal instance (operator already paid for the cold
    # install on the previous launch). Cold launch (no instance, or
    # last instance is terminal) routes through the PEP gateway —
    # the standard ``tier_unlisted`` HOLD fires because
    # ``web_sandbox_preview`` is intentionally never on a tier
    # whitelist.
    pep_decision_id: str | None = None
    if requires_first_preview_hold(manager.get, body.workspace_id):
        evaluator = get_pep_evaluator()
        result: WebPreviewPepResult = await evaluator(
            workspace_id=body.workspace_id,
            workspace_path=workspace_path,
            image_tag=body.image_tag,
            actor_email=operator_email or None,
            git_ref=body.git_ref,
            container_port=body.container_port,
        )
        if result.is_rejected:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "pep_first_preview_rejected",
                    "message": (
                        "First-preview launch was not approved by an "
                        "operator. Click Launch preview again to resubmit."
                    ),
                    "reason": result.reason,
                    "rule": result.rule,
                    "decision_id": result.decision_id,
                    "degraded": result.degraded,
                },
            )
        if result.is_error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "pep_gateway_unavailable",
                    "message": (
                        "PEP gateway could not evaluate the first-preview "
                        "HOLD — try again in a moment, or contact platform "
                        "ops if this persists."
                    ),
                    "reason": result.reason,
                },
            )
        pep_decision_id = result.decision_id

    try:
        instance = manager.launch(config)
    except WebSandboxAlreadyExists as exc:  # pragma: no cover - idempotent=True path
        raise HTTPException(status_code=409, detail=str(exc))
    except WebSandboxError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    response = _instance_to_response(instance)
    # W14.8 — surface the PEP decision id on the response so the
    # frontend can cross-link the toast with the running sandbox.
    # Only present when a HOLD was actually evaluated (idempotent
    # re-launch returns ``None`` as expected).
    if pep_decision_id:
        response["pep_decision_id"] = pep_decision_id
    return response


@router.get("/preview")
async def list_previews(
    user: _au.User = Depends(_au.require_viewer),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """List all live sidecars known to this worker."""

    return manager.snapshot()


@router.get("/preview/{workspace_id}")
async def get_preview(
    workspace_id: str,
    user: _au.User = Depends(_au.require_viewer),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """Return the current snapshot for ``workspace_id``."""

    instance = manager.get(workspace_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"no sandbox for {workspace_id!r}")
    return _instance_to_response(instance)


@router.post("/preview/{workspace_id}/touch")
async def touch_preview(
    workspace_id: str,
    user: _au.User = Depends(_au.require_operator),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """Bump ``last_request_at`` so the W14.5 idle reaper does not
    collect this sidecar."""

    try:
        instance = manager.touch(workspace_id)
    except WebSandboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _instance_to_response(instance)


@router.post("/preview/{workspace_id}/ready")
async def mark_preview_ready(
    workspace_id: str,
    user: _au.User = Depends(_au.require_operator),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """Caller signals that the dev server has reported ready.

    W16.4 — emits a ``preview.ready`` SSE event carrying the sandbox
    URL so the orchestrator-chat surface can append an inline-iframe
    message without polling docker logs from the browser.  The emit is
    best-effort; a transport failure does not break the existing
    operator-tier ready signal contract.
    """

    try:
        instance = manager.mark_ready(workspace_id)
    except WebSandboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except WebSandboxError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    response = _instance_to_response(instance)
    try:
        from backend.web.web_preview_ready import (
            preview_ready_payload_from_instance_dict,
            emit_preview_ready,
        )
        payload = preview_ready_payload_from_instance_dict(response)
        if payload is not None:
            emit_preview_ready(
                workspace_id=payload.workspace_id,
                preview_url=payload.preview_url,
                label=payload.label,
                sandbox_id=payload.sandbox_id,
                ingress_url=payload.ingress_url,
                host_port=payload.host_port,
                broadcast_scope="session",
            )
    except Exception as exc:  # pragma: no cover - best-effort SSE
        logger.warning(
            "web_sandbox.preview_ready: SSE emit failed for %s: %s",
            workspace_id, exc,
        )
    return response


@router.delete("/preview/{workspace_id}")
async def stop_preview(
    workspace_id: str,
    reason: str | None = Query(None, max_length=200),
    user: _au.User = Depends(_au.require_operator),
    manager: WebSandboxManager = Depends(get_manager),
) -> dict[str, Any]:
    """Stop + remove the sidecar. ``reason`` is optional and gets
    stored on :attr:`WebSandboxInstance.killed_reason`."""

    try:
        instance = manager.stop(workspace_id, reason=reason)
    except WebSandboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _instance_to_response(instance)


# ─────────────── W15.1 — Vite plugin error reporting ───────────────
#
# The W14.1 sidecar's W15.5 vite.config scaffold loads
# ``packages/omnisight-vite-plugin`` which captures every compile-time
# and runtime error and POSTs it here.  W15.2
# (``backend/web/vite_error_relay.py``) will read from the buffer and
# fold the entries into LangGraph ``state.error_history``; W15.3 will
# quote them in the system-prompt template; W15.4 will escalate after
# 3 identical failures.  This row only owns the receiving endpoint and
# the in-memory buffer (see ``backend.web_sandbox_vite_errors``).
#
# Auth: ``require_operator`` mirrors the rest of the router.  This works
# for the browser-side runtime overlay (the iframe carries the operator
# session cookie) but the Node-side compile branch from inside the
# sidecar process will land 401 until W15.5 wires either a same-session
# reverse proxy or a bearer-token env knob.  The plugin already supports
# ``options.authToken`` for that follow-up.

_vite_error_buffer: ViteErrorBuffer | None = None


def get_vite_error_buffer() -> ViteErrorBuffer:
    """Return the per-worker Vite error buffer used by
    ``POST /web-sandbox/preview/{workspace_id}/error``.

    Lazy passthrough to
    :func:`backend.web_sandbox_vite_errors.get_default_buffer` so test
    fixtures can override the buffer with
    :func:`set_vite_error_buffer_for_tests` without touching module
    globals across the whole process.
    """

    return _vite_error_buffer if _vite_error_buffer is not None else get_default_buffer()


def set_vite_error_buffer_for_tests(buffer: ViteErrorBuffer | None) -> None:
    """Test-only injection point for the per-worker buffer."""

    global _vite_error_buffer
    _vite_error_buffer = buffer


class ViteErrorReport(BaseModel):
    """Wire shape posted by ``packages/omnisight-vite-plugin``.

    Frozen — additions need a matching bump in
    :data:`backend.web_sandbox_vite_errors.WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION`
    *and* in ``OMNISIGHT_VITE_ERROR_SCHEMA_VERSION`` in the JS plugin.
    The drift-guard tests (vitest + pytest) assert the two literals
    byte-equal; an unmatched bump fails CI red on both sides.
    """

    schema_version: str = Field(
        ...,
        description=(
            "Wire-shape pin.  Must equal the backend's "
            "WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION literal — anything "
            "else 422s so the JS plugin sees a clean schema mismatch."
        ),
    )
    kind: str = Field(
        ...,
        description=(
            "Error origin.  ``compile`` for Vite plugin hooks "
            "(buildStart / load / transform / hmr / config); "
            "``runtime`` for browser-side window.onerror + "
            "unhandledrejection handlers."
        ),
    )
    phase: str = Field(
        ...,
        description=(
            "Which Vite phase produced the error.  One of "
            f"{list(VITE_ERROR_ALLOWED_PHASES)}."
        ),
    )
    message: str = Field(..., description="Vite or browser error message.")
    file: str | None = Field(
        None, description="Resolved file id when known, else null."
    )
    line: int | None = Field(
        None, ge=0, description="1-based line number when known, else null."
    )
    column: int | None = Field(
        None, ge=0, description="0-based column when known, else null."
    )
    stack: str | None = Field(
        None,
        description=(
            "Stack trace when the error carries one (truncated to "
            "8 KiB by the plugin)."
        ),
    )
    plugin: str = Field(
        ...,
        description=(
            "Plugin identifier.  Today only ``omnisight-vite-plugin`` "
            "is accepted; the W15.5 Rolldown / Webpack siblings register "
            "their own ids."
        ),
    )
    plugin_version: str = Field(..., description="Plugin semver.")
    occurred_at: float = Field(
        ...,
        ge=0,
        description="POSIX seconds when the plugin captured the error.",
    )

    model_config = {"extra": "forbid"}


@router.post("/preview/{workspace_id}/error", status_code=200)
async def report_preview_error(
    workspace_id: str,
    report: ViteErrorReport,
    user: _au.User = Depends(_au.require_operator),
) -> dict[str, Any]:
    """Record a compile-time or runtime error for ``workspace_id``.

    Returns the recorded entry (with the server-side ``received_at``
    populated) plus the current buffer count.  Best-effort transport
    on the JS side — the plugin swallows all non-2xx responses; this
    endpoint therefore never raises 5xx for ordinary contract
    failures, only 422 for shape errors.
    """

    payload = report.model_dump()
    try:
        recorded = validate_error_payload(payload)
    except ViteBuildErrorValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    buffer = get_vite_error_buffer()
    stored = buffer.record(workspace_id, recorded)
    logger.info(
        "web_sandbox.vite_error: workspace=%s kind=%s phase=%s file=%s line=%s",
        workspace_id,
        stored.kind,
        stored.phase,
        stored.file,
        stored.line,
    )
    # W16.6 — surface a "我看到 X 有 error，正在修…" chat trace via
    # SSE so the operator's orchestrator-chat surface can render the
    # in-flight indicator without polling the W15.1 buffer.  Best-
    # effort; transport failure does not break the W15.1 ingest.
    try:
        from backend.web.vite_error_relay import format_vite_error_for_history
        from backend.web.preview_vite_error import (
            preview_vite_error_payload_from_history_entry,
            emit_preview_vite_error,
            PREVIEW_VITE_ERROR_STATUS_DETECTED,
        )
        history_entry = format_vite_error_for_history(stored)
        proj = preview_vite_error_payload_from_history_entry(
            history_entry,
            workspace_id=workspace_id,
            status=PREVIEW_VITE_ERROR_STATUS_DETECTED,
        )
        if proj is not None:
            emit_preview_vite_error(
                workspace_id=proj.workspace_id,
                status=proj.status,
                label=proj.label,
                error_class=proj.error_class,
                target=proj.target,
                error_signature=proj.error_signature,
                source_path=proj.source_path,
                source_line=proj.source_line,
                broadcast_scope="session",
            )
    except Exception as exc:  # pragma: no cover - best-effort SSE
        logger.warning(
            "web_sandbox.vite_error: SSE emit failed for %s: %s",
            workspace_id, exc,
        )
    return {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "recorded": stored.to_dict(),
        "buffer_count": buffer.count(workspace_id),
    }


@router.get("/preview/{workspace_id}/errors")
async def list_preview_errors(
    workspace_id: str,
    limit: int | None = Query(
        None,
        ge=1,
        le=500,
        description="Cap on the number of recent errors returned.",
    ),
    user: _au.User = Depends(_au.require_viewer),
) -> dict[str, Any]:
    """Return the ring-buffer of recent Vite errors for the workspace.

    W15.2's LangGraph integration will be the primary consumer; this
    GET exists today so operators can manually inspect pending errors
    via the W14.6 panel without waiting for the W15.2 wiring.
    """

    buffer = get_vite_error_buffer()
    entries = buffer.recent(workspace_id, limit=limit)
    return {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "errors": [e.to_dict() for e in entries],
        "buffer_count": buffer.count(workspace_id),
    }
