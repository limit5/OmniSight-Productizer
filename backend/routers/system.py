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

logger = logging.getLogger(__name__)

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _auth
from backend.routers import _pagination as _pg

from backend.models import (
    SystemInfoResponse, SystemStatusResponse, TokenBudgetResponse,
    DeployRequest,
)

# Router-level auth baseline: every /system/* route requires an
# authenticated session. Individual write endpoints stack an
# admin-role check on top via their own `dependencies=` list.
#
# Audit H1 (2026-04-19): this router previously had zero auth; the
# CF WAF rule + Zero Trust Access at the edge are defence-in-depth,
# but the application itself must not rely on edge mitigation. See
# docs/ops/deploy_postmortem_2026-04-19.md security follow-ups.
router = APIRouter(
    prefix="/system",
    tags=["system"],
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
    hostname = await _sh("hostname")
    os_info = await _sh("grep PRETTY_NAME /etc/os-release | cut -d'\"' -f2")
    if kernel and "microsoft" in kernel.lower() and "WSL" not in (os_info or ""):
        os_info = f"{os_info} (WSL2)"

    return {
        "hostname": hostname or platform.node(),
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
async def get_debug_state():
    """Comprehensive debug state: agent errors, blocked tasks, debug findings."""
    from backend import db
    from backend.routers.agents import _agents
    from backend.routers.tasks import _tasks
    from backend.models import AgentStatus, TaskStatus

    agents = list(_agents.values())
    tasks = list(_tasks.values())
    findings = await db.list_debug_findings(limit=50)

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

_PRICING = {
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "gpt-4o": (5.0, 15.0),
    "gemini-1.5-pro": (0.5, 1.5),
    "grok-3-mini": (2.0, 10.0),
    "llama-3.3-70b-versatile": (0.6, 0.6),
    "deepseek-chat": (0.14, 0.28),
}


def track_tokens(model: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
    """Track token usage for a model (called synchronously from LLM callback)."""
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
        }
    u = _token_usage[model]
    prev_cost = u["cost"]
    u["input_tokens"] += input_tokens
    u["output_tokens"] += output_tokens
    u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    u["request_count"] += 1
    u["avg_latency"] = int((u["avg_latency"] * (u["request_count"] - 1) + latency_ms) / u["request_count"])
    u["last_used"] = datetime.now().strftime("%H:%M:%S")
    inp_rate, out_rate = _PRICING.get(model, (1.0, 3.0))
    u["cost"] = round(u["input_tokens"] / 1_000_000 * inp_rate + u["output_tokens"] / 1_000_000 * out_rate, 4)
    cost_delta = u["cost"] - prev_cost
    _record_hourly(cost_delta)

    # I10: track in shared state for cross-worker visibility
    _token_usage_shared.track(model, input_tokens, output_tokens, latency_ms, cost_delta)
    if cost_delta > 0:
        _hourly_ledger_shared.record(cost_delta)

    # Persist asynchronously (fire-and-forget) + check budget
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_token_usage(u.copy()))
        loop.create_task(_safe_check_budget())
    except RuntimeError:
        pass  # No event loop — skip persistence (e.g. during tests)


async def _persist_token_usage(data: dict) -> None:
    from backend import db
    try:
        await db.upsert_token_usage(data)
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


async def load_token_usage_from_db() -> None:
    """Load persisted token data into memory (called at startup)."""
    from backend import db
    for row in await db.list_token_usage():
        _token_usage[row["model"]] = row
    if _token_usage:
        _token_usage_shared.set_all(_token_usage)


@router.get("/tokens")
async def get_token_usage():
    """Return token usage stats per model."""
    shared = _token_usage_shared.get_all()
    if shared:
        return list(shared.values())
    return list(_token_usage.values())


@router.get("/compression")
async def get_compression_stats():
    """Return RTK output compression statistics."""
    from backend.output_compressor import get_compression_stats as _get_stats
    stats = _get_stats()
    # Estimate token savings (rough: 1 token ≈ 4 bytes)
    stats["estimated_tokens_saved"] = stats.get("total_original_bytes", 0) - stats.get("total_compressed_bytes", 0)
    stats["estimated_tokens_saved"] //= 4
    return stats


@router.delete("/tokens", dependencies=_REQUIRE_ADMIN)
async def reset_token_usage():
    """Reset all token usage counters."""
    global token_frozen, _last_budget_level
    _token_usage.clear()
    _token_usage_shared.clear()
    token_frozen = False
    _token_frozen_shared.set(False)
    _last_budget_level = ""
    _budget_flags.set("level", "normal")
    _hourly_ledger_shared.clear()
    from backend import db
    await db.clear_token_usage()
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/notifications/unread-count")
async def unread_count():
    """Count unread notifications (L2+)."""
    from backend import db
    count = await db.count_unread_notifications("warning")
    return {"count": count}


@router.get("/notifications")
async def get_notifications(limit: int = _pg.Limit(default=50, max_cap=200), level: str = ""):
    """List notifications, optionally filtered by level."""
    from backend import db
    return await db.list_notifications(limit=limit, level=level)


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: str):
    """Mark a notification as read."""
    from backend import db
    ok = await db.mark_notification_read(notification_id)
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
async def get_npi_state():
    """Return the current NPI lifecycle state."""
    from backend import db
    state = await db.get_npi_state()
    if state:
        return state
    # First load: read from SSOT config file
    if _NPI_CONFIG.exists():
        import json as _json
        try:
            data = _json.loads(_NPI_CONFIG.read_text(encoding="utf-8"))
            await db.save_npi_state(data)
            return data
        except (ValueError, OSError) as exc:
            import logging
            logging.getLogger(__name__).error("Failed to load NPI config %s: %s", _NPI_CONFIG, exc)
    return {"business_model": "odm", "phases": [], "current_phase_id": None}


@router.put("/npi")
async def update_npi_state(
    business_model: str | None = None,
    current_phase_id: str | None = None,
):
    """Update NPI project-level settings."""
    from backend import db
    state = await db.get_npi_state()
    if not state:
        state = {"business_model": "odm", "phases": [], "current_phase_id": None}
    if business_model is not None:
        state["business_model"] = business_model
    if current_phase_id is not None:
        state["current_phase_id"] = current_phase_id
    await db.save_npi_state(state)
    return state


_VALID_PHASE_STATUSES = {"pending", "active", "completed", "blocked"}
_VALID_MILESTONE_STATUSES = {"pending", "in_progress", "completed", "blocked"}


@router.patch("/npi/phases/{phase_id}")
async def update_npi_phase(phase_id: str, status: str | None = None, target_date: str | None = None):
    """Update a specific NPI phase."""
    if status is not None and status not in _VALID_PHASE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid phase status: {status}. Must be one of {_VALID_PHASE_STATUSES}")
    from backend import db
    state = await db.get_npi_state()
    if not state:
        raise HTTPException(status_code=404, detail="NPI state not initialized")
    for phase in state.get("phases", []):
        if phase["id"] == phase_id:
            if status is not None:
                phase["status"] = status
            if target_date is not None:
                phase["target_date"] = target_date
            await db.save_npi_state(state)
            return phase
    raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")


@router.patch("/npi/milestones/{milestone_id}")
async def update_npi_milestone(milestone_id: str, status: str | None = None, due_date: str | None = None):
    """Update a specific NPI milestone."""
    if status is not None and status not in _VALID_MILESTONE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid milestone status: {status}. Must be one of {_VALID_MILESTONE_STATUSES}")
    from backend import db
    state = await db.get_npi_state()
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
                await db.save_npi_state(state)
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
