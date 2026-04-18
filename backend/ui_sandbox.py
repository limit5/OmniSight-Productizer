"""V2 #1 (issue #318) — Per-session Next.js dev server sandbox manager.

Runs a Next.js (or compatible) dev server inside a Docker container
per agent session.  The agent's workspace is bind-mounted into the
container, so any file the agent writes triggers the dev server's
hot-module-replacement (HMR) channel automatically — no container
restart required.

Why this module exists
----------------------

The V1 pipeline (``backend/ui_component_registry.py`` /
``design_token_loader.py`` / ``component_consistency_linter.py`` /
``vision_to_ui.py`` / ``figma_to_ui.py`` / ``url_to_reference.py`` /
``edit_complexity_router.py`` + NL integration test) produced
*static* TSX artefacts.  V2 (#318) closes the loop by actually
*rendering* those artefacts so the agent can visually verify its
output before handing off.  The three V2 primitives are:

  1. **This module** — Docker-level lifecycle of a per-session dev
     server keyed on ``session_id``.  Volume-mount the workspace,
     expose the dev port, capture stdout/stderr, expose structured
     state (``SandboxInstance``).  **Does not** screenshot — that's
     V2 row 3 (``ui_screenshot.py``).
  2. Sandbox lifecycle policy (V2 row 2) — builds on top of this
     module to enforce "1 sandbox per session, idle 15 min auto-reap".
     Primitives here are written to make that row a thin policy wrapper.
  3. Playwright screenshot service (V2 row 3) — talks to the running
     dev server via ``preview_url`` produced here.

Design decisions
----------------

* **Dependency-injected Docker client.**  The backend never imports
  ``docker`` (the Python SDK), because (a) backend requirements stay
  lean and (b) tests can use an in-memory :class:`FakeDockerClient`
  fixture without needing a real daemon.  A default
  :class:`SubprocessDockerClient` that shells out to the ``docker``
  CLI is provided so production callers aren't forced to write one.
* **Frozen records.**  :class:`SandboxConfig` and
  :class:`SandboxInstance` are frozen dataclasses with
  ``to_dict()`` — state transitions go through
  :func:`dataclasses.replace` so the manager's history log is
  trivially auditable.
* **Deterministic run spec.**  :func:`build_docker_run_spec` is a
  pure function of :class:`SandboxConfig`: same input → byte-identical
  ``dict`` output.  This lets tests assert the exact ``docker run``
  argv without stubbing time/os.
* **One sandbox per session.**  :meth:`SandboxManager.create` raises
  :class:`SandboxAlreadyExists` if the session already has a
  sandbox.  Callers must explicitly :meth:`stop` + :meth:`remove`
  before creating another.
* **Graceful fallback.**  When the Docker client raises,
  :meth:`SandboxManager.start` captures the error into
  ``SandboxInstance.error`` and marks status ``failed`` — it does not
  propagate an exception mid-agent-loop.

Contract (pinned by ``backend/tests/test_ui_sandbox.py``)
---------------------------------------------------------

* :data:`UI_SANDBOX_SCHEMA_VERSION` is a semver string; bump on any
  change to the shape of :class:`SandboxConfig.to_dict()` or
  :class:`SandboxInstance.to_dict()`.
* :data:`DEFAULT_SANDBOX_IMAGE` pins the base image tag — changing
  it is a visible ops event (ships as major).
* :data:`DEFAULT_IDLE_LIMIT_S` == ``900.0`` matches the V2 row 2
  "idle 15 min 自動回收" spec.  The limit is a *recommendation*
  here — actual reaping belongs to the lifecycle policy module.
* :func:`build_docker_run_spec` is pure and deterministic.
* :func:`build_preview_url` returns
  ``http://{host}:{host_port}{path}`` with path normalised.
* :func:`detect_dev_server_ready` matches the Next.js / Vite "ready"
  banners — used by callers polling container logs during startup.
* :func:`parse_compile_error` best-effort parses Next.js-style
  compile errors from stderr.  Returns an empty tuple when nothing
  matches — never raises.
* :class:`SandboxManager` is thread-safe.  All public methods take
  the internal lock.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)


__all__ = [
    "UI_SANDBOX_SCHEMA_VERSION",
    "DEFAULT_SANDBOX_IMAGE",
    "DEFAULT_DEV_COMMAND",
    "DEFAULT_CONTAINER_PORT",
    "DEFAULT_HOST_PORT_RANGE",
    "DEFAULT_WORKDIR",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "DEFAULT_IDLE_LIMIT_S",
    "DEFAULT_NODE_ENV",
    "DEFAULT_PREVIEW_HOST",
    "MAX_LOG_CHARS",
    "READY_PATTERNS",
    "SandboxStatus",
    "SandboxConfig",
    "SandboxInstance",
    "CompileError",
    "DockerClient",
    "SubprocessDockerClient",
    "SandboxManager",
    "SandboxError",
    "SandboxAlreadyExists",
    "SandboxNotFound",
    "build_docker_run_spec",
    "build_preview_url",
    "detect_dev_server_ready",
    "parse_compile_error",
    "validate_workspace",
    "allocate_host_port",
    "format_container_name",
    "render_sandbox_status_markdown",
]


#: Bump on any change to the :class:`SandboxConfig` / :class:`SandboxInstance`
#: dict shape — callers cache snapshots keyed on this.
UI_SANDBOX_SCHEMA_VERSION = "1.0.0"

#: Base image for the Next.js dev container. Node 22 LTS (Alpine) has
#: the smallest footprint that ships ``npm`` / ``pnpm`` / ``yarn``.
DEFAULT_SANDBOX_IMAGE = "node:22-alpine"

#: Default dev-server command. We bind to ``0.0.0.0`` so the host
#: can reach the exposed port, and keep ``npm run dev`` as the
#: canonical entrypoint — it matches every template in
#: ``configs/roles/ui-designer.md``.
DEFAULT_DEV_COMMAND: tuple[str, ...] = (
    "sh",
    "-c",
    "npm run dev -- --port 3000 --hostname 0.0.0.0",
)

#: Port the dev server listens on inside the container.
DEFAULT_CONTAINER_PORT = 3000

#: Host-side port range used by :func:`allocate_host_port` when the
#: caller doesn't pin one. 40000-40999 is outside the IANA
#: well-known / registered ranges and the typical ephemeral default.
DEFAULT_HOST_PORT_RANGE: tuple[int, int] = (40000, 40999)

#: Where the agent's workspace is mounted inside the container.
DEFAULT_WORKDIR = "/app"

#: How long :meth:`SandboxManager.start` will wait for the dev
#: server to report ready before marking the sandbox failed.
DEFAULT_STARTUP_TIMEOUT_S = 60.0

#: ``docker stop -t`` grace period.
DEFAULT_STOP_TIMEOUT_S = 10.0

#: Default idle reaper limit — 15 minutes matches V2 row 2 spec.
DEFAULT_IDLE_LIMIT_S = 900.0

#: Value injected into ``NODE_ENV``. Production templates would use
#: ``production`` but the whole point of this sandbox is *dev*.
DEFAULT_NODE_ENV = "development"

#: Host the preview URL is built against.  Callers running behind a
#: reverse proxy override via ``SandboxManager(preview_host=…)``.
DEFAULT_PREVIEW_HOST = "127.0.0.1"

#: Hard cap on how much container log we retain per sandbox. Prevents
#: unbounded memory growth if a dev server spews errors forever.
MAX_LOG_CHARS = 200_000

#: Heuristic regexes for detecting dev-server readiness across the
#: common JS tooling we might see (Next.js / Vite / CRA).  The order
#: is stable — :func:`detect_dev_server_ready` returns on first match.
READY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ready\s*(?:in|started|on)\s+", re.IGNORECASE),
    re.compile(r"local:\s*https?://", re.IGNORECASE),
    re.compile(r"compiled\s+(?:successfully|client and server)", re.IGNORECASE),
    re.compile(r"started server on", re.IGNORECASE),
    re.compile(r"listening on", re.IGNORECASE),
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class SandboxError(RuntimeError):
    """Base class for sandbox-manager errors."""


class SandboxAlreadyExists(SandboxError):
    """Raised by :meth:`SandboxManager.create` when the session already
    has a live sandbox.  Callers must stop + remove it first."""


class SandboxNotFound(SandboxError):
    """Raised when the caller references an unknown ``session_id``."""


# ───────────────────────────────────────────────────────────────────
#  Enum + dataclasses
# ───────────────────────────────────────────────────────────────────


class SandboxStatus(str, Enum):
    """Lifecycle states of a sandbox.

    ``pending``  → created, container not yet requested
    ``starting`` → ``docker run`` issued, waiting for dev server ready
    ``running``  → dev server is responding on ``preview_url``
    ``stopping`` → stop requested, container still winding down
    ``stopped``  → container no longer running (graceful or manual)
    ``failed``   → unrecoverable error; ``error`` field holds detail
    """

    pending = "pending"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"


# Terminal statuses — cannot transition further.  Used by the manager
# to refuse ``touch`` / ``mark_ready`` after the fact.
_TERMINAL_STATUSES = frozenset({SandboxStatus.stopped, SandboxStatus.failed})


@dataclass(frozen=True)
class SandboxConfig:
    """Inputs to :meth:`SandboxManager.create`.

    Frozen + deterministic — two configs with the same field values
    produce byte-identical :func:`build_docker_run_spec` output.
    """

    session_id: str
    workspace_path: str
    image: str = DEFAULT_SANDBOX_IMAGE
    container_port: int = DEFAULT_CONTAINER_PORT
    host_port: int | None = None
    command: tuple[str, ...] = DEFAULT_DEV_COMMAND
    workdir: str = DEFAULT_WORKDIR
    env: Mapping[str, str] = field(default_factory=dict)
    node_env: str = DEFAULT_NODE_ENV
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not _SAFE_SESSION_RE.fullmatch(self.session_id):
            raise ValueError(
                "session_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.session_id!r}"
            )
        if not isinstance(self.workspace_path, str) or not self.workspace_path.strip():
            raise ValueError("workspace_path must be a non-empty string")
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("image must be non-empty")
        if not isinstance(self.container_port, int) or not (
            1 <= self.container_port <= 65535
        ):
            raise ValueError(f"container_port out of range: {self.container_port!r}")
        if self.host_port is not None:
            if not isinstance(self.host_port, int) or not (
                1 <= self.host_port <= 65535
            ):
                raise ValueError(f"host_port out of range: {self.host_port!r}")
        if not isinstance(self.startup_timeout_s, (int, float)) or self.startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be positive")
        if not isinstance(self.stop_timeout_s, (int, float)) or self.stop_timeout_s <= 0:
            raise ValueError("stop_timeout_s must be positive")
        if not isinstance(self.workdir, str) or not self.workdir.startswith("/"):
            raise ValueError("workdir must be an absolute path")
        # Normalise command to a tuple (accept lists defensively).
        if isinstance(self.command, (list, tuple)):
            command = tuple(str(part) for part in self.command)
        else:
            raise ValueError("command must be a sequence of strings")
        if not command:
            raise ValueError("command must be non-empty")
        object.__setattr__(self, "command", command)
        # Env — reject non-str keys/values; freeze into MappingProxyType.
        env_src = dict(self.env) if self.env else {}
        for key, value in env_src.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("env keys and values must be strings")
        object.__setattr__(self, "env", MappingProxyType(env_src))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_SANDBOX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "workspace_path": self.workspace_path,
            "image": self.image,
            "container_port": self.container_port,
            "host_port": self.host_port,
            "command": list(self.command),
            "workdir": self.workdir,
            "env": dict(self.env),
            "node_env": self.node_env,
            "startup_timeout_s": float(self.startup_timeout_s),
            "stop_timeout_s": float(self.stop_timeout_s),
        }


@dataclass(frozen=True)
class SandboxInstance:
    """Snapshot of a sandbox's state.

    Frozen — state transitions happen by :func:`dataclasses.replace`.
    The manager stores the *current* instance and exposes it via
    :meth:`SandboxManager.get`.  Historical transitions are not
    retained here; callers log via event emit callbacks.
    """

    session_id: str
    container_name: str
    config: SandboxConfig
    status: SandboxStatus = SandboxStatus.pending
    container_id: str | None = None
    host_port: int | None = None
    preview_url: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    ready_at: float | None = None
    stopped_at: float | None = None
    last_active_at: float = 0.0
    error: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(self.container_name, str) or not self.container_name.strip():
            raise ValueError("container_name must be non-empty")
        if not isinstance(self.status, SandboxStatus):
            raise ValueError(f"status must be SandboxStatus, got {type(self.status)!r}")
        if self.created_at < 0 or self.last_active_at < 0:
            raise ValueError("timestamps must be non-negative")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def is_running(self) -> bool:
        return self.status is SandboxStatus.running

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def idle_seconds(self, now: float | None = None) -> float:
        """Seconds since the caller last ``touch()``ed this sandbox.

        A freshly-created sandbox (``last_active_at == 0``) reports
        ``0.0`` so it isn't reaped on creation.
        """

        if self.last_active_at <= 0:
            return 0.0
        ref = time.time() if now is None else now
        return max(0.0, ref - self.last_active_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_SANDBOX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "container_name": self.container_name,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "container_id": self.container_id,
            "host_port": self.host_port,
            "preview_url": self.preview_url,
            "created_at": float(self.created_at),
            "started_at": None if self.started_at is None else float(self.started_at),
            "ready_at": None if self.ready_at is None else float(self.ready_at),
            "stopped_at": None if self.stopped_at is None else float(self.stopped_at),
            "last_active_at": float(self.last_active_at),
            "error": self.error,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CompileError:
    """One structured dev-server compile / runtime error, parsed from
    stderr by :func:`parse_compile_error`."""

    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    error_type: str = "compile"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "error_type": self.error_type,
        }


# ───────────────────────────────────────────────────────────────────
#  Docker client protocol
# ───────────────────────────────────────────────────────────────────


class DockerClient(Protocol):
    """Minimal shim the sandbox manager speaks to.

    Intentionally small — production wraps ``docker`` CLI; tests plug
    in an in-memory fake.  Implementations MUST be thread-safe.
    """

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        command: Sequence[str],
        mounts: Sequence[Mapping[str, str]],
        ports: Mapping[int, int],
        env: Mapping[str, str],
        workdir: str,
    ) -> str:  # returns container_id
        ...

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        ...

    def remove(self, container_id: str, *, force: bool = False) -> None:
        ...

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        ...

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        ...


class SubprocessDockerClient:
    """Default :class:`DockerClient` implementation that shells out to
    the ``docker`` CLI.

    Provided for convenience — production callers use this, tests
    plug in an in-memory fake.  The implementation is deliberately
    thin: callers who want fine-grained control (e.g. docker-py,
    podman, remote daemon) write their own.
    """

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        timeout_s: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._docker_bin = docker_bin
        self._timeout_s = timeout_s
        self._runner = runner or subprocess.run

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        logger.debug("docker cmd: %s", " ".join(argv))
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"docker CLI not found ({self._docker_bin!r})") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"docker command timed out: {argv}") from exc
        if result.returncode != 0:
            raise SandboxError(
                f"docker command failed ({result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        return result

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        command: Sequence[str],
        mounts: Sequence[Mapping[str, str]],
        ports: Mapping[int, int],
        env: Mapping[str, str],
        workdir: str,
    ) -> str:
        argv: list[str] = [self._docker_bin, "run", "-d", "--rm", "--name", name, "-w", workdir]
        for m in mounts:
            src = m["source"]
            dst = m["target"]
            ro = m.get("read_only", False)
            argv += ["-v", f"{src}:{dst}" + (":ro" if ro else "")]
        for host_port, container_port in ports.items():
            argv += ["-p", f"{host_port}:{container_port}"]
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(image)
        argv.extend(command)
        out = self._run(argv).stdout.strip()
        if not out:
            raise SandboxError("docker run returned no container id")
        return out.splitlines()[-1].strip()

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        self._run([self._docker_bin, "stop", "-t", str(int(max(0, timeout_s))), container_id])

    def remove(self, container_id: str, *, force: bool = False) -> None:
        argv = [self._docker_bin, "rm"]
        if force:
            argv.append("-f")
        argv.append(container_id)
        self._run(argv)

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        argv = [self._docker_bin, "logs"]
        if tail is not None:
            argv += ["--tail", str(int(tail))]
        argv.append(container_id)
        try:
            return self._run(argv).stdout
        except SandboxError:
            return ""

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        try:
            out = self._run([self._docker_bin, "inspect", container_id]).stdout
        except SandboxError:
            return {}
        import json as _json

        try:
            parsed = _json.loads(out)
        except _json.JSONDecodeError:
            return {}
        if isinstance(parsed, list) and parsed:
            return parsed[0] if isinstance(parsed[0], Mapping) else {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {}


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


_SAFE_SESSION_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

# Next.js compile-error heuristic: matches either
#   "./pages/index.tsx:12:5"
# or "Module not found: Error: Can't resolve 'foo' in '/app'"
_COMPILE_FILE_RE = re.compile(
    r"(?P<file>(?:\.{1,2}/|/)?[A-Za-z0-9_.\-/\\]+\.(?:tsx?|jsx?|mjs|cjs|css|scss))"
    r"(?::(?P<line>\d+)(?::(?P<col>\d+))?)?"
)
_COMPILE_TRIGGER_RE = re.compile(
    r"(?P<type>Module not found|Module parse failed|Failed to compile|"
    r"Parsing error|SyntaxError|TypeError|ReferenceError|RangeError|"
    r"Error(?=:))",
    re.IGNORECASE,
)


def format_container_name(session_id: str, *, prefix: str = "omnisight-ui") -> str:
    """Produce a Docker-safe container name for ``session_id``.

    Docker allows ``[a-zA-Z0-9][a-zA-Z0-9_.-]*`` so we lowercase,
    strip illegal chars, and truncate to 63 chars (the Docker DNS
    label cap).  Prefix is prepended to make the name identifiable
    in ``docker ps``.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be non-empty")
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "-", session_id.strip().lower())
    safe = safe.strip("-_.") or "sess"
    full = f"{prefix}-{safe}"
    return full[:63]


