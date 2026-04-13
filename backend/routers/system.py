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

logger = logging.getLogger(__name__)

import yaml
from fastapi import APIRouter, HTTPException

from backend.models import (
    SystemInfoResponse, SystemStatusResponse, TokenBudgetResponse,
    Notification, Simulation, Artifact, DebugFinding,
)

router = APIRouter(prefix="/system", tags=["system"])

_BASH_TIMEOUT = 5
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


async def _sh(cmd: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT)
        return out.decode(errors="replace").strip()
    except Exception:
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

    return devices


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  System Status (for header)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/debug")
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
    results = []
    for f in sorted(platforms_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
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


@router.put("/spec")
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
    """List real git repositories: the main repo + any agent worktrees."""
    repos = []

    # Main repo
    branch = await _sh(f"git -C {_PROJECT_ROOT} rev-parse --abbrev-ref HEAD")
    commit = await _sh(f"git -C {_PROJECT_ROOT} log -1 --format='%h' 2>/dev/null")
    commit_time = await _sh(f"git -C {_PROJECT_ROOT} log -1 --format='%cr' 2>/dev/null")
    # Gather all remotes
    remotes_raw = await _sh(f"git -C {_PROJECT_ROOT} remote -v 2>/dev/null")
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
    })

    # Agent worktrees
    from backend.workspace import list_workspaces
    for ws in list_workspaces():
        ws_branch = await _sh(f"git -C {ws.path} rev-parse --abbrev-ref HEAD 2>/dev/null")
        ws_commit = await _sh(f"git -C {ws.path} log -1 --format='%h' 2>/dev/null")
        ws_time = await _sh(f"git -C {ws.path} log -1 --format='%cr' 2>/dev/null")
        repos.append({
            "id": f"ws-{ws.agent_id}",
            "name": f"{ws.agent_id} workspace",
            "url": str(ws.path),
            "branch": ws_branch or ws.branch,
            "status": "synced",
            "lastCommit": ws_commit or "",
            "lastCommitTime": ws_time or "",
            "tetheredAgentId": ws.agent_id,
        })

    return repos


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Logs — real system log ring buffer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_log_buffer: deque[dict] = deque(maxlen=200)


def add_system_log(message: str, level: str = "info") -> None:
    """Add a log entry (called from other modules)."""
    _log_buffer.append({
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "level": level,
    })


def get_recent_logs(limit: int = 50) -> list[dict]:
    """Return recent log entries (most recent first). Used by conversation node."""
    return list(reversed(list(_log_buffer)))[:limit]


# Seed with startup log
add_system_log("OmniSight Engine started", "info")
add_system_log(f"Python {platform.python_version()} on {platform.system()}", "info")


@router.get("/logs")
async def get_logs(limit: int = 50):
    """Return recent system logs."""
    return list(_log_buffer)[-limit:]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage tracking (in-memory + SQLite)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_token_usage: dict[str, dict] = {}

_PRICING = {
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
    u["input_tokens"] += input_tokens
    u["output_tokens"] += output_tokens
    u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    u["request_count"] += 1
    u["avg_latency"] = int((u["avg_latency"] * (u["request_count"] - 1) + latency_ms) / u["request_count"])
    u["last_used"] = datetime.now().strftime("%H:%M:%S")
    inp_rate, out_rate = _PRICING.get(model, (1.0, 3.0))
    u["cost"] = round(u["input_tokens"] / 1_000_000 * inp_rate + u["output_tokens"] / 1_000_000 * out_rate, 4)

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


def _maybe_reset_daily_budget() -> None:
    """Auto-reset token freeze at midnight (new day)."""
    global token_frozen, _last_budget_level, _token_daily_reset_date
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    if today != _token_daily_reset_date:
        _token_daily_reset_date = today
        if token_frozen:
            token_frozen = False
            _last_budget_level = "normal"
            from backend.events import emit_token_warning
            emit_token_warning("reset", "Daily token budget auto-reset")
            logger.info("Daily token budget auto-reset")


def get_daily_cost() -> float:
    """Sum all model costs for the current session."""
    return round(sum(u.get("cost", 0) for u in _token_usage.values()), 4)


async def _safe_check_budget() -> None:
    try:
        await asyncio.wait_for(_check_token_budget(), timeout=2.0)
    except Exception as exc:
        logger.warning("Token budget check failed: %s", exc)


async def _check_token_budget() -> None:
    """Check if daily cost exceeds budget thresholds. Called after each track_tokens."""
    global token_frozen, _last_budget_level
    from backend.config import settings
    from backend.events import emit_token_warning

    budget = settings.token_budget_daily
    if budget <= 0:
        return  # Unlimited

    cost = get_daily_cost()
    ratio = cost / budget

    from backend.notifications import notify

    if ratio >= settings.token_freeze_threshold and _last_budget_level != "frozen":
        token_frozen = True
        _last_budget_level = "frozen"
        emit_token_warning("frozen", f"Token budget exhausted (${cost:.4f}/${budget:.2f}). All LLM calls frozen.", cost, budget)
        await notify("critical", "Token budget exhausted — LLM frozen",
                      message=f"Daily cost ${cost:.4f} exceeded budget ${budget:.2f}. All LLM calls disabled.",
                      source="token_budget")

    elif ratio >= settings.token_downgrade_threshold and _last_budget_level not in ("downgrade", "frozen"):
        _last_budget_level = "downgrade"
        try:
            from backend.routers.providers import _do_switch_provider
            await _do_switch_provider(settings.token_fallback_provider, settings.token_fallback_model)
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


@router.get("/tokens")
async def get_token_usage():
    """Return token usage stats per model."""
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


@router.delete("/tokens")
async def reset_token_usage():
    """Reset all token usage counters."""
    global token_frozen, _last_budget_level
    _token_usage.clear()
    token_frozen = False
    _last_budget_level = ""
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
        "frozen": token_frozen,
        "level": _last_budget_level or "normal",
        "warn_threshold": settings.token_warn_threshold,
        "downgrade_threshold": settings.token_downgrade_threshold,
        "freeze_threshold": settings.token_freeze_threshold,
        "fallback_provider": settings.token_fallback_provider,
        "fallback_model": settings.token_fallback_model,
    }


@router.put("/token-budget")
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


@router.post("/token-budget/reset")
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
async def get_notifications(limit: int = 50, level: str = ""):
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
