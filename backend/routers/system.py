"""Real system information endpoints — replaces all frontend mock data.

Reads actual host metrics: CPU, memory, disk, kernel, uptime, USB devices.
Also serves spec (from hardware_manifest.yaml), logs, and token usage.
"""

import asyncio
import logging
import os
import platform
import re
from collections import deque
from datetime import datetime
from pathlib import Path

from backend.config import settings as _settings
from backend import db

logger = logging.getLogger(__name__)

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _auth
from backend.db_pool import get_conn as _get_conn
from backend.routers import _pagination as _pg

from backend.models import (
    SystemInfoResponse, SystemStatusResponse, TokenBudgetResponse, TokenUsageEntry,
    TokenBurnRatePoint, TokenBurnRateResponse,
    TokenHeatmapCell, TokenHeatmapResponse,
    PromptVersionEntry, PromptVersionsListResponse, PromptDiffResponse,
    DeployRequest,
)

# Router-level auth baseline: every /runtime/* route requires an
# authenticated session. Individual write endpoints stack an
# admin-role check on top via their own `dependencies=` list.
#
# Audit H1 (2026-04-19): this router previously had zero auth; the
# CF WAF rule + Zero Trust Access at the edge are defence-in-depth,
# but the application itself must not rely on edge mitigation. See
# docs/ops/deploy_postmortem_2026-04-19.md security follow-ups.
#
# Phase-3 P6 (2026-04-20): prefix renamed ``/system`` → ``/runtime``
# because the CF WAF custom rule documented in
# ``docs/ops/cloudflare_settings.md`` (line 30) — originally added
# as unauth-era defence-in-depth — was returning 403 "Just a
# moment..." challenge pages for every dashboard load, since the
# dashboard legitimately reads ``/system/info`` etc. on mount. The
# application-layer auth here (``Depends(_auth.current_user)`` +
# per-endpoint ``require_role("admin")``) is now load-bearing;
# operators who want edge defence back should re-point the CF rule
# at ``/api/v1/runtime/admin/`` (or a narrower mutation-only
# sub-path) AFTER this rename lands, not the old ``/system/`` one.
router = APIRouter(
    prefix="/runtime",
    tags=["runtime"],
    dependencies=[Depends(_auth.current_user)],
)

# Reusable admin gate for mutating / privileged endpoints.
_REQUIRE_ADMIN = [Depends(_auth.require_role("admin"))]

_BASH_TIMEOUT = 5
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PLATFORMS_DIR = _PROJECT_ROOT / "configs" / "platforms"
_TIER_RULES_PATH = _PROJECT_ROOT / "configs" / "tier_capabilities.yaml"


def _collect_toolchains() -> dict:
    """Scan platform YAMLs + tier_capabilities to build the toolchain enum."""
    # W0 #274: skip schema.yaml — it's a schema declaration, not a profile.
    from backend.platform import _NON_PROFILE_FILES
    by_platform: dict[str, str] = {}
    for p in sorted(_PLATFORMS_DIR.glob("*.yaml")):
        if p.name in _NON_PROFILE_FILES:
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            tc = data.get("toolchain")
            if tc:
                by_platform[data.get("platform", p.stem)] = tc
        except Exception:
            logger.warning("Failed to read platform YAML %s", p)

    by_tier: dict[str, list[str]] = {}
    try:
        tier_data = yaml.safe_load(_TIER_RULES_PATH.read_text(encoding="utf-8"))
        for tid, rules in (tier_data.get("tiers") or {}).items():
            allowed = rules.get("toolchains_allowed") or []
            by_tier[tid] = sorted(allowed)
    except Exception:
        logger.warning("Failed to read tier_capabilities.yaml")

    all_names: set[str] = set(by_platform.values())
    for lst in by_tier.values():
        all_names.update(lst)

    return {
        "all": sorted(all_names),
        "by_platform": by_platform,
        "by_tier": by_tier,
    }


async def _sh(cmd: str) -> str:
    # Fix-B B4: retain empty-string fallback for callers, but surface the
    # failure so diagnostics aren't completely silent. `cmd` is always a
    # compile-time string here (os facts: uptime, df, etc.), not user input.
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT)
        return out.decode(errors="replace").strip()
    except FileNotFoundError as exc:
        logger.debug("_sh(%s): command not found: %s", cmd.split()[:1], exc)
        return ""
    except asyncio.TimeoutError:
        logger.warning("_sh(%s): timed out after %ds", cmd.split()[:1], _BASH_TIMEOUT)
        return ""
    except Exception as exc:
        logger.warning("_sh(%s): unexpected error: %s", cmd.split()[:1], exc)
        return ""


