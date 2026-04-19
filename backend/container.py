"""Docker Container Manager for agent isolated execution.

Layer 2 of workspace isolation: each agent can optionally run commands
inside a Docker container with its worktree mounted as /workspace.

This provides:
  - Fully isolated toolchain (aarch64 cross-compiler, kernel headers)
  - Clean environment per agent (no host pollution)
  - Container destroyed after cleanup (no residual state)

Integration with Layer 1 (workspace.py):
  - workspace.py creates the git worktree on the host
  - container.py mounts that worktree into a Docker container
  - tools.py detects if a container is active and routes commands through it
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import hashlib

from backend.events import emit_agent_update, emit_pipeline_phase, emit_container

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _PROJECT_ROOT / "backend" / "docker" / "Dockerfile.agent"


def _dockerfile_hash() -> str:
    """SHA256 of Dockerfile content → deterministic image tag."""
    try:
        return hashlib.sha256(_DOCKERFILE.read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return "latest"


DOCKER_IMAGE = f"omnisight-agent:{_dockerfile_hash()}"
DOCKER_TIMEOUT = 60  # seconds for commands
BUILD_TIMEOUT = 300  # seconds for image build

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M1: DRF token → cgroup hard-limit mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# 1 DRF token (I6 SandboxCostWeight unit) ≈ 1 CPU core × 512 MiB RAM.
# We translate the per-tenant budget acquired from sandbox_capacity into
# concrete docker flags so the kernel — not just the scheduler — enforces
# the share. `--cpus` is the CFS quota cap, `--cpu-shares` becomes the
# cgroup v2 `cpu.weight` proportional share under contention, and
# `--memory` is the hard OOM trigger.
#
# Both bounds clamp to sane safety rails so a buggy caller can't ask for
# 10 000 cores; defaults preserve the legacy `_settings.docker_*_limit`
# behaviour when no tenant_budget is supplied (callers that haven't been
# updated yet still work).
M1_TOKEN_CPU = 1.0          # 1 token → 1.0 CPU core
M1_TOKEN_MEM_MB = 512       # 1 token → 512 MiB
M1_TOKEN_SHARES = 1024      # 1 token → 1024 cpu-shares (Docker default)
M1_MAX_TOKENS = 12.0        # safety clamp; matches CAPACITY_MAX
M1_MIN_TOKENS = 0.25        # never starve below 0.25 token (256 MiB / 0.25 cpu)


def _compute_resource_limits(
    tenant_budget: float | None,
) -> tuple[str, str, int]:
    """Translate DRF token budget → (--cpus, --memory, --cpu-shares).

    `tenant_budget` is the SandboxCostWeight value the I6 layer accepted
    on this tenant's behalf (1.0 lightweight, 4.0 compile, …). When None
    we fall back to the legacy settings.docker_{cpu,memory}_limit so the
    pre-M1 callsites don't change behaviour.
    """
    from backend.config import settings as _settings

    if tenant_budget is None or tenant_budget <= 0:
        # Legacy path — keep current behaviour. cpu-shares stays at the
        # Docker default (1024), so unbudgeted containers don't get an
        # unfair edge over budgeted ones.
        return (
            str(_settings.docker_cpu_limit or "2"),
            str(_settings.docker_memory_limit or "1g"),
            1024,
        )

    tokens = max(M1_MIN_TOKENS, min(float(tenant_budget), M1_MAX_TOKENS))
    cpus = f"{tokens * M1_TOKEN_CPU:.2f}"
    mem_mib = int(round(tokens * M1_TOKEN_MEM_MB))
    mem = f"{mem_mib}m"
    shares = max(2, int(round(tokens * M1_TOKEN_SHARES)))
    return cpus, mem, shares


# Phase 64-A S1: cache for runtime probe. None = not probed yet.
_RUNTIME_RESOLVED: str | None = None


async def _detect_available_runtimes() -> set[str]:
    """Return the set of OCI runtimes registered with the local docker.

    Parses `docker info --format '{{json .Runtimes}}'`. On any failure we
    return {"runc"} since that's the daemon default.
    """
    rc, out, _ = await _run(
        "docker info --format '{{json .Runtimes}}'", timeout=10,
    )
    if rc != 0 or not out:
        return {"runc"}
    try:
        import json
        data = json.loads(out)
        if isinstance(data, dict):
            return set(data.keys())
    except Exception as exc:
        logger.debug("runtime probe parse failed: %s", exc)
    return {"runc"}


async def resolve_runtime(force_redetect: bool = False) -> str:
    """Resolve the docker runtime to use, honouring the configured
    preference but falling back to runc when the preferred runtime
    isn't installed. Cached after first call (idempotent for callers)."""
    global _RUNTIME_RESOLVED
    if _RUNTIME_RESOLVED is not None and not force_redetect:
        return _RUNTIME_RESOLVED
    from backend.config import settings as _settings
    preferred = (_settings.docker_runtime or "runc").strip().lower()
    if preferred not in {"runsc", "runc"}:
        logger.warning(
            "OMNISIGHT_DOCKER_RUNTIME=%r not in {runsc,runc}; using runc",
            preferred,
        )
        _RUNTIME_RESOLVED = "runc"
        return _RUNTIME_RESOLVED
    available = await _detect_available_runtimes()
    if preferred in available:
        _RUNTIME_RESOLVED = preferred
    else:
        if preferred != "runc":
            logger.warning(
                "Tier-1 sandbox runtime %r not registered with docker "
                "(available=%s); falling back to runc — escape-resistance "
                "DOWNGRADED. Install gVisor for production.",
                preferred, sorted(available),
            )
            try:
                from backend.events import emit_pipeline_phase as _emit
                _emit("sandbox_runtime_fallback",
                      "runsc unavailable; using runc")
            except Exception:
                pass
        _RUNTIME_RESOLVED = "runc"
    return _RUNTIME_RESOLVED


