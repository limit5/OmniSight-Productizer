"""Backend slash command handler — intercepts / commands before LLM pipeline.

Called from chat.py when message starts with /. Returns a formatted
response string or None if the command should fall through to LLM.

Phase-3-Runtime-v2 SP-3.1 (2026-04-20): the entry point + every
handler now take a pool-backed ``asyncpg.Connection`` as the first
parameter. Handlers that don't touch the DB (``/status``,
``/help``, etc.) simply ignore it, but the uniform signature means
future SP ports (``/assign``, ``/clear``, ``/release`` etc. that
touch tasks / releases / etc.) can use it without re-signaturing
the dispatch table.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)


async def handle_slash_command(
    conn: asyncpg.Connection, command: str, args: str,
) -> str | None:
    """Handle a slash command. Returns response text or None for non-/ input."""
    if not command:
        return "Type `/help` to see available commands."
    handler = _HANDLERS.get(command)
    if handler:
        try:
            return await handler(conn, args)
        except Exception as exc:
            logger.warning("Slash command /%s failed: %s", command, exc)
            return f"[ERROR] /{command} failed: {exc}"
    # Unknown slash command — return error instead of falling through to LLM
    known = ", ".join(f"/{n}" for n in sorted(_HANDLERS.keys())[:10])
    return f"Unknown command: `/{command}`. Available: {known}... Type `/help` for full list."


async def _status(conn: asyncpg.Connection, args: str) -> str:
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


async def _info(conn: asyncpg.Connection, args: str) -> str:
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


async def _debug(conn: asyncpg.Connection, args: str) -> str:
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


async def _logs(conn: asyncpg.Connection, args: str) -> str:
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


async def _devices(conn: asyncpg.Connection, args: str) -> str:
    from backend.routers.system import get_devices
    data = await get_devices()
    if not data:
        return "No devices detected."
    lines = ["**Devices**"]
    for d in data[:10]:
        lines.append(f"  - [{d.get('type', '?')}] {d.get('name', 'Unknown')} ({d.get('status', '?')})")
    return "\n".join(lines)


async def _agents(conn: asyncpg.Connection, args: str) -> str:
    from backend.routers.agents import _agents
    agents = list(_agents.values())
    if not agents:
        return "No agents registered."
    lines = [f"**Agents** ({len(agents)})"]
    for a in agents:
        status = a.status.value if hasattr(a.status, "value") else str(a.status)
        lines.append(f"  - [{status}] {a.name} ({a.type.value}/{a.sub_type or '-'})")
    return "\n".join(lines)


async def _tasks(conn: asyncpg.Connection, args: str) -> str:
    from backend.routers.tasks import _tasks
    tasks = list(_tasks.values())
    if not tasks:
        return "No tasks in system."
    lines = [f"**Tasks** ({len(tasks)})"]
    for t in tasks[:15]:
        status = t.status.value if hasattr(t.status, "value") else str(t.status)
        lines.append(f"  - [{status}] {t.title[:50]}")
    return "\n".join(lines)


async def _provider(conn: asyncpg.Connection, args: str) -> str:
    from backend.routers.providers import get_provider_health
    data = await get_provider_health()
    lines = ["**Provider Fallback Chain**"]
    for h in data["health"]:
        icon = "●" if h["status"] == "active" else "○" if h["status"] == "available" else "⏳" if h["status"] == "cooldown" else "✗"
        extra = f" ({h['cooldown_remaining']}s)" if h["status"] == "cooldown" else ""
        lines.append(f"  {icon} {h['name']} [{h['status']}]{extra}")
    return "\n".join(lines)


async def _budget(conn: asyncpg.Connection, args: str) -> str:
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


async def _npi(conn: asyncpg.Connection, args: str) -> str:
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


async def _sdks(conn: asyncpg.Connection, args: str) -> str:
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


async def _platform(conn: asyncpg.Connection, args: str) -> str:
    from backend.agents.tools import get_platform_config
    platform = args.strip() or "aarch64"
    result = await get_platform_config.ainvoke({"platform": platform})
    return result


async def _spawn(conn: asyncpg.Connection, args: str) -> str:
    agent_type = args.strip().lower() or "general"
    from backend.routers.agents import _agents, _persist
    from backend.models import Agent, AgentType, AgentStatus
    import uuid
    valid = {e.value for e in AgentType}
    if agent_type not in valid:
        return f"[ERROR] Unknown agent type: {agent_type}. Valid: {', '.join(sorted(valid))}"
    agent = Agent(
        id=f"{agent_type}-{uuid.uuid4().hex[:6]}",
        name=f"{agent_type.title()} Agent",
        type=AgentType(agent_type),
        status=AgentStatus.idle,
    )
    _agents[agent.id] = agent
    # SP-3.1/3.2: pass the request-scoped pool conn to the ported
    # _persist. Do NOT swallow DB failures silently — if the persist
    # fails, the in-memory _agents dict is ahead of the DB, which is
    # only safe if the operator sees the error and re-tries (or
    # restarts, which reloads from DB via seed_defaults_if_empty).
    await _persist(agent, conn)
    return f"Agent spawned: **{agent.name}** (`{agent.id}`) — status: idle"


async def _switch(conn: asyncpg.Connection, args: str) -> str:
    parts = args.strip().split()
    provider = parts[0] if parts else ""
    model = parts[1] if len(parts) > 1 else ""
    if not provider:
        return "[ERROR] Usage: /switch [provider] [model]"
    from backend.routers.providers import switch_provider, SwitchProviderRequest
    try:
        result = await switch_provider(SwitchProviderRequest(provider=provider, model=model or None))
        return f"Switched to **{result['provider']}** ({result['model']}). LLM active: {result['llm_active']}"
    except Exception as exc:
        return f"[ERROR] Switch failed: {exc}"


async def _build(conn: asyncpg.Connection, args: str) -> str:
    module = args.strip() or "firmware"
    return f"[ROUTE TO LLM] Build request for `{module}`. This command will be processed by the agent pipeline.\n\n_Tip: Use INVOKE to auto-dispatch build tasks._"


async def _test(conn: asyncpg.Connection, args: str) -> str:
    module = args.strip() or "all"
    return f"[ROUTE TO LLM] Test request for `{module}`. This command will be processed by the agent pipeline."


async def _simulate(conn: asyncpg.Connection, args: str) -> str:
    module = args.strip()
    if not module:
        return "[ERROR] Usage: /simulate [module_name]"
    return f"[ROUTE TO LLM] Simulation request for `{module}`. Agent will call run_simulation tool."


async def _review(conn: asyncpg.Connection, args: str) -> str:
    return "[ROUTE TO LLM] Code review request. Agent will use Gerrit tools to review pending changes."


async def _assign(conn: asyncpg.Connection, args: str) -> str:
    if not args.strip():
        return "[ERROR] Usage: /assign [task_id or title] [agent_id or name]"
    return f"[ROUTE TO LLM] Assignment request: `{args.strip()}`. Orchestrator will match task to best agent."


async def _clear(conn: asyncpg.Connection, args: str) -> str:
    return "[CLEAR] Chat history cleared."


async def _refresh(conn: asyncpg.Connection, args: str) -> str:
    return "[REFRESH] System data refresh requested. Frontend will reload all state."


async def _invoke(conn: asyncpg.Connection, args: str) -> str:
    cmd = args.strip()
    if cmd:
        return f"[INVOKE] Singularity sync with command: `{cmd}`. Press the INVOKE button or use the frontend."
    return "[INVOKE] Singularity sync requested. Press the ⚡ INVOKE button to execute."


async def _deploy(conn: asyncpg.Connection, args: str) -> str:
    """Deploy compiled artifacts to an EVK board."""
    if not args.strip():
        # Show EVK status
        try:
            from backend.agents.tools import check_evk_connection
            result = await check_evk_connection.ainvoke({"platform": ""})
            return f"**EVK Deploy Status**\n\n{result}\n\nUsage: `/deploy [platform] [module]` — deploy module to EVK"
        except Exception as exc:
            return f"[ERROR] EVK check failed: {exc}"
    parts = args.strip().split()
    platform = parts[0] if len(parts) >= 1 else ""
    module = parts[1] if len(parts) >= 2 else ""
    if not module:
        return f"[ERROR] Usage: `/deploy {platform} [module]` — e.g. `/deploy vendor-example sensor`"
    return f"[ROUTE TO LLM] Deploy request: module=`{module}` to platform=`{platform}`. Agent will cross-compile and transfer."


async def _evk(conn: asyncpg.Connection, args: str) -> str:
    """Check EVK board connectivity and status."""
    try:
        from backend.agents.tools import check_evk_connection
        platform = args.strip() or ""
        result = await check_evk_connection.ainvoke({"platform": platform})
        return f"**EVK Connection Check**\n\n{result}"
    except Exception as exc:
        return f"[ERROR] EVK check failed: {exc}"


async def _stream(conn: asyncpg.Connection, args: str) -> str:
    """List UVC camera devices and streaming capabilities."""
    try:
        from backend.agents.tools import list_uvc_devices
        result = await list_uvc_devices.ainvoke({})
        return f"**UVC Camera Devices**\n\n{result}"
    except Exception as exc:
        return f"[ERROR] UVC device scan failed: {exc}"


async def _release(conn: asyncpg.Connection, args: str) -> str:
    """Create a release bundle or show current version."""
    from backend.release import resolve_version
    version = await resolve_version()

    if not args.strip():
        # Show version + artifact count
        try:
            from backend import db
            arts = await db.list_artifacts(limit=200)
            return (
                f"**Release Info**\n\n"
                f"  Version: `{version}`\n"
                f"  Artifacts: {len(arts)}\n\n"
                f"Usage: `/release create [version]` — create release bundle\n"
                f"       `/release upload github` — upload to GitHub Releases"
            )
        except Exception as exc:
            return f"[ERROR] {exc}"

    parts = args.strip().split()
    action = parts[0].lower()

    if action == "create":
        ver = parts[1] if len(parts) > 1 else ""
        try:
            from backend.release import create_release_bundle
            bundle = await create_release_bundle(version=ver)
            return (
                f"**Release Bundle Created**\n\n"
                f"  Name: `{bundle['name']}`\n"
                f"  Version: `{bundle['version']}`\n"
                f"  Size: {bundle['size']} bytes\n"
                f"  Artifacts: {bundle['manifest']['artifact_count']}\n"
                f"  Download: `GET {bundle['download_url']}`"
            )
        except Exception as exc:
            return f"[ERROR] Release creation failed: {exc}"

    if action == "upload":
        target = parts[1] if len(parts) > 1 else "github"
        return f"[ROUTE TO LLM] Upload release {version} to {target}. Use POST /system/release with upload_{target}=true."

    return f"[ERROR] Unknown release action: {action}. Use `create` or `upload`."


async def _pipeline(conn: asyncpg.Connection, args: str) -> str:
    """E2E pipeline control — start, status, advance."""
    from backend.pipeline import get_pipeline_status, run_pipeline, force_advance

    if not args.strip():
        status = get_pipeline_status()
        step = status.get("current_step", "idle")
        return (
            f"**Pipeline Status**\n\n"
            f"  Status: `{status['status']}`\n"
            f"  Current Step: `{step}`\n"
            f"  Steps: {' → '.join(status.get('steps', []))}\n\n"
            f"Usage:\n"
            f"  `/pipeline start [spec description]` — start full E2E pipeline\n"
            f"  `/pipeline advance` — force past human checkpoint\n"
        )

    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower()

    if action == "start":
        spec = parts[1] if len(parts) > 1 else ""
        try:
            result = await run_pipeline(spec)
            return (
                f"**Pipeline Started**\n\n"
                f"  ID: `{result.get('id', '')}`\n"
                f"  First Step: `{result.get('current_step', '')}`\n"
                f"  Tasks Created: {result.get('tasks_created', 0)}\n"
            )
        except Exception as exc:
            return f"[ERROR] Pipeline start failed: {exc}"

    if action == "advance":
        try:
            result = await force_advance()
            return f"**Pipeline Advanced**\n\n  Status: `{result['status']}`\n  Step: `{result.get('step', '')}`"
        except Exception as exc:
            return f"[ERROR] Pipeline advance failed: {exc}"

    return f"[ERROR] Unknown pipeline action: {action}. Use `start`, `advance`."


async def _help(conn: asyncpg.Connection, args: str) -> str:
    categories = {
        "System": ["status", "info", "debug", "logs", "devices"],
        "Development": ["build", "test", "simulate", "review", "platform"],
        "Hardware": ["deploy", "evk", "stream", "release", "pipeline"],
        "Agent": ["spawn", "agents", "tasks", "assign", "invoke"],
        "Provider": ["provider", "switch", "budget"],
        "NPI": ["npi", "sdks"],
        "Tools": ["help", "clear", "refresh"],
    }
    lines = ["**Available Commands**"]
    for cat, cmds in categories.items():
        lines.append(f"\n**{cat}**")
        for c in cmds:
            if c in _HANDLERS:
                lines.append(f"  `/{c}`")
    return "\n".join(lines)


from typing import Callable, Awaitable
_HANDLERS: dict[str, Callable[[str], Awaitable[str]]] = {
    # System
    "status": _status, "info": _info, "debug": _debug, "logs": _logs, "devices": _devices,
    # Development
    "build": _build, "test": _test, "simulate": _simulate, "review": _review, "platform": _platform,
    # Hardware
    "deploy": _deploy, "evk": _evk, "stream": _stream, "release": _release, "pipeline": _pipeline,
    # Agent
    "spawn": _spawn, "agents": _agents, "tasks": _tasks, "assign": _assign, "invoke": _invoke,
    # Provider
    "provider": _provider, "switch": _switch, "budget": _budget,
    # NPI
    "npi": _npi, "sdks": _sdks,
    # Tools
    "help": _help, "clear": _clear, "refresh": _refresh,
}