def _parse_uptime(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  System Info
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/info", response_model=SystemInfoResponse)
async def get_system_info():
    cpu_model = (await _sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2")).strip() or platform.processor()
    cpu_cores = os.cpu_count() or 1

    s1 = await _sh("grep 'cpu ' /proc/stat | awk '{u=$2+$4; t=$2+$4+$5; print u, t}'")
    await asyncio.sleep(0.2)
    s2 = await _sh("grep 'cpu ' /proc/stat | awk '{u=$2+$4; t=$2+$4+$5; print u, t}'")
    cpu_usage = 0.0
    try:
        u1, t1 = map(int, s1.split()); u2, t2 = map(int, s2.split())
        if t2 - t1 > 0:
            cpu_usage = round((u2 - u1) / (t2 - t1) * 100, 1)
    except (ValueError, IndexError):
        pass

    mem_info = await _sh("grep -E 'MemTotal|MemAvailable' /proc/meminfo")
    mem_total = mem_used = 0
    try:
        for line in mem_info.splitlines():
            v = int(re.search(r"(\d+)", line).group(1)) // 1024
            if "MemTotal" in line:
                mem_total = v
            elif "MemAvailable" in line:
                mem_used = mem_total - v
    except (AttributeError, ValueError):
        pass

    disk = await _sh("df -BM / | tail -1 | awk '{print $2, $3, $5}'")
    disk_total = disk_used = 0
    disk_pct = ""
    try:
        p = disk.split(); disk_total = int(p[0].rstrip("M")); disk_used = int(p[1].rstrip("M")); disk_pct = p[2]
    except (IndexError, ValueError):
        pass

    uptime_raw = await _sh("cat /proc/uptime | awk '{print $1}'")
    uptime_str = _parse_uptime(float(uptime_raw)) if uptime_raw else "unknown"

    kernel = await _sh("uname -r")
    # Dashboard hostname: prefer the operator-facing public hostname
    # (``OMNISIGHT_PUBLIC_HOSTNAME`` — e.g. ``ai.sora-dev.app``) over
    # the backend container's random docker ID (``3ffe5d490f84``),
    # which is what the raw ``hostname`` command returns inside the
    # container and isn't useful to an operator looking at the
    # SYSTEM INFO card. Falls back through: env var → container
    # hostname → Python ``platform.node()``.
    hostname_env = os.environ.get("OMNISIGHT_PUBLIC_HOSTNAME", "").strip()
    hostname_raw = await _sh("hostname")
    display_hostname = hostname_env or hostname_raw or platform.node()
    os_info = await _sh("grep PRETTY_NAME /etc/os-release | cut -d'\"' -f2")
    if kernel and "microsoft" in kernel.lower() and "WSL" not in (os_info or ""):
        os_info = f"{os_info} (WSL2)"

    return {
        "hostname": display_hostname,
        "os": os_info or f"{platform.system()} {platform.release()}",
        "kernel": kernel or platform.release(),
        "arch": platform.machine(),
        "cpu_model": cpu_model or "Unknown",
        "cpu_cores": cpu_cores,
        "cpu_usage": cpu_usage,
        "memory_total": mem_total,
        "memory_used": mem_used,
        "disk_total_mb": disk_total,
        "disk_used_mb": disk_used,
        "disk_use_pct": disk_pct,
        "uptime": uptime_str,
        "wsl": "microsoft" in (kernel or "").lower(),
        "docker": bool(await _sh("docker info --format '{{.ServerVersion}}' 2>/dev/null")),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Devices
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/devices")
async def get_devices():
    devices = []

    lsusb = await _sh("lsusb 2>/dev/null")
    for line in lsusb.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID (\w+):(\w+)\s+(.*)", line)
        if m:
            bus, dev, vid, pid, name = m.groups()
            devices.append({
                "id": f"usb-{bus}-{dev}", "name": name.strip() or f"USB {vid}:{pid}",
                "type": "camera" if any(k in name.lower() for k in ["cam", "video", "uvc", "webcam"]) else "usb",
                "status": "connected", "vendorId": vid, "productId": pid, "speed": None,
            })

    lsblk = await _sh("lsblk -dno NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null")
    for line in lsblk.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in ("disk", "part"):
            devices.append({
                "id": f"storage-{parts[0]}", "name": f"{parts[0]} ({parts[1]})",
                "type": "storage", "status": "connected",
                "mountPoint": parts[3] if len(parts) > 3 else f"/dev/{parts[0]}",
            })

    ip_out = await _sh("ip -o link show | awk '{print $2, $9}'")
    for line in ip_out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            speed = await _sh(f"cat /sys/class/net/{iface}/speed 2>/dev/null")
            devices.append({
                "id": f"net-{iface}", "name": iface, "type": "network",
                "status": "connected" if parts[1] == "UP" else "disconnected",
                "speed": f"{speed} Mbps" if speed and speed != "-1" else None,
            })

    # V4L2 UVC camera enumeration (enhanced camera detection)
    import glob
    for dev in sorted(glob.glob("/dev/video*"))[:8]:
        dev_name = os.path.basename(dev)
        # Check if already found via lsusb
        if any(d["type"] == "camera" for d in devices):
            # Enhance existing camera entry with V4L2 path
            for d in devices:
                if d["type"] == "camera" and "v4l2_device" not in d:
                    d["v4l2_device"] = dev
                    break
        else:
            card = await _sh(f"v4l2-ctl -d {dev} --info 2>/dev/null | grep 'Card type' | cut -d: -f2")
            devices.append({
                "id": f"v4l2-{dev_name}", "name": card.strip() or f"Camera {dev_name}",
                "type": "camera", "status": "connected",
                "v4l2_device": dev,
            })

    return devices


@router.get("/evk")
async def get_evk_status():
    """Check EVK board reachability for all platforms with deploy config."""
    results = []
    platforms_dir = _PROJECT_ROOT / "configs" / "platforms"
    if not platforms_dir.is_dir():
        return results

    # W0 #274: skip schema.yaml — it's a schema declaration, not a profile.
    from backend.platform import _NON_PROFILE_FILES
    for yf in sorted(platforms_dir.glob("*.yaml")):
        if yf.name in _NON_PROFILE_FILES:
            continue
        try:
            data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        method = data.get("deploy_method", "")
        ip = data.get("deploy_target_ip", "")
        if not method:
            continue

        entry = {
            "platform": yf.stem,
            "board_name": data.get("soc_model", data.get("label", yf.stem)),
            "deploy_method": method,
            "deploy_target_ip": ip,
            "deploy_user": data.get("deploy_user", "root"),
            "deploy_path": data.get("deploy_path", "/opt/app"),
            "reachable": False,
            "last_check": "",
        }

        if ip and method == "ssh":
            user = data.get("deploy_user", "root")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes", f"{user}@{ip}", "echo", "OK",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                entry["reachable"] = "OK" in stdout.decode()
            except Exception:
                entry["reachable"] = False
            from datetime import datetime
            entry["last_check"] = datetime.now().isoformat()
        elif not ip:
            entry["reachable"] = False

        results.append(entry)
    return results


@router.post("/deploy", dependencies=_REQUIRE_ADMIN)
async def trigger_deploy(body: DeployRequest):
    """Trigger deployment to an EVK board. Admin only."""
    from backend.agents.tools import deploy_to_evk
    # Audit H1: reject `..`-containing module / binary_path — without this
    # an authenticated admin could still escape the `build/` prefix to
    # deploy e.g. `/etc/passwd` or `~/.ssh/id_rsa` to attached hardware.
    for name, value in (("module", body.module), ("binary_path", body.binary_path or "")):
        if ".." in value or value.startswith("/"):
            raise HTTPException(
                status_code=400,
                detail=f"invalid {name}: path traversal or absolute paths forbidden",
            )
    binary_path = body.binary_path or f"build/{body.module}"
    result = await deploy_to_evk.ainvoke({
        "platform": body.platform,
        "binary_path": binary_path,
        "run_after_deploy": body.run_after_deploy,
    })
    return {"result": result, "platform": body.platform, "module": body.module}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E2E Pipeline (Phase 46)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PipelineStartRequest(BaseModel):
    spec_context: str = ""  # Optional SPEC description to pass to pipeline tasks


@router.get("/pipeline/status")
async def get_pipeline_status_endpoint():
    """Get the current E2E pipeline run status."""
    from backend.pipeline import get_pipeline_status
    return get_pipeline_status()


@router.post("/pipeline/start", dependencies=_REQUIRE_ADMIN)
async def start_pipeline(body: PipelineStartRequest):
    """Start a full E2E pipeline: SPEC → develop → review → test → deploy → package → docs. Admin only."""
    from backend.pipeline import run_pipeline
    return await run_pipeline(body.spec_context)


@router.post("/pipeline/advance", dependencies=_REQUIRE_ADMIN)
async def advance_pipeline_endpoint():
    """Force-advance past a human checkpoint (Gerrit +2 or HVT confirmed). Admin only."""
    from backend.pipeline import force_advance
    return await force_advance()


@router.get("/pipeline/timeline")
async def get_pipeline_timeline():
    """Phase 50A — timeline with per-step timing + velocity rollup.

    Returns:
      steps: [{ id, name, npi_phase, auto_advance, human_checkpoint,
               planned_at, started_at, completed_at, deadline_at,
               status: idle|active|done|overdue }]
      velocity:
        avg_step_seconds:  mean observed duration across completed steps
        eta_completion:    ISO timestamp estimate for pipeline finish, or null
        tasks_completed_7d: tasks the invoke-layer marked done in the last 7 d
    """
    from backend.pipeline import PIPELINE_STEPS, _active_pipeline, _last_completed_pipeline
    from backend.routers.invoke import _tasks
    from backend.models import TaskStatus
    from datetime import datetime, timedelta

    run = _active_pipeline or _last_completed_pipeline
    history = run.get("step_history") if run else None
    current_idx = (_active_pipeline or {}).get("current_step_index", -1)

    # Rough deadline heuristic: each step gets a default 1-hour budget
    # unless it's already observed a longer run. Real SLAs will come
    # with the Decision Rules phase (50B).
    DEFAULT_STEP_SECONDS = 3600
    now = datetime.now()

    def parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    # Average duration of every step that has both start + end stamps.
    durations: list[float] = []
    if history:
        for h in history:
            s, c = parse(h.get("started_at")), parse(h.get("completed_at"))
            if s and c:
                durations.append((c - s).total_seconds())
    avg_step_sec = sum(durations) / len(durations) if durations else float(DEFAULT_STEP_SECONDS)

    steps_out: list[dict] = []
    for idx, s in enumerate(PIPELINE_STEPS):
        rec = history[idx] if history and idx < len(history) else {}
        started = parse(rec.get("started_at"))
        completed = parse(rec.get("completed_at"))
        deadline = (started + timedelta(seconds=avg_step_sec * 1.5)) if started else None

        if completed:
            status = "done"
        elif idx == current_idx and _active_pipeline:
            status = "overdue" if (deadline and now > deadline) else "active"
        else:
            status = "idle"

        steps_out.append({
            "id": s["id"],
            "name": s["name"],
            "npi_phase": s["npi_phase"],
            "auto_advance": s.get("auto_advance", True),
            "human_checkpoint": s.get("human_checkpoint"),
            "planned_at": None,  # reserved for future SLA-driven planning
            "started_at": started.isoformat() if started else None,
            "completed_at": completed.isoformat() if completed else None,
            "deadline_at": deadline.isoformat() if deadline else None,
            "status": status,
        })

    # ETA: remaining steps × avg_step_sec from now if a run is active.
    eta: str | None = None
    if _active_pipeline and current_idx >= 0:
        remaining = len(PIPELINE_STEPS) - current_idx
        eta = (now + timedelta(seconds=avg_step_sec * remaining)).isoformat()

    # 7-day completion count from the task store. TaskStatus.completed is
    # the shipping terminal state; other terminals (failed/cancelled) are
    # ignored so velocity tracks genuine throughput.
    week_ago = now - timedelta(days=7)
    completed_7d = 0
    for t in _tasks.values():
        if t.status != getattr(TaskStatus, "completed", None):
            continue
        ts = parse(getattr(t, "updated_at", None)) or parse(getattr(t, "created_at", None))
        if ts and ts >= week_ago:
            completed_7d += 1

    return {
        "steps": steps_out,
        "velocity": {
            "avg_step_seconds": avg_step_sec,
            "eta_completion": eta,
            "tasks_completed_7d": completed_7d,
            "pipeline_id": run.get("id") if run else None,
            "pipeline_status": run.get("status") if run else "idle",
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Release Packaging (Phase 40)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_release_lock = asyncio.Lock()


class ReleaseRequest(BaseModel):
    version: str = ""           # Override version (empty = auto-resolve from git)
    artifact_ids: list[str] = Field(default_factory=list)  # Empty = include all
    upload_github: bool = False
    upload_gitlab: bool = False


@router.get("/release/version")
async def get_release_version():
    """Get the current resolved version."""
    from backend.release import resolve_version
    version = await resolve_version()
    return {"version": version}


@router.get("/release/manifest")
async def get_release_manifest(version: str = ""):
    """Generate a release manifest (JSON) listing all artifacts."""
    from backend.release import generate_release_manifest
    manifest = await generate_release_manifest(version)
    return manifest


@router.post("/release", dependencies=_REQUIRE_ADMIN)
async def create_release(body: ReleaseRequest):
    """Create a release bundle and optionally upload to GitHub/GitLab.

    Returns bundle metadata, manifest, and upload results.
    """
    if not _settings.release_enabled:
        # Allow but warn
        logger.info("Release created (release_enabled=False — set to True for production)")

    if _release_lock.locked():
        raise HTTPException(status_code=409, detail="A release is already in progress")

    async with _release_lock:
        from backend.release import create_release_bundle, upload_to_github, upload_to_gitlab

        bundle = await create_release_bundle(
            version=body.version,
            artifact_ids=body.artifact_ids or None,
        )

        result = {
            "bundle": {
                "id": bundle["id"],
                "name": bundle["name"],
                "version": bundle["version"],
                "size": bundle["size"],
                "checksum": bundle["checksum"],
                "download_url": bundle["download_url"],
                "artifact_count": bundle["manifest"]["artifact_count"],
            },
            "uploads": {},
        }

        if body.upload_github:
            gh_result = await upload_to_github(
                bundle["file_path"], bundle["version"], bundle["manifest"],
            )
            result["uploads"]["github"] = gh_result

        if body.upload_gitlab:
            gl_result = await upload_to_gitlab(
                bundle["file_path"], bundle["version"], bundle["manifest"],
            )
            result["uploads"]["gitlab"] = gl_result

        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  System Status (for header)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/debug", dependencies=_REQUIRE_ADMIN)
async def get_debug_state(
    conn=Depends(_get_conn),
):
    """Comprehensive debug state: agent errors, blocked tasks, debug findings."""
    from backend.routers.agents import _agents
    from backend.routers.tasks import _tasks
    from backend.models import AgentStatus, TaskStatus

    agents = list(_agents.values())
    tasks = list(_tasks.values())
    findings = await db.list_debug_findings(conn, limit=50)

    agent_errors = [
        {"id": a.id, "name": a.name, "status": a.status.value, "thought_chain": a.thought_chain[:200]}
        for a in agents if a.status in (AgentStatus.error, AgentStatus.warning, AgentStatus.awaiting_confirmation)
    ]
    blocked_tasks = [
        {"id": t.id, "title": t.title, "status": t.status.value, "assigned_agent_id": t.assigned_agent_id}
        for t in tasks if t.status == TaskStatus.blocked
    ]

    return {
        "timestamp": datetime.now().isoformat(),
        "agent_errors": agent_errors,
        "blocked_tasks": blocked_tasks,
        "total_findings": len(findings),
        "findings_by_type": {
            ft: sum(1 for f in findings if f.get("finding_type") == ft)
            for ft in ("stuck_loop", "error_repeated", "retries_exhausted", "timeout")
        },
        "recent_findings": findings[:20],
    }


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status():
    from backend.routers.agents import _agents
    from backend.routers.tasks import _tasks
    from backend.models import TaskStatus, AgentStatus
    from backend.workspace import list_workspaces
    from backend.container import list_containers

    agents = list(_agents.values())
    tasks = list(_tasks.values())
    kernel = await _sh("uname -r")
    usb_count = (await _sh("lsusb 2>/dev/null | wc -l")).strip()
    mem_raw = await _sh("free -m | awk 'NR==2{printf \"%d/%dMB (%.0f%%)\", $3,$2,$3*100/$2}'")

    cpu_raw = await _sh("grep -c ^processor /proc/cpuinfo 2>/dev/null")
    cpu_cores = cpu_raw.strip() if cpu_raw.strip() else str(os.cpu_count() or 1)

    return {
        "tasks_completed": sum(1 for t in tasks if t.status == TaskStatus.completed),
        "tasks_total": len(tasks),
        "agents_running": sum(1 for a in agents if a.status == AgentStatus.running),
        "wsl_status": "OK" if "microsoft" in (kernel or "").lower() else "N/A",
        "usb_status": f"{usb_count} USB device(s)" if usb_count != "0" else "No USB",
        "cpu_summary": f"{cpu_cores} cores",
        "memory_summary": mem_raw or "N/A",
        "workspaces_active": len(list_workspaces()),
        "containers_active": len(list_containers()),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Spec — reads hardware_manifest.yaml
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _yaml_to_spec(data: dict, parent_type: str = "hardware") -> list[dict]:
    """Convert nested YAML dict to SpecValue[] structure for the frontend."""
    result = []
    for key, val in data.items():
        if isinstance(val, dict):
            result.append({"key": key, "value": _yaml_to_spec(val, parent_type)})
        elif isinstance(val, list):
            if val and isinstance(val[0], dict):
                result.append({"key": key, "value": [
                    {"key": f"{key}[{i}]", "value": _yaml_to_spec(item) if isinstance(item, dict) else str(item)}
                    for i, item in enumerate(val)
                ]})
            else:
                result.append({"key": key, "value": ", ".join(str(v) for v in val)})
        elif isinstance(val, bool):
            result.append({"key": key, "value": val})
        elif isinstance(val, (int, float)):
            result.append({"key": key, "value": val})
        else:
            result.append({"key": key, "value": str(val)})
    return result


@router.get("/sandbox/capacity")
async def get_sandbox_capacity():
    """I6: DRF per-tenant sandbox capacity snapshot."""
    from backend import sandbox_capacity as _sc
    return _sc.snapshot()


@router.get("/sandbox/capacity/{tenant_id}")
async def get_tenant_capacity(tenant_id: str):
    """I6: Per-tenant sandbox capacity usage."""
    from backend import sandbox_capacity as _sc
    return _sc.tenant_usage(tenant_id)


@router.get("/sse-schema")
async def get_sse_schema():
    """A4/C7: return JSON-Schema for every SSE event type keyed by event
    name, so the frontend can detect drift between the hand-maintained
    TS union in lib/api.ts and the backend Pydantic models. Shape matches
    the `get_sse_schema_export()` helper — i.e. flat `{ event_name:
    { description, schema } }` map."""
    from backend.sse_schemas import get_sse_schema_export
    return get_sse_schema_export()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform / arch awareness  (H1 — host vs target indicator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Normalise the many synonyms `uname -m` / cmake / kernel use into the
# canonical short id we display in the UI chip.
_ARCH_ALIASES: dict[str, str] = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm32", "armv7": "arm32", "armhf": "arm32", "arm": "arm32",
    "armv6l": "arm32",
    "riscv64": "riscv64", "rv64": "riscv64",
    "riscv32": "riscv32",
    "mipsel": "mips", "mips": "mips", "mips64": "mips64",
    "i686": "x86", "i386": "x86",
    "ppc64le": "ppc64le", "s390x": "s390x",
    "loongarch64": "loong64",
}


def _canon_arch(raw: str | None) -> str:
    """Canonicalise an arch string. Caps length at 16 chars so a
    malformed source can't push hundreds of characters into the UI
    chip — frontend also truncates to 8 for display."""
    if not raw:
        return "unknown"
    cleaned = raw.lower().strip()[:16]
    return _ARCH_ALIASES.get(cleaned, cleaned)


# Mapping from platform-profile id (configs/platforms/*.yaml) to the
# canonical target arch the toolchain produces.
_PROFILE_ARCH: dict[str, str] = {
    "aarch64": "arm64",
    "armv7":   "arm32",
    "riscv64": "riscv64",
    "host_native": "",        # filled at runtime to match host
    "vendor-example": "arm64",
}


@router.get("/platform-status")
async def get_platform_status() -> dict:
    """H1: surface host arch + active target arch + toolchain readiness so
    the operator chip in the dashboard header can warn about
    cross-compile mismatch *before* a build starts.

    Status values:
      * `no_target`         — hardware_manifest.target_platform is empty
      * `native`            — host arch == target arch (Phase 59 fast-path)
      * `cross_ready`       — different arch + cross-compiler installed
      * `toolchain_missing` — different arch + cross-compiler NOT on PATH
      * `unknown_target`    — target_platform set but no matching profile yaml
    """
    import shutil
    import yaml as _yaml

    host_raw = platform.machine()
    host_canon = _canon_arch(host_raw)

    manifest_path = _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    target_profile_id = ""
    if manifest_path.exists():
        try:
            data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            target_profile_id = (data.get("project") or {}).get("target_platform", "") or ""
        except Exception:
            target_profile_id = ""

    if not target_profile_id:
        return {
            "host":   {"arch": host_canon, "raw": host_raw, "os": platform.system()},
            "target": None,
            "match":  None,
            "status": "no_target",
            "advice": "Set project.target_platform in configs/hardware_manifest.yaml",
        }

    profile_path = _PROJECT_ROOT / "configs" / "platforms" / f"{target_profile_id}.yaml"
    if not profile_path.exists():
        return {
            "host":   {"arch": host_canon, "raw": host_raw, "os": platform.system()},
            "target": {"profile_id": target_profile_id},
            "match":  False,
            "status": "unknown_target",
            "advice": f"No configs/platforms/{target_profile_id}.yaml — create or pick a known profile",
        }

    try:
        prof = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "host":   {"arch": host_canon, "raw": host_raw, "os": platform.system()},
            "target": {"profile_id": target_profile_id},
            "match":  False,
            "status": "unknown_target",
            "advice": f"Cannot parse profile yaml: {exc}",
        }

    target_canon = _PROFILE_ARCH.get(target_profile_id) or _canon_arch(prof.get("platform"))
    if target_profile_id == "host_native":
        target_canon = host_canon

    toolchain = prof.get("toolchain", "")
    qemu = prof.get("qemu", "")
    cross_compile = host_canon != target_canon
    toolchain_present = bool(toolchain) and shutil.which(toolchain) is not None
    qemu_present = bool(qemu) and shutil.which(qemu) is not None

    if not cross_compile:
        status = "native"
        advice = "Same arch — no cross-compile needed (host-native fast path)"
    elif toolchain_present:
        status = "cross_ready"
        advice = f"Cross-compile toolchain `{toolchain}` ready on PATH"
    else:
        status = "toolchain_missing"
        advice = (
            f"Toolchain `{toolchain}` not on PATH. Install it (e.g. "
            f"`apt install gcc-{target_profile_id}-linux-gnu`) before building."
        )

    return {
        "host": {
            "arch": host_canon,
            "raw":  host_raw,
            "os":   platform.system(),
        },
        "target": {
            "profile_id": target_profile_id,
            "arch":       target_canon,
            "label":      prof.get("label") or target_profile_id,
            "toolchain":  toolchain,
            "toolchain_present": toolchain_present,
            "qemu":       qemu,
            "qemu_present": qemu_present,
            "sysroot":    prof.get("sysroot_path") or None,
            "cmake_toolchain_file": prof.get("cmake_toolchain_file") or None,
            "vendor_id":  prof.get("vendor_id") or "",
            "sdk_version": prof.get("sdk_version") or "",
        },
        "match":  not cross_compile,
        "status": status,
        "advice": advice,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Project forecast (Phase 60 v0 prototype)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_FORECAST_CACHE: tuple[float, dict] | None = None
_FORECAST_TTL_S = 300  # 5 min


@router.get("/forecast")
async def get_project_forecast(provider: str | None = None) -> dict:
    """Phase 60: project-level estimates (tasks / agents / hours /
    tokens / USD / confidence) computed from the active
    hardware_manifest.yaml. Template-based v0 — see backend/forecast.py
    for the model evolution roadmap."""
    global _FORECAST_CACHE
    import time as _time
    from backend import forecast as _fc
    now = _time.time()
    if _FORECAST_CACHE and (now - _FORECAST_CACHE[0]) < _FORECAST_TTL_S and provider is None:
        return _FORECAST_CACHE[1]
    f = _fc.from_manifest(provider=provider).to_dict()
    if provider is None:
        _FORECAST_CACHE = (now, f)
    return f


@router.post("/forecast/recompute", dependencies=_REQUIRE_ADMIN)
async def recompute_project_forecast() -> dict:
    """Bust the 5-min cache and re-read the manifest."""
    global _FORECAST_CACHE
    _FORECAST_CACHE = None
    return await get_project_forecast()


@router.get("/spec")
async def get_spec():
    """Load spec from hardware_manifest.yaml (or return empty if not found)."""
    manifest = _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    if not manifest.exists():
        return []
    raw = manifest.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    return _yaml_to_spec(data)


@router.get("/vendor/sdks")
async def list_vendor_sdks():
    """List available vendor SDK platform profiles and their mount status."""
    platforms_dir = _PROJECT_ROOT / "configs" / "platforms"
    if not platforms_dir.is_dir():
        return []
    # W0 #274: skip schema.yaml — it's a schema declaration, not a profile.
    # W1 #275: only embedded profiles carry vendor SDK data; web /
    # mobile / software profiles must not pollute this endpoint or the
    # UI lists them with empty vendor fields and a misleading "ready"
    # status. We dispatch on target_kind (defaulting to embedded for
    # pre-W0 profiles per the W0 backward-compat rule).
    from backend.platform import _NON_PROFILE_FILES, DEFAULT_TARGET_KIND
    results = []
    for f in sorted(platforms_dir.glob("*.yaml")):
        if f.name in _NON_PROFILE_FILES:
            continue
        try:
            data = yaml.safe_load(f.read_text()) or {}
            if (data.get("target_kind") or DEFAULT_TARGET_KIND) != "embedded":
                continue
            vendor_id = data.get("vendor_id", "")
            sysroot = data.get("sysroot_path", "")
            cmake_tc = data.get("cmake_toolchain_file", "")
            results.append({
                "platform": data.get("platform", f.stem),
                "label": data.get("label", f.stem),
                "vendor_id": vendor_id,
                "sdk_version": data.get("sdk_version", ""),
                "soc_model": data.get("soc_model", ""),
                "npu_enabled": data.get("npu_enabled", False),
                "sysroot_mounted": bool(sysroot and Path(sysroot).is_dir()),
                "toolchain_available": bool(cmake_tc and Path(cmake_tc).is_file()),
                "status": "ready" if (not vendor_id) or (sysroot and Path(sysroot).is_dir()) else "not_installed",
            })
        except Exception:
            continue
    return results


@router.put("/spec", dependencies=_REQUIRE_ADMIN)
async def update_spec_field(path: list[str], value: str | int | float | bool):
    """Update a single field in hardware_manifest.yaml."""
    manifest = _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    if not manifest.exists():
        return {"error": "Manifest not found"}
    raw = manifest.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}

    # Navigate to parent
    node = data
    for key in path[:-1]:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return {"error": f"Path not found: {'.'.join(path)}"}
    node[path[-1]] = value

    manifest.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    return {"status": "updated", "path": ".".join(path), "value": value}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repos — real git data from workspaces
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/repos")
async def get_repos():
    """List git repositories: main repo + credential registry + agent worktrees."""
    repos = []
    from backend.git_auth import detect_platform

    # Main repo (shell paths quoted for safety)
    pr = str(_PROJECT_ROOT)
    branch = await _sh(f'git -C "{pr}" rev-parse --abbrev-ref HEAD')
    commit = await _sh(f'git -C "{pr}" log -1 --format="%h" 2>/dev/null')
    commit_time = await _sh(f'git -C "{pr}" log -1 --format="%cr" 2>/dev/null')
    remotes_raw = await _sh(f'git -C "{pr}" remote -v 2>/dev/null')
    remotes: dict[str, str] = {}
    for line in (remotes_raw or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    primary_url = remotes.get("origin", next(iter(remotes.values()), str(_PROJECT_ROOT)))

    repos.append({
        "id": "main-repo",
        "name": _PROJECT_ROOT.name,
        "url": primary_url,
        "branch": branch or "master",
        "status": "synced",
        "lastCommit": commit or "",
        "lastCommitTime": commit_time or "",
        "remotes": remotes,
        "tetheredAgentId": None,
        "platform": detect_platform(primary_url),
        "repoId": "main-repo",
        "authStatus": "ok",
    })

    # Credential registry repos (not yet cloned)
    try:
        from backend.git_credentials import get_credential_registry
        seen_urls = {primary_url.lower()}
        for cred in get_credential_registry():
            cred_url = cred.get("url", "")
            if not cred_url or cred_url.lower() in seen_urls:
                continue
            seen_urls.add(cred_url.lower())
            has_auth = bool(cred.get("token") or cred.get("ssh_key"))
            repos.append({
                "id": cred.get("id", ""),
                "name": cred.get("id", cred_url.split("/")[-1]),
                "url": cred_url,
                "branch": "",
                "status": "unconfigured",
                "lastCommit": "",
                "lastCommitTime": "",
                "tetheredAgentId": None,
                "platform": cred.get("platform", detect_platform(cred_url)),
                "repoId": cred.get("id", ""),
                "authStatus": "ok" if has_auth else "no_token",
            })
    except Exception:
        pass

    # Agent worktrees
    from backend.workspace import list_workspaces
    for ws in list_workspaces():
        wp = str(ws.path)
        ws_branch = await _sh(f'git -C "{wp}" rev-parse --abbrev-ref HEAD 2>/dev/null')
        ws_commit = await _sh(f'git -C "{wp}" log -1 --format="%h" 2>/dev/null')
        ws_time = await _sh(f'git -C "{wp}" log -1 --format="%cr" 2>/dev/null')
        ws_url = ws.repo_source if (ws.repo_source.startswith("http") or ws.repo_source.startswith("git@")) else str(ws.path)
        repos.append({
            "id": f"ws-{ws.agent_id}",
            "name": f"{ws.agent_id} workspace",
            "url": ws_url,
            "branch": ws_branch or ws.branch,
            "status": "synced",
            "lastCommit": ws_commit or "",
            "lastCommitTime": ws_time or "",
            "tetheredAgentId": ws.agent_id,
            "platform": detect_platform(ws_url),
            "repoId": ws.repo_id or "",
            "authStatus": "ok",
        })

    return repos


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Logs — real system log ring buffer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from backend.shared_state import SharedLogBuffer as _SharedLogBuffer
_log_buffer_shared = _SharedLogBuffer("system", maxlen=200)
_log_buffer: deque[dict] = deque(maxlen=200)


def add_system_log(message: str, level: str = "info") -> None:
    """Add a log entry (called from other modules)."""
    entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "level": level,
    }
    _log_buffer.append(entry)
    _log_buffer_shared.append(entry)


def get_recent_logs(limit: int = 50) -> list[dict]:
    """Return recent log entries (most recent first). Used by conversation node."""
    logs = _log_buffer_shared.get_recent(limit)
    if not logs:
        logs = list(_log_buffer)[-limit:]
    return list(reversed(logs))


# Seed with startup log
add_system_log("OmniSight Engine started", "info")
add_system_log(f"Python {platform.python_version()} on {platform.system()}", "info")


@router.get("/logs")
async def get_logs(limit: int = _pg.Limit(default=50, max_cap=500)):
    """Return recent system logs."""
    logs = _log_buffer_shared.get_recent(limit)
    if not logs:
        logs = list(_log_buffer)[-limit:]
    return logs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage tracking (in-memory + SQLite)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from backend.shared_state import SharedTokenUsage as _SharedTokenUsage
from backend.shared_state import SharedFlag as _SharedFlag
from backend.shared_state import SharedHourlyLedger as _SharedHourlyLedger
from backend.shared_state import SharedKV as _SharedKV

_token_usage_shared = _SharedTokenUsage()
_token_usage: dict[str, dict] = {}

_budget_flags = _SharedKV("token_budget")
_token_frozen_shared = _SharedFlag("token_frozen")
_hourly_ledger_shared = _SharedHourlyLedger(window_seconds=3600.0)

# Z.3 (#292) checkbox 2: pricing rates moved to config/llm_pricing.yaml +
# backend/pricing.py::get_pricing. The hard-coded dict that lived here
# pre-Z.3 is preserved verbatim inside `backend.pricing._HARD_CODED_FALLBACK`
# so a missing/corrupt YAML at boot still bills at the historical rates.
# `provider=None` triggers a model-only scan across all provider tables;
# `track_tokens()` callers (LLM callback at backend/agents/llm.py:675 and
# tests) only know the model id, so this preserves the legacy call site
# shape while routing every lookup through the YAML-backed loader.
from backend.pricing import get_pricing as _get_pricing


def track_tokens(model: str, input_tokens: int, output_tokens: int,
                 latency_ms: int, cache_read_tokens: int = 0,
                 cache_create_tokens: int = 0,
                 turn_started_at: str | None = None,
                 turn_ended_at: str | None = None) -> None:
    """Track token usage for a model (called synchronously from LLM callback).

    ZZ.A1 (#303-1): ``cache_read_tokens`` / ``cache_create_tokens``
    are accepted positionally-backward-compat (defaulting to 0) so
    pre-ZZ callers keep working; the LLM callback plumbs real values
    from the provider response. ``cache_hit_ratio`` is derived, not
    accepted — it's always ``cache_read / (input + cache_read)`` on
    the lifetime running totals (source of truth is the dict here;
    SharedTokenUsage recomputes independently from its own lifetime
    totals and the two should match).

    ZZ.A3 (#303-3): ``turn_started_at`` / ``turn_ended_at`` are
    ISO-8601 UTC strings captured in the LLM callback at
    ``on_llm_start`` / ``on_llm_end``. Stored as last-turn snapshots
    (overwrite, not accumulate) so the dashboard can compute per-turn
    LLM compute time (end - start of the same row) and the inter-turn
    gap (this_turn.start - last_turn.end) — the tool + event-bus +
    context-gather wait that falls outside the LLM compute window.
    Callers that didn't capture stamps pass ``None`` (default) and
    the stored field is left untouched, so back-compat tests and
    rule-based code paths don't fabricate wall-clock values.
    """
    _maybe_reset_daily_budget()
    if model not in _token_usage:
        _token_usage[model] = {
            "model": model,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "request_count": 0,
            "avg_latency": 0,
            "last_used": "",
            # ZZ.A1 (#303-1): cache observability fields. Fresh rows
            # start at 0 (not NULL) — a brand-new entry implies at
            # least one ZZ-era track() call, so the counters are
            # authoritative from the first sample.
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
            "cache_hit_ratio": 0.0,
            # ZZ.A3 (#303-3): per-turn boundary stamps start empty on
            # fresh ZZ rows (same convention as ``last_used``) and
            # get populated by the first track() call; legacy rows
            # loaded via load_token_usage_from_db preserve None
            # through _normalize_token_entry so the UI can distinguish
            # "no data" from a real 0ms gap.
            "turn_started_at": "",
            "turn_ended_at": "",
        }
    u = _token_usage[model]
    prev_cost = u["cost"]
    u["input_tokens"] += input_tokens
    u["output_tokens"] += output_tokens
    u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    u["request_count"] += 1
    u["avg_latency"] = int((u["avg_latency"] * (u["request_count"] - 1) + latency_ms) / u["request_count"])
    u["last_used"] = datetime.now().strftime("%H:%M:%S")
    # Z.3 checkbox 7 (#292): bill only THIS call's tokens at the current
    # rate and accumulate onto the prior cost. The previous formulation
    # (`u["cost"] = lifetime_input × current_rate + lifetime_output ×
    # current_rate`) silently re-billed every historical token at the new
    # rate on the first track_tokens() call after a YAML reload — which
    # violates "既有 token usage 紀錄不重算成本，保留當時計費，只影響
    # 未來計價". Under a steady rate the two formulations are
    # algebraically equivalent so this change is price-neutral for
    # unchanged pricing; only price changes between calls diverge, and
    # the new behaviour is the one the spec mandates.
    inp_rate, out_rate = _get_pricing(None, model)
    this_call_cost = round(
        input_tokens / 1_000_000 * inp_rate
        + output_tokens / 1_000_000 * out_rate,
        4,
    )
    u["cost"] = round(prev_cost + this_call_cost, 4)
    cost_delta = this_call_cost
    _record_hourly(cost_delta)

    # ZZ.A1 (#303-1): accumulate cache counters + recompute hit ratio.
    # "Upgrade" any pre-ZZ NULL to 0 on the first ZZ-era write so
    # downstream callers see a consistent shape; after this line the
    # in-memory dict always has numeric cache fields.
    prev_read = u.get("cache_read_tokens") or 0
    prev_create = u.get("cache_create_tokens") or 0
    u["cache_read_tokens"] = prev_read + int(cache_read_tokens)
    u["cache_create_tokens"] = prev_create + int(cache_create_tokens)
    denom = u["input_tokens"] + u["cache_read_tokens"]
    u["cache_hit_ratio"] = (
        round(u["cache_read_tokens"] / denom, 6) if denom > 0 else 0.0
    )

    # ZZ.A3 (#303-3): overwrite (not accumulate) per-turn boundaries.
    # When the caller omits stamps (e.g. rule-based fallback path
    # inside the graph, or a test fixture calling track_tokens
    # directly) we leave the stored value alone so a previously
    # populated last-turn snapshot isn't clobbered by a "no data"
    # write. The legacy NULL → string upgrade happens implicitly —
    # once a ZZ-era caller provides stamps the field becomes
    # authoritative.
    if turn_started_at is not None:
        u["turn_started_at"] = turn_started_at
    if turn_ended_at is not None:
        u["turn_ended_at"] = turn_ended_at

    # I10: track in shared state for cross-worker visibility
    _token_usage_shared.track(
        model, input_tokens, output_tokens, latency_ms, cost_delta,
        cache_read_tokens=cache_read_tokens,
        cache_create_tokens=cache_create_tokens,
        turn_started_at=turn_started_at,
        turn_ended_at=turn_ended_at,
    )
    if cost_delta > 0:
        _hourly_ledger_shared.record(cost_delta)

    # Persist asynchronously (fire-and-forget) + check budget
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_token_usage(u.copy()))
        loop.create_task(_safe_check_budget())
        # Y9 #285 row 3 — per-(tenant_id, project_id) billing fan-out.
        # Reads request-scope ContextVars (set by ``require_tenant`` /
        # ``require_project_member``) so the billing event picks up
        # the active tenant + project automatically; falls through to
        # the deterministic ``(t-default, p-default-default)`` bucket
        # for system-issued / cron LLM calls without a request scope
        # (Y9 row 5 acceptance criterion: "走 'default' project 歸因").
        # Best-effort — a billing-emit failure must never regress the
        # LLM call that triggered it; ``billing_usage._write_event``
        # logs and swallows.  ``loop.create_task`` snapshots the current
        # contextvars so the fan-out runs against the same tenant /
        # project tuple even if the parent task tears down before the
        # task is scheduled.
        try:
            from backend import billing_usage as _billing
            loop.create_task(_billing.record_llm_call(
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cost_usd=float(this_call_cost),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_create_tokens=int(cache_create_tokens or 0),
            ))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug("billing_usage.record_llm_call schedule failed: %s", exc)
    except RuntimeError:
        pass  # No event loop — skip persistence (e.g. during tests)


async def _persist_token_usage(data: dict) -> None:
    # SP-3.5 (2026-04-20): fire-and-forget from track_tokens's
    # ``loop.create_task`` — no request conn in scope. Borrow one
    # from the pool just for this single upsert. DB failures stay
    # non-fatal (a missed persist causes memory/DB drift, recovered
    # on next cold start via load_token_usage_from_db).
    from backend.db_pool import get_pool
    try:
        async with get_pool().acquire() as _conn:
            await db.upsert_token_usage(_conn, data)
    except Exception as exc:
        logger.warning("Token usage DB persist failed: %s", exc)


# ── Token budget enforcement ──

token_frozen: bool = False
_last_budget_level: str = ""  # Track to avoid repeat events
_token_daily_reset_date: str = ""

# L1-06: rolling hourly spend ledger. Each entry is (timestamp_s, cost_usd).
_hourly_ledger: list[tuple[float, float]] = []
_HOURLY_WINDOW_S = 3600


def is_token_frozen() -> bool:
    """Check if token usage is frozen (shared across workers)."""
    return token_frozen or _token_frozen_shared.get()


def _maybe_reset_daily_budget() -> None:
    """Auto-reset token freeze at midnight (new day)."""
    global token_frozen, _last_budget_level, _token_daily_reset_date
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    if today != _token_daily_reset_date:
        _token_daily_reset_date = today
        if token_frozen or _token_frozen_shared.get():
            token_frozen = False
            _token_frozen_shared.set(False)
            _last_budget_level = "normal"
            _budget_flags.set("level", "normal")
            from backend.events import emit_token_warning
            emit_token_warning("reset", "Daily token budget auto-reset")
            logger.info("Daily token budget auto-reset")


def get_daily_cost() -> float:
    """Sum all model costs for the current session."""
    shared_cost = _token_usage_shared.total_cost()
    if shared_cost > 0:
        return round(shared_cost, 4)
    return round(sum(u.get("cost", 0) for u in _token_usage.values()), 4)


def _record_hourly(cost_delta: float) -> None:
    """Append a cost sample and prune anything older than the window.
    Called from track_tokens when a call actually incremented spend."""
    import time as _time
    if cost_delta <= 0:
        return
    now = _time.time()
    _hourly_ledger.append((now, cost_delta))
    cutoff = now - _HOURLY_WINDOW_S
    while _hourly_ledger and _hourly_ledger[0][0] < cutoff:
        _hourly_ledger.pop(0)


def get_hourly_cost() -> float:
    """Sum cost deltas recorded in the last 60 minutes."""
    shared_cost = _hourly_ledger_shared.total_in_window()
    if shared_cost > 0:
        return round(shared_cost, 4)
    import time as _time
    cutoff = _time.time() - _HOURLY_WINDOW_S
    return round(
        sum(c for (t, c) in _hourly_ledger if t >= cutoff),
        4,
    )


async def _safe_check_budget() -> None:
    try:
        await asyncio.wait_for(_check_token_budget(), timeout=2.0)
    except Exception as exc:
        logger.warning("Token budget check failed: %s", exc)


async def _check_token_budget() -> None:
    """Check daily + hourly cost thresholds. Called after each
    track_tokens. The hourly guard fires independent of the daily
    one — a burst that won't hit the daily cap today can still blow
    a month's budget in a few hours."""
    global token_frozen, _last_budget_level
    from backend.config import settings
    from backend.events import emit_token_warning
    from backend.notifications import notify

    # ── Hourly burn-rate guard (L1-06) ──
    hourly_cap = settings.token_budget_hourly
    if hourly_cap > 0:
        hourly = get_hourly_cost()
        if hourly >= hourly_cap and _last_budget_level != "frozen":
            token_frozen = True
            _token_frozen_shared.set(True)
            _last_budget_level = "hourly_frozen"
            _budget_flags.set("level", "hourly_frozen")
            msg = (
                f"Hourly burn-rate cap exceeded "
                f"(${hourly:.4f} in last 60 min / ${hourly_cap:.2f}). "
                "All LLM calls frozen. Will auto-reset at top of next "
                "hour as old spend ages out of the window."
            )
            emit_token_warning("frozen", msg, hourly, hourly_cap)
            await notify(
                "critical", "LLM hourly burn-rate cap — frozen",
                message=msg, source="token_budget",
            )
            return  # Don't also trip daily warnings this call.

    budget = settings.token_budget_daily
    if budget <= 0:
        return  # Unlimited

    cost = get_daily_cost()
    ratio = cost / budget

    if ratio >= settings.token_freeze_threshold and _last_budget_level != "frozen":
        token_frozen = True
        _token_frozen_shared.set(True)
        _last_budget_level = "frozen"
        _budget_flags.set("level", "frozen")
        emit_token_warning("frozen", f"Token budget exhausted (${cost:.4f}/${budget:.2f}). All LLM calls frozen.", cost, budget)
        await notify("critical", "Token budget exhausted — LLM frozen",
                      message=f"Daily cost ${cost:.4f} exceeded budget ${budget:.2f}. All LLM calls disabled.",
                      source="token_budget")

    elif ratio >= settings.token_downgrade_threshold and _last_budget_level not in ("downgrade", "frozen"):
        _last_budget_level = "downgrade"
        try:
            from backend.agents.llm import _cache
            settings.llm_provider = settings.token_fallback_provider
            settings.llm_model = settings.token_fallback_model
            _cache.clear()
        except Exception:
            pass
        emit_token_warning("downgrade", f"Token budget at {ratio:.0%} (${cost:.4f}/${budget:.2f}). Auto-downgraded to {settings.token_fallback_provider}.", cost, budget)
        await notify("action", f"Token budget at {ratio:.0%} — auto-downgraded",
                      message=f"Switched to {settings.token_fallback_provider}. Cost: ${cost:.4f}/${budget:.2f}.",
                      source="token_budget")

    elif ratio >= settings.token_warn_threshold and _last_budget_level not in ("warn", "downgrade", "frozen"):
        _last_budget_level = "warn"
        emit_token_warning("warn", f"Token budget at {ratio:.0%} (${cost:.4f}/${budget:.2f}).", cost, budget)
        await notify("warning", f"Token budget at {ratio:.0%}",
                      message=f"Daily cost: ${cost:.4f} / ${budget:.2f}.",
                      source="token_budget")


async def load_token_usage_from_db(conn) -> None:
    """Load persisted token data into memory (called at startup).

    SP-3.5 (2026-04-20): takes an explicit ``asyncpg.Connection`` —
    mirrors the ``seed_defaults_if_empty(conn)`` shape for agents /
    tasks. Lifespan acquires from the pool and passes conn here.
    """
    for row in await db.list_token_usage(conn):
        _token_usage[row["model"]] = row
    if _token_usage:
        _token_usage_shared.set_all(_token_usage)


@router.get("/tokens", response_model=list[TokenUsageEntry])
async def get_token_usage():
    """Return token usage stats per model.

    ZZ.A1 (#303-2, 2026-04-24): response schema formalised as
    ``list[TokenUsageEntry]`` so the openapi.json advertises the
    prompt-cache fields (``cache_read_tokens`` / ``cache_create_tokens``
    / ``cache_hit_ratio``) introduced in ZZ.A1-1 / ZZ.A1-2. Values are
    ``None`` on pre-ZZ legacy rows (distinguishes "no data" from zero
    hits) and populated on ZZ-era rows — see ``TokenUsageEntry`` and
    ``backend/shared_state.py::_normalize_token_entry``.
    """
    shared = _token_usage_shared.get_all()
    if shared:
        return list(shared.values())
    return list(_token_usage.values())


# ZZ.B3 #304-3 checkbox 1 (2026-04-24): burn-rate time series.
# Window keyword → seconds. 60 s bucket width is fixed per the row spec
# (``bucket 60 秒``); if a future checkbox widens 24 h to 5-min buckets
# the map changes shape and ``bucket_seconds`` in the response tells
# clients the new width.
_BURN_RATE_WINDOWS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
}
_BURN_RATE_BUCKET_SECONDS = 60


@router.get("/tokens/burn-rate", response_model=TokenBurnRateResponse)
async def get_token_burn_rate(
    window: str = "1h",
    conn=Depends(_get_conn),
):
    """Return a 60-second-bucketed token + cost time series.

    ZZ.B3 #304-3 checkbox 1: feeds the dashboard sparkline + per-hour
    burn-rate badge next to ``TokenUsageStats`` Row 1 (checkbox 2) and
    the daily-budget extrapolation toast (checkbox 3).

    **Source**: The TODO row's spec phrases the source as "aggregate
    ``token_usage`` 表的 ``created_at``", but ``token_usage`` is an
    UPSERTed per-model aggregate — it has no ``created_at`` column and
    can't be bucketed over time. The only authoritative per-turn
    time-series source is ``event_log`` rows with ``event_type =
    'turn.complete'`` (persisted via ``_PERSIST_EVENT_TYPES``; see
    ``emit_turn_complete`` for payload shape). Each row carries
    ``tokens_used`` and ``cost_usd`` in ``data_json`` and the column
    ``created_at`` (TEXT, ``YYYY-MM-DD HH24:MI:SS`` format — set by
    ``alembic_pg_compat``'s ``datetime('now')`` rewrite to ``to_char(
    now(), …)``) is the authoritative bucket key.

    **Rate normalisation**: ``tokens_per_hour`` = ``SUM(bucket_tokens) /
    60 * 3600`` = ``SUM(bucket_tokens) * 60`` for the fixed 60 s bucket
    width. Same shape for ``cost_per_hour``. This lets the UI render a
    single y-axis regardless of which window the operator picks.

    **NULL-vs-genuine-zero contract**: ``cost_usd`` on a ``turn.complete``
    payload is ``null`` for unknown models (see ``_estimate_turn_cost_usd``
    — preserves the frontend ``$—`` rendering). ``COALESCE(…::numeric, 0)``
    maps that to zero in the aggregate so a single unknown-model turn
    doesn't drop the whole bucket's cost. Tokens are always authoritative
    integers so no NULL path is needed there.

    **Tenant isolation**: ``tenant_where_pg`` narrows to the caller's
    tenant so dashboard readings don't leak cross-tenant spend.

    Module-global audit (SOP Step 1): ``_BURN_RATE_WINDOWS`` and
    ``_BURN_RATE_BUCKET_SECONDS`` are module-const literals — each
    uvicorn worker derives the same dict/int from the same source
    code, matching SOP acceptable answer #1 ("不共享,因為每 worker
    從同樣來源推導出同樣的值").
    """
    from backend.db_context import tenant_where_pg

    window_seconds = _BURN_RATE_WINDOWS.get(window)
    if window_seconds is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported window '{window}'. "
                f"Expected one of: {sorted(_BURN_RATE_WINDOWS.keys())}"
            ),
        )

    # Caller-side tenant filter: ``event_log`` has ``tenant_id`` and
    # the ported helper returns a ``tenant_id = $N`` clause when a
    # tenant is active in the context. Placeholders are allocated in
    # the order they're appended — we reserve $1 for window_seconds
    # BEFORE calling tenant_where_pg so the indices stay stable
    # regardless of whether a tenant is set.
    conditions: list[str] = ["event_type = 'turn.complete'"]
    params: list = [window_seconds]
    conditions.append(
        "to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS') "
        ">= NOW() - make_interval(secs => $1)"
    )
    tenant_where_pg(conditions, params)

    where_sql = " AND ".join(conditions)
    sql = f"""
        WITH buckets AS (
          SELECT
            date_trunc(
              'minute',
              to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS')
            ) AS bucket_ts,
            COALESCE((data_json::jsonb->>'tokens_used')::bigint, 0) AS tokens,
            COALESCE((data_json::jsonb->>'cost_usd')::numeric, 0) AS cost
          FROM event_log
          WHERE {where_sql}
        )
        SELECT
          to_char(bucket_ts, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS ts,
          SUM(tokens) AS bucket_tokens,
          SUM(cost) AS bucket_cost
        FROM buckets
        GROUP BY bucket_ts
        ORDER BY bucket_ts ASC
    """

    rows = await conn.fetch(sql, *params)

    points: list[dict] = []
    for row in rows:
        bucket_tokens = int(row["bucket_tokens"] or 0)
        bucket_cost = float(row["bucket_cost"] or 0.0)
        # 60 s bucket → multiply by 60 to project to tokens/hour.
        points.append({
            "timestamp": row["ts"],
            "tokens_per_hour": bucket_tokens * (3600 // _BURN_RATE_BUCKET_SECONDS),
            "cost_per_hour": round(
                bucket_cost * (3600 / _BURN_RATE_BUCKET_SECONDS),
                6,
            ),
        })

    return {
        "window": window,
        "bucket_seconds": _BURN_RATE_BUCKET_SECONDS,
        "points": points,
    }


# ZZ.C2 #305-2 checkbox 1 (2026-04-24): session-heatmap windows.
# ``7d`` and ``30d`` are the two operator-facing views locked by the
# TODO spec; the matrix is ``N × 24`` (N = 7 or 30). Adding a third
# window (e.g. ``90d``) is a dict entry here plus a frontend
# grid-height branch — the endpoint shape doesn't need to change.
_HEATMAP_WINDOWS: dict[str, int] = {
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}

# ZZ.C2 #305-2 checkbox 4 (2026-04-24): per-model filter slug fence.
# Model names are sent as a query param and interpolated as ``$N``
# into parameterised SQL — parameterisation already neutralises SQL
# injection, but we still reject obviously-malformed input (shell
# metachars, spaces, empty string) so the endpoint returns a clean
# 400 instead of passing garbage through to ``event_log`` where it
# would always miss. Matches the slug shape Anthropic / OpenAI /
# Google emit (``claude-opus-4-7``, ``gpt-4o``, ``gemini-2.5-pro``).
import re as _re  # noqa: E402 — local alias keeps other imports stable
_HEATMAP_MODEL_RE = _re.compile(r"^[A-Za-z0-9_.\-]+$")
_HEATMAP_MODEL_MAX_LEN = 120


@router.get("/tokens/heatmap", response_model=TokenHeatmapResponse)
async def get_token_heatmap(
    window: str = "7d",
    model: str | None = None,
    conn=Depends(_get_conn),
):
    """Return a ``(day, hour)``-bucketed token + cost matrix.

    ZZ.C2 #305-2 checkbox 1: feeds the Calendar-style heatmap
    beneath ``<TokenUsageStats>`` (checkbox 2) — x-axis is hour-of-
    day, y-axis is calendar date, cell shade scales with
    ``token_total``.

    **Source**: ``event_log`` rows with ``event_type='turn.complete'``
    — the same authoritative per-turn time series that
    ``/runtime/tokens/burn-rate`` consumes. Each row carries
    ``tokens_used`` + ``cost_usd`` in ``data_json`` and the TEXT
    ``created_at`` column (``YYYY-MM-DD HH24:MI:SS``) is the bucket
    key, parsed via ``to_timestamp(created_at, …)`` to align with
    PG's session timezone.

    **Bucket keys**: ``day`` is ``to_char(…, 'YYYY-MM-DD')`` in UTC
    (we call ``to_timestamp(…) AT TIME ZONE 'UTC'`` to pin the
    bucket boundary independently of the PG session timezone) and
    ``hour`` is ``EXTRACT(HOUR FROM …)::int`` in the same UTC
    frame. Operators see local time because the frontend (checkbox
    2) shifts the grid by the browser offset at render time — the
    backend is authoritative UTC so two operators in different
    regions see the same cells just painted at different grid
    positions.

    **Sparse payload**: the ``GROUP BY`` emits only ``(day, hour)``
    pairs that actually had at least one ``turn.complete`` row.
    The frontend treats a missing cell as genuine zero activity;
    this keeps the payload bounded by real traffic rather than
    always paying 168 (7 × 24) or 720 (30 × 24) cells.

    **NULL-vs-genuine-zero contract**: mirrors burn-rate —
    ``COALESCE((data_json::jsonb->>'cost_usd')::numeric, 0)`` maps
    unknown-model ``null`` to 0 so the bucket's tokens still
    contribute even if one row had no pricing coverage.

    **Tenant isolation**: ``tenant_where_pg`` narrows to the
    caller's tenant so one tenant's nightly-batch burst doesn't
    light up a neighbour's heatmap.

    **Per-model filter** (ZZ.C2 checkbox 4, 2026-04-24): optional
    ``model`` query param restricts cells to rows whose
    ``data_json->>'model'`` matches the slug exactly. ``None`` /
    empty string means "all models" (backward-compatible with
    checkbox 1/2/3 callers who never pass the param). The response
    always carries ``available_models`` — the distinct model slugs
    observed across the *unfiltered* window + tenant + event-type
    fence — so the frontend dropdown can render every choice even
    after a filter is applied.

    Module-global audit (SOP Step 1): ``_HEATMAP_WINDOWS`` is a
    module-const literal dict and ``_HEATMAP_MODEL_RE`` +
    ``_HEATMAP_MODEL_MAX_LEN`` are module-level literals — every
    uvicorn worker derives the same values from the same source
    code, matching SOP acceptable answer #1 ("不共享,因為每
    worker 從同樣來源推導出同樣的值"). The endpoint handler is
    pure-read request-scoped — no caches, queues, or counters are
    mutated.

    Read-after-write timing audit (SOP Step 1): pure-read path
    over ``event_log``; writers are ``emit_turn_complete`` via
    ``_PERSIST_EVENT_TYPES`` which commit on each event. Heatmap
    reads are eventually-consistent vs. in-flight turns — the
    dashboard refreshes on SSE events anyway, so a ~second lag
    between ``turn.complete`` emit and GET-heatmap visibility is
    invisible to the operator.
    """
    from backend.db_context import tenant_where_pg

    window_seconds = _HEATMAP_WINDOWS.get(window)
    if window_seconds is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported window '{window}'. "
                f"Expected one of: {sorted(_HEATMAP_WINDOWS.keys())}"
            ),
        )

    # ZZ.C2 #305-2 checkbox 4 (2026-04-24): per-model filter.
    # ``model`` is optional; ``None`` / empty means "all models"
    # (backward-compatible with checkbox 1/2/3 callers). When
    # provided, the slug must match ``_HEATMAP_MODEL_RE`` to reject
    # shell metachars and control whitespace early — the real SQL
    # injection defence is the parameterised query below, but
    # pre-filtering garbage gives the frontend a clean 400 instead
    # of a silently-empty result set from a bogus slug that never
    # matches any row.
    model_filter: str | None = None
    if model is not None:
        stripped = model.strip()
        if stripped != "":
            if (
                len(stripped) > _HEATMAP_MODEL_MAX_LEN
                or not _HEATMAP_MODEL_RE.match(stripped)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid model slug '{model}'. "
                        "Expected alphanumeric + '-_.' only, "
                        f"≤{_HEATMAP_MODEL_MAX_LEN} chars."
                    ),
                )
            model_filter = stripped

    # Reserve $1 for window_seconds BEFORE calling tenant_where_pg so
    # placeholder indices stay stable regardless of whether a tenant
    # is set — same ordering discipline as burn-rate.
    base_conditions: list[str] = ["event_type = 'turn.complete'"]
    base_params: list = [window_seconds]
    base_conditions.append(
        "to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS') "
        ">= NOW() - make_interval(secs => $1)"
    )
    tenant_where_pg(base_conditions, base_params)

    # available_models: distinct model slugs under the SAME window +
    # tenant + event-type fence BUT without the model filter applied,
    # so the frontend dropdown always shows every option — otherwise
    # selecting ``claude-opus-4-7`` would hide every other choice the
    # next time the drawer mounts.
    models_where_sql = " AND ".join(base_conditions)
    models_sql = f"""
        SELECT DISTINCT data_json::jsonb->>'model' AS model
        FROM event_log
        WHERE {models_where_sql}
          AND data_json::jsonb->>'model' IS NOT NULL
          AND (data_json::jsonb->>'model') <> ''
        ORDER BY model ASC
    """
    available_rows = await conn.fetch(models_sql, *base_params)
    available_models = [r["model"] for r in available_rows if r["model"]]

    # Cells query: copies base_conditions + base_params and tacks on
    # the optional model filter. Keeping ``base_*`` immutable means
    # the available_models query above stays invariant regardless of
    # the caller's filter choice.
    cell_conditions = list(base_conditions)
    cell_params = list(base_params)
    if model_filter is not None:
        cell_params.append(model_filter)
        cell_conditions.append(
            f"data_json::jsonb->>'model' = ${len(cell_params)}"
        )
    where_sql = " AND ".join(cell_conditions)

    # UTC-anchored bucket keys: ``AT TIME ZONE 'UTC'`` pins the
    # date/hour boundary regardless of the PG session timezone so
    # two replicas in different regions produce identical cells.
    sql = f"""
        WITH buckets AS (
          SELECT
            to_char(
              to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS')
                AT TIME ZONE 'UTC',
              'YYYY-MM-DD'
            ) AS day,
            EXTRACT(
              HOUR FROM
                to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS')
                  AT TIME ZONE 'UTC'
            )::int AS hour,
            COALESCE((data_json::jsonb->>'tokens_used')::bigint, 0) AS tokens,
            COALESCE((data_json::jsonb->>'cost_usd')::numeric, 0) AS cost
          FROM event_log
          WHERE {where_sql}
        )
        SELECT
          day,
          hour,
          SUM(tokens) AS token_total,
          SUM(cost) AS cost_total
        FROM buckets
        GROUP BY day, hour
        ORDER BY day ASC, hour ASC
    """

    rows = await conn.fetch(sql, *cell_params)

    cells: list[dict] = []
    for row in rows:
        cells.append({
            "day": row["day"],
            "hour": int(row["hour"]),
            "token_total": int(row["token_total"] or 0),
            "cost_total": round(float(row["cost_total"] or 0.0), 6),
        })

    return {
        "window": window,
        "cells": cells,
        "available_models": available_models,
        "model": model_filter,
    }


