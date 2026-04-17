"""W4 #320 (V4 #2) — Instant preview URL quick mode for web deploy adapters.

Goal
----
Give an operator a *shareable URL in seconds* without walking the full
``provision → deploy → promote`` CI/CD path. Two backends:

    vercel-preview   →  POST /v13/deployments with ``target="preview"`` so
                        Vercel serves the build under an ephemeral
                        ``<slug>-<hash>.vercel.app`` host (the same URL
                        surface as the ``vercel deploy --preview`` CLI,
                        minus the subprocess + auth-config churn).
    docker-run       →  ``docker build`` + ``docker run -d -p <host>:8080``
                        on the local Docker daemon, grabbing a free host
                        port so multiple previews can coexist. The URL is
                        ``http://localhost:<host_port>`` and a
                        ``cleanup_command`` tells the operator how to tear
                        the container down.

Both paths reuse the existing ``VercelAdapter`` / ``DockerNginxAdapter``
(base.py) and add ZERO new abstract methods — the quick-mode entry
points are free functions so the abstract base stays a 4-verb contract
(provision / deploy / rollback / get_url). This keeps the W4 #278
interface invariant intact while V4 #2 layers a thinner "share-link"
helper on top.

Non-goals (NOT this module's job)
---------------------------------
- HTTPS certs for docker-run (it's http:// only — localhost or LAN)
- Auth / access-control on the preview URL (Vercel's link is world-
  readable by design; docker-run is on whatever interface you bind to)
- Production promotion (use ``adapter.deploy(...)`` for that)
- Secret injection beyond ``build_artifact`` (instant preview is a
  share-the-latest-build affair, not an env-var rollout)

The helpers here are all pure where possible so unit tests can pin
the parsing + TTL + cleanup-command logic without any subprocess or
network activity.
"""

from __future__ import annotations

import datetime
import logging
import re
import shlex
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.deploy.base import (
    BuildArtifact,
    DeployArtifactError,
    DeployError,
    WebDeployAdapter,
)
from backend.deploy.docker_nginx import DockerNginxAdapter
from backend.deploy.vercel import VercelAdapter

logger = logging.getLogger(__name__)


# ── Canonical mode ids (string constants, not Enum — callers serialise
#    these straight into JSON HTTP responses so plain strings avoid the
#    ``Enum`` vs ``str`` branching across the codebase).
MODE_VERCEL_PREVIEW = "vercel-preview"
MODE_DOCKER_RUN = "docker-run"

INSTANT_PREVIEW_MODES: tuple[str, ...] = (MODE_VERCEL_PREVIEW, MODE_DOCKER_RUN)

# Defaults for docker-run local port selection.
_DOCKER_HOST_PORT_RANGE = (49152, 65535)  # IANA dynamic / private range.
_DOCKER_CONTAINER_PORT = 8080             # must match DockerNginxAdapter default

# Default TTLs (seconds). Vercel preview URLs live until the next deploy
# overwrites them — we surface 24 h as a soft "operator should tear down"
# hint. Docker-run previews live until the container is killed; default
# TTL is 1 h so forgotten containers get flagged by monitoring.
_DEFAULT_TTL_SECONDS: dict[str, int] = {
    MODE_VERCEL_PREVIEW: 24 * 3600,
    MODE_DOCKER_RUN: 3600,
}

# Safe tag pattern — letters, digits, dash, underscore, dot. Enforced
# before we interpolate into ``docker build -t`` to keep callers from
# injecting shell metacharacters via untrusted project names.
_SAFE_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Vercel preview URL sniffer — the CLI prints lines like
# ``Preview: https://my-app-abc123.vercel.app [Copied to clipboard]``.
_VERCEL_PREVIEW_URL_RE = re.compile(
    r"https://[A-Za-z0-9][A-Za-z0-9.-]+\.vercel\.app",
)


# ── Errors ────────────────────────────────────────────────────────

