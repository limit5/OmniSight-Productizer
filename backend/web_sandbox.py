"""W14.2 — Backend launcher for the ``omnisight-web-preview`` sidecar.

Owns ``POST /web-sandbox/preview``. Given a ``workspace_id``, it
materialises one sidecar container per workspace from the W14.1 image
(``omnisight-web-preview:dev``), bind-mounts the operator's checked-out
source at ``/workspace``, runs ``pnpm install``, and then ``pnpm dev``.

Why a dedicated module (sibling to :mod:`backend.ui_sandbox`)
============================================================

* :mod:`backend.ui_sandbox` (V2 #1) ships **per-session** Next.js dev
  containers keyed on ``session_id`` and aimed at the UI-component
  ReAct loop. Image is plain ``node:22-alpine``, command is
  ``npm run dev``, no Bun, no Vite-global, no manifest-driven version
  pinning.
* W14 ships **per-workspace** Vite/Bun/Nuxt SSR containers keyed on
  ``workspace_id`` and built from the W14.1 ``omnisight-web-preview``
  image. The CF Tunnel ingress (W14.3) routes
  ``preview-{sandbox_id}.{tunnel_host}`` to this container, so the
  naming + lifecycle is workspace-scoped, not session-scoped.

The two managers share the :class:`backend.ui_sandbox.DockerClient`
Protocol (structural typing — both call ``run_detached`` / ``stop`` /
``remove`` / ``logs`` / ``inspect``) so a single
:class:`SubprocessDockerClient` instance can drive both.

Row boundary
============

W14.2 owns:

  1. Resolving ``workspace_id`` → ``workspace_path`` (delegated to
     :func:`backend.workspace.get_workspace`).
  2. Optional ``git fetch`` + checkout pre-install (when the caller
     supplies ``git_ref``).
  3. ``pnpm install`` execution.
  4. Dev-command selection (``pnpm dev`` default; ``bun --bun nuxt dev``
     / ``vite preview`` etc. when overridden).
  5. ``docker run`` against the W14.1 image with bind-mount + env.
  6. In-memory bookkeeping of the live instance keyed on
     ``workspace_id``.

W14.2 explicitly does NOT own:

  - CF Tunnel ingress dynamic create/delete (W14.3).
  - Cloudflare Access SSO (W14.4).
  - Idle-timeout auto-kill reaper (W14.5).
  - ``<LivePreviewPanel/>`` frontend / iframe wiring (W14.6).
  - HMR WebSocket passthrough (W14.7).
  - PEP HOLD before first preview (W14.8).
  - cgroup resource limits (2GB / 1CPU / 5GB) (W14.9 — composition).
  - Alembic 0059 ``web_sandbox_instances`` table (W14.10) — until that
    lands, durable cross-worker state is the docker daemon itself
    (deterministic container name lookup) and the per-worker in-memory
    cache; W14.10 will replace the cache with row-level state.
  - R28-R30 risk register (W14.11).
  - Lifecycle / bypass-attempt tests (W14.12).

Module-global state audit (SOP §1)
==================================

The :class:`WebSandboxManager` keeps a per-worker dict
``_instances: dict[str, WebSandboxInstance]`` keyed on ``workspace_id``,
guarded by an ``RLock``. Under ``uvicorn --workers N`` each worker has
its own dict — they do **not** share Python state.

Cross-worker consistency answer falls under SOP §1 answer **#2 (PG /
docker coordination)** with explicit deferral:

  * Today the canonical state is the **docker daemon**: every worker
    can ``docker inspect <deterministic_container_name>`` to discover
    whether a sidecar is already up for ``workspace_id``.
    :func:`format_container_name` is pure, so all workers compute the
    same name — and docker enforces uniqueness on ``--name``.
  * The launcher tolerates two workers racing on the same
    ``workspace_id`` by catching the docker name-conflict on the loser
    and reading back the existing container via ``inspect``.
  * Persistent metadata (sandbox_id history, started_at, killed_at,
    killed_reason) lands in **Alembic 0059** (W14.10). Until then, two
    workers' in-memory caches will diverge on those fields — the row's
    ``Production status`` is documented as ``dev-only`` and W14.2 is
    **not** wired into the ``OMNISIGHT_*`` feature-flag environment
    until W14.10 closes that gap.

Read-after-write timing audit (SOP §2)
======================================

This row introduces a fresh module — there is no compat→pool migration
of existing serialisation. The only race surface is two workers
launching the same ``workspace_id`` concurrently:

  * Worker A calls ``docker run --name omnisight-web-preview-{sid}``
    → succeeds → updates A's ``_instances``.
  * Worker B calls ``docker run --name omnisight-web-preview-{sid}``
    → docker rejects (name conflict) → B catches and treats as
    idempotent: ``inspect`` recovers the existing container_id and
    rebuilds the instance snapshot.

The launcher is therefore safe under concurrent invocation; the
``ConflictError`` branch in :meth:`WebSandboxManager.launch` is the
one place that exercises it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from backend.ui_sandbox import (
    DockerClient,
    detect_dev_server_ready as _detect_dev_server_ready_shared,
)
# W14.3 — optional CF Tunnel ingress manager. Imported under a private
# alias to keep the public ``__all__`` of this module unchanged; the
# launcher accepts an optional manager via constructor injection so
# callers without W14.3 settings can keep using the W14.2 host-port
# preview_url path unchanged. The B12-style typed errors propagate
# back via per-instance warnings rather than failing the launch.
from backend.cf_ingress import (
    CFIngressError as _CFIngressError,
    CFIngressManager as _CFIngressManager,
)
# W14.4 — optional CF Access SSO manager. Imported under a private
# alias to keep the public ``__all__`` of this module unchanged; the
# launcher accepts an optional manager via constructor injection so
# callers without W14.4 settings can keep using the W14.3 ingress-only
# path unchanged. The CF Access errors propagate back via per-instance
# warnings rather than failing the launch (CF Access is best-effort
# during launch — the host-port preview_url still works for the
# operator's local tooling even if the SSO gate is briefly broken).
from backend.cf_access import (
    CFAccessError as _CFAccessError,
    CFAccessManager as _CFAccessManager,
)
# W14.9 — cgroup resource limits. A frozen value object that ships
# with row-spec defaults (2 GiB RAM / 1 CPU / 5 GiB disk) and renders
# to the docker-run argv extension at launch time. Per-launch override
# is via :attr:`WebSandboxConfig.resource_limits`; operator-wide policy
# lives in :class:`backend.web_sandbox_resource_limits.WebPreviewResourceLimits.from_settings`.
from backend.web_sandbox_resource_limits import (
    CGROUP_OOM_REASON as _CGROUP_OOM_REASON,
    WebPreviewResourceLimits as _WebPreviewResourceLimits,
)

logger = logging.getLogger(__name__)


__all__ = [
    "WEB_SANDBOX_SCHEMA_VERSION",
    "DEFAULT_IMAGE_TAG",
    "DEFAULT_INSTALL_COMMAND",
    "DEFAULT_DEV_COMMAND",
    "DEFAULT_CONTAINER_PORT",
    "DEFAULT_HOST_PORT_RANGE",
    "DEFAULT_WORKDIR",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "DEFAULT_PREVIEW_HOST",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_ABSOLUTE_IN_CONTAINER",
    "MAX_LOG_CHARS",
    "WebSandboxStatus",
    "WebPreviewManifest",
    "WebSandboxConfig",
    "WebSandboxInstance",
    "WebSandboxError",
    "WebSandboxAlreadyExists",
    "WebSandboxNotFound",
    "WebSandboxNameConflict",
    "WebSandboxManager",
    "DEFAULT_RESOURCE_LIMITS",
    "load_image_manifest",
    "format_sandbox_id",
    "format_container_name",
    "build_preview_url",
    "build_install_argv",
    "build_dev_argv",
    "build_composite_command",
    "build_docker_run_spec",
    "allocate_host_port",
    "validate_workspace_path",
    "detect_dev_server_ready",
]


#: Bump when :class:`WebSandboxConfig.to_dict()` /
#: :class:`WebSandboxInstance.to_dict()` shape changes — the W14.10
#: alembic 0059 row depends on this for forward-compat parsing.
WEB_SANDBOX_SCHEMA_VERSION = "1.0.0"

#: Default tag for the W14.1 sidecar image. The operator builds it
#: locally with ``docker build -f Dockerfile.web-preview .`` and tags
#: it ``omnisight-web-preview:dev``; CI pushes the same tag to the
#: registry the backend pulls from. Caller can override per-request
#: when running multi-version A/B (e.g. validating a Bun bump).
DEFAULT_IMAGE_TAG = "omnisight-web-preview:dev"

#: Default install command. ``--frozen-lockfile`` is intentional —
#: the operator's checked-out workspace already carries
#: ``pnpm-lock.yaml`` and a Vite-scaffolded preview must not silently
#: bump deps mid-install. If the lockfile is stale, ``pnpm install``
#: fails fast and the agent loop sees the failure rather than
#: discovering it three previews later.
DEFAULT_INSTALL_COMMAND: tuple[str, ...] = ("pnpm", "install", "--frozen-lockfile")

#: Default dev-server command. ``--host 0.0.0.0`` is required so the
#: dev server binds to every interface inside the container — the
#: docker port-publish only forwards traffic that hits the bound
#: socket, so a default ``localhost`` bind would silently 502 from
#: the host side.
DEFAULT_DEV_COMMAND: tuple[str, ...] = ("pnpm", "dev", "--host", "0.0.0.0")

#: Vite dev server's default port (matches ``EXPOSE 5173`` in the
#: W14.1 Dockerfile). The W14.1 image also exposes ``3000`` for Nuxt
#: SSR; callers building a Nuxt sandbox override
#: :attr:`WebSandboxConfig.container_port` to ``3000``.
DEFAULT_CONTAINER_PORT = 5173

#: Host-side port range. Disjoint from the V2 ``ui_sandbox`` range
#: (40000-40999) so an operator running both managers concurrently
#: doesn't trip a port collision. 41000-41999 keeps us inside the
#: typical ephemeral default while staying out of the registered
#: range.
DEFAULT_HOST_PORT_RANGE: tuple[int, int] = (41000, 41999)

#: Where the operator's workspace is bind-mounted inside the
#: container — must match :data:`web-preview/manifest.json::workdir`.
#: The manifest loader cross-checks this on launch.
DEFAULT_WORKDIR = "/workspace"

#: Hard cap on how long the launcher will wait for the dev server to
#: report ready. ``pnpm install`` cold-cache is 30-90s on a typical
#: Vite scaffold, so 180s gives 90s headroom for ``pnpm dev``
#: warm-up + esbuild optimisation. The W14.5 idle-kill reaper is
#: orthogonal and uses a separate 30-min budget.
DEFAULT_STARTUP_TIMEOUT_S = 180.0

#: ``docker stop -t`` grace period.
DEFAULT_STOP_TIMEOUT_S = 10.0

#: Host the preview URL is built against. Callers behind the
#: W14.3 CF Tunnel override to the tunnel host; until W14.3 lands,
#: ``127.0.0.1`` on the same host that runs the daemon is the only
#: reachable address.
DEFAULT_PREVIEW_HOST = "127.0.0.1"

#: Path from repo root to the W14.1 image manifest. Single source of
#: truth for image_name / version_pins / workdir / exposed_ports / uid
#: — read by the launcher to verify the docker-run spec lines up with
#: the image's promises.
MANIFEST_RELATIVE_PATH = Path("web-preview") / "manifest.json"

#: Where the manifest lives **inside** a running container — useful
#: for debug exec from inside the sidecar but not used by the
#: launcher (the launcher reads from the host's repo tree).
MANIFEST_ABSOLUTE_IN_CONTAINER = "/etc/omnisight/web-preview-manifest.json"

#: Hard cap on retained log bytes per instance — matches
#: :data:`backend.ui_sandbox.MAX_LOG_CHARS`.
MAX_LOG_CHARS = 200_000

#: Module-level default resource limits applied to every web-preview
#: sidecar that does not carry an explicit
#: :attr:`WebSandboxConfig.resource_limits` override. Matches the
#: W14.9 row spec literals: 2 GiB RAM / 1 CPU / 5 GiB writable-layer
#: disk. Frozen — consumers receive a single shared instance.
DEFAULT_RESOURCE_LIMITS = _WebPreviewResourceLimits.default()


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class WebSandboxError(RuntimeError):
    """Base class for web-sandbox launcher errors."""


class WebSandboxAlreadyExists(WebSandboxError):
    """Raised when the caller asks for a fresh sandbox but the
    workspace already has a non-terminal instance.

    Distinguished from :class:`WebSandboxNameConflict` so the router
    can return ``409`` for the in-memory case and fold the docker-side
    case into idempotent recovery.
    """


class WebSandboxNotFound(WebSandboxError):
    """Raised when the caller references an unknown ``workspace_id``."""


class WebSandboxNameConflict(WebSandboxError):
    """Raised when the docker daemon rejects a ``run`` because the
    deterministic container name already exists.

    The launcher catches this internally and recovers the existing
    instance via ``inspect`` — callers should never see it raised.
    """


# ───────────────────────────────────────────────────────────────────
#  Enum + dataclasses
# ───────────────────────────────────────────────────────────────────


class WebSandboxStatus(str, Enum):
    """Lifecycle states matching the W14.10 alembic 0059 ``status`` enum.

    ``pending`` → registered, ``docker run`` not yet issued.
    ``installing`` → ``pnpm install`` running inside the container.
    ``running`` → dev server has reported ready.
    ``stopping`` → stop requested, container winding down.
    ``stopped`` → container no longer running (graceful).
    ``failed`` → unrecoverable error; ``error`` field carries detail.
    """

    pending = "pending"
    installing = "installing"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"


_TERMINAL_STATUSES = frozenset({WebSandboxStatus.stopped, WebSandboxStatus.failed})


@dataclass(frozen=True)
class WebPreviewManifest:
    """Snapshot of ``web-preview/manifest.json`` (W14.1 contract).

    Only the fields the launcher consumes are surfaced as attributes;
    the full payload is retained as ``raw`` so callers needing extra
    metadata (tool ``version_check`` arrays, schema_version, etc.)
    don't need to re-parse the file.
    """

    image_name: str
    runtime_uid: int
    runtime_gid: int
    workdir: str
    exposed_ports: tuple[int, ...]
    version_pins: Mapping[str, str]
    entrypoint: str
    default_cmd: tuple[str, ...]
    schema_version: str
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.image_name, str) or not self.image_name.strip():
            raise ValueError("image_name must be non-empty")
        if not isinstance(self.runtime_uid, int) or self.runtime_uid <= 0:
            raise ValueError(
                f"runtime_uid must be a positive integer, got {self.runtime_uid!r}"
            )
        if not isinstance(self.runtime_gid, int) or self.runtime_gid <= 0:
            raise ValueError(
                f"runtime_gid must be a positive integer, got {self.runtime_gid!r}"
            )
        if not isinstance(self.workdir, str) or not self.workdir.startswith("/"):
            raise ValueError("workdir must be an absolute path")
        if not self.exposed_ports:
            raise ValueError("exposed_ports must be non-empty")
        for port in self.exposed_ports:
            if not isinstance(port, int) or not (1 <= port <= 65535):
                raise ValueError(f"exposed_ports entry out of range: {port!r}")
        if not isinstance(self.entrypoint, str) or not self.entrypoint.startswith("/"):
            raise ValueError("entrypoint must be an absolute path")
        if not self.default_cmd:
            raise ValueError("default_cmd must be non-empty")
        # Freeze nested containers so callers cannot mutate them.
        object.__setattr__(self, "exposed_ports", tuple(self.exposed_ports))
        object.__setattr__(self, "default_cmd", tuple(self.default_cmd))
        pins = dict(self.version_pins)
        for key, value in pins.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("version_pins keys/values must be strings")
        object.__setattr__(self, "version_pins", MappingProxyType(pins))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True)
class WebSandboxConfig:
    """Inputs to :meth:`WebSandboxManager.launch`.

    Frozen + deterministic — two configs with the same field values
    produce byte-identical :func:`build_docker_run_spec` output.

    ``git_ref`` opt-in: when ``None`` the launcher trusts the
    operator's already-checked-out workspace as-is. When set, the
    composite shell command does ``git fetch --all --tags`` followed
    by ``git checkout {ref}`` before running ``pnpm install``. The
    ref is shell-quoted before composition.
    """

    workspace_id: str
    workspace_path: str
    image_tag: str = DEFAULT_IMAGE_TAG
    git_ref: str | None = None
    install_command: tuple[str, ...] = DEFAULT_INSTALL_COMMAND
    dev_command: tuple[str, ...] = DEFAULT_DEV_COMMAND
    container_port: int = DEFAULT_CONTAINER_PORT
    host_port: int | None = None
    workdir: str = DEFAULT_WORKDIR
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S
    # W14.4 — emails to allow through the CF Access SSO gate. The
    # launcher unions this with the operator-wide
    # ``cf_access_default_emails`` allowlist before POSTing the policy
    # to CF. Empty tuple ⇒ rely entirely on the admin allowlist; when
    # both are empty and a CFAccessManager is wired in, launch will
    # surface a per-instance warning and skip CF Access app creation
    # (the ingress URL stays publicly reachable, which is the W14.3
    # behaviour). The router auto-prepends the launching operator's
    # email so a default launch always at least authorises the caller.
    allowed_emails: tuple[str, ...] = ()
    # W14.9 — cgroup hard caps for the sidecar. ``None`` means "use
    # the manager's default", which is :data:`DEFAULT_RESOURCE_LIMITS`
    # (2 GiB / 1 CPU / 5 GiB) unless the manager was constructed with
    # an explicit operator-policy override. Per-launch override is
    # rare — almost every caller wants the row's defaults — but the
    # field exists so the W14.10 audit row can record exactly what the
    # cgroup contract was when launch hit docker, even if the operator
    # later flips the manager-wide default.
    resource_limits: _WebPreviewResourceLimits | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("workspace_id must be a non-empty string")
        if not _SAFE_WORKSPACE_RE.fullmatch(self.workspace_id):
            raise ValueError(
                "workspace_id must match [A-Za-z0-9_.-]{1,128} — got "
                f"{self.workspace_id!r}"
            )
        if not isinstance(self.workspace_path, str) or not self.workspace_path.strip():
            raise ValueError("workspace_path must be a non-empty string")
        if not isinstance(self.image_tag, str) or not self.image_tag.strip():
            raise ValueError("image_tag must be non-empty")
        if self.git_ref is not None:
            if not isinstance(self.git_ref, str) or not self.git_ref.strip():
                raise ValueError("git_ref must be a non-empty string when set")
            if not _SAFE_GIT_REF_RE.fullmatch(self.git_ref):
                raise ValueError(
                    "git_ref must match [A-Za-z0-9_.\\-/]{1,200} — got "
                    f"{self.git_ref!r}"
                )
            # ``..`` is forbidden in git refs (git itself rejects it,
            # see git-check-ref-format(1)) — defence in depth so
            # composite-shell traversal is impossible.
            if ".." in self.git_ref:
                raise ValueError(
                    f"git_ref must not contain '..' — got {self.git_ref!r}"
                )
        if not isinstance(self.container_port, int) or not (
            1 <= self.container_port <= 65535
        ):
            raise ValueError(
                f"container_port out of range: {self.container_port!r}"
            )
        if self.host_port is not None:
            if not isinstance(self.host_port, int) or not (
                1 <= self.host_port <= 65535
            ):
                raise ValueError(f"host_port out of range: {self.host_port!r}")
        if not isinstance(self.workdir, str) or not self.workdir.startswith("/"):
            raise ValueError("workdir must be an absolute path")
        if not isinstance(self.startup_timeout_s, (int, float)) or self.startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be positive")
        if not isinstance(self.stop_timeout_s, (int, float)) or self.stop_timeout_s <= 0:
            raise ValueError("stop_timeout_s must be positive")
        # Normalise commands to tuples (accept lists defensively).
        for attr in ("install_command", "dev_command"):
            value = getattr(self, attr)
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{attr} must be a sequence of strings")
            normalised = tuple(str(part) for part in value)
            if not normalised:
                raise ValueError(f"{attr} must be non-empty")
            object.__setattr__(self, attr, normalised)
        env_src = dict(self.env) if self.env else {}
        for key, value in env_src.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("env keys and values must be strings")
        object.__setattr__(self, "env", MappingProxyType(env_src))
        # W14.4: normalise + validate allowed_emails. Accept lists
        # defensively (router builds it as a list) and reject any
        # non-string entry. Empty entries (whitespace) are dropped
        # silently — a CSV split with trailing comma is the most
        # common shape the operator hands us. The deep email-shape
        # validation happens later in cf_access.compute_effective_emails;
        # here we only enforce the structural shape so a malformed
        # request fails at config-construction time rather than
        # mid-launch.
        emails = self.allowed_emails
        if isinstance(emails, str):
            raise ValueError(
                "allowed_emails must be a sequence of strings, not a CSV "
                "string — split on ',' before constructing WebSandboxConfig"
            )
        if not isinstance(emails, (list, tuple)):
            raise ValueError("allowed_emails must be a sequence of strings")
        cleaned: list[str] = []
        for entry in emails:
            if not isinstance(entry, str):
                raise ValueError(
                    f"allowed_emails entry must be a string: {entry!r}"
                )
            stripped = entry.strip()
            if stripped:
                cleaned.append(stripped)
        object.__setattr__(self, "allowed_emails", tuple(cleaned))
        # W14.9: validate resource_limits. ``None`` ⇒ defer to manager
        # default at launch time. Reject every other type so an
        # accidental dict / mapping at the call site fails fast.
        if self.resource_limits is not None and not isinstance(
            self.resource_limits, _WebPreviewResourceLimits
        ):
            raise ValueError(
                "resource_limits must be WebPreviewResourceLimits or None: "
                f"got {type(self.resource_limits).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WEB_SANDBOX_SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "image_tag": self.image_tag,
            "git_ref": self.git_ref,
            "install_command": list(self.install_command),
            "dev_command": list(self.dev_command),
            "container_port": self.container_port,
            "host_port": self.host_port,
            "workdir": self.workdir,
            "env": dict(self.env),
            "startup_timeout_s": float(self.startup_timeout_s),
            "stop_timeout_s": float(self.stop_timeout_s),
            "allowed_emails": list(self.allowed_emails),
            "resource_limits": (
                None if self.resource_limits is None
                else self.resource_limits.to_dict()
            ),
        }


@dataclass(frozen=True)
class WebSandboxInstance:
    """Snapshot of a sandbox's state.

    Frozen — state transitions go through :func:`dataclasses.replace`.
    The manager stores the *current* instance and exposes it via
    :meth:`WebSandboxManager.get`. Audit log lives in W14.10 alembic
    0059 (future row).
    """

    workspace_id: str
    sandbox_id: str
    container_name: str
    config: WebSandboxConfig
    status: WebSandboxStatus = WebSandboxStatus.pending
    container_id: str | None = None
    host_port: int | None = None
    preview_url: str | None = None
    ingress_url: str | None = None
    # W14.4: CF Access application id (UUID) returned by the CF API
    # when the manager creates the per-sandbox SSO gate. Stored on the
    # instance so :meth:`WebSandboxManager.stop` can DELETE it without
    # re-listing every app on the account. ``None`` when no
    # CFAccessManager was wired in OR the launch-time create_application
    # call failed (the failure mode is folded into ``warnings``).
    access_app_id: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    ready_at: float | None = None
    stopped_at: float | None = None
    last_request_at: float = 0.0
    error: str | None = None
    killed_reason: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("workspace_id must be non-empty")
        if not isinstance(self.sandbox_id, str) or not self.sandbox_id.strip():
            raise ValueError("sandbox_id must be non-empty")
        if not isinstance(self.container_name, str) or not self.container_name.strip():
            raise ValueError("container_name must be non-empty")
        if not isinstance(self.status, WebSandboxStatus):
            raise ValueError(
                f"status must be WebSandboxStatus, got {type(self.status)!r}"
            )
        if self.created_at < 0 or self.last_request_at < 0:
            raise ValueError("timestamps must be non-negative")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def is_running(self) -> bool:
        return self.status is WebSandboxStatus.running

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def idle_seconds(self, now: float | None = None) -> float:
        """Seconds since the caller last bumped ``last_request_at``.

        A freshly-launched sandbox (``last_request_at == 0``) reports
        ``0.0`` so the W14.5 idle reaper does not collect it on the
        very first tick after launch.
        """

        if self.last_request_at <= 0:
            return 0.0
        ref = time.time() if now is None else now
        return max(0.0, ref - self.last_request_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WEB_SANDBOX_SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "sandbox_id": self.sandbox_id,
            "container_name": self.container_name,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "container_id": self.container_id,
            "host_port": self.host_port,
            "preview_url": self.preview_url,
            "ingress_url": self.ingress_url,
            "access_app_id": self.access_app_id,
            "created_at": float(self.created_at),
            "started_at": None if self.started_at is None else float(self.started_at),
            "ready_at": None if self.ready_at is None else float(self.ready_at),
            "stopped_at": None if self.stopped_at is None else float(self.stopped_at),
            "last_request_at": float(self.last_request_at),
            "error": self.error,
            "killed_reason": self.killed_reason,
            "warnings": list(self.warnings),
        }


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


_SAFE_WORKSPACE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
# Git refs are mostly safe but allow ``/`` for branch namespaces
# (``feature/foo``) and ``.`` for tags (``v1.2.3``). We deliberately
# refuse spaces, shell metachars, and the ``..`` traversal that git
# itself rejects but docker shell composition would pass.
_SAFE_GIT_REF_RE = re.compile(r"[A-Za-z0-9_./\-]{1,200}")


def load_image_manifest(repo_root: str | Path | None = None) -> WebPreviewManifest:
    """Load and validate ``web-preview/manifest.json``.

    The file is the W14.1 sidecar's machine-readable contract. The
    launcher reads it once at construction time so the docker-run
    spec we emit is consistent with the image's promises (workdir,
    runtime_uid, exposed_ports). Drift between Dockerfile and manifest
    is the W14.1 contract test's job; this loader only insists the
    file exists and contains the keys the launcher consumes.
    """

    if repo_root is None:
        # Default to the repo root inferred from this module's location:
        # backend/web_sandbox.py → repo_root/backend/web_sandbox.py.
        repo_root = Path(__file__).resolve().parents[1]
    repo_root_path = Path(repo_root)
    manifest_path = repo_root_path / MANIFEST_RELATIVE_PATH
    if not manifest_path.exists():
        raise WebSandboxError(
            f"web-preview manifest missing at {manifest_path}; "
            "ensure W14.1 artefacts are checked into the repo before "
            "calling load_image_manifest()."
        )
    try:
        payload = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise WebSandboxError(
            f"web-preview manifest at {manifest_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise WebSandboxError(
            f"web-preview manifest at {manifest_path} must be a JSON object"
        )
    try:
        return WebPreviewManifest(
            image_name=str(payload["image_name"]),
            runtime_uid=int(payload["runtime_uid"]),
            runtime_gid=int(payload["runtime_gid"]),
            workdir=str(payload["workdir"]),
            exposed_ports=tuple(int(p) for p in payload["exposed_ports"]),
            version_pins=dict(payload.get("version_pins", {})),
            entrypoint=str(payload["entrypoint"]),
            default_cmd=tuple(str(part) for part in payload["default_cmd"]),
            schema_version=str(payload.get("schema_version", "1")),
            raw=dict(payload),
        )
    except KeyError as exc:
        raise WebSandboxError(
            f"web-preview manifest missing required key: {exc}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise WebSandboxError(
            f"web-preview manifest invalid: {exc}"
        ) from exc


def format_sandbox_id(workspace_id: str) -> str:
    """Return a deterministic sandbox identifier for ``workspace_id``.

    The W14.3 CF Tunnel ingress will route
    ``https://preview-{sandbox_id}.{tunnel_host}`` to the container,
    so the ID needs to be DNS-label-safe (``[a-z0-9-]``, ≤ 63 chars).
    We hash the workspace_id (collision-free) and prefix ``ws-`` so
    it shows up sortable in ``docker ps`` next to W14.1 sibling
    sidecars (e.g. ``installer-`` from BS.4).

    Pure function — same input always returns the same ID, which is
    what makes :func:`format_container_name` deterministic across
    workers and re-launches.
    """

    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace_id must be a non-empty string")
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]
    return f"ws-{digest}"


def format_container_name(workspace_id: str, *, prefix: str = "omnisight-web-preview") -> str:
    """Produce a docker-safe container name keyed on ``workspace_id``.

    Uses :func:`format_sandbox_id` as the unique suffix so a worker
    that did not launch the sandbox can still compute the same name
    and look it up via ``docker inspect``. Truncated to 63 chars
    (the docker DNS label cap) — with a 12-char hex digest + the
    fixed ``ws-`` prefix + the default container prefix, the result
    is 32 chars, well under the cap.
    """

    sandbox_id = format_sandbox_id(workspace_id)
    full = f"{prefix}-{sandbox_id}"
    return full[:63]


def build_preview_url(
    host_port: int, *, host: str = DEFAULT_PREVIEW_HOST, path: str = "/"
) -> str:
    """Return ``http://{host}:{host_port}{path}`` with path normalised.

    Used until W14.3 lands the CF Tunnel ingress, after which the
    public URL switches to ``https://preview-{sandbox_id}.{tunnel_host}``
    (built by the W14.3 router and stored on
    :attr:`WebSandboxInstance.ingress_url`).
    """

    if not isinstance(host_port, int) or not (1 <= host_port <= 65535):
        raise ValueError(f"host_port out of range: {host_port!r}")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be non-empty")
    if not isinstance(path, str):
        raise ValueError("path must be string")
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{host}:{host_port}{path}"


def validate_workspace_path(workspace_path: str) -> Path:
    """Return the resolved ``Path`` if ``workspace_path`` exists and
    is a directory; raise :class:`ValueError` otherwise.

    Y6 #282 :func:`backend.workspace.get_workspace` returns a
    ``WorkspaceInfo`` whose ``path`` attribute is what callers feed
    here. We re-check rather than trusting the workspace registry
    because the operator can ``rm -rf`` a worktree out from under us
    between provision and launch.
    """

    if not isinstance(workspace_path, str) or not workspace_path.strip():
        raise ValueError("workspace_path must be non-empty")
    path = Path(workspace_path)
    if not path.is_absolute():
        raise ValueError(f"workspace_path must be absolute: {workspace_path!r}")
    if not path.exists():
        raise ValueError(f"workspace_path does not exist: {workspace_path!r}")
    if not path.is_dir():
        raise ValueError(f"workspace_path is not a directory: {workspace_path!r}")
    return path


def allocate_host_port(
    workspace_id: str,
    *,
    in_use: Iterable[int] = (),
    port_range: tuple[int, int] = DEFAULT_HOST_PORT_RANGE,
) -> int:
    """Deterministically allocate a host port for ``workspace_id``.

    Hashes the workspace_id to pick a starting slot, then linear-probes
    around ``in_use``. Determinism is the point — same workspace_id
    keeps landing on the same port across re-launches, which makes
    the operator's ``netstat`` / ``ss`` triage trivially predictable.
    Falls back to the first free slot only when the deterministic
    one is taken (e.g. a stale container the daemon hasn't reaped).
    """

    lo, hi = port_range
    if not (1 <= lo <= hi <= 65535):
        raise ValueError(f"port_range invalid: {port_range!r}")
    span = hi - lo + 1
    digest = hashlib.sha256(workspace_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % span
    taken = {int(p) for p in in_use}
    for offset in range(span):
        candidate = lo + (start + offset) % span
        if candidate not in taken:
            return candidate
    raise WebSandboxError(
        f"no host port available in range {port_range!r} "
        f"(all {span} slots in use)"
    )


def build_install_argv(config: WebSandboxConfig) -> list[str]:
    """Return the install argv as a list of strings — pure function."""

    return list(config.install_command)


def build_dev_argv(config: WebSandboxConfig) -> list[str]:
    """Return the dev-server argv as a list of strings — pure function."""

    return list(config.dev_command)


def build_composite_command(config: WebSandboxConfig) -> tuple[str, ...]:
    """Compose the single-shell command the container runs.

    Layout (all in one ``sh -c`` so signal handling stays clean —
    the W14.1 entrypoint shim just exec's whatever we pass):

    ::

        [git fetch + checkout (optional, when git_ref is set)] && \\
        pnpm install --frozen-lockfile && \\
        pnpm dev --host 0.0.0.0

    The composition runs as a single process tree under the W14.1
    image's tini PID-1 + entrypoint shim; tini reaps any pnpm child
    processes, and ``set -e`` semantics under ``sh -c`` mean a failed
    install short-circuits before we waste time on a doomed dev start.

    Each fragment is ``shlex.quote``'d, so an attacker-controlled
    ``git_ref`` (already constrained by :data:`_SAFE_GIT_REF_RE`)
    cannot break out of the install command.
    """

    fragments: list[str] = ["set -e"]
    if config.git_ref is not None:
        fragments.append("git fetch --all --tags")
        fragments.append(f"git checkout {shlex.quote(config.git_ref)}")
    install = " ".join(shlex.quote(part) for part in config.install_command)
    dev = " ".join(shlex.quote(part) for part in config.dev_command)
    fragments.append(install)
    fragments.append(dev)
    composite = " && ".join(fragments)
    return ("sh", "-c", composite)


def build_docker_run_spec(
    config: WebSandboxConfig,
    manifest: WebPreviewManifest | None = None,
    *,
    resource_limits: _WebPreviewResourceLimits | None = None,
) -> dict[str, Any]:
    """Return a deterministic dict describing the ``docker run`` invocation.

    Pure function — same ``(config, manifest, resource_limits)``
    always yields the same dict. Used by :class:`WebSandboxManager`
    to assemble the container spec; tests assert the exact shape
    without stubbing docker.

    When ``manifest`` is provided, the spec cross-checks
    ``config.workdir == manifest.workdir`` and
    ``config.container_port in manifest.exposed_ports`` — drift
    between W14.2 caller and W14.1 image fails fast with a clear
    error rather than silently 502'ing the iframe.

    When ``manifest`` is ``None`` the spec is emitted with the
    caller's literal config values; this is the test-time path
    where the manifest may not exist yet.

    W14.9 — when ``resource_limits`` is supplied (non-None), the
    spec includes ``memory_limit_bytes`` / ``cpu_limit`` /
    ``storage_limit_bytes`` / ``memory_swap_disabled`` keys so the
    docker-run argv carries the cgroup caps. A ``None`` value
    omits those keys entirely so the container starts with no caps
    (test/dev path; production always passes the row-spec defaults
    via :attr:`WebSandboxManager._resource_limits`).
    """

    if not isinstance(config, WebSandboxConfig):
        raise TypeError("config must be a WebSandboxConfig")
    if manifest is not None and not isinstance(manifest, WebPreviewManifest):
        raise TypeError("manifest must be a WebPreviewManifest or None")
    if resource_limits is not None and not isinstance(
        resource_limits, _WebPreviewResourceLimits
    ):
        raise TypeError(
            "resource_limits must be WebPreviewResourceLimits or None"
        )

    if manifest is not None:
        if config.workdir != manifest.workdir:
            raise WebSandboxError(
                f"config.workdir={config.workdir!r} disagrees with "
                f"manifest.workdir={manifest.workdir!r} — operator must "
                "set WebSandboxConfig.workdir to match the W14.1 image."
            )
        if config.container_port not in manifest.exposed_ports:
            raise WebSandboxError(
                f"config.container_port={config.container_port!r} not in "
                f"manifest.exposed_ports={list(manifest.exposed_ports)!r} — "
                "the W14.3 CF Tunnel ingress will only route to "
                "ports the image explicitly EXPOSEs."
            )

    mounts = (
        {
            "source": config.workspace_path,
            "target": config.workdir,
            "type": "bind",
            "read_only": False,
        },
    )
    env = dict(config.env)
    env.setdefault("HOST", "0.0.0.0")
    env.setdefault("PORT", str(config.container_port))
    env.setdefault("NODE_ENV", "development")

    ports: dict[int, int] = {}
    if config.host_port is not None:
        ports[config.host_port] = config.container_port

    spec: dict[str, Any] = {
        "schema_version": WEB_SANDBOX_SCHEMA_VERSION,
        "image": config.image_tag,
        "container_name": format_container_name(config.workspace_id),
        "command": list(build_composite_command(config)),
        "mounts": [dict(m) for m in mounts],
        "ports": ports,
        "env": dict(sorted(env.items())),
        "workdir": config.workdir,
    }
    if resource_limits is not None:
        spec["memory_limit_bytes"] = int(resource_limits.memory_limit_bytes)
        spec["cpu_limit"] = float(resource_limits.cpu_limit)
        spec["storage_limit_bytes"] = (
            None if resource_limits.storage_limit_bytes is None
            else int(resource_limits.storage_limit_bytes)
        )
        spec["memory_swap_disabled"] = bool(resource_limits.memory_swap_disabled)
        spec["resource_limits"] = resource_limits.to_dict()
    return spec


def detect_dev_server_ready(log_text: str) -> bool:
    """Pass-through to :func:`backend.ui_sandbox.detect_dev_server_ready`.

    Re-exported here so callers can ``from backend.web_sandbox import
    detect_dev_server_ready`` without a cross-module import. The
    ready patterns are shared because Vite / Next / Nuxt all emit one
    of the same ready banners (``Local: http://...``,
    ``ready in N ms``, ``listening on``).
    """

    return _detect_dev_server_ready_shared(log_text)


# ───────────────────────────────────────────────────────────────────
#  Manager
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class WebSandboxManager:
    """Thread-safe registry of live web-preview sidecars keyed on
    ``workspace_id``.

    One sidecar per workspace. Callers ``launch()`` (idempotent — a
    second call with the same workspace_id while the first is running
    returns the existing instance), ``touch()`` on every operator
    interaction, ``stop()`` when done.

    The W14.5 idle-kill reaper sits on top of this manager and reads
    :attr:`WebSandboxInstance.last_request_at` to decide what to
    collect.
    """

    def __init__(
        self,
        *,
        docker_client: DockerClient,
        manifest: WebPreviewManifest | None = None,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        preview_host: str = DEFAULT_PREVIEW_HOST,
        port_range: tuple[int, int] = DEFAULT_HOST_PORT_RANGE,
        cf_ingress_manager: _CFIngressManager | None = None,
        cf_access_manager: _CFAccessManager | None = None,
        resource_limits: _WebPreviewResourceLimits | None = None,
    ) -> None:
        self._docker = docker_client
        self._manifest = manifest
        self._clock = clock
        self._event_cb = event_cb
        self._preview_host = preview_host
        self._port_range = port_range
        # W14.9: operator-policy resource caps. None ⇒ row-spec
        # defaults (2 GiB / 1 CPU / 5 GiB). Per-launch override via
        # :attr:`WebSandboxConfig.resource_limits` takes precedence.
        if resource_limits is not None and not isinstance(
            resource_limits, _WebPreviewResourceLimits
        ):
            raise TypeError(
                "resource_limits must be WebPreviewResourceLimits or None"
            )
        self._resource_limits = (
            resource_limits if resource_limits is not None
            else DEFAULT_RESOURCE_LIMITS
        )
        # W14.3: optional CF ingress manager. ``None`` ⇒ no public
        # https URL is provisioned and ``ingress_url`` stays ``None``
        # (W14.2 dev path). When set, launch/stop call into the
        # manager and append warnings on per-launch CF errors rather
        # than failing the launch — a transient CF outage should not
        # prevent the operator from touching the localhost preview.
        self._cf_ingress = cf_ingress_manager
        # W14.4: optional CF Access SSO manager. ``None`` ⇒ ingress URL
        # (when present) is publicly reachable with no auth gate (W14.3
        # dev path). When set, launch creates a per-sandbox CF Access
        # application that bounces unauthenticated visitors to the
        # operator's IdP; the OIDC token CF Access issues lines up with
        # the OmniSight session via the email allowlist on the policy
        # (operator's email + ``cf_access_default_emails``). A
        # transient CF Access outage during launch is folded into a
        # per-instance warning rather than failing the launch.
        self._cf_access = cf_access_manager
        self._lock = threading.RLock()
        self._instances: dict[str, WebSandboxInstance] = {}

    @property
    def manifest(self) -> WebPreviewManifest | None:
        """The W14.1 manifest the manager validates against, or
        ``None`` when running in test/dev mode without one."""

        return self._manifest

    @property
    def resource_limits(self) -> _WebPreviewResourceLimits:
        """Operator-policy cgroup caps applied at launch when the
        per-config override is absent. Frozen — return a single
        shared instance per manager."""

        return self._resource_limits

    # ─────────────── Public API ───────────────

    def launch(
        self,
        config: WebSandboxConfig,
        *,
        idempotent: bool = True,
    ) -> WebSandboxInstance:
        """Launch (or recover) the sidecar for ``config.workspace_id``.

        Idempotent by default: if an instance already exists in a
        non-terminal state, return it without re-running ``docker``.
        Set ``idempotent=False`` to require a fresh launch and raise
        :class:`WebSandboxAlreadyExists` when one is already up.

        On docker name-conflict (another worker won the race), the
        launcher recovers the existing container's id via
        ``inspect`` and rebuilds the instance snapshot — callers
        never see :class:`WebSandboxNameConflict`.
        """

        if not isinstance(config, WebSandboxConfig):
            raise TypeError("config must be a WebSandboxConfig")
        validate_workspace_path(config.workspace_path)

        with self._lock:
            existing = self._instances.get(config.workspace_id)
            if existing is not None and not existing.is_terminal:
                if idempotent:
                    bumped = replace(existing, last_request_at=self._clock())
                    self._instances[config.workspace_id] = bumped
                    return bumped
                raise WebSandboxAlreadyExists(
                    f"workspace_id {config.workspace_id!r} already has a "
                    f"running sandbox (status={existing.status.value})"
                )

            host_port = config.host_port
            if host_port is None:
                in_use = {
                    inst.host_port
                    for inst in self._instances.values()
                    if inst.host_port is not None
                }
                host_port = allocate_host_port(
                    config.workspace_id,
                    in_use=in_use,
                    port_range=self._port_range,
                )
                config = replace(config, host_port=host_port)

            sandbox_id = format_sandbox_id(config.workspace_id)
            container_name = format_container_name(config.workspace_id)
            now = self._clock()
            pending = WebSandboxInstance(
                workspace_id=config.workspace_id,
                sandbox_id=sandbox_id,
                container_name=container_name,
                config=config,
                status=WebSandboxStatus.pending,
                host_port=host_port,
                created_at=now,
                last_request_at=now,
            )
            self._instances[config.workspace_id] = pending

            # W14.9: resolve effective resource limits — per-config
            # override beats manager-wide policy. The result is folded
            # into the docker-run argv via run_detached's keyword args.
            effective_limits = (
                config.resource_limits
                if config.resource_limits is not None
                else self._resource_limits
            )
            spec = build_docker_run_spec(
                config, self._manifest, resource_limits=effective_limits
            )
            try:
                container_id = self._docker.run_detached(
                    image=spec["image"],
                    name=spec["container_name"],
                    command=spec["command"],
                    mounts=spec["mounts"],
                    ports=spec["ports"],
                    env=spec["env"],
                    workdir=spec["workdir"],
                    memory_limit_bytes=spec.get("memory_limit_bytes"),
                    cpu_limit=spec.get("cpu_limit"),
                    storage_limit_bytes=spec.get("storage_limit_bytes"),
                    memory_swap_disabled=spec.get("memory_swap_disabled", True),
                )
            except Exception as exc:
                if _is_name_conflict(exc):
                    container_id = self._recover_existing_container_id(container_name)
                    if container_id is None:
                        failed = replace(
                            pending,
                            status=WebSandboxStatus.failed,
                            error=f"name_conflict_unrecoverable: {exc}",
                            last_request_at=self._clock(),
                        )
                        self._instances[config.workspace_id] = failed
                        self._emit("web_sandbox.failed", failed)
                        return failed
                else:
                    failed = replace(
                        pending,
                        status=WebSandboxStatus.failed,
                        error=f"docker_run_failed: {exc}",
                        last_request_at=self._clock(),
                    )
                    self._instances[config.workspace_id] = failed
                    self._emit("web_sandbox.failed", failed)
                    return failed

            preview_url = build_preview_url(host_port, host=self._preview_host)
            ingress_url: str | None = None
            launch_warnings: list[str] = list(pending.warnings)
            if self._cf_ingress is not None:
                try:
                    ingress_url = self._cf_ingress.create_rule(
                        sandbox_id=sandbox_id,
                        host_port=host_port,
                    )
                except _CFIngressError as exc:
                    # CF API is best-effort during launch — the host-port
                    # preview_url remains valid for the operator's local
                    # tooling and the iframe panel can still render.
                    # W14.5 idle-kill or W14.10 audit row will surface
                    # the warning to operator triage.
                    launch_warnings.append(f"cf_ingress_create_failed: {exc}")
                    logger.warning(
                        "web_sandbox: cf_ingress create_rule failed for "
                        "workspace_id=%s sandbox_id=%s: %s",
                        config.workspace_id,
                        sandbox_id,
                        exc,
                    )
            # W14.4: provision the CF Access SSO gate. Best-effort —
            # a CF Access outage at launch time leaves the ingress URL
            # publicly reachable (W14.3 behaviour); the failure is
            # surfaced as a per-instance warning so operator triage
            # can spot it. We deliberately attempt CF Access AFTER CF
            # ingress so the order matches the lifecycle the public
            # URL goes through (DNS / tunnel routing first, then SSO
            # gate). Skipping CF Access when the operator supplied no
            # emails AND the manager has no defaults avoids posting an
            # always-deny policy that would lock everyone out — we
            # surface that as a warning instead.
            access_app_id: str | None = None
            if self._cf_access is not None:
                requested_emails = tuple(config.allowed_emails)
                default_emails = self._cf_access.config.default_emails
                if not requested_emails and not default_emails:
                    launch_warnings.append(
                        "cf_access_skipped: no emails to allow — set "
                        "WebSandboxConfig.allowed_emails or configure "
                        "OMNISIGHT_CF_ACCESS_DEFAULT_EMAILS to enable SSO"
                    )
                    logger.warning(
                        "web_sandbox: cf_access skipped for workspace_id=%s "
                        "sandbox_id=%s — no emails to allow",
                        config.workspace_id,
                        sandbox_id,
                    )
                else:
                    try:
                        record = self._cf_access.create_application(
                            sandbox_id=sandbox_id,
                            emails=requested_emails,
                        )
                        access_app_id = record.app_id
                    except _CFAccessError as exc:
                        launch_warnings.append(
                            f"cf_access_create_failed: {exc}"
                        )
                        logger.warning(
                            "web_sandbox: cf_access create_application failed "
                            "for workspace_id=%s sandbox_id=%s: %s",
                            config.workspace_id,
                            sandbox_id,
                            exc,
                        )
            started = replace(
                pending,
                status=WebSandboxStatus.installing,
                container_id=container_id,
                preview_url=preview_url,
                ingress_url=ingress_url,
                access_app_id=access_app_id,
                started_at=self._clock(),
                last_request_at=self._clock(),
                warnings=tuple(launch_warnings),
            )
            self._instances[config.workspace_id] = started
        self._emit("web_sandbox.launched", started)
        return started

    def mark_ready(self, workspace_id: str) -> WebSandboxInstance:
        """Caller's signal that the dev server is responding. Called
        after polling container logs with
        :func:`detect_dev_server_ready`. Idempotent."""

        with self._lock:
            instance = self._require(workspace_id)
            if instance.status is WebSandboxStatus.running:
                return instance
            if instance.status is not WebSandboxStatus.installing:
                raise WebSandboxError(
                    f"cannot mark ready from status {instance.status.value!r}"
                )
            ready = replace(
                instance,
                status=WebSandboxStatus.running,
                ready_at=self._clock(),
                last_request_at=self._clock(),
            )
            self._instances[workspace_id] = ready
        self._emit("web_sandbox.ready", ready)
        return ready

    def touch(self, workspace_id: str) -> WebSandboxInstance:
        """Update ``last_request_at`` to the current clock — keeps
        the W14.5 idle-kill reaper from collecting an actively-used
        sandbox."""

        with self._lock:
            instance = self._require(workspace_id)
            if instance.is_terminal:
                return instance
            touched = replace(instance, last_request_at=self._clock())
            self._instances[workspace_id] = touched
            return touched

    def stop(
        self,
        workspace_id: str,
        *,
        remove: bool = True,
        reason: str | None = None,
    ) -> WebSandboxInstance:
        """Stop (and optionally ``docker rm``) the container.

        ``reason`` is recorded on the instance as ``killed_reason``
        — values like ``idle_timeout`` (W14.5), ``operator_request``,
        ``cgroup_oom`` (W14.9) so the future W14.10 audit row can
        explain *why* the sandbox died.
        """

        with self._lock:
            instance = self._require(workspace_id)
            if instance.is_terminal:
                return instance
            stopping = replace(
                instance,
                status=WebSandboxStatus.stopping,
                last_request_at=self._clock(),
            )
            self._instances[workspace_id] = stopping
            warnings: list[str] = list(instance.warnings)
            # W14.9: detect cgroup OOM-kill BEFORE docker stop+rm tears
            # down the container — the inspect payload disappears with
            # ``docker rm`` so this is our only window. ``State.OOMKilled``
            # is True iff the kernel oom-killer fired because the
            # container exceeded its --memory cap. We treat that as a
            # stronger reason than whatever the caller passed (e.g. the
            # idle reaper might call stop(reason="idle_timeout") on a
            # container that actually died of OOM minutes ago).
            oom_detected = self._inspect_oom_killed(instance.container_id)
            if oom_detected:
                # Override caller-supplied reason so the audit trail
                # reflects the kernel verdict, not the manager's guess.
                reason = _CGROUP_OOM_REASON
                warnings.append(
                    "cgroup_oom_detected: container exceeded memory limit "
                    "before stop()"
                )
            if instance.container_id:
                try:
                    self._docker.stop(
                        instance.container_id,
                        timeout_s=instance.config.stop_timeout_s,
                    )
                except Exception as exc:
                    warnings.append(f"stop_failed: {exc}")
                if remove:
                    try:
                        self._docker.remove(instance.container_id, force=True)
                    except Exception as exc:
                        warnings.append(f"remove_failed: {exc}")
            # W14.3: best-effort CF ingress cleanup. A failure here does
            # not block the local stop — an orphan CF rule pointing at a
            # dead container returns 502 to the public URL but does not
            # prevent the operator from launching a fresh sandbox (which
            # would replace the rule via create_rule's idempotent splice).
            if self._cf_ingress is not None and instance.ingress_url:
                try:
                    self._cf_ingress.delete_rule(instance.sandbox_id)
                except _CFIngressError as exc:
                    warnings.append(f"cf_ingress_delete_failed: {exc}")
                    logger.warning(
                        "web_sandbox: cf_ingress delete_rule failed for "
                        "workspace_id=%s sandbox_id=%s: %s",
                        workspace_id,
                        instance.sandbox_id,
                        exc,
                    )
            # W14.4: best-effort CF Access SSO cleanup. A failure here
            # leaves an orphan Access app pointing at a hostname whose
            # tunnel rule we just removed — visitors hit the SSO gate
            # then 502 from the now-missing tunnel route. The next
            # ``cleanup()`` call (or the W14.5 idle reaper or the
            # operator's CF dashboard) reaps it; we don't block the
            # local stop on it. Skip when ``access_app_id`` is None
            # (CF Access wasn't wired in OR launch-time create failed).
            if self._cf_access is not None and instance.access_app_id:
                try:
                    self._cf_access.delete_application(instance.sandbox_id)
                except _CFAccessError as exc:
                    warnings.append(f"cf_access_delete_failed: {exc}")
                    logger.warning(
                        "web_sandbox: cf_access delete_application failed for "
                        "workspace_id=%s sandbox_id=%s: %s",
                        workspace_id,
                        instance.sandbox_id,
                        exc,
                    )
            stopped = replace(
                stopping,
                status=WebSandboxStatus.stopped,
                stopped_at=self._clock(),
                last_request_at=self._clock(),
                killed_reason=reason or instance.killed_reason,
                warnings=tuple(warnings),
            )
            self._instances[workspace_id] = stopped
        self._emit("web_sandbox.stopped", stopped)
        return stopped

    def remove(self, workspace_id: str) -> WebSandboxInstance:
        """Forget a workspace. Must be in a terminal state — call
        :meth:`stop` first."""

        with self._lock:
            instance = self._require(workspace_id)
            if not instance.is_terminal:
                raise WebSandboxError(
                    f"cannot remove sandbox in status {instance.status.value!r} "
                    "— call stop() first"
                )
            del self._instances[workspace_id]
        return instance

    def get(self, workspace_id: str) -> WebSandboxInstance | None:
        with self._lock:
            return self._instances.get(workspace_id)

    def list(self) -> tuple[WebSandboxInstance, ...]:
        with self._lock:
            return tuple(self._instances.values())

    def logs(self, workspace_id: str, *, tail: int | None = 200) -> str:
        """Fetch container logs (capped at :data:`MAX_LOG_CHARS`)."""

        with self._lock:
            instance = self._require(workspace_id)
            container_id = instance.container_id
        if not container_id:
            return ""
        try:
            raw = self._docker.logs(container_id, tail=tail)
        except Exception as exc:  # pragma: no cover
            logger.warning("docker logs failed for %s: %s", workspace_id, exc)
            return ""
        if len(raw) > MAX_LOG_CHARS:
            return raw[-MAX_LOG_CHARS:]
        return raw

    def poll_ready(self, workspace_id: str, *, tail: int = 200) -> bool:
        """Convenience: fetch recent logs + :func:`detect_dev_server_ready`.

        Used by callers that don't have a custom ready signal — for
        Vite / Nuxt the dev server emits "Local: http://..." which
        :data:`backend.ui_sandbox.READY_PATTERNS` matches.
        """

        return detect_dev_server_ready(self.logs(workspace_id, tail=tail))

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dict describing all live sidecars."""

        with self._lock:
            return {
                "schema_version": WEB_SANDBOX_SCHEMA_VERSION,
                "sandboxes": [inst.to_dict() for inst in self._instances.values()],
                "count": len(self._instances),
            }

    # ─────────────── Internal ───────────────

    def _require(self, workspace_id: str) -> WebSandboxInstance:
        instance = self._instances.get(workspace_id)
        if instance is None:
            raise WebSandboxNotFound(
                f"no sandbox for workspace_id={workspace_id!r}"
            )
        return instance

    def _recover_existing_container_id(self, container_name: str) -> str | None:
        """When docker rejects ``run`` due to name collision, look up
        the existing container's id via ``inspect`` so the manager
        can resume tracking it.
        """

        try:
            data = self._docker.inspect(container_name)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "web_sandbox: could not inspect existing %s: %s", container_name, exc
            )
            return None
        if not data:
            return None
        cid = data.get("Id") or data.get("id")
        if isinstance(cid, str) and cid:
            return cid
        return None

    def _inspect_oom_killed(self, container_id: str | None) -> bool:
        """Return True iff docker reports the container was OOM-killed.

        W14.9: docker's ``inspect`` payload carries
        ``State.OOMKilled = true`` when the kernel oom-killer fired
        because the container's cgroup hit its --memory cap. We read
        this on the way out of :meth:`stop` so the killed_reason on
        the final instance reflects what actually happened, not what
        the caller guessed. Best-effort — any inspect error returns
        False (no false positives — better to record the caller's
        reason than make up a kernel event).
        """

        if not container_id:
            return False
        try:
            data = self._docker.inspect(container_id)
        except Exception as exc:  # pragma: no cover - inspect is best-effort
            logger.warning(
                "web_sandbox: oom-detection inspect failed for %s: %s",
                container_id, exc,
            )
            return False
        if not data:
            return False
        state = data.get("State") if isinstance(data, Mapping) else None
        if not isinstance(state, Mapping):
            return False
        return bool(state.get("OOMKilled"))

    def _emit(self, event_type: str, instance: WebSandboxInstance) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, instance.to_dict())
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning("web_sandbox event callback raised: %s", exc)


def _is_name_conflict(exc: BaseException) -> bool:
    """Heuristic for the ``docker run --name`` collision error.

    The CLI message is:
    ``Error response from daemon: Conflict. The container name "..." is already in use``
    — different docker versions vary the prose so we match the
    distinctive substrings rather than the full string.
    """

    text = str(exc).lower()
    return (
        "conflict" in text
        and "container name" in text
        and "already in use" in text
    )