@router.get("/turns")
async def get_turn_history(
    limit: int = 50,
    session_id: str | None = None,
    conn=Depends(_get_conn),
):
    """Return the most recent ``turn.complete`` records, newest first.

    ZZ.B1 #304-1 checkbox 3 (2026-04-24): frontends mount
    ``<TurnTimeline>`` with an empty ring buffer and depend on live
    ``turn.complete`` SSE events to populate it. When a session
    reconnects (page reload, worker restart, operator switching tabs),
    the SSE stream carries only *future* events — history is lost.
    This endpoint backfills by reading persisted rows from
    ``event_log`` (see ``_PERSIST_EVENT_TYPES`` for the allow-list)
    and handing the frontend the last ``limit`` turns (capped to 100
    to match the ring-buffer size).

    ``session_id`` filters the result in-memory on the payload's
    ``_session_id`` field — a full jsonb index would be premature;
    turn volume is low (< a few hundred per session) and the natural
    ORDER BY id DESC LIMIT N already caps the scan.
    """
    import json as _json

    # Clamp to the ring-buffer size so a misconfigured client can't
    # request a huge batch. Minimum 1 so a pathological ``limit=0``
    # doesn't silently return an empty list that looks like no history.
    if limit <= 0:
        limit = 1
    limit = min(limit, 100)

    # Over-fetch when filtering by session_id so the post-filter
    # result still has a reasonable chance of reaching ``limit``.
    fetch_limit = limit * 5 if session_id else limit

    rows = await db.list_events(
        conn,
        event_types=["turn.complete"],
        limit=fetch_limit,
    )
    turns: list[dict] = []
    for row in rows:
        try:
            payload = _json.loads(row.get("data_json") or "{}")
        except (ValueError, TypeError):
            continue
        if session_id and payload.get("_session_id", "") != session_id:
            continue
        turns.append(payload)
        if len(turns) >= limit:
            break
    return {"turns": turns, "count": len(turns)}


