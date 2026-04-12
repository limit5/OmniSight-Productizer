"""Backend slash command handler — intercepts / commands before LLM pipeline.

Called from chat.py when message starts with /. Returns a formatted
response string or None if the command should fall through to LLM.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def handle_slash_command(command: str, args: str) -> str | None:
    """Handle a slash command. Returns response text or None to fall through to LLM."""
    handler = _HANDLERS.get(command)
    if handler:
        try:
            return await handler(args)
        except Exception as exc:
            logger.warning("Slash command /%s failed: %s", command, exc)
            return f"[ERROR] /{command} failed: {exc}"
    return None  # Unknown command — let LLM handle it


async def _status(args: str) -> str:
    from backend.routers.system import get_system_status
    data = await get_system_status()
    return (
        f"**System Status**\n"
        f"- Tasks: {data['tasks_completed']}/{data['tasks_total']} completed\n"
        f"- Agents running: {data['agents_running']}\n"
        f"- Workspaces: {data['workspaces_active']} active\n"
        f"- Containers: {data['containers_active']} active\n"
        f"- Memory: {data['memory_summary']}\n"
        f"- USB: {data['usb_status']}"
    )


async def _info(args: str) -> str:
    from backend.routers.system import get_system_info
    data = await get_system_info()
    return (
        f"**Host Info**\n"
        f"- Hostname: {data['hostname']}\n"
        f"- OS: {data['os']} ({data['arch']})\n"
        f"- Kernel: {data['kernel']}\n"
        f"- CPU: {data['cpu_model']} ({data['cpu_cores']} cores, {data['cpu_usage']}%)\n"
        f"- Memory: {data['memory_used']}/{data['memory_total']}\n"
        f"- Uptime: {data['uptime']}"
    )


async def _debug(args: str) -> str:
    from backend.routers.system import get_debug_state
    data = await get_debug_state()
    lines = [f"**Debug State** ({data['timestamp'][:19]})"]
    if data["agent_errors"]:
        lines.append(f"\nAgent Errors ({len(data['agent_errors'])}):")
        for ae in data["agent_errors"][:5]:
            lines.append(f"  - [{ae['status']}] {ae['name']}: {ae['thought_chain'][:60]}")
    if data["blocked_tasks"]:
        lines.append(f"\nBlocked Tasks ({len(data['blocked_tasks'])}):")
        for bt in data["blocked_tasks"][:5]:
            lines.append(f"  - {bt['title'][:60]}")
    if data["total_findings"] > 0:
        lines.append(f"\nDebug Findings: {data['total_findings']} total")
        for ft, count in data["findings_by_type"].items():
            if count > 0:
                lines.append(f"  - {ft}: {count}")
    if not data["agent_errors"] and not data["blocked_tasks"] and data["total_findings"] == 0:
        lines.append("\nAll clear — no errors or findings.")
    return "\n".join(lines)


async def _logs(args: str) -> str:
    from backend.routers.system import get_recent_logs
    limit = 10
    if args.strip().isdigit():
        limit = min(int(args.strip()), 50)
    logs = get_recent_logs(limit)
    if not logs:
        return "No recent logs."
    lines = [f"**Recent Logs** (last {len(logs)})"]
    for log in logs:
        lines.append(f"`{log['timestamp']}` [{log['level']}] {log['message'][:80]}")
    return "\n".join(lines)


async def _devices(args: str) -> str:
    from backend.routers.system import get_devices
    data = await get_devices()
    if not data:
        return "No devices detected."
    lines = ["**Devices**"]
    for d in data[:10]:
        lines.append(f"  - [{d.get('type', '?')}] {d.get('name', 'Unknown')} ({d.get('status', '?')})")
    return "\n".join(lines)


async def _agents(args: str) -> str:
    from backend.routers.agents import _agents
    agents = list(_agents.values())
    if not agents:
        return "No agents registered."
    lines = [f"**Agents** ({len(agents)})"]
    for a in agents:
        status = a.status.value if hasattr(a.status, "value") else str(a.status)
        lines.append(f"  - [{status}] {a.name} ({a.type.value}/{a.sub_type or '-'})")
    return "\n".join(lines)


async def _tasks(args: str) -> str:
    from backend.routers.tasks import _tasks
    tasks = list(_tasks.values())
    if not tasks:
        return "No tasks in system."
    lines = [f"**Tasks** ({len(tasks)})"]
    for t in tasks[:15]:
        status = t.status.value if hasattr(t.status, "value") else str(t.status)
        lines.append(f"  - [{status}] {t.title[:50]}")
    return "\n".join(lines)


async def _provider(args: str) -> str:
    from backend.routers.providers import get_provider_health
    data = await get_provider_health()
    lines = ["**Provider Fallback Chain**"]
    for h in data["health"]:
        icon = "●" if h["status"] == "active" else "○" if h["status"] == "available" else "⏳" if h["status"] == "cooldown" else "✗"
        extra = f" ({h['cooldown_remaining']}s)" if h["status"] == "cooldown" else ""
        lines.append(f"  {icon} {h['name']} [{h['status']}]{extra}")
    return "\n".join(lines)


async def _budget(args: str) -> str:
    from backend.routers.system import get_token_budget
    data = await get_token_budget()
    return (
        f"**Token Budget**\n"
        f"- Daily budget: ${data.get('budget', 0)}\n"
        f"- Used today: ${data.get('usage', 0)}\n"
        f"- Frozen: {'YES' if data.get('frozen') else 'No'}\n"
        f"- Warn threshold: {data.get('warn_threshold', 0.8)*100:.0f}%\n"
        f"- Downgrade threshold: {data.get('downgrade_threshold', 0.9)*100:.0f}%"
    )


async def _npi(args: str) -> str:
    from backend import db
    state = await db.get_npi_state()
    if not state or not state.get("phases"):
        return "NPI data not loaded."
    phases = state["phases"]
    completed = sum(1 for p in phases if p.get("status") == "completed")
    lines = [f"**NPI Lifecycle** ({completed}/{len(phases)} phases, model: {state.get('business_model', '?')})"]
    for p in phases:
        status_icon = "✅" if p["status"] == "completed" else "🔄" if p["status"] == "active" else "⏸" if p["status"] == "pending" else "🚫"
        ms = p.get("milestones", [])
        ms_done = sum(1 for m in ms if m.get("status") == "completed")
        lines.append(f"  {status_icon} {p['short_name']} — {p['name']} ({ms_done}/{len(ms)})")
    return "\n".join(lines)


async def _sdks(args: str) -> str:
    from backend.routers.system import list_vendor_sdks
    data = await list_vendor_sdks()
    if not data:
        return "No platform profiles found."
    lines = ["**Vendor SDKs**"]
    for s in data:
        icon = "✅" if s["status"] == "ready" else "❌"
        vendor = f" ({s['vendor_id']})" if s["vendor_id"] else ""
        lines.append(f"  {icon} {s['platform']}{vendor} [{s['status']}]")
    return "\n".join(lines)


async def _help(args: str) -> str:
    lines = ["**Available Commands**\n"]
    categories = {}
    for name, handler in sorted(_HANDLERS.items()):
        # Group by first letter for simple display
        lines.append(f"  `/{name}`")
    return "\n".join(lines)


_HANDLERS: dict[str, object] = {
    "status": _status,
    "info": _info,
    "debug": _debug,
    "logs": _logs,
    "devices": _devices,
    "agents": _agents,
    "tasks": _tasks,
    "provider": _provider,
    "budget": _budget,
    "npi": _npi,
    "sdks": _sdks,
    "help": _help,
}