def _reset_runtime_cache_for_tests() -> None:
    """Test hook — clear the cached runtime so monkeypatched envs apply."""
    global _RUNTIME_RESOLVED
    _RUNTIME_RESOLVED = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 64-A S3: Image digest allow-list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ImageNotTrusted(RuntimeError):
    """Raised when the agent image's sha256 digest is not in the
    configured allow-list."""


def _parse_allowed_digests(raw: str) -> set[str]:
    """CSV of `sha256:...` digests → normalised set. Strips whitespace,
    rejects entries that don't look like sha256."""
    out: set[str] = set()
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("sha256:") or len(item) != 7 + 64:
            logger.warning(
                "ignoring malformed digest in allow-list: %r "
                "(expected 'sha256:' + 64 hex chars)", item,
            )
            continue
        out.add(item)
    return out


async def _inspect_image_digest(image: str) -> str | None:
    """Return the local image's `sha256:...` digest, or None if the image
    is missing / inspect fails. We use `.Id` (the local content digest),
    not `.RepoDigests` (which is registry-specific) — this catches a
    mutated layer even if someone keeps the same tag.
    """
    rc, out, err = await _run(
        f"docker image inspect --format '{{{{.Id}}}}' {image}", timeout=10,
    )
    if rc != 0:
        logger.debug("image inspect failed for %s: %s", image, err or out)
        return None
    digest = out.strip().lower()
    return digest or None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 64-A S4: Sandbox lifetime cap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _lifetime_killswitch(agent_id: str, container_name: str,
                               lifetime_s: float, *, tier: str = "t1") -> None:
    """Wait `lifetime_s`, then force-kill the container if it's still
    in our registry. Cancelled cleanly when the agent finishes via the
    normal stop_container path."""
    try:
        await asyncio.sleep(lifetime_s)
    except asyncio.CancelledError:
        return  # agent finished normally — nothing to do
    info = _containers.get(agent_id)
    if not info or info.container_name != container_name:
        return  # already removed / replaced
    logger.warning(
        "[SANDBOX KILL] %s exceeded lifetime cap %.0fs — SIGKILL",
        container_name, lifetime_s,
    )
    info.status = "killed_lifetime"
    # Best-effort force-remove. We don't await stop_container here
    # because it would re-trigger this same task's cancellation logic;
    # do the docker call directly and pop the registry.
    await _run(f"docker rm -f {container_name} 2>/dev/null", timeout=15)
    _containers.pop(agent_id, None)
    # Audit + metric (best-effort).
    try:
        from backend import audit as _audit
        await _audit.log(
            action="sandbox_killed",
            entity_kind="container",
            entity_id=container_name,
            after={"reason": "lifetime", "tier": tier,
                   "lifetime_s": int(lifetime_s)},
            actor="system:lifetime-watchdog",
        )
    except Exception as exc:
        logger.debug("audit log for sandbox_killed failed: %s", exc)
    try:
        from backend import metrics as _m
        _m.sandbox_lifetime_killed_total.labels(tier=tier).inc()
    except Exception:
        pass
    try:
        emit_container(agent_id, "killed", f"{container_name} (lifetime cap)")
        emit_agent_update(
            agent_id, "error",
            f"Container killed after {int(lifetime_s)}s lifetime cap",
        )
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M1: OOM watchdog
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OOM_POLL_INTERVAL_S = float("0.5")  # how often to inspect a (still-running) container
OOM_POLL_TIMEOUT = 8                # seconds for a single docker inspect