@router.get("/compression")
async def get_compression_stats():
    """Return RTK output compression statistics."""
    from backend.output_compressor import get_compression_stats as _get_stats
    stats = _get_stats()
    # Estimate token savings (rough: 1 token ≈ 4 bytes)
    stats["estimated_tokens_saved"] = stats.get("total_original_bytes", 0) - stats.get("total_compressed_bytes", 0)
    stats["estimated_tokens_saved"] //= 4
    return stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ZZ.C1 #305-1 checkbox 1 — prompt-version timeline + diff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Clamp for ``GET /runtime/prompts?limit=…``. The row spec names 20 as
# the default; we hard-cap at 200 so a misconfigured drawer can't pull
# a year of archive rows onto a single page render.
_PROMPT_LIMIT_DEFAULT = 20
_PROMPT_LIMIT_MAX = 200

# Fence for the ``agent_type`` query param. The shipped
# ``prompt_versions.path`` values are all
# ``backend/agents/prompts/<slug>.md`` (see
# ``backend/prompt_registry.py::_normalise_path``). Rejecting anything
# outside ``[a-z0-9_-]+`` before it reaches the DB means a crafted
# ``agent_type=../../etc/passwd`` can't sneak through even though the
# column is the authoritative store — defence-in-depth.
_AGENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _path_for_agent_type(agent_type: str) -> str:
    """Resolve the ``agent_type`` query param to the canonical
    ``prompt_versions.path`` value.

    The shipped registry writes rows with
    ``backend/agents/prompts/<agent_type>.md`` — we recompose the same
    string here so the WHERE clause is an exact match (no LIKE, no
    regex). Raises :class:`fastapi.HTTPException(400)` on a malformed
    slug.
    """
    if not agent_type or not _AGENT_TYPE_RE.match(agent_type):
        raise HTTPException(
            status_code=400,
            detail=(
                "agent_type must match [A-Za-z0-9_-]+ (got "
                f"{agent_type!r}); e.g. 'orchestrator', 'firmware'."
            ),
        )
    return f"backend/agents/prompts/{agent_type}.md"


