"""Phase 64-C-SSH — paramiko-based remote runner for cross-arch targets.

When the host arch does not match the target arch, the T3 resolver
selects SSH as the runner kind. This module connects to the remote
board via SSH, syncs workspace files (sftp), executes commands, and
collects output — all within a timeout + heartbeat envelope.

Credential lookup follows the git_credentials.yaml pattern: a
dedicated ssh_credentials.yaml (or per-platform deploy fields)
provides host, user, key path, and optional port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import paramiko
except ImportError:
    paramiko = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _require_paramiko():
    """Raise a clear error if paramiko is not installed."""
    if paramiko is None:
        raise ImportError(
            "paramiko is required for SSH runner (C1 remote execution). "
            "Install it with: pip install paramiko\n"
            "Or install OmniSight with SSH extras: pip install -e '.[ssh]'"
        )

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class SSHTarget:
    host: str
    port: int = 22
    user: str = "root"
    key_path: str = "~/.ssh/id_ed25519"
    sysroot_path: str = ""
    scratch_dir: str = "/tmp/omnisight"


@dataclass
class SSHRunnerInfo:
    agent_id: str
    target: SSHTarget
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "connected"
    pid: Optional[int] = None


_active_sessions: dict[str, SSHRunnerInfo] = {}


def _resolve_key_path(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def _load_ssh_credentials() -> list[dict]:
    """Load SSH target credentials from ssh_credentials.yaml or
    platform profile deploy fields."""
    import yaml
    from backend.config import settings

    candidates = []
    if settings.ssh_credentials_file:
        candidates.append(Path(settings.ssh_credentials_file).expanduser())
    candidates.append(_PROJECT_ROOT / "configs" / "ssh_credentials.yaml")
    candidates.append(Path("~/.config/omnisight/ssh_credentials.yaml").expanduser())

    for path in candidates:
        try:
            resolved = path.resolve(strict=False)
            if resolved.exists():
                data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
                targets = data.get("targets", [])
                if isinstance(targets, list):
                    logger.info("Loaded %d SSH targets from %s", len(targets), resolved)
                    return targets
        except Exception as exc:
            logger.warning("SSH credential load from %s failed: %s", path, exc)

    return []


_SSH_CRED_CACHE: list[dict] | None = None
_SSH_CRED_LOCK = threading.Lock()


def get_ssh_targets() -> list[dict]:
    global _SSH_CRED_CACHE
    cached = _SSH_CRED_CACHE
    if cached is not None:
        return list(cached)
    with _SSH_CRED_LOCK:
        if _SSH_CRED_CACHE is not None:
            return list(_SSH_CRED_CACHE)
        _SSH_CRED_CACHE = _load_ssh_credentials()
        return list(_SSH_CRED_CACHE)


def clear_ssh_credential_cache() -> None:
    global _SSH_CRED_CACHE
    with _SSH_CRED_LOCK:
        _SSH_CRED_CACHE = None


def find_target_for_arch(target_arch: str, target_os: str = "linux") -> SSHTarget | None:
    """Find a registered SSH target matching the requested arch/os."""
    from backend.routers.system import _canon_arch

    wanted = _canon_arch(target_arch)
    for entry in get_ssh_targets():
        entry_arch = _canon_arch(entry.get("arch", ""))
        entry_os = (entry.get("os", "linux")).lower()
        if entry_arch == wanted and entry_os == target_os.lower():
            return SSHTarget(
                host=entry.get("host", ""),
                port=int(entry.get("port", 22)),
                user=entry.get("user", "root"),
                key_path=entry.get("key_path", "~/.ssh/id_ed25519"),
                sysroot_path=entry.get("sysroot_path", ""),
                scratch_dir=entry.get("scratch_dir", "/tmp/omnisight"),
            )

    # Fallback: check platform profile deploy fields
    try:
        import yaml
        from backend.platform_profile import _NON_PROFILE_FILES  # W0 #274 (renamed in FX.9.3)
        platforms_dir = _PROJECT_ROOT / "configs" / "platforms"
        for yf in platforms_dir.glob("*.yaml"):
            if yf.name in _NON_PROFILE_FILES:
                continue
            data = yaml.safe_load(yf.read_text()) or {}
            profile_arch = _canon_arch(data.get("kernel_arch") or data.get("arch") or "")
            if profile_arch == wanted and data.get("deploy_method") == "ssh":
                host = data.get("deploy_target_ip", "")
                if host:
                    return SSHTarget(
                        host=host,
                        port=int(data.get("deploy_port", 22)),
                        user=data.get("deploy_user", "root"),
                        key_path=data.get("ssh_key", "~/.ssh/id_ed25519"),
                        sysroot_path=data.get("sysroot_path", ""),
                        scratch_dir=data.get("deploy_path", "/tmp/omnisight"),
                    )
    except Exception as exc:
        logger.debug("Platform profile SSH fallback failed: %s", exc)

    return None


def _connect(target: SSHTarget) -> "paramiko.SSHClient":
    """Create and return a connected SSH client."""
    _require_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    known_hosts = Path("~/.ssh/known_hosts").expanduser()
    if known_hosts.exists():
        client.load_host_keys(str(known_hosts))

    key_path = _resolve_key_path(target.key_path)
    if not Path(key_path).exists():
        raise FileNotFoundError(f"SSH key not found: {key_path}")

    key_stat = os.stat(key_path)
    if key_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise PermissionError(
            f"SSH key {key_path} has group/other read permissions — "
            "SSH will refuse it. Run: chmod 600 " + key_path
        )

    client.connect(
        hostname=target.host,
        port=target.port,
        username=target.user,
        key_filename=key_path,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _setup_sandbox(
    client: paramiko.SSHClient,
    target: SSHTarget,
) -> str:
    """Create per-run scratch directory on the remote. Returns the path.
    If sysroot is specified, verify it exists and is read-only."""
    scratch = target.scratch_dir.rstrip("/") + f"/run-{int(time.time())}"
    _exec_sync(client, f"mkdir -p {scratch}")

    if target.sysroot_path:
        rc, out = _exec_sync(client, f"test -d {target.sysroot_path} && echo OK")
        if "OK" not in out:
            logger.warning("Sysroot %s not found on remote", target.sysroot_path)
        else:
            rc, mount_out = _exec_sync(
                client,
                f"mount | grep '{target.sysroot_path}' | grep -q 'ro,' && echo RO"
            )
            if "RO" not in mount_out:
                logger.warning(
                    "Sysroot %s is not mounted read-only — safety degraded",
                    target.sysroot_path,
                )

    return scratch


def _exec_sync(
    client: paramiko.SSHClient,
    command: str,
    timeout: int = 30,
) -> tuple[int, str]:
    """Execute a command synchronously, return (exit_code, output)."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    combined = out
    if err:
        combined += f"\n[STDERR] {err}" if out else err
    return rc, combined.strip()