async def _oom_watchdog(agent_id: str, container_name: str,
                        tenant_id: str, *, tier: str = "t1",
                        memory_limit: str = "") -> None:
    """Poll `docker inspect .State` until the container exits, then
    record an audit + metric if it was OOM-killed.

    The watchdog deliberately polls instead of `docker events` so that:
      * we don't share a single global event stream across containers
        (one bad event-stream consumer breaks all watchdogs);
      * cancel-on-stop is trivial (just `task.cancel()`);
      * we work even when the daemon is rate-limiting events.

    Cancelled cleanly when stop_container removes the container.
    """
    try:
        while True:
            try:
                await asyncio.sleep(OOM_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return  # caller stopped us → done
            # If the registry no longer tracks this agent (test reset,
            # crash recovery wipe, etc.) the watchdog has nothing to
            # attribute to — exit instead of spinning.
            current = _containers.get(agent_id)
            if current is None or current.container_name != container_name:
                return
            rc, out, _ = await _run(
                f"docker inspect --format "
                f"'{{{{.State.Status}}}}|{{{{.State.OOMKilled}}}}|"
                f"{{{{.State.ExitCode}}}}' {container_name}",
                timeout=OOM_POLL_TIMEOUT,
            )
            if rc != 0:
                # container removed (exit + auto-rm or stop_container ran)
                # — in either case we can't read the OOM bit any more.
                return
            parts = out.strip().split("|")
            if len(parts) < 3:
                continue
            status, oom_str, exit_str = parts[0], parts[1].lower(), parts[2]
            if status not in ("exited", "dead"):
                continue
            # Reached terminal state — decide if it was an OOM.
            oom_killed = oom_str == "true"
            # Some kernels don't set OOMKilled but still SIGKILL via
            # cgroup memory.events; exit code 137 = 128 + SIGKILL.
            try:
                exit_code = int(exit_str)
            except ValueError:
                exit_code = 0
            if not oom_killed and exit_code == 137:
                # Best-effort: re-check via memory.events oom counter.
                oom_killed = await _read_cgroup_oom_count(container_name) > 0
            if oom_killed:
                await _record_sandbox_oom(
                    agent_id, container_name, tenant_id,
                    tier=tier, memory_limit=memory_limit,
                    exit_code=exit_code,
                )
            return
    except asyncio.CancelledError:
        return
    except Exception as exc:  # never let the watchdog crash silently
        logger.debug("oom watchdog for %s aborted: %s", container_name, exc)


async def _read_cgroup_oom_count(container_name: str) -> int:
    """Best-effort read of the kernel oom counter from the container's
    cgroup. Returns 0 on any failure (cgroup gone, permission denied,
    cgroup v1, etc.) — we'd rather under-report than crash."""
    rc, cid, _ = await _run(
        f"docker inspect --format '{{{{.Id}}}}' {container_name}", timeout=5,
    )
    if rc != 0 or not cid.strip():
        return 0
    cid = cid.strip().strip("'\"")
    # cgroup v2 path under systemd-managed docker:
    candidate = Path(
        f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.events"
    )
    if not candidate.is_file():
        # rootless / cgroupns / non-systemd layouts vary; try the
        # generic v2 path that the daemon writes when in cgroupfs mode.
        candidate = Path(f"/sys/fs/cgroup/docker/{cid}/memory.events")
    if not candidate.is_file():
        return 0
    try:
        for line in candidate.read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except Exception:
        return 0
    return 0


async def _record_sandbox_oom(agent_id: str, container_name: str,
                              tenant_id: str, *, tier: str,
                              memory_limit: str, exit_code: int) -> None:
    """Atomic side-effects for a confirmed OOM kill: metric, audit row,
    SSE event, in-memory status bump. Each side-effect is best-effort
    so a single failure (e.g., audit table missing in tests) doesn't
    swallow the others."""
    info = _containers.get(agent_id)
    if info is not None and info.container_name == container_name:
        info.status = "killed_oom"
    logger.warning(
        "[SANDBOX OOM] %s (tenant=%s, mem=%s) — kernel OOM-killer fired",
        container_name, tenant_id, memory_limit,
    )
    try:
        from backend import metrics as _m
        _m.sandbox_oom_total.labels(tenant_id=tenant_id, tier=tier).inc()
    except Exception:
        pass
    try:
        from backend import audit as _audit
        await _audit.log(
            action="sandbox.oom",
            entity_kind="container",
            entity_id=container_name,
            after={
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "tier": tier,
                "memory_limit": memory_limit,
                "exit_code": exit_code,
                "reason": "cgroup_oom_killer",
            },
            actor="system:oom-watchdog",
        )
    except Exception as exc:
        logger.debug("audit log for sandbox.oom failed: %s", exc)
    try:
        emit_container(agent_id, "oom_killed",
                       f"{container_name} (tenant={tenant_id}, mem={memory_limit})")
        emit_agent_update(
            agent_id, "error",
            f"Sandbox OOM-killed (tenant={tenant_id}, memory={memory_limit})",
        )
    except Exception:
        pass


async def assert_image_trusted(image: str = DOCKER_IMAGE) -> None:
    """Reject launch if the configured allow-list is non-empty and the
    image's digest isn't in it. Empty allow-list = open mode (today's
    behaviour, doesn't break dev)."""
    from backend.config import settings as _settings
    allowed = _parse_allowed_digests(_settings.docker_image_allowed_digests or "")
    if not allowed:
        return  # open mode — explicit decision by operator
    digest = await _inspect_image_digest(image)
    if digest is None:
        logger.warning(
            "cannot resolve digest for %s — refusing launch under "
            "strict allow-list mode", image,
        )
        try:
            from backend import metrics as _m
            _m.sandbox_image_rejected_total.labels(image=image).inc()
        except Exception:
            pass
        raise ImageNotTrusted(
            f"image {image!r}: digest unresolvable, cannot verify trust"
        )
    if digest not in allowed:
        logger.error(
            "image %s digest %s NOT in OMNISIGHT_DOCKER_IMAGE_ALLOWED_DIGESTS — "
            "refusing launch (possible tampering or stale image)",
            image, digest,
        )
        try:
            from backend import metrics as _m
            _m.sandbox_image_rejected_total.labels(image=image).inc()
        except Exception:
            pass
        raise ImageNotTrusted(
            f"image {image!r} digest {digest} not in trust list"
        )
    logger.debug("image %s trusted (digest %s)", image, digest)


@dataclass
class ContainerInfo:
    """Tracks a running agent container."""
    agent_id: str
    container_id: str
    container_name: str
    workspace_path: Path
    image: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "running"  # running | stopped | removed | killed_lifetime | killed_oom
    # Phase 64-A S4: handle to the lifetime-cap watchdog so stop_container
    # can cancel it cleanly when the agent finishes naturally.
    lifetime_task: object | None = None  # asyncio.Task
    # M1: handle to the OOM watchdog (similar lifecycle to lifetime_task).
    oom_task: object | None = None  # asyncio.Task
    # M1: tenant_id + tokens used for this container, recorded so the
    # OOM watchdog and audit trail can attribute the kill correctly.
    tenant_id: str = "t-default"
    tenant_budget: float = 0.0
    cpus: str = ""
    memory: str = ""
    cpu_shares: int = 1024


# Registry of active containers
_containers: dict[str, ContainerInfo] = {}


async def _run(cmd: str, timeout: int = DOCKER_TIMEOUT) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def ensure_image() -> bool:
    """Build the agent Docker image if it doesn't exist. Returns True if ready."""
    rc, out, _ = await _run(f"docker image inspect {DOCKER_IMAGE} 2>/dev/null")
    if rc == 0:
        logger.info("Docker image %s already exists", DOCKER_IMAGE)
        return True

    emit_pipeline_phase("docker_build", f"Building agent image: {DOCKER_IMAGE}")

    dockerfile = Path(__file__).parent / "docker" / "Dockerfile.agent"
    if not dockerfile.exists():
        logger.error("Dockerfile not found: %s", dockerfile)
        return False

    rc, out, err = await _run(
        f"docker build -t {DOCKER_IMAGE} -f {dockerfile} {dockerfile.parent}",
        timeout=BUILD_TIMEOUT,
    )
    if rc != 0:
        logger.error("Docker build failed: %s", err or out)
        emit_pipeline_phase("docker_build_error", err[:300])
        return False

    emit_pipeline_phase("docker_build_done", f"Image {DOCKER_IMAGE} built successfully")
    return True


async def start_container(agent_id: str, workspace_path: Path,
                          *, tier: str = "t1",
                          tenant_id: str | None = None,
                          tenant_budget: float | None = None) -> ContainerInfo:
    """Start a Docker container for an agent with its workspace mounted.

    The workspace directory is bind-mounted to /workspace inside the
    container. `tier` selects the network policy:

      * ``"t1"`` (default): air-gap by default, optional T1 egress
        whitelist via OMNISIGHT_T1_ALLOW_EGRESS double-gate.
      * ``"networked"``: place container on the omnisight-egress-t2
        bridge with public-internet egress (RFC1918 still DROPped by
        the host iptables script). Caller is responsible for any
        Decision-Engine gate (sandbox/networked, severity=risky).

    M1 — Resource hard isolation:

      * ``tenant_id``: stamped as a docker label so M4 cgroup metrics
        can aggregate per-tenant usage and the OOM watchdog can attach
        the right tenant to ``sandbox.oom`` audit rows. Defaults to
        ``current_tenant_id()`` from the request context, then
        ``"t-default"``.
      * ``tenant_budget``: DRF tokens (SandboxCostWeight) the I6 layer
        granted this launch. Translated by ``_compute_resource_limits``
        into ``--cpus`` (CFS quota cap), ``--memory`` (OOM trigger),
        and ``--cpu-shares`` (cgroup v2 cpu.weight proportional share).
        ``None`` = legacy settings.docker_*_limit fallback.
    """
    container_name = f"omnisight-agent-{agent_id}"

    # Stop existing container if any
    if agent_id in _containers:
        await stop_container(agent_id)

    # M2: hard-quota gate. If the tenant is at/over their disk quota,
    # refuse the launch with QuotaExceeded — the sandbox-create router
    # translates this into HTTP 507 (RFC 4918 Insufficient Storage).
    # We resolve the tenant first so the gate fires even when callers
    # don't pass tenant_id explicitly.
    _gate_tenant_id = tenant_id
    if _gate_tenant_id is None:
        try:
            from backend.db_context import current_tenant_id as _ctid
            _gate_tenant_id = _ctid()
        except Exception:
            _gate_tenant_id = None
    try:
        from backend import tenant_quota as _tq
        _tq.check_hard_quota(_gate_tenant_id or "t-default")
    except Exception as exc:
        if exc.__class__.__name__ == "QuotaExceeded":
            try:
                from backend import metrics as _m
                _m.sandbox_launch_total.labels(
                    tier=tier, runtime="?", result="quota_exceeded",
                ).inc()
            except Exception:
                pass
            try:
                from backend import audit as _audit
                await _audit.log(
                    action="sandbox_quota_exceeded",
                    entity_kind="container",
                    entity_id=container_name,
                    after={
                        "tenant_id": _gate_tenant_id or "t-default",
                        "used_bytes": getattr(exc, "used", 0),
                        "hard_bytes": getattr(exc, "hard", 0),
                    },
                    actor=f"agent:{agent_id}",
                )
            except Exception:
                pass
            raise

    emit_pipeline_phase("container_start", f"Starting container for {agent_id}")

    # Ensure image exists
    if not await ensure_image():
        raise RuntimeError(f"Docker image {DOCKER_IMAGE} not available")

    # Phase 64-A S3: refuse launch if a digest allow-list is configured
    # and this image isn't on it. No-op when the allow-list is empty.
    try:
        await assert_image_trusted(DOCKER_IMAGE)
    except ImageNotTrusted as exc:
        # S5: count + audit the rejection so an attacker swapping the
        # image is visible in both metrics and the hash-chained log.
        try:
            from backend import metrics as _m
            _m.sandbox_launch_total.labels(
                tier=tier, runtime="?", result="image_rejected",
            ).inc()
        except Exception:
            pass
        try:
            from backend import audit as _audit
            await _audit.log(
                action="sandbox_image_rejected",
                entity_kind="container",
                entity_id=container_name,
                after={"image": DOCKER_IMAGE, "reason": str(exc)},
                actor=f"agent:{agent_id}",
            )
        except Exception:
            pass
        raise

    # Build mount list — workspace always, test_assets/simulate.sh conditionally (:ro)
    ws_abs = str(workspace_path.resolve())
    mounts = f'-v "{ws_abs}":/workspace '
    test_assets_path = _PROJECT_ROOT / "test_assets"
    if test_assets_path.is_dir() and any(test_assets_path.iterdir()):
        mounts += f'-v "{test_assets_path.resolve()}":/workspace/test_assets:ro '
    scripts_path = _PROJECT_ROOT / "scripts" / "simulate.sh"
    if scripts_path.is_file():
        mounts += f'-v "{scripts_path.resolve()}":/opt/omnisight/simulate.sh:ro '

    # Vendor SDK mount: read platform hint from workspace, mount sysroot + toolchain :ro
    platform_hint = workspace_path / ".omnisight" / "platform"
    if platform_hint.is_file():
        try:
            import yaml
            from backend.sdk_provisioner import _platform_profile
            platform_name = platform_hint.read_text().strip()
            platform_yaml = _platform_profile(platform_name)
            if platform_yaml is not None and platform_yaml.is_file():
                pdata = yaml.safe_load(platform_yaml.read_text())
                sysroot = pdata.get("sysroot_path", "")
                if sysroot and Path(sysroot).is_dir():
                    mounts += f'-v "{Path(sysroot).resolve()}":/opt/vendor_sysroot:ro '
                elif sysroot:
                    logger.warning("Sysroot not found: %s — container will use host compiler. Run: /sdks install %s", sysroot, platform_name)
                    # S5 fix: local re-import here used to shadow the
                    # module-level `emit_pipeline_phase` for the WHOLE
                    # function (Python scope rules), making line 324
                    # raise UnboundLocalError when this branch wasn't
                    # taken. Use the already-imported name.
                    emit_pipeline_phase("env_check", f"[WARNING] Sysroot missing: {sysroot} — cross-compile may fail")
                cmake_tc = pdata.get("cmake_toolchain_file", "")
                if cmake_tc and Path(cmake_tc).is_file():
                    mounts += f'-v "{Path(cmake_tc).resolve()}":/opt/toolchain.cmake:ro '
                elif cmake_tc:
                    logger.warning("CMake toolchain not found: %s", cmake_tc)
        except Exception as exc:
            logger.warning("Vendor SDK mount (best-effort) failed: %s", exc)

    # Start container with workspace mounted + resource limits
    from backend.config import settings as _settings

    # M1 — translate DRF token budget into cgroup hard limits. When
    # tenant_budget is None this preserves the legacy behaviour
    # (settings-based --cpus / --memory, default cpu-shares).
    cpus, mem, cpu_shares = _compute_resource_limits(tenant_budget)

    # M1 — resolve effective tenant_id. Caller wins; otherwise pull
    # from the request context so M4 cgroup metrics + the OOM watchdog
    # can label correctly even when older callers haven't been updated.
    if tenant_id is None:
        try:
            from backend.db_context import current_tenant_id as _ctid
            tenant_id = _ctid()
        except Exception:
            tenant_id = None
    effective_tenant_id = tenant_id or "t-default"

    # Phase 64-A S1: gVisor (runsc) when available; runc fallback otherwise.
    runtime = await resolve_runtime()
    # Phase 64-A S2 / 64-B / 64-C-LOCAL S2: pick the network arg per tier.
    from backend import sandbox_net as _sn
    if tier == "networked":
        network_arg = await _sn.resolve_t2_network_arg()
    elif tier == "t3-local":
        # Phase 64-C-LOCAL: the whole point of T3-LOCAL is that the
        # "target" IS the host. Smoke-tests hitting localhost, app
        # servers binding 0.0.0.0, systemd reloads — all need the
        # host network namespace. runsc still contains syscalls; we
        # don't lose the gVisor sandbox just because networking is
        # shared. If a deployment needs stricter isolation the
        # operator falls back to tier="networked" + egress rules.
        network_arg = "--network host"
    else:
        # M6 — let the per-tenant egress policy participate in the
        # decision. tenant_id is fully resolved a few lines above.
        network_arg = await _sn.resolve_network_arg(tenant_id=effective_tenant_id)
    # M1: per-tenant labels + cpu-shares for cgroup v2 cpu.weight.
    # The "tenant_id" / "tokens" labels are what M4 will key its
    # /sys/fs/cgroup scrape off, and what the OOM watchdog reads back
    # via `docker inspect`. Quote values defensively in case a future
    # tenant_id ever contains a shell metacharacter.
    tokens_label = f"{tenant_budget:.2f}" if tenant_budget else "0"
    rc, out, err = await _run(
        f"docker run -d "
        f"--runtime={runtime} "
        f"--name {container_name} "
        f"--label tenant_id={effective_tenant_id} "
        f"--label tokens={tokens_label} "
        f"{mounts}"
        f"-w /workspace "
        f"{network_arg} "
        f"--memory={mem} --cpus={cpus} --cpu-shares={cpu_shares} "
        f"--pids-limit=256 "
        f"{DOCKER_IMAGE}"
    )
    if rc != 0:
        # Phase 64-A S5: count the failed launch.
        try:
            from backend import metrics as _m
            _m.sandbox_launch_total.labels(
                tier=tier, runtime=runtime, result="error",
            ).inc()
        except Exception:
            pass
        raise RuntimeError(f"Failed to start container: {err or out}")

    container_id = out.strip()[:12]

    # Configure git inside container
    await exec_in_container(
        container_id,
        f'git config --global user.name "Agent-{agent_id}" '
        f'&& git config --global user.email "{agent_id}@omnisight.local"'
    )

    info = ContainerInfo(
        agent_id=agent_id,
        container_id=container_id,
        container_name=container_name,
        workspace_path=workspace_path,
        image=DOCKER_IMAGE,
        tenant_id=effective_tenant_id,
        tenant_budget=float(tenant_budget or 0.0),
        cpus=cpus,
        memory=mem,
        cpu_shares=cpu_shares,
    )
    _containers[agent_id] = info

    # Phase 64-A S5: count the successful launch + write an audit row.
    try:
        from backend import metrics as _m
        _m.sandbox_launch_total.labels(
            tier=tier, runtime=runtime, result="success",
        ).inc()
    except Exception:
        pass
    try:
        from backend import audit as _audit
        await _audit.log(
            action="sandbox_launched",
            entity_kind="container",
            entity_id=container_name,
            after={
                "agent_id": agent_id,
                "container_id": container_id,
                "image": DOCKER_IMAGE,
                "tier": tier,
                "runtime": runtime,
                "network": network_arg.split()[-1],  # "none" or bridge name
                # M1: persist the kernel-enforced share so an auditor can
                # reconstruct who got what at launch time.
                "tenant_id": effective_tenant_id,
                "tenant_budget": float(tenant_budget or 0.0),
                "cpus": cpus,
                "memory": mem,
                "cpu_shares": cpu_shares,
            },
            actor=f"agent:{agent_id}",
        )
    except Exception as exc:
        logger.debug("audit log for sandbox_launched failed: %s", exc)

    # Phase 64-A S4: start the lifetime killswitch (0 = disabled).
    lifetime = int(_settings.sandbox_lifetime_s or 0)
    if lifetime > 0:
        info.lifetime_task = asyncio.create_task(
            _lifetime_killswitch(agent_id, container_name, float(lifetime), tier=tier),
            name=f"sandbox-lifetime-{container_name}",
        )

    # M1: start the OOM watchdog. Polls docker inspect; on terminal
    # state, attributes any cgroup OOM kill to the right tenant. No
    # cost when the container exits cleanly — the watchdog just
    # returns.
    info.oom_task = asyncio.create_task(
        _oom_watchdog(
            agent_id, container_name, effective_tenant_id,
            tier=tier, memory_limit=mem,
        ),
        name=f"sandbox-oom-{container_name}",
    )

    emit_container(agent_id, "started", f"{container_name} ({container_id})")
    emit_agent_update(agent_id, "running", f"Container {container_id} running")
    logger.info("Container started: %s (%s)", container_name, container_id)
    return info


async def start_networked_container(agent_id: str, workspace_path: Path,
                                    *, tenant_id: str | None = None,
                                    tenant_budget: float | None = None) -> ContainerInfo:
    """Phase 64-B convenience wrapper. Equivalent to
    ``start_container(..., tier="networked")``. Use this from MLOps /
    third-party-API agent paths *after* the caller has cleared the
    Decision Engine ``sandbox/networked`` gate (severity=risky)."""
    return await start_container(
        agent_id, workspace_path, tier="networked",
        tenant_id=tenant_id, tenant_budget=tenant_budget,
    )


async def start_t3_local_container(agent_id: str, workspace_path: Path,
                                   *, tenant_id: str | None = None,
                                   tenant_budget: float | None = None) -> ContainerInfo:
    """Phase 64-C-LOCAL S2 — T3 executor for the host==target path.

    Used by the T3 runner when `t3_resolver.resolve_t3_runner()` has
    picked the LOCAL kind: same arch, same OS, so a binary built in
    this sandbox can run on the host. Concretely, `--network host`
    so smoke-tests against localhost / systemd-managed services work,
    plus the same runsc/workspace envelope T1 ships with.

    Callers should go through `t3_resolver.resolve_t3_runner()` rather
    than invoking this directly — it records the metric and delegates.

    Deploy actions that need to mutate host state (systemctl enable,
    write to /etc/nginx/, etc.) should still NOT happen inside this
    container. The pattern is: agent produces artefact + install.sh
    in /workspace, orchestrator runs install.sh on the host (outside
    the sandbox) as a separate pipeline step. That keeps the sandbox
    a containment boundary even on the happy-path.
    """
    return await start_container(
        agent_id, workspace_path, tier="t3-local",
        tenant_id=tenant_id, tenant_budget=tenant_budget,
    )


async def dispatch_t3(
    agent_id: str,
    workspace_path: Path,
    target_arch: str = "",
    target_os: str = "linux",
    *,
    tenant_id: str | None = None,
    tenant_budget: float | None = None,
) -> "tuple[ContainerInfo | None, T3RunnerKind]":  # noqa: F821
    """Phase 64-C-LOCAL S2 — single entry point for the T3 dispatcher.

    Consults the resolver, bumps the prometheus counter so the Ops
    Summary panel (S4) can show a live runner distribution, and
    returns (ContainerInfo, RunnerKind). Returns (None, BUNDLE) when
    no live runner is available yet — caller is expected to fall
    back to the artefact-bundle path.

    Extending to SSH/QEMU is additive in this function: as each kind
    gains a backing runner, add a branch here; call sites don't change.
    """
    from backend.t3_resolver import (
        T3RunnerKind, record_dispatch, resolve_t3_runner,
    )
    res = resolve_t3_runner(target_arch, target_os)
    record_dispatch(res.kind)
    logger.info(
        "dispatch_t3: agent=%s target=%s/%s → %s (%s)",
        agent_id, res.target_arch or "?", res.target_os,
        res.kind.value, res.reason,
    )
    if res.kind == T3RunnerKind.LOCAL:
        info = await start_t3_local_container(
            agent_id, workspace_path,
            tenant_id=tenant_id, tenant_budget=tenant_budget,
        )
        return info, res.kind
    if res.kind == T3RunnerKind.SSH:
        from backend.ssh_runner import find_target_for_arch, SSHRunnerInfo
        ssh_target = find_target_for_arch(res.target_arch, res.target_os)
        if ssh_target is not None:
            ssh_info = SSHRunnerInfo(
                agent_id=agent_id,
                target=ssh_target,
                status="ready",
            )
            return ssh_info, res.kind
        logger.warning(
            "dispatch_t3: resolver said SSH but no target found for %s/%s",
            res.target_arch, res.target_os,
        )
        return None, T3RunnerKind.BUNDLE
    # BUNDLE / QEMU (64-C-QEMU): no live runner yet; caller handles
    # the bundling / remote dispatch at the orchestrator layer.
    return None, res.kind


async def exec_in_container(
    container_id_or_name: str,
    command: str,
    timeout: int = DOCKER_TIMEOUT,
    *,
    tier: str = "t1",
) -> tuple[int, str]:
    """Execute a command inside a running container.

    Returns (exit_code, combined output). Output is hard-capped at
    `OMNISIGHT_SANDBOX_MAX_OUTPUT_BYTES` (default 10 KB) to keep a
    runaway command from blowing up the Tier-0 LLM context. Truncation
    appends a one-line marker and bumps
    `omnisight_sandbox_output_truncated_total{tier}`.
    """
    # C2 audit (2026-04-19): previous impl used bash -c "{command}" with
    # only a manual replace('"', '\\"') upstream in tools.py. That stops
    # literal double-quotes but lets `$(...)`, backticks, `$VAR`, and
    # newlines escape the outer-shell layer and execute BEFORE docker exec
    # sees the argument. shlex.quote single-quotes the whole command so
    # the outer /bin/sh sees it as one argv slot; `bash -c` inside the
    # container then unwraps the single-quoted literal.
    import shlex as _shlex
    rc, out, err = await _run(
        f'docker exec {container_id_or_name} bash -c {_shlex.quote(command)}',
        timeout=timeout,
    )
    combined = out
    if err:
        combined += f"\n[STDERR] {err}" if out else err
    # Phase 64-D D3: enforce per-exec output cap.
    from backend.config import settings as _settings
    cap = int(_settings.sandbox_max_output_bytes or 0)
    if cap > 0:
        b = combined.encode("utf-8", errors="replace")
        if len(b) > cap:
            head = b[:cap].decode("utf-8", errors="replace")
            combined = (
                f"{head}\n[TRUNCATED — {len(b)} bytes total, "
                f"cap={cap} via OMNISIGHT_SANDBOX_MAX_OUTPUT_BYTES]"
            )
            try:
                from backend import metrics as _m
                _m.sandbox_output_truncated_total.labels(tier=tier).inc()
            except Exception:
                pass
    return rc, combined


async def stop_container(agent_id: str) -> bool:
    """Stop and remove a container. Returns True if stopped."""
    info = _containers.pop(agent_id, None)
    if not info:
        return False

    # Phase 64-A S4: cancel the lifetime watchdog so it doesn't fire
    # against an already-removed name.
    task = getattr(info, "lifetime_task", None)
    if task is not None:
        try:
            task.cancel()
        except Exception:
            pass

    # M1: cancel the OOM watchdog. We're tearing the container down
    # explicitly, so any poll-after-this would just hit "no such
    # container" and noisy-log.
    oom = getattr(info, "oom_task", None)
    if oom is not None:
        try:
            oom.cancel()
        except Exception:
            pass

    emit_pipeline_phase("container_stop", f"Stopping container for {agent_id}")

    await _run(f"docker stop {info.container_name} 2>/dev/null", timeout=15)
    await _run(f"docker rm -f {info.container_name} 2>/dev/null", timeout=15)

    # M2: force-clear the tenant's /tmp/omnisight_ingest/<tid>/ namespace
    # so a sandbox's scratch space doesn't accumulate across runs. Best-
    # effort: a failure must never block the actual container teardown.
    try:
        from backend import tenant_quota as _tq
        freed = _tq.cleanup_tenant_tmp(info.tenant_id or "t-default")
        if freed:
            logger.debug(
                "tenant tmp cleared on container stop: %s freed=%d",
                info.tenant_id, freed,
            )
    except Exception as exc:
        logger.debug("tenant tmp cleanup failed: %s", exc)

    info.status = "removed"
    emit_container(agent_id, "stopped", info.container_name)
    logger.info("Container stopped: %s", info.container_name)
    return True


async def pause_container(agent_id: str) -> bool:
    """Phase 47-Fix Batch E: docker pause the agent container without
    removing it. Worktree state preserved; CPU/mem reservation released
    by Docker. Returns True on success."""
    info = _containers.get(agent_id)
    if not info:
        return False
    rc, out, err = await _run(f"docker pause {info.container_name}", timeout=10)
    if rc == 0:
        info.status = "paused"
        emit_container(agent_id, "paused", info.container_name)
        logger.info("Container paused: %s", info.container_name)
        return True
    logger.warning("docker pause failed for %s: %s", info.container_name, (err or out)[:120])
    return False


async def unpause_container(agent_id: str) -> bool:
    """Resume a previously paused container. Returns True on success."""
    info = _containers.get(agent_id)
    if not info:
        return False
    rc, out, err = await _run(f"docker unpause {info.container_name}", timeout=10)
    if rc == 0:
        info.status = "running"
        emit_container(agent_id, "resumed", info.container_name)
        logger.info("Container resumed: %s", info.container_name)
        return True
    logger.warning("docker unpause failed for %s: %s", info.container_name, (err or out)[:120])
    return False


def get_container(agent_id: str) -> ContainerInfo | None:
    """Get container info for an agent."""
    return _containers.get(agent_id)


def list_containers() -> list[ContainerInfo]:
    """List all active containers."""
    return list(_containers.values())


async def container_exec_tool(agent_id: str, command: str) -> str:
    """Execute a command in an agent's container (used by tools.py).

    If no container exists for the agent, returns None so tools
    can fall back to host execution.
    """
    info = _containers.get(agent_id)
    if not info:
        return ""  # empty = no container, fall back to host
    rc, output = await exec_in_container(info.container_id, command)
    if rc != 0 and not output:
        output = f"[CONTAINER EXIT CODE: {rc}]"
    return output


async def cleanup_orphaned_containers() -> int:
    """Remove any omnisight-agent-* containers left from a previous crash."""
    rc, out, _ = await _run(
        "docker ps -a --filter name=omnisight-agent- --format '{{.Names}}'",
        timeout=15,
    )
    if rc != 0 or not out.strip():
        return 0
    count = 0
    for name in out.strip().splitlines():
        name = name.strip()
        if name:
            await _run(f'docker rm -f "{name}"', timeout=15)
            count += 1
            logger.info("Removed orphaned container: %s", name)
    return count


async def stop_all_containers() -> int:
    """Stop all tracked containers (used by emergency halt)."""
    count = 0
    for agent_id in list(_containers.keys()):
        try:
            await stop_container(agent_id)
            count += 1
        except Exception:
            pass
    return count