def _agent_type_from_path(path: str) -> str:
    """Inverse of :func:`_path_for_agent_type` — strip prefix + ``.md``.

    Resilient to ``prompt_versions`` rows that might eventually live
    outside ``backend/agents/prompts/`` (falls back to the file stem).
    """
    stem = path.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return stem


def _preview(body: str) -> str:
    """First two non-empty lines of ``body``, joined with ``\\n``.

    Row spec: "timeline list 每版顯示 created_at + hash prefix + content
    頭兩行". Keeps the preview bounded (~500 chars) so a 40 KB prompt
    doesn't inflate the list response."""
    lines: list[str] = []
    for raw in body.splitlines():
        stripped = raw.rstrip()
        if stripped:
            lines.append(stripped)
        if len(lines) == 2:
            break
    preview = "\n".join(lines)
    return preview[:500] + ("…" if len(preview) > 500 else "")


def _format_prompt_created_at(raw) -> str:
    """Normalise the ``prompt_versions.created_at`` storage format to an
    ISO-8601 UTC string for the JSON response.

    The column is ``REAL`` (epoch seconds) in the shipped schema — see
    ``0016_pg_schema_sync.py``. Returning epoch numerically would force
    every client to know the Unix-epoch convention; emitting ``…Z`` is
    immediately human-readable in the drawer and matches the shape of
    ``/runtime/tokens/burn-rate``'s ``timestamp`` field.
    """
    if raw is None:
        return ""
    try:
        from datetime import timezone as _tz
        return datetime.fromtimestamp(float(raw), tz=_tz.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        # Future-proof: if a migration moves the column to TEXT, pass
        # the value through verbatim rather than raising.
        return str(raw)


@router.get("/prompts", response_model=PromptVersionsListResponse)
async def list_prompt_versions(
    agent_type: str,
    limit: int = _PROMPT_LIMIT_DEFAULT,
    conn=Depends(_get_conn),
):
    """ZZ.C1 #305-1: return the prompt-version timeline for one agent.

    Deduped by ``content_hash`` (``body_sha256``) — if the same body was
    re-registered across multiple ``version`` rows (e.g. an
    ``active → archive → active`` flap that re-emits the same content),
    only the most recent copy shows in the list. The ``supersedes_id``
    field on each entry points at the id of the next-older distinct-
    hash row, so the drawer can anchor "v7 replaced v5 at HH:MM" lines
    without a second request.

    Module-global audit (SOP Step 1): ``_PROMPT_LIMIT_DEFAULT`` /
    ``_PROMPT_LIMIT_MAX`` / ``_AGENT_TYPE_RE`` are module-const literals
    — every uvicorn worker derives the same values from source (SOP
    acceptable answer #1).
    """
    path = _path_for_agent_type(agent_type)

    # Clamp the limit. Negative / zero caps at 1 so the frontend drawer
    # always gets at least the head version if any row exists; oversize
    # values fall back to the hard cap. We deliberately avoid ``limit
    # or default`` here because Python treats 0 as falsy — a client
    # asking for limit=0 must clamp to 1, not silently jump to 20.
    effective_limit = max(1, min(int(limit), _PROMPT_LIMIT_MAX))

    # Fetch more than the target so dedupe-by-hash can still return up
    # to ``effective_limit`` distinct hashes in the pathological case
    # where every other row collides. 4× is plenty in practice — a
    # prompt file that flaps 4× on average per ship would itself be a
    # finding — and hard-capped at ``_PROMPT_LIMIT_MAX × 4`` so we
    # never scan the entire archive even on a crafted query.
    overfetch = min(effective_limit * 4, _PROMPT_LIMIT_MAX * 4)

    rows = await conn.fetch(
        """
        SELECT id, path, version, role, body, body_sha256,
               created_at, promoted_at, rolled_back_at, rollback_reason
        FROM prompt_versions
        WHERE path = $1
        ORDER BY version DESC
        LIMIT $2
        """,
        path,
        overfetch,
    )

    # Dedupe by hash while preserving the newest-first order. Since
    # rows are ORDER BY version DESC, the first occurrence of a hash
    # IS the most recent copy.
    seen_hashes: set[str] = set()
    entries: list[PromptVersionEntry] = []
    for row in rows:
        h = row["body_sha256"]
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        entries.append(PromptVersionEntry(
            id=int(row["id"]),
            agent_type=agent_type,
            content_hash=h,
            content=row["body"] or "",
            content_preview=_preview(row["body"] or ""),
            created_at=_format_prompt_created_at(row["created_at"]),
            supersedes_id=None,  # filled in below
            version=int(row["version"]),
            role=row["role"] or "",
        ))
        if len(entries) >= effective_limit:
            break

    # Populate supersedes_id: each entry (newer) supersedes the next
    # entry in the deduped list (older). Bottom of the timeline → None.
    for i, entry in enumerate(entries):
        if i + 1 < len(entries):
            entry.supersedes_id = entries[i + 1].id

    return PromptVersionsListResponse(
        agent_type=agent_type,
        path=path,
        limit=effective_limit,
        versions=entries,
    )


@router.get("/prompts/diff", response_model=PromptDiffResponse)
async def get_prompt_diff(
    from_: int = Query(..., alias="from", ge=1),
    to: int = Query(..., ge=1),
    conn=Depends(_get_conn),
):
    """ZZ.C1 #305-1: unified diff between two prompt_versions rows.

    Row spec: "回 unified diff text". We return a Pydantic envelope so
    the same endpoint can carry both sides' metadata (hash prefix,
    version, created_at) without forcing the drawer to fetch the list
    again — the ``diff`` field itself is the verbatim ``difflib.
    unified_diff`` output (context=3 lines, matching git's default +
    the "unfold context 預設 3 行" line in the row spec).

    Both ids must resolve to rows sharing the same ``path``; a
    cross-agent diff is meaningless and rejected with 400 so a
    misconfigured drawer fails loudly instead of rendering a giant
    add-everything/remove-everything hunk. Missing ids → 404.
    """
    import difflib

    # Sequential fetchrows on the same connection: asyncpg forbids
    # concurrent operations on a single connection (serialised via
    # `_stmt_exclusive_section`) so ``asyncio.gather`` would raise
    # ``InterfaceError: another operation is in progress``.
    row_from = await conn.fetchrow(
        "SELECT id, path, version, body, body_sha256, created_at "
        "FROM prompt_versions WHERE id = $1",
        from_,
    )
    row_to = await conn.fetchrow(
        "SELECT id, path, version, body, body_sha256, created_at "
        "FROM prompt_versions WHERE id = $1",
        to,
    )
    if row_from is None:
        raise HTTPException(status_code=404, detail=f"no prompt_versions id={from_}")
    if row_to is None:
        raise HTTPException(status_code=404, detail=f"no prompt_versions id={to}")
    if row_from["path"] != row_to["path"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"cross-agent diff rejected: from.path={row_from['path']!r} "
                f"vs to.path={row_to['path']!r}"
            ),
        )

    agent_type = _agent_type_from_path(row_from["path"])
    from_label = f"{agent_type}@v{row_from['version']}"
    to_label = f"{agent_type}@v{row_to['version']}"

    # ``splitlines(keepends=True)`` keeps trailing newlines so the diff
    # includes "\\ No newline at end of file" markers when present —
    # matches `git diff` behaviour operators expect.
    diff_lines = difflib.unified_diff(
        (row_from["body"] or "").splitlines(keepends=True),
        (row_to["body"] or "").splitlines(keepends=True),
        fromfile=from_label,
        tofile=to_label,
        n=3,
    )
    diff_text = "".join(diff_lines)

    return PromptDiffResponse(
        from_id=int(row_from["id"]),
        to_id=int(row_to["id"]),
        agent_type=agent_type,
        from_hash=row_from["body_sha256"],
        to_hash=row_to["body_sha256"],
        from_version=int(row_from["version"]),
        to_version=int(row_to["version"]),
        from_created_at=_format_prompt_created_at(row_from["created_at"]),
        to_created_at=_format_prompt_created_at(row_to["created_at"]),
        diff=diff_text,
    )