async def sync_files_to_remote(
    client: paramiko.SSHClient,
    local_path: Path,
    remote_dir: str,
    *,
    exclude_patterns: list[str] | None = None,
) -> int:
    """Upload workspace files to the remote scratch directory via SFTP.
    Returns the number of files transferred."""
    exclude = set(exclude_patterns or [".git", "__pycache__", ".venv", "node_modules"])
    sftp = client.open_sftp()
    count = 0

    def _should_exclude(name: str) -> bool:
        return name in exclude

    def _upload_dir(local: Path, remote: str) -> None:
        nonlocal count
        try:
            sftp.stat(remote)
        except FileNotFoundError:
            sftp.mkdir(remote)

        for item in sorted(local.iterdir()):
            if _should_exclude(item.name):
                continue
            remote_item = f"{remote}/{item.name}"
            if item.is_dir():
                _upload_dir(item, remote_item)
            elif item.is_file():
                sftp.put(str(item), remote_item)
                count += 1

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _upload_dir, local_path, remote_dir)
    sftp.close()
    return count


async def sync_files_from_remote(
    client: paramiko.SSHClient,
    remote_dir: str,
    local_path: Path,
    *,
    patterns: list[str] | None = None,
) -> int:
    """Download output artifacts from remote to local. Returns file count."""
    sftp = client.open_sftp()
    count = 0

    def _download_dir(remote: str, local: Path) -> None:
        nonlocal count
        local.mkdir(parents=True, exist_ok=True)
        for attr in sftp.listdir_attr(remote):
            remote_item = f"{remote}/{attr.filename}"
            local_item = local / attr.filename
            if stat.S_ISDIR(attr.st_mode or 0):
                _download_dir(remote_item, local_item)
            else:
                if patterns and not any(
                    attr.filename.endswith(p.lstrip("*")) for p in patterns
                ):
                    continue
                sftp.get(remote_item, str(local_item))
                count += 1

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _download_dir, remote_dir, local_path)
    sftp.close()
    return count