def build_preview_url(
    host_port: int, *, host: str = DEFAULT_PREVIEW_HOST, path: str = "/"
) -> str:
    """Return ``http://{host}:{host_port}{path}`` with path normalised.

    Always ``http://`` — the dev server is local-only by default and
    Next.js dev mode doesn't serve TLS.  Callers running behind a
    tunnel (Cloudflare / ngrok) override ``host`` to the tunnel
    hostname and should upgrade to ``https``; we keep the helper
    deliberately dumb.
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


def validate_workspace(workspace_path: str) -> Path:
    """Return the resolved Path of ``workspace_path`` if it exists and
    is a directory — raise :class:`ValueError` otherwise.

    Used by :meth:`SandboxManager.create` to fail fast when the
    agent points at a non-existent workspace (common bug when the
    harness deletes temp dirs between sessions).
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
    session_id: str,
    *,
    in_use: Iterable[int] = (),
    port_range: tuple[int, int] = DEFAULT_HOST_PORT_RANGE,
) -> int:
    """Return a host port in ``port_range`` not in ``in_use``.

    Deterministic — hashes ``session_id`` to pick the starting slot,
    then linear-probes.  Using hashing (rather than ``random``)
    keeps the mapping reproducible across restarts, which simplifies
    debugging ("session X always lives on 40137").  Tests pin the
    hash behaviour.
    """

    lo, hi = port_range
    if not (1 <= lo <= hi <= 65535):
        raise ValueError(f"port_range invalid: {port_range!r}")
    span = hi - lo + 1
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % span
    taken = {int(p) for p in in_use}
    for offset in range(span):
        candidate = lo + (start + offset) % span
        if candidate not in taken:
            return candidate
    raise SandboxError(
        f"no host port available in range {port_range!r} "
        f"(all {span} slots in use)"
    )