@router.delete("/tokens", dependencies=_REQUIRE_ADMIN)
async def reset_token_usage(
    conn=Depends(_get_conn),
):
    """Reset all token usage counters."""
    global token_frozen, _last_budget_level
    _token_usage.clear()
    _token_usage_shared.clear()
    token_frozen = False
    _token_frozen_shared.set(False)
    _last_budget_level = ""
    _budget_flags.set("level", "normal")
    _hourly_ledger_shared.clear()
    await db.clear_token_usage(conn)
    from backend.events import emit_token_warning
    emit_token_warning("reset", "Token usage and freeze state cleared.")
    return {"status": "reset"}


@router.get("/token-budget", response_model=TokenBudgetResponse)
async def get_token_budget():
    """Return current budget settings, daily usage, and freeze status."""
    from backend.config import settings
    cost = get_daily_cost()
    budget = settings.token_budget_daily
    return {
        "budget": budget,
        "usage": cost,
        "ratio": round(cost / budget, 4) if budget > 0 else 0,
        "frozen": is_token_frozen(),
        "level": _budget_flags.get("level", _last_budget_level or "normal"),
        "warn_threshold": settings.token_warn_threshold,
        "downgrade_threshold": settings.token_downgrade_threshold,
        "freeze_threshold": settings.token_freeze_threshold,
        "fallback_provider": settings.token_fallback_provider,
        "fallback_model": settings.token_fallback_model,
    }