class InstantPreviewError(DeployError):
    """Instant-preview quick mode failed (subprocess, daemon, API)."""


class InstantPreviewUnavailable(InstantPreviewError):
    """Runtime pre-requisite missing (``docker`` not installed, no free
    port, etc.). Routers should map this to 503/409 rather than 500."""


# ── Data model ────────────────────────────────────────────────────

@dataclass
class InstantPreviewResult:
    """Outcome of ``create_instant_preview(...)``.

    Always mode-stamped so the caller can surface a "Cleanup this
    preview" button whose wiring depends on the adapter kind.
    """

    mode: str                                  # MODE_VERCEL_PREVIEW / MODE_DOCKER_RUN
    url: str                                   # shareable URL
    provider: str                              # adapter.provider (for audit log alignment)
    project_name: str
    deployment_id: str                         # vercel: deployment uid, docker: container id
    host_port: Optional[int] = None            # docker-run host port; None for vercel
    image_tag: Optional[str] = None            # docker-run image tag; None for vercel
    expires_at: Optional[str] = None           # ISO-8601 Z; None = indefinite
    ttl_seconds: Optional[int] = None
    cleanup_command: str = ""                  # operator-visible teardown command
    commit_sha: Optional[str] = None
    full_ci_cd: bool = False                   # always False — this is the QUICK path
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "url": self.url,
            "provider": self.provider,
            "project_name": self.project_name,
            "deployment_id": self.deployment_id,
            "host_port": self.host_port,
            "image_tag": self.image_tag,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "cleanup_command": self.cleanup_command,
            "commit_sha": self.commit_sha,
            "full_ci_cd": self.full_ci_cd,
        }


# ── Pure helpers (unit-tested without subprocess / network) ───────

def default_ttl_seconds(mode: str) -> int:
    """Return the default soft-expiry TTL (seconds) for ``mode``.

    Unknown modes fall back to 3600 so the caller never hits a KeyError
    on a typo at runtime.
    """
    return _DEFAULT_TTL_SECONDS.get(mode, 3600)