def build_docker_run_spec(config: SandboxConfig) -> dict[str, Any]:
    """Return a deterministic dict describing how to invoke ``docker run``.

    Pure function — same input → byte-identical dict.  The returned
    shape is what :meth:`DockerClient.run_detached` consumes, plus
    ``container_name`` and a derived ``preview_host_port`` the
    manager reads.

    Note: when ``config.host_port`` is ``None`` the caller is
    expected to run :func:`allocate_host_port` and pass that port in
    a fresh :class:`SandboxConfig` before calling this — ``None``
    here stays ``None`` so the spec remains deterministic.
    """

    if not isinstance(config, SandboxConfig):
        raise TypeError("config must be a SandboxConfig")

    mounts = (
        {
            "source": config.workspace_path,
            "target": config.workdir,
            "type": "bind",
            "read_only": False,
        },
    )
    env = dict(config.env)
    # Ensure NODE_ENV is set — dev servers occasionally misbehave
    # when it's missing and we want the spec reproducible.
    env.setdefault("NODE_ENV", config.node_env)
    # HOST / PORT hint for frameworks that read env (Next.js does).
    env.setdefault("HOST", "0.0.0.0")
    env.setdefault("PORT", str(config.container_port))

    ports: dict[int, int] = {}
    if config.host_port is not None:
        ports[config.host_port] = config.container_port

    return {
        "schema_version": UI_SANDBOX_SCHEMA_VERSION,
        "image": config.image,
        "container_name": format_container_name(config.session_id),
        "command": list(config.command),
        "mounts": [dict(m) for m in mounts],
        "ports": ports,
        "env": dict(sorted(env.items())),
        "workdir": config.workdir,
    }