@router.put("/token-budget", dependencies=_REQUIRE_ADMIN)
async def update_token_budget(
    budget: float | None = None,
    warn_threshold: float | None = None,
    downgrade_threshold: float | None = None,
    freeze_threshold: float | None = None,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
):
    """Update token budget settings at runtime."""
    from backend.config import settings
    if budget is not None:
        settings.token_budget_daily = budget
    if warn_threshold is not None:
        settings.token_warn_threshold = warn_threshold
    if downgrade_threshold is not None:
        settings.token_downgrade_threshold = downgrade_threshold
    if freeze_threshold is not None:
        settings.token_freeze_threshold = freeze_threshold
    if fallback_provider is not None:
        settings.token_fallback_provider = fallback_provider
    if fallback_model is not None:
        settings.token_fallback_model = fallback_model
    return await get_token_budget()


@router.post("/token-budget/reset", dependencies=_REQUIRE_ADMIN)
async def reset_token_freeze():
    """Reset the token freeze state (human intervention)."""
    global token_frozen, _last_budget_level
    token_frozen = False
    _last_budget_level = ""
    from backend.events import emit_token_warning
    emit_token_warning("reset", "Token freeze manually cleared by operator.")
    return {"status": "unfrozen"}


@router.get("/pricing")
async def get_pricing_snapshot():
    """Return the current LLM pricing table + metadata.

    Z.3 checkbox 5 (#292). Read-only view onto `config/llm_pricing.yaml`
    so a dashboard / operator can render the live rates without parsing
    the YAML themselves and so an operator can verify the table state
    after `POST /runtime/pricing/reload`. Authenticated users only —
    no admin gate, since pricing is non-sensitive informational data
    (matches peer GETs like `/runtime/info` and `/runtime/status`).

    Response shape: see `backend.pricing.get_pricing_table` — `providers`
    map (per-provider, per-model `{input, output}` USD per 1M tokens),
    a global `defaults` pair, the YAML's `metadata` block (notably
    `updated_at` + `source` URL), and `loaded_from_yaml` so a dashboard
    can flag the degraded "YAML missing/corrupt → hard-coded fallback"
    state instead of silently rendering the boot-safety table.
    """
    from backend import pricing as _pricing
    return _pricing.get_pricing_table()