async def exec_on_remote(
    client: paramiko.SSHClient,
    command: str,
    *,
    timeout: int = 300,
    heartbeat_interval: int = 30,
    max_output_bytes: int = 10_000,
) -> tuple[int, str]:
    """Execute a command on the remote with timeout + heartbeat monitoring.

    The heartbeat checks that the SSH channel is alive every
    `heartbeat_interval` seconds. If the channel dies or the command
    exceeds `timeout`, the remote process is killed.
    """
    loop = asyncio.get_event_loop()

    def _run() -> tuple[int, str]:
        transport = client.get_transport()
        if transport is None:
            return -1, "[ERROR] SSH transport is None"

        channel = transport.open_session()
        channel.settimeout(float(timeout))
        channel.exec_command(command)

        output_chunks: list[bytes] = []
        total_bytes = 0
        truncated = False
        start = time.monotonic()

        while not channel.exit_status_ready():
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                channel.close()
                try:
                    _exec_sync(client, f"kill -9 $(pgrep -f '{command[:40]}')", timeout=5)
                except Exception:
                    pass
                return -1, "[TIMEOUT] Command exceeded {}s limit".format(timeout)

            if not transport.is_active():
                return -1, "[DISCONNECTED] SSH transport lost"

            if channel.recv_ready():
                chunk = channel.recv(4096)
                if not truncated:
                    if total_bytes + len(chunk) > max_output_bytes:
                        remaining = max_output_bytes - total_bytes
                        output_chunks.append(chunk[:remaining])
                        truncated = True
                    else:
                        output_chunks.append(chunk)
                total_bytes += len(chunk)

            time.sleep(min(1.0, heartbeat_interval / 10))

        while channel.recv_ready():
            chunk = channel.recv(4096)
            if not truncated:
                if total_bytes + len(chunk) > max_output_bytes:
                    remaining = max_output_bytes - total_bytes
                    output_chunks.append(chunk[:remaining])
                    truncated = True
                else:
                    output_chunks.append(chunk)
            total_bytes += len(chunk)

        stderr_chunks: list[bytes] = []
        while channel.recv_stderr_ready():
            stderr_chunks.append(channel.recv_stderr(4096))

        rc = channel.recv_exit_status()
        channel.close()

        out = b"".join(output_chunks).decode(errors="replace")
        err = b"".join(stderr_chunks).decode(errors="replace")
        if err:
            out += f"\n[STDERR] {err}" if out else err
        if truncated:
            out += (
                f"\n[TRUNCATED — {total_bytes} bytes total, "
                f"cap={max_output_bytes}]"
            )
        return rc, out.strip()

    return await loop.run_in_executor(None, _run)


async def run_on_target(
    agent_id: str,
    workspace_path: Path,
    target: SSHTarget,
    command: str,
    *,
    sync_workspace: bool = True,
    sync_back_patterns: list[str] | None = None,
) -> tuple[int, str, SSHRunnerInfo]:
    """Full SSH runner lifecycle: connect → sandbox → sync → exec → collect.

    Returns (exit_code, output, runner_info).
    """
    from backend.config import settings
    from backend.events import emit_pipeline_phase

    timeout = settings.ssh_runner_timeout
    heartbeat = settings.ssh_runner_heartbeat_interval
    max_output = settings.ssh_runner_max_output_bytes

    emit_pipeline_phase(
        "ssh_runner_start",
        f"Connecting to {target.user}@{target.host}:{target.port} for {agent_id}",
    )

    loop = asyncio.get_event_loop()
    try:
        client = await loop.run_in_executor(None, _connect, target)
    except Exception as exc:
        logger.error("SSH connect failed for %s: %s", target.host, exc)
        emit_pipeline_phase("ssh_runner_error", f"Connect failed: {exc}")
        info = SSHRunnerInfo(agent_id=agent_id, target=target, status="connect_failed")
        return -1, f"[SSH CONNECT ERROR] {exc}", info

    info = SSHRunnerInfo(agent_id=agent_id, target=target, status="connected")
    _active_sessions[agent_id] = info

    try:
        scratch = await loop.run_in_executor(
            None, _setup_sandbox, client, target,
        )
        logger.info("SSH sandbox: %s on %s", scratch, target.host)

        if sync_workspace:
            emit_pipeline_phase("ssh_sync_upload", f"Syncing workspace to {target.host}")
            file_count = await sync_files_to_remote(
                client, workspace_path, scratch,
            )
            logger.info("Synced %d files to %s:%s", file_count, target.host, scratch)

        full_cmd = f"cd {scratch} && {command}"
        emit_pipeline_phase("ssh_exec", f"Running: {command[:80]}")

        rc, output = await exec_on_remote(
            client,
            full_cmd,
            timeout=timeout,
            heartbeat_interval=heartbeat,
            max_output_bytes=max_output,
        )

        if sync_back_patterns:
            emit_pipeline_phase("ssh_sync_download", "Collecting artifacts")
            dl_count = await sync_files_from_remote(
                client, scratch, workspace_path / ".ssh_output",
                patterns=sync_back_patterns,
            )
            logger.info("Downloaded %d artifacts from %s", dl_count, target.host)

        info.status = "completed" if rc == 0 else "failed"
        emit_pipeline_phase(
            "ssh_runner_done",
            f"Exit code {rc} on {target.host}",
        )
        return rc, output, info

    except Exception as exc:
        logger.error("SSH runner error for %s: %s", agent_id, exc)
        info.status = "error"
        emit_pipeline_phase("ssh_runner_error", str(exc))
        return -1, f"[SSH ERROR] {exc}", info

    finally:
        try:
            client.close()
        except Exception:
            pass
        _active_sessions.pop(agent_id, None)


def get_active_session(agent_id: str) -> SSHRunnerInfo | None:
    return _active_sessions.get(agent_id)


def list_active_sessions() -> list[SSHRunnerInfo]:
    return list(_active_sessions.values())


async def kill_session(agent_id: str) -> bool:
    """Kill an active SSH session."""
    info = _active_sessions.pop(agent_id, None)
    if not info:
        return False
    info.status = "killed"
    logger.info("SSH session killed for %s", agent_id)
    return True