def detect_dev_server_ready(log_text: str) -> bool:
    """True if any of the :data:`READY_PATTERNS` match ``log_text``.

    Case-insensitive, scans the full text — callers typically feed
    this the last ~80 lines of container logs.
    """

    if not log_text:
        return False
    for pattern in READY_PATTERNS:
        if pattern.search(log_text):
            return True
    return False


def parse_compile_error(stderr_text: str) -> tuple[CompileError, ...]:
    """Best-effort parse of Next.js / Vite compile errors in ``stderr_text``.

    Returns an empty tuple when no recognisable pattern is found —
    never raises.  This is a *signal* helper for the lifecycle
    module (V2 row 4) to decide whether to surface an error bridge
    event; the agent consumes the result via SSE.
    """

    if not stderr_text or not isinstance(stderr_text, str):
        return ()

    # Walk lines; when a trigger ("Error:" / "Module not found:" etc.)
    # fires, collect the first path:line:col seen within the next 5
    # lines and emit one CompileError per trigger.  Two triggers that
    # point at the same file+line are merged (Next.js often prints
    # both a summary line and the annotated fragment).
    lines = stderr_text.splitlines()
    seen: set[tuple[str, str | None, int | None]] = set()
    out: list[CompileError] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        trigger = _COMPILE_TRIGGER_RE.search(line)
        if not trigger:
            i += 1
            continue
        error_type = trigger.group("type").lower().replace(" ", "_")
        message = line.strip()
        file_match: re.Match[str] | None = _COMPILE_FILE_RE.search(line)
        scan_limit = min(len(lines), i + 6)
        j = i + 1
        while file_match is None and j < scan_limit:
            file_match = _COMPILE_FILE_RE.search(lines[j])
            j += 1
        if file_match is not None:
            file = file_match.group("file")
            raw_line = file_match.group("line")
            raw_col = file_match.group("col")
            line_no = int(raw_line) if raw_line else None
            col_no = int(raw_col) if raw_col else None
        else:
            file, line_no, col_no = None, None, None
        key = (message, file, line_no)
        if key in seen:
            i += 1
            continue
        seen.add(key)
        out.append(
            CompileError(
                message=message,
                file=file,
                line=line_no,
                column=col_no,
                error_type=error_type,
            )
        )
        i += 1
    return tuple(out)