def compute_expires_at(
    now: datetime.datetime, ttl_seconds: int,
) -> str:
    """Return an ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` expiry stamp."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    delta = datetime.timedelta(seconds=max(0, int(ttl_seconds)))
    return (
        (now + delta).astimezone(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def assert_safe_image_tag(tag: str) -> str:
    """Validate a docker image tag to prevent shell-injection.

    We accept the standard OCI tag character set. On violation we raise
    ``InstantPreviewError`` (not ``ValueError``) so routers map to a
    typed HTTP status without re-catching.
    """
    if not isinstance(tag, str) or not _SAFE_IMAGE_TAG_RE.match(tag):
        raise InstantPreviewError(
            f"Unsafe image tag '{tag}': must match {_SAFE_IMAGE_TAG_RE.pattern}",
        )
    return tag


def normalize_project_for_image(project_name: str) -> str:
    """Lower-case and sanitise a project name so it can be used as a
    docker image name.

    Docker refuses uppercase + certain punctuation. We:

    - lowercase
    - strip surrounding whitespace
    - collapse any non-alnum/dot/dash/underscore to ``-``
    - trim leading non-alnum chars so the first char is always safe
    """
    if not project_name:
        raise InstantPreviewError("project_name must be non-empty")
    s = project_name.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = s.lstrip("-._")
    if not s:
        raise InstantPreviewError(
            f"project_name '{project_name}' is empty after sanitisation",
        )
    # Docker image names cap at 128 chars per component.
    return s[:64]


def extract_vercel_preview_url(text: str) -> Optional[str]:
    """Pull the first ``https://*.vercel.app`` URL out of a blob.

    Used for the legacy CLI path (``vercel deploy --preview`` output) +
    safety net when the REST payload ``url`` field is empty.
    """
    if not text:
        return None
    m = _VERCEL_PREVIEW_URL_RE.search(text)
    return m.group(0) if m else None


def parse_docker_port_line(line: str) -> Optional[tuple[str, int]]:
    """Parse one line of ``docker port <container>`` output.

    Input looks like ``8080/tcp -> 0.0.0.0:32768`` — we return
    ``("0.0.0.0", 32768)``. Returns ``None`` for malformed / unrelated
    lines instead of raising so callers can iterate defensively.
    """
    if not line or "->" not in line:
        return None
    _, mapping = line.split("->", 1)
    mapping = mapping.strip()
    # Strip brackets for IPv6 hosts: "[::]:32768" or "0.0.0.0:32768"
    if mapping.startswith("["):
        end = mapping.find("]")
        if end == -1:
            return None
        host = mapping[1:end]
        rest = mapping[end + 1:]
    else:
        if ":" not in mapping:
            return None
        host, rest = mapping.rsplit(":", 1)
        rest = ":" + rest
    if not rest.startswith(":"):
        return None
    try:
        port = int(rest[1:].split(" ")[0])
    except ValueError:
        return None
    if port <= 0 or port > 65535:
        return None
    return host, port


def find_free_port(
    start: int = _DOCKER_HOST_PORT_RANGE[0],
    end: int = _DOCKER_HOST_PORT_RANGE[1],
    _probe=None,
) -> int:
    """Bind ``0`` on localhost inside ``[start, end]`` and return an open
    port.

    The kernel handles the race-free allocation (``SO_REUSEADDR`` + bind
    to port 0). ``_probe`` is a test seam — pass a callable that yields
    candidate ports to pin behaviour under deterministic tests.
    """
    if start >= end:
        raise InstantPreviewError(
            f"find_free_port: invalid range {start}-{end}",
        )
    if _probe is None:
        def _probe():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
            finally:
                s.close()
    # First try the kernel's port-0 allocation (99% path); clamp into
    # the requested window. Fall back to iterating the window.
    port = int(_probe())
    if start <= port <= end:
        return port
    # Out-of-range probe result: scan deterministically.
    for p in range(start, end + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    raise InstantPreviewUnavailable(
        f"No free TCP port available in [{start}, {end}]",
    )


def validate_preview_port(port: int) -> int:
    """Range-check a user-supplied port or raise
    ``InstantPreviewError``."""
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise InstantPreviewError(
            f"invalid preview port: {port!r}",
        )
    return port


def format_cleanup_command(
    mode: str,
    *,
    container_name: Optional[str] = None,
    deployment_id: Optional[str] = None,
    project_name: Optional[str] = None,
) -> str:
    """Render the operator-visible teardown command for a given mode.

    Docker-run: ``docker rm -f <container_name>``. Vercel-preview:
    ``vercel remove <deployment-or-project> --safe --yes`` (safe = only
    removes non-production deployments; matches the Vercel CLI contract).
    """
    if mode == MODE_DOCKER_RUN:
        if not container_name:
            return "docker ps"  # degraded — still better than empty string
        return f"docker rm -f {shlex.quote(container_name)}"
    if mode == MODE_VERCEL_PREVIEW:
        target = deployment_id or project_name or ""
        if not target:
            return "vercel list"
        return f"vercel remove {shlex.quote(target)} --safe --yes"
    # Unknown modes → empty string rather than throw; callers decide
    # how loud to be.
    return ""


def build_preview_container_name(project_name: str, suffix: Optional[str] = None) -> str:
    """Produce a deterministic, unique-ish container name.

    Sample output: ``omnisight-preview-demo-site-a1b2c3d4``. The
    ``suffix`` parameter lets callers pass a deterministic hex (useful
    in tests); default is a random 8-char hex so multiple previews can
    coexist.
    """
    safe_project = normalize_project_for_image(project_name)
    sfx = suffix or uuid.uuid4().hex[:8]
    sfx = re.sub(r"[^A-Za-z0-9]", "", sfx)[:16] or uuid.uuid4().hex[:8]
    return f"omnisight-preview-{safe_project}-{sfx}"


# ── docker-run quick mode ─────────────────────────────────────────

def _docker_cli() -> str:
    """Resolve the ``docker`` binary or raise ``InstantPreviewUnavailable``."""
    docker = shutil.which("docker")
    if not docker:
        raise InstantPreviewUnavailable(
            "docker CLI not found on PATH — install Docker or pick "
            "mode='vercel-preview'.",
            provider="docker-nginx",
        )
    return docker


def create_docker_run_preview(
    adapter: DockerNginxAdapter,
    build_artifact: BuildArtifact,
    *,
    host_port: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
    run_subprocess=None,
    now: Optional[datetime.datetime] = None,
    port_probe=None,
) -> InstantPreviewResult:
    """Build a local image from ``build_artifact`` and launch it with
    ``docker run -d``, returning the ephemeral localhost URL.

    ``run_subprocess`` is a test seam — defaults to
    ``subprocess.run``. It must match the stdlib signature so the caller
    can swap in a recording stub without monkeypatching a module global.
    """
    if not isinstance(adapter, DockerNginxAdapter):
        raise InstantPreviewError(
            f"create_docker_run_preview requires a DockerNginxAdapter, "
            f"got {type(adapter).__name__}",
            provider="docker-nginx",
        )
    build_artifact.validate()

    # Resolve docker binary (raises InstantPreviewUnavailable if absent).
    docker = _docker_cli()
    run = run_subprocess or subprocess.run

    # Reuse adapter.provision() + deploy() on-disk render so the image
    # build context is canonical. Both are synchronous-safe (docker
    # adapter is filesystem-only aside from the subprocess escape).
    output_dir = adapter.output_dir
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
    # Render the Dockerfile / nginx.conf / public/ tree via the public
    # adapter helpers. We do not call ``adapter.deploy()`` here because
    # that wraps around ``asyncio`` — instead we reuse the low-level
    # sync rendering + copy.
    adapter._write_build_context()  # private helper; stable since W4 #278
    public_dir = output_dir / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    copied = adapter._copy_tree(build_artifact.path, public_dir)
    if copied == 0:
        raise DeployArtifactError(
            f"Build artifact at {build_artifact.path} has no files",
        )

    image = f"{normalize_project_for_image(adapter.project_name)}:preview"
    assert_safe_image_tag(image.split(":", 1)[1])

    container_name = build_preview_container_name(adapter.project_name)

    chosen_port = (
        validate_preview_port(host_port)
        if host_port is not None
        else find_free_port(_probe=port_probe)
    )

    # Build.
    build_cmd = [docker, "build", "-t", image, str(output_dir)]
    rb = run(build_cmd, check=False, capture_output=True, text=True)
    if getattr(rb, "returncode", 0) != 0:
        raise InstantPreviewError(
            f"docker build failed: {getattr(rb, 'stderr', '') or getattr(rb, 'stdout', '')}",
            provider="docker-nginx",
        )

    # Remove any stale container with the same name (safe, ignore rc).
    run([docker, "rm", "-f", container_name], check=False, capture_output=True, text=True)

    # Run detached; expose the adapter's internal port on the chosen
    # host port. We bind to 127.0.0.1 explicitly so the preview stays
    # on the developer machine — operators who want LAN exposure can
    # use adapter.deploy() instead (which goes through the full path).
    run_cmd = [
        docker, "run", "-d",
        "--name", container_name,
        "-p", f"127.0.0.1:{chosen_port}:{adapter.port}",
        "--label", "org.opencontainers.omnisight.mode=instant-preview",
        image,
    ]
    rr = run(run_cmd, check=False, capture_output=True, text=True)
    if getattr(rr, "returncode", 0) != 0:
        raise InstantPreviewError(
            f"docker run failed: {getattr(rr, 'stderr', '') or getattr(rr, 'stdout', '')}",
            provider="docker-nginx",
        )
    container_id = (getattr(rr, "stdout", "") or "").strip() or container_name

    ttl = ttl_seconds if ttl_seconds is not None else default_ttl_seconds(MODE_DOCKER_RUN)
    expires = compute_expires_at(now or datetime.datetime.now(datetime.timezone.utc), ttl)
    url = f"http://localhost:{chosen_port}"

    logger.info(
        "instant_preview.docker_run project=%s container=%s port=%d ttl=%ds",
        adapter.project_name, container_name, chosen_port, ttl,
    )

    return InstantPreviewResult(
        mode=MODE_DOCKER_RUN,
        url=url,
        provider=adapter.provider,
        project_name=adapter.project_name,
        deployment_id=container_id,
        host_port=chosen_port,
        image_tag=image,
        expires_at=expires,
        ttl_seconds=ttl,
        cleanup_command=format_cleanup_command(
            MODE_DOCKER_RUN, container_name=container_name,
        ),
        commit_sha=build_artifact.commit_sha,
        full_ci_cd=False,
        raw={
            "container_name": container_name,
            "container_id": container_id,
            "image": image,
            "output_dir": str(output_dir),
            "host_port": chosen_port,
            "internal_port": adapter.port,
            "files_copied": copied,
        },
    )


# ── vercel-preview quick mode ─────────────────────────────────────

async def create_vercel_preview(
    adapter: VercelAdapter,
    build_artifact: BuildArtifact,
    *,
    ttl_seconds: Optional[int] = None,
    now: Optional[datetime.datetime] = None,
) -> InstantPreviewResult:
    """Upload ``build_artifact`` and create a Vercel deployment with
    ``target="preview"``.

    Unlike production ``adapter.deploy()`` (target=production), this
    does not promote the deployment to the root alias — callers get
    the auto-generated ``<project>-<hash>.vercel.app`` URL that's
    intended for short-term sharing, identical to what
    ``vercel deploy --preview`` would emit via the CLI.
    """
    if not isinstance(adapter, VercelAdapter):
        raise InstantPreviewError(
            f"create_vercel_preview requires a VercelAdapter, got "
            f"{type(adapter).__name__}",
            provider="vercel",
        )
    build_artifact.validate()

    # Best-effort project lookup: if the caller already ran provision(),
    # ``_project_id`` is set. Otherwise peek — this keeps the quick path
    # quick without forcing a full ``provision()`` rewrite of env vars.
    if not adapter._project_id:
        existing = await adapter._get_project()
        if existing:
            adapter._project_id = existing.get("id")

    # File upload (identical dedup strategy as adapter.deploy()).
    files = adapter._collect_files(build_artifact.path)
    if not files:
        raise DeployArtifactError(
            f"No files found under {build_artifact.path}"
        )
    seen: set[str] = set()
    for _, _, data, sha1 in files:
        if sha1 in seen:
            continue
        seen.add(sha1)
        await adapter._upload_file(data, sha1)

    manifest = [
        {"file": rel, "sha": sha1, "size": len(data)}
        for _, rel, data, sha1 in files
    ]
    body: dict[str, Any] = {
        "name": adapter.project_name,
        "target": "preview",                  # ← the one-character change
        "files": manifest,
    }
    if adapter._project_id:
        body["project"] = adapter._project_id
    if build_artifact.commit_sha:
        body.setdefault("meta", {})["commitSha"] = build_artifact.commit_sha
    if build_artifact.framework or adapter._framework:
        body["projectSettings"] = {
            "framework": build_artifact.framework or adapter._framework,
        }

    resp = await adapter._request("POST", "/v13/deployments", json=body)
    deployment_id = resp.get("id") or resp.get("uid") or ""
    host = resp.get("url") or ""
    url = f"https://{host}" if host and not host.startswith("http") else (host or "")
    if not url:
        # Degrade gracefully if Vercel omits url in the POST response
        # (rare, but the API docs only guarantee it on GET).
        url = f"https://{adapter.project_name}.vercel.app"

    ttl = ttl_seconds if ttl_seconds is not None else default_ttl_seconds(MODE_VERCEL_PREVIEW)
    expires = compute_expires_at(now or datetime.datetime.now(datetime.timezone.utc), ttl)

    logger.info(
        "instant_preview.vercel project=%s deployment=%s target=preview files=%d",
        adapter.project_name, deployment_id, len(files),
    )

    return InstantPreviewResult(
        mode=MODE_VERCEL_PREVIEW,
        url=url,
        provider=adapter.provider,
        project_name=adapter.project_name,
        deployment_id=deployment_id,
        host_port=None,
        image_tag=None,
        expires_at=expires,
        ttl_seconds=ttl,
        cleanup_command=format_cleanup_command(
            MODE_VERCEL_PREVIEW,
            deployment_id=deployment_id,
            project_name=adapter.project_name,
        ),
        commit_sha=build_artifact.commit_sha,
        full_ci_cd=False,
        raw={
            "target": "preview",
            "readyState": str(resp.get("readyState", "QUEUED")),
            "files": len(files),
        },
    )


# ── Dispatcher ────────────────────────────────────────────────────

async def create_instant_preview(
    adapter: WebDeployAdapter,
    build_artifact: BuildArtifact,
    *,
    mode: Optional[str] = None,
    **kwargs: Any,
) -> InstantPreviewResult:
    """Dispatcher: pick the right quick-mode entry point for ``adapter``.

    Passing ``mode`` explicitly lets the caller override the default
    (e.g. use docker-run even when adapter is a VercelAdapter — which
    we reject here because mixing adapters + modes would silently miss
    the build context render).
    """
    # Default mode is inferred from the adapter kind.
    default_mode = (
        MODE_DOCKER_RUN if isinstance(adapter, DockerNginxAdapter)
        else MODE_VERCEL_PREVIEW if isinstance(adapter, VercelAdapter)
        else None
    )
    if default_mode is None:
        raise InstantPreviewError(
            f"Instant preview is not supported for adapter "
            f"{type(adapter).__name__!r}; use 'vercel' or 'docker-nginx'.",
            provider=getattr(adapter, "provider", ""),
        )
    chosen = mode or default_mode
    if chosen not in INSTANT_PREVIEW_MODES:
        raise InstantPreviewError(
            f"Unknown instant preview mode {chosen!r}; "
            f"expected one of {INSTANT_PREVIEW_MODES}.",
        )
    if chosen == MODE_DOCKER_RUN and not isinstance(adapter, DockerNginxAdapter):
        raise InstantPreviewError(
            "mode='docker-run' requires a DockerNginxAdapter",
            provider=adapter.provider,
        )
    if chosen == MODE_VERCEL_PREVIEW and not isinstance(adapter, VercelAdapter):
        raise InstantPreviewError(
            "mode='vercel-preview' requires a VercelAdapter",
            provider=adapter.provider,
        )

    if chosen == MODE_DOCKER_RUN:
        return create_docker_run_preview(adapter, build_artifact, **kwargs)
    return await create_vercel_preview(adapter, build_artifact, **kwargs)


__all__ = [
    "InstantPreviewError",
    "InstantPreviewResult",
    "InstantPreviewUnavailable",
    "INSTANT_PREVIEW_MODES",
    "MODE_DOCKER_RUN",
    "MODE_VERCEL_PREVIEW",
    "assert_safe_image_tag",
    "build_preview_container_name",
    "compute_expires_at",
    "create_docker_run_preview",
    "create_instant_preview",
    "create_vercel_preview",
    "default_ttl_seconds",
    "extract_vercel_preview_url",
    "find_free_port",
    "format_cleanup_command",
    "normalize_project_for_image",
    "parse_docker_port_line",
    "validate_preview_port",
]
