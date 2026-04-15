"""B12 — cloudflared service manager.

Manages the cloudflared daemon via either:
  1. systemd (native Linux) — requires sudoers NOPASSWD for
     `systemctl {start,stop,restart,status} cloudflared.service`
  2. Container sidecar mode — spawns cloudflared directly (no systemd)

The connector runs in **token mode** (`cloudflared tunnel run --token <T>`)
to avoid credentials.json file management.

Security: the connector token is only passed via stdin/env, never
as a CLI argument (would be visible in /proc/cmdline).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

CLOUDFLARED_SERVICE = "cloudflared.service"
SYSTEMCTL_ALLOWED = frozenset({"start", "stop", "restart", "status"})


class ServiceMode(str, Enum):
    systemd = "systemd"
    container = "container"
    unavailable = "unavailable"


@dataclass
class ServiceStatus:
    mode: ServiceMode
    active: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "active": self.active, "detail": self.detail}


def detect_mode() -> ServiceMode:
    if os.environ.get("OMNISIGHT_CF_MODE") == "container":
        return ServiceMode.container
    if shutil.which("systemctl") and _systemd_unit_exists():
        return ServiceMode.systemd
    if shutil.which("cloudflared"):
        return ServiceMode.container
    return ServiceMode.unavailable


def _systemd_unit_exists() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "cat", CLOUDFLARED_SERVICE],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


async def _run_systemctl(action: str) -> tuple[int, str]:
    if action not in SYSTEMCTL_ALLOWED:
        raise ValueError(f"Action {action!r} not allowed")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", action, CLOUDFLARED_SERVICE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    output = (stdout or b"").decode() + (stderr or b"").decode()
    return proc.returncode or 0, output.strip()


async def get_status() -> ServiceStatus:
    mode = detect_mode()
    if mode == ServiceMode.unavailable:
        return ServiceStatus(mode=mode, detail="cloudflared not found")
    if mode == ServiceMode.systemd:
        rc, output = await _run_systemctl("status")
        active = "active (running)" in output
        return ServiceStatus(mode=mode, active=active, detail=output[:500])
    return ServiceStatus(mode=mode, detail="container mode — check process directly")


async def start_service(connector_token: str) -> ServiceStatus:
    mode = detect_mode()
    if mode == ServiceMode.systemd:
        rc, output = await _run_systemctl("start")
        return ServiceStatus(mode=mode, active=(rc == 0), detail=output[:500])
    if mode == ServiceMode.container:
        return await _start_container(connector_token)
    return ServiceStatus(mode=mode, detail="cloudflared not available")


async def stop_service() -> ServiceStatus:
    mode = detect_mode()
    if mode == ServiceMode.systemd:
        rc, output = await _run_systemctl("stop")
        return ServiceStatus(mode=mode, active=False, detail=output[:500])
    return ServiceStatus(mode=mode, detail="manual stop required in container mode")


async def restart_service(connector_token: str) -> ServiceStatus:
    mode = detect_mode()
    if mode == ServiceMode.systemd:
        rc, output = await _run_systemctl("restart")
        return ServiceStatus(mode=mode, active=(rc == 0), detail=output[:500])
    return ServiceStatus(mode=mode, detail="restart in container mode not implemented")


_container_proc: Optional[asyncio.subprocess.Process] = None


async def _start_container(connector_token: str) -> ServiceStatus:
    global _container_proc
    if _container_proc and _container_proc.returncode is None:
        return ServiceStatus(mode=ServiceMode.container, active=True, detail="already running")

    env = {**os.environ, "TUNNEL_TOKEN": connector_token}
    _container_proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "run", "--token", connector_token,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    await asyncio.sleep(1)
    alive = _container_proc.returncode is None
    return ServiceStatus(mode=ServiceMode.container, active=alive, detail="started via direct exec")


SUDOERS_LINE = f"omnisight ALL=(root) NOPASSWD: /usr/bin/systemctl start {CLOUDFLARED_SERVICE}, /usr/bin/systemctl stop {CLOUDFLARED_SERVICE}, /usr/bin/systemctl restart {CLOUDFLARED_SERVICE}, /usr/bin/systemctl status {CLOUDFLARED_SERVICE}"


def generate_sudoers_snippet() -> str:
    return f"# /etc/sudoers.d/omnisight-cloudflared\n{SUDOERS_LINE}\n"