def render_sandbox_status_markdown(instance: SandboxInstance) -> str:
    """Deterministic markdown summary for operator logs / SSE bodies."""

    if not isinstance(instance, SandboxInstance):
        raise TypeError("instance must be SandboxInstance")

    lines: list[str] = [
        f"### Sandbox `{instance.session_id}`",
        "",
        f"- status: **{instance.status.value}**",
        f"- container: `{instance.container_name}`",
        f"- container_id: `{instance.container_id or '(none)'}`",
        f"- host_port: `{instance.host_port if instance.host_port is not None else '(none)'}`",
        f"- preview_url: {instance.preview_url or '(none)'}",
        f"- workspace: `{instance.config.workspace_path}`",
        f"- image: `{instance.config.image}`",
    ]
    if instance.error:
        lines.append(f"- error: {instance.error}")
    if instance.warnings:
        lines.append("- warnings: " + ", ".join(instance.warnings))
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────
#  Manager
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class SandboxManager:
    """Thread-safe registry of live sandboxes, keyed on ``session_id``.

    One sandbox per session.  Callers create → start → touch during
    usage → stop when done.  The manager never mounts an idle
    reaper itself — the lifecycle policy module (V2 row 2) layers
    that on top.

    ``docker_client`` is the only required external dependency.
    ``clock`` + ``event_cb`` are injection points for deterministic
    testing.  ``preview_host`` is exposed so callers behind a reverse
    proxy can override.
    """

    def __init__(
        self,
        *,
        docker_client: DockerClient,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        preview_host: str = DEFAULT_PREVIEW_HOST,
        port_range: tuple[int, int] = DEFAULT_HOST_PORT_RANGE,
    ) -> None:
        self._docker = docker_client
        self._clock = clock
        self._event_cb = event_cb
        self._preview_host = preview_host
        self._port_range = port_range
        self._lock = threading.RLock()
        self._instances: dict[str, SandboxInstance] = {}

    # ─────────────── Public API ───────────────

    def create(self, config: SandboxConfig) -> SandboxInstance:
        """Register a new sandbox.  Does **not** start Docker — that's
        :meth:`start`.  Raises :class:`SandboxAlreadyExists` if the
        session already has an entry (even a stopped / failed one).
        """

        if not isinstance(config, SandboxConfig):
            raise TypeError("config must be SandboxConfig")
        validate_workspace(config.workspace_path)
        with self._lock:
            if config.session_id in self._instances:
                raise SandboxAlreadyExists(
                    f"session_id {config.session_id!r} already has a sandbox — "
                    "stop/remove it first"
                )
            container_name = format_container_name(config.session_id)
            instance = SandboxInstance(
                session_id=config.session_id,
                container_name=container_name,
                config=config,
                status=SandboxStatus.pending,
                created_at=self._clock(),
                last_active_at=self._clock(),
            )
            self._instances[config.session_id] = instance
        self._emit("ui_sandbox.created", instance)
        return instance

    def start(self, session_id: str) -> SandboxInstance:
        """Request ``docker run`` for the sandbox.  Transitions
        ``pending → starting``.  On docker error, transitions to
        ``failed`` and captures the message — does not re-raise so
        the agent loop keeps running.
        """

        with self._lock:
            instance = self._require(session_id)
            if instance.status is SandboxStatus.running:
                return instance  # idempotent
            if instance.status not in {SandboxStatus.pending, SandboxStatus.stopped}:
                raise SandboxError(
                    f"cannot start sandbox in status {instance.status.value!r}"
                )
            config = instance.config
            host_port = config.host_port
            if host_port is None:
                in_use = {
                    inst.host_port
                    for inst in self._instances.values()
                    if inst.host_port is not None
                }
                host_port = allocate_host_port(
                    config.session_id, in_use=in_use, port_range=self._port_range
                )
                config = replace(config, host_port=host_port)
            spec = build_docker_run_spec(config)
            try:
                container_id = self._docker.run_detached(
                    image=spec["image"],
                    name=spec["container_name"],
                    command=spec["command"],
                    mounts=spec["mounts"],
                    ports=spec["ports"],
                    env=spec["env"],
                    workdir=spec["workdir"],
                )
            except Exception as exc:  # pragma: no cover - surfaced via test fake
                failed = replace(
                    instance,
                    status=SandboxStatus.failed,
                    error=f"docker_run_failed: {exc}",
                    last_active_at=self._clock(),
                )
                self._instances[session_id] = failed
                self._emit("ui_sandbox.failed", failed)
                return failed

            preview_url = build_preview_url(host_port, host=self._preview_host)
            started = replace(
                instance,
                config=config,
                status=SandboxStatus.starting,
                container_id=container_id,
                host_port=host_port,
                preview_url=preview_url,
                started_at=self._clock(),
                last_active_at=self._clock(),
            )
            self._instances[session_id] = started
        self._emit("ui_sandbox.starting", started)
        return started

    def mark_ready(self, session_id: str) -> SandboxInstance:
        """Caller's signal that the dev server is responding.  Called
        by the lifecycle module after polling container logs with
        :func:`detect_dev_server_ready`.  Idempotent."""

        with self._lock:
            instance = self._require(session_id)
            if instance.status is SandboxStatus.running:
                return instance
            if instance.status is not SandboxStatus.starting:
                raise SandboxError(
                    f"cannot mark ready from status {instance.status.value!r}"
                )
            ready = replace(
                instance,
                status=SandboxStatus.running,
                ready_at=self._clock(),
                last_active_at=self._clock(),
            )
            self._instances[session_id] = ready
        self._emit("ui_sandbox.ready", ready)
        return ready

    def touch(self, session_id: str) -> SandboxInstance:
        """Update ``last_active_at`` to the current clock.  Called by
        the agent loop on every ReAct cycle — prevents the idle
        reaper from collecting an actively-used sandbox."""

        with self._lock:
            instance = self._require(session_id)
            if instance.is_terminal:
                return instance  # terminal sandboxes don't bump
            touched = replace(instance, last_active_at=self._clock())
            self._instances[session_id] = touched
            return touched

    def stop(self, session_id: str, *, remove: bool = True) -> SandboxInstance:
        """Stop (and optionally ``docker rm``) the container.
        Transitions ``* → stopping → stopped``.  Errors are captured
        as warnings on the returned instance rather than raised."""

        with self._lock:
            instance = self._require(session_id)
            if instance.is_terminal:
                return instance
            stopping = replace(
                instance,
                status=SandboxStatus.stopping,
                last_active_at=self._clock(),
            )
            self._instances[session_id] = stopping
            warnings: list[str] = list(instance.warnings)
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
            stopped = replace(
                stopping,
                status=SandboxStatus.stopped,
                stopped_at=self._clock(),
                last_active_at=self._clock(),
                warnings=tuple(warnings),
            )
            self._instances[session_id] = stopped
        self._emit("ui_sandbox.stopped", stopped)
        return stopped

    def remove(self, session_id: str) -> SandboxInstance:
        """Forget a session.  Must be stopped first — raises otherwise.
        Returns the final instance snapshot for callers' audit log."""

        with self._lock:
            instance = self._require(session_id)
            if not instance.is_terminal:
                raise SandboxError(
                    f"cannot remove sandbox still in status {instance.status.value!r} "
                    "— call stop() first"
                )
            del self._instances[session_id]
        return instance

    def get(self, session_id: str) -> SandboxInstance | None:
        with self._lock:
            return self._instances.get(session_id)

    def list(self) -> tuple[SandboxInstance, ...]:
        with self._lock:
            return tuple(self._instances.values())

    def logs(
        self, session_id: str, *, tail: int | None = 200
    ) -> str:
        """Fetch container logs (capped at :data:`MAX_LOG_CHARS`)."""

        with self._lock:
            instance = self._require(session_id)
            container_id = instance.container_id
        if not container_id:
            return ""
        try:
            raw = self._docker.logs(container_id, tail=tail)
        except Exception as exc:  # pragma: no cover
            logger.warning("docker logs failed for %s: %s", session_id, exc)
            return ""
        if len(raw) > MAX_LOG_CHARS:
            return raw[-MAX_LOG_CHARS:]
        return raw

    def poll_ready(self, session_id: str, *, tail: int = 200) -> bool:
        """Convenience: fetch recent logs + :func:`detect_dev_server_ready`."""

        return detect_dev_server_ready(self.logs(session_id, tail=tail))

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dict describing all live sandboxes — callers
        pass this to the SSE event bus or admin endpoints."""

        with self._lock:
            return {
                "schema_version": UI_SANDBOX_SCHEMA_VERSION,
                "sandboxes": [inst.to_dict() for inst in self._instances.values()],
                "count": len(self._instances),
            }

    # ─────────────── Internal ───────────────

    def _require(self, session_id: str) -> SandboxInstance:
        instance = self._instances.get(session_id)
        if instance is None:
            raise SandboxNotFound(f"no sandbox for session_id={session_id!r}")
        return instance

    def _emit(self, event_type: str, instance: SandboxInstance) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, instance.to_dict())
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning("ui_sandbox event callback raised: %s", exc)
