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

PEP HOLD (W14.8) is a separate row that will gate this endpoint
behind an operator-confirmation flow because ``pnpm install`` can
download 50-500 MB of node_modules on a first-touch workspace. Until
W14.8 lands, the operator role gate is the only friction.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import workspace as _ws
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

    global _manager
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
        _manager = WebSandboxManager(
            docker_client=SubprocessDockerClient(),
            manifest=manifest,
            cf_ingress_manager=cf_ingress,
        )
    return _manager


def set_manager_for_tests(manager: WebSandboxManager | None) -> None:
    """Test-only injection point. ``None`` resets the singleton."""

    global _manager
    _manager = manager


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


class WebSandboxInstanceResponse(BaseModel):
    """Wire shape for :class:`WebSandboxInstance`. Intentionally
    a thin pass-through of ``to_dict()`` so the wire format tracks
    :data:`WEB_SANDBOX_SCHEMA_VERSION` without duplicating field
    declarations."""

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
    created_at: float
    started_at: float | None
    ready_at: float | None
    stopped_at: float | None
    last_request_at: float
    error: str | None
    killed_reason: str | None
    warnings: list[str]


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
    """Launch (or recover) a web-preview sidecar for ``workspace_id``."""

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
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        instance = manager.launch(config)
    except WebSandboxAlreadyExists as exc:  # pragma: no cover - idempotent=True path
        raise HTTPException(status_code=409, detail=str(exc))
    except WebSandboxError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _instance_to_response(instance)


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
    """Caller signals that the dev server has reported ready."""

    try:
        instance = manager.mark_ready(workspace_id)
    except WebSandboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except WebSandboxError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _instance_to_response(instance)


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
