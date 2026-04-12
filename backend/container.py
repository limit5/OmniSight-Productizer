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


@dataclass
class ContainerInfo:
    """Tracks a running agent container."""
    agent_id: str
    container_id: str
    container_name: str
    workspace_path: Path
    image: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "running"  # running | stopped | removed


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


async def start_container(agent_id: str, workspace_path: Path) -> ContainerInfo:
    """Start a Docker container for an agent with its workspace mounted.

    The workspace directory is bind-mounted to /workspace inside the container.
    """
    container_name = f"omnisight-agent-{agent_id}"

    # Stop existing container if any
    if agent_id in _containers:
        await stop_container(agent_id)

    emit_pipeline_phase("container_start", f"Starting container for {agent_id}")

    # Ensure image exists
    if not await ensure_image():
        raise RuntimeError(f"Docker image {DOCKER_IMAGE} not available")

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
            platform_name = platform_hint.read_text().strip()
            platform_yaml = _PROJECT_ROOT / "configs" / "platforms" / f"{platform_name}.yaml"
            if platform_yaml.is_file():
                pdata = yaml.safe_load(platform_yaml.read_text())
                sysroot = pdata.get("sysroot_path", "")
                if sysroot and Path(sysroot).is_dir():
                    mounts += f'-v "{Path(sysroot).resolve()}":/opt/vendor_sysroot:ro '
                cmake_tc = pdata.get("cmake_toolchain_file", "")
                if cmake_tc and Path(cmake_tc).is_file():
                    mounts += f'-v "{Path(cmake_tc).resolve()}":/opt/toolchain.cmake:ro '
        except Exception as exc:
            logger.warning("Vendor SDK mount (best-effort) failed: %s", exc)

    # Start container with workspace mounted + resource limits
    from backend.config import settings as _settings
    mem = _settings.docker_memory_limit or "1g"
    cpus = _settings.docker_cpu_limit or "2"
    rc, out, err = await _run(
        f"docker run -d "
        f"--name {container_name} "
        f"{mounts}"
        f"-w /workspace "
        f"--network none "
        f"--memory={mem} --cpus={cpus} --pids-limit=256 "
        f"{DOCKER_IMAGE}"
    )
    if rc != 0:
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
    )
    _containers[agent_id] = info

    emit_container(agent_id, "started", f"{container_name} ({container_id})")
    emit_agent_update(agent_id, "running", f"Container {container_id} running")
    logger.info("Container started: %s (%s)", container_name, container_id)
    return info


async def exec_in_container(
    container_id_or_name: str,
    command: str,
    timeout: int = DOCKER_TIMEOUT,
) -> tuple[int, str]:
    """Execute a command inside a running container.

    Returns (exit_code, combined output).
    """
    rc, out, err = await _run(
        f'docker exec {container_id_or_name} bash -c "{command}"',
        timeout=timeout,
    )
    combined = out
    if err:
        combined += f"\n[STDERR] {err}" if out else err
    return rc, combined


async def stop_container(agent_id: str) -> bool:
    """Stop and remove a container. Returns True if stopped."""
    info = _containers.pop(agent_id, None)
    if not info:
        return False

    emit_pipeline_phase("container_stop", f"Stopping container for {agent_id}")

    await _run(f"docker stop {info.container_name} 2>/dev/null", timeout=15)
    await _run(f"docker rm -f {info.container_name} 2>/dev/null", timeout=15)

    info.status = "removed"
    emit_container(agent_id, "stopped", info.container_name)
    logger.info("Container stopped: %s", info.container_name)
    return True


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