@router.post("/pricing/reload", dependencies=_REQUIRE_ADMIN)
async def reload_pricing_table():
    """Hot-reload `config/llm_pricing.yaml` and broadcast to all workers.

    Z.3 checkbox 4 (#292). Operator workflow: edit the YAML on disk
    (e.g. anthropic raises Sonnet from 3/15 to 3.5/16), POST here, every
    uvicorn worker re-reads the file. Without this endpoint the only
    way to pick up a price change is a rolling restart through Caddy.

    Local + remote reload semantics:
        - This worker calls `pricing.reload()` synchronously and uses
          the returned status as the response payload.
        - `publish_cross_worker(PRICING_RELOAD_EVENT, ...)` fans the
          signal out via Redis pub/sub. Every peer (including this
          worker — the listener filters on event name only) calls
          `pricing._on_pricing_reload_event` which clears its local
          cache so its next `get_pricing()` re-reads the YAML.
        - When Redis is unavailable `publish_cross_worker` returns
          False and only this worker reloads. Operator runbook
          documents the manual rolling restart fallback.
    """
    from backend import pricing as _pricing
    from backend.shared_state import publish_cross_worker as _publish
    status = _pricing.reload()
    broadcast_ok = _publish(_pricing.PRICING_RELOAD_EVENT, {
        "origin_worker": str(os.getpid()),
    })
    return {
        "status": "reloaded",
        "loaded_from_yaml": status["loaded_from_yaml"],
        "providers": status["providers"],
        "metadata": status["metadata"],
        "broadcast": "redis_pubsub" if broadcast_ok else "local_only",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ollama tool-call observability (Z.6.5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/ollama/tool-failures")
async def get_ollama_tool_failures():
    """Return Ollama tool-call failure counters from SharedKV.

    Z.6.5: when the ollama adapter degrades to pure-chat (model does not
    support tool calling, daemon is unreachable, or the tool_calls block
    cannot be parsed), it increments SharedKV("ollama_tool_failures")
    counters.  This endpoint exposes those counts for the dashboard.

    ``has_warning`` is True whenever total > 0, signalling that at least
    one Ollama tool-call silently degraded since the last counter reset.

    Module-global state: SharedKV reads from Redis (cross-worker) or
    in-memory fallback (per-process in degraded mode — 故意每 worker 獨立
    when Redis is absent, acceptable for observability counters).
    """
    from backend.models import OllamaToolFailuresResponse
    from backend.shared_state import SharedKV
    kv = SharedKV("ollama_tool_failures")
    raw = kv.get_all()

    def _int(key: str) -> int:
        try:
            return int(raw.get(key, 0))
        except (ValueError, TypeError):
            return 0

    total = _int("total")
    return OllamaToolFailuresResponse(
        total=total,
        daemon_error=_int("daemon_error"),
        parse_error=_int("parse_error"),
        unsupported=_int("unsupported"),
        has_warning=total > 0,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/notifications/unread-count")
async def unread_count(
    conn=Depends(_get_conn),
):
    """Count unread notifications (L2+)."""
    count = await db.count_unread_notifications(conn, "warning")
    return {"count": count}


@router.get("/notifications")
async def get_notifications(
    limit: int = _pg.Limit(default=50, max_cap=200),
    level: str = "",
    conn=Depends(_get_conn),
):
    """List notifications, optionally filtered by level."""
    return await db.list_notifications(conn, limit=limit, level=level)


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: str,
    conn=Depends(_get_conn),
    user: _auth.User = Depends(_auth.current_user),
):
    """Mark a notification as read.

    Q.3-SUB-3 (#297): on a successful flip, emit ``notification.read``
    on the event bus so other devices owned by the same user can
    decrement their bell badge and drop the row from their local list
    without waiting for the next ``/notifications/unread-count`` poll.
    Emit is best-effort (``broadcast_scope='user'``, advisory until
    Q.4 #298) and is swallowed on failure — the PG row is the
    source of truth, the SSE push is latency-optimisation only.
    """
    ok = await db.mark_notification_read(conn, notification_id)
    if ok:
        try:
            from backend.events import emit_notification_read
            emit_notification_read(notification_id, user.id)
        except Exception as exc:
            logger.debug(
                "emit_notification_read failed for %s: %s",
                notification_id, exc,
            )
    return {"status": "ok" if ok else "not_found"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Roles & Model Rules registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/roles")
async def get_available_roles():
    """List all available agent roles from configs/roles/."""
    from backend.prompt_loader import list_available_roles
    return list_available_roles()


@router.get("/model-rules")
async def get_available_model_rules():
    """List all available model rule definitions from configs/models/."""
    from backend.prompt_loader import list_available_models
    return list_available_models()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPI Lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_NPI_CONFIG = Path(__file__).resolve().parent.parent.parent / "configs" / "npi_lifecycle.json"


@router.get("/npi")
async def get_npi_state(
    conn=Depends(_get_conn),
):
    """Return the current NPI lifecycle state.

    Q.7 #301: response now carries ``version`` so the frontend can
    echo it back in ``If-Match`` on the PUT.
    """
    state = await db.get_npi_state(conn)
    version = await db.get_npi_state_version(conn)
    if state:
        return {**state, "version": version}
    # First load: read from SSOT config file
    if _NPI_CONFIG.exists():
        import json as _json
        try:
            data = _json.loads(_NPI_CONFIG.read_text(encoding="utf-8"))
            await db.save_npi_state(conn, data)
            version = await db.get_npi_state_version(conn)
            return {**data, "version": version}
        except (ValueError, OSError) as exc:
            import logging
            logging.getLogger(__name__).error("Failed to load NPI config %s: %s", _NPI_CONFIG, exc)
    return {
        "business_model": "odm", "phases": [],
        "current_phase_id": None, "version": 0,
    }


@router.put("/npi")
async def update_npi_state(
    business_model: str | None = None,
    current_phase_id: str | None = None,
    if_match: str | None = Header(None, alias="If-Match"),
    conn=Depends(_get_conn),
):
    """Update NPI project-level settings.

    Q.7 #301 — requires ``If-Match: <version>`` header. Runtime
    settings are a singleton row (``id = 'current'``) so the lock
    guards a different race than workflow_runs — two admins on
    separate devices toggling ``business_model`` at the same time.
    Loser receives 409 with ``{current_version, your_version,
    hint}`` and the frontend hook offers 重載 / 覆蓋 / 合併.
    """
    from backend import optimistic_lock as _ol
    expected_version = _ol.parse_if_match(if_match)
    state = await db.get_npi_state(conn)
    if not state:
        state = {"business_model": "odm", "phases": [], "current_phase_id": None}
    if business_model is not None:
        state["business_model"] = business_model
    if current_phase_id is not None:
        state["current_phase_id"] = current_phase_id
    try:
        new_version = await db.save_npi_state_versioned(
            conn, state, expected_version=expected_version,
        )
    except _ol.VersionConflict as conflict:
        _ol.raise_conflict(
            conflict.current_version,
            conflict.your_version,
            resource="runtime_settings",
        )
    return {**state, "version": new_version}


_VALID_PHASE_STATUSES = {"pending", "active", "completed", "blocked"}
_VALID_MILESTONE_STATUSES = {"pending", "in_progress", "completed", "blocked"}


@router.patch("/npi/phases/{phase_id}")
async def update_npi_phase(
    phase_id: str,
    status: str | None = None,
    target_date: str | None = None,
    conn=Depends(_get_conn),
):
    """Update a specific NPI phase."""
    if status is not None and status not in _VALID_PHASE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid phase status: {status}. Must be one of {_VALID_PHASE_STATUSES}")
    state = await db.get_npi_state(conn)
    if not state:
        raise HTTPException(status_code=404, detail="NPI state not initialized")
    for phase in state.get("phases", []):
        if phase["id"] == phase_id:
            if status is not None:
                phase["status"] = status
            if target_date is not None:
                phase["target_date"] = target_date
            await db.save_npi_state(conn, state)
            return phase
    raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")


@router.patch("/npi/milestones/{milestone_id}")
async def update_npi_milestone(
    milestone_id: str,
    status: str | None = None,
    due_date: str | None = None,
    conn=Depends(_get_conn),
):
    """Update a specific NPI milestone."""
    if status is not None and status not in _VALID_MILESTONE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid milestone status: {status}. Must be one of {_VALID_MILESTONE_STATUSES}")
    state = await db.get_npi_state(conn)
    if not state:
        raise HTTPException(status_code=404, detail="NPI state not initialized")
    for phase in state.get("phases", []):
        for ms in phase.get("milestones", []):
            if ms["id"] == milestone_id:
                if status is not None:
                    ms["status"] = status
                if due_date is not None:
                    ms["due_date"] = due_date
                # Auto-compute phase status from milestones
                all_ms = phase.get("milestones", [])
                if all_ms:
                    if all(m["status"] == "completed" for m in all_ms):
                        phase["status"] = "completed"
                    elif any(m["status"] == "blocked" for m in all_ms):
                        phase["status"] = "blocked"
                    elif any(m["status"] in ("in_progress", "completed") for m in all_ms):
                        phase["status"] = "active"
                    else:
                        phase["status"] = "pending"
                await db.save_npi_state(conn, state)
                return ms
    raise HTTPException(status_code=404, detail=f"Milestone {milestone_id} not found")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /system/platforms/toolchains — B8
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/platforms/toolchains")
async def list_toolchains() -> dict:
    """Return every known toolchain name, grouped by platform and tier.

    Response shape:
      all:          sorted unique list of all toolchain strings
      by_platform:  { platform_name: default_toolchain }
      by_tier:      { tier_id: [allowed_toolchains] }
    """
    return _collect_toolchains()
