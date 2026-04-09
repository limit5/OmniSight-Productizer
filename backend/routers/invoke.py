"""INVOKE (Singularity Sync) — context-aware global orchestration endpoint.

Analyses current system state (agents, tasks) and automatically determines
the highest-value action to perform. Streams progress via SSE.
"""

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from backend.agents.graph import run_graph
from backend.agents.llm import get_llm
from backend.routers.agents import _agents
from backend.routers.tasks import _tasks
from backend.models import AgentStatus, AgentWorkspace, TaskStatus
from backend.workspace import provision as ws_provision, get_workspace
from backend.events import emit_invoke

router = APIRouter(prefix="/invoke", tags=["invoke"])


def _now() -> str:
    return datetime.now().isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6]


# ─── State analysis ───

def _analyze_state() -> dict:
    """Scan agents and tasks to determine what needs attention."""
    agents = list(_agents.values())
    tasks = list(_tasks.values())

    unassigned = [t for t in tasks if t.status == TaskStatus.backlog]
    in_progress = [t for t in tasks if t.status in (TaskStatus.assigned, TaskStatus.in_progress)]
    completed = [t for t in tasks if t.status == TaskStatus.completed]
    blocked = [t for t in tasks if t.status == TaskStatus.blocked]

    idle_agents = [a for a in agents if a.status == AgentStatus.idle]
    running_agents = [a for a in agents if a.status == AgentStatus.running]
    error_agents = [a for a in agents if a.status == AgentStatus.error]
    warning_agents = [a for a in agents if a.status == AgentStatus.warning]

    return {
        "agents": agents,
        "tasks": tasks,
        "unassigned": unassigned,
        "in_progress": in_progress,
        "completed": completed,
        "blocked": blocked,
        "idle_agents": idle_agents,
        "running_agents": running_agents,
        "error_agents": error_agents,
        "warning_agents": warning_agents,
    }


def _plan_actions(state: dict, command: str | None) -> list[dict]:
    """Decide what actions to take based on current state.

    Returns a list of action dicts, each with:
      - type: "assign" | "retry" | "report" | "health" | "command"
      - detail fields depending on type
    """
    actions: list[dict] = []

    # Priority 0: If user typed a command, that takes precedence
    if command:
        actions.append({"type": "command", "command": command})
        return actions

    # Priority 1: Error agents → retry
    for agent in state["error_agents"]:
        actions.append({
            "type": "retry",
            "agent_id": agent.id,
            "agent_name": agent.name,
        })

    # Priority 2: Unassigned tasks → auto-assign to matching idle agents
    idle_by_type: dict[str, list] = {}
    for agent in state["idle_agents"]:
        idle_by_type.setdefault(agent.type, []).append(agent)

    for task in sorted(state["unassigned"], key=lambda t: _priority_rank(t.priority)):
        # Find a matching idle agent
        preferred_type = task.suggested_agent_type or "custom"
        candidates = idle_by_type.get(preferred_type, [])
        if not candidates:
            # Try any idle agent
            for atype, alist in idle_by_type.items():
                if alist:
                    candidates = alist
                    break
        if candidates:
            agent = candidates.pop(0)
            actions.append({
                "type": "assign",
                "task_id": task.id,
                "task_title": task.title,
                "agent_id": agent.id,
                "agent_name": agent.name,
            })

    # Priority 3: Completed tasks → generate report
    if state["completed"] and not state["unassigned"] and not state["error_agents"]:
        actions.append({
            "type": "report",
            "completed_count": len(state["completed"]),
            "tasks": [t.title for t in state["completed"]],
        })

    # Priority 4: Nothing to do → health check
    if not actions:
        actions.append({
            "type": "health",
            "agent_count": len(state["agents"]),
            "task_count": len(state["tasks"]),
            "running": len(state["running_agents"]),
            "idle": len(state["idle_agents"]),
            "pending": len(state["unassigned"]),
        })

    return actions


def _priority_rank(priority: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(priority, 4)


# ─── Action execution ───

async def _execute_actions(actions: list[dict], state: dict):
    """Execute planned actions and yield SSE events."""
    emit_invoke("start", f"Executing {len(actions)} action(s)")
    results = []

    for action in actions:
        if action["type"] == "command":
            # Route command through LangGraph pipeline
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "command",
                    "message": f"Processing command: {action['command']}",
                }),
            }
            try:
                result = await run_graph(action["command"])
                yield {
                    "event": "action",
                    "data": json.dumps({
                        "type": "command",
                        "routed_to": result.routed_to,
                        "answer": result.answer,
                        "tool_results": [
                            {"tool": tr.tool_name, "output": tr.output[:500], "success": tr.success}
                            for tr in result.tool_results
                        ],
                    }),
                }
                results.append(f"Command processed by {result.routed_to.upper()} agent")
            except Exception as exc:
                yield {
                    "event": "action",
                    "data": json.dumps({"type": "command", "error": str(exc)}),
                }
                results.append(f"Command failed: {exc}")

        elif action["type"] == "retry":
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "retry",
                    "message": f"Retrying agent: {action['agent_name']}",
                }),
            }
            agent = _agents.get(action["agent_id"])
            if agent:
                agent.status = AgentStatus.running
                agent.thought_chain = "Auto-retry initiated by INVOKE sync."
                agent.progress.current = 0
            yield {
                "event": "action",
                "data": json.dumps({
                    "type": "retry",
                    "agent_id": action["agent_id"],
                    "agent_name": action["agent_name"],
                    "new_status": "running",
                }),
            }
            results.append(f"Retried {action['agent_name']}")
            await asyncio.sleep(0.1)

        elif action["type"] == "assign":
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "assign",
                    "message": f"Assigning '{action['task_title']}' → {action['agent_name']}",
                }),
            }
            # Update backend state
            task = _tasks.get(action["task_id"])
            agent = _agents.get(action["agent_id"])
            workspace_branch = None
            workspace_path = None
            if task and agent:
                task.status = TaskStatus.assigned
                task.assigned_agent_id = action["agent_id"]
                agent.status = AgentStatus.running
                agent.thought_chain = f"Task assigned: {task.title}. Provisioning workspace..."

                # Auto-provision isolated workspace
                try:
                    ws_info = await ws_provision(action["agent_id"], action["task_id"])
                    agent.workspace = AgentWorkspace(
                        branch=ws_info.branch,
                        path=str(ws_info.path),
                        status="active",
                        task_id=ws_info.task_id,
                    )
                    agent.thought_chain = f"Workspace ready on branch {ws_info.branch}. Processing task..."
                    workspace_branch = ws_info.branch
                    workspace_path = str(ws_info.path)
                except Exception as exc:
                    agent.thought_chain = f"Task assigned (no workspace: {exc}). Processing..."

            yield {
                "event": "action",
                "data": json.dumps({
                    "type": "assign",
                    "task_id": action["task_id"],
                    "task_title": action["task_title"],
                    "agent_id": action["agent_id"],
                    "agent_name": action["agent_name"],
                    "workspace_branch": workspace_branch,
                    "workspace_path": workspace_path,
                }),
            }
            results.append(f"Assigned '{action['task_title']}' → {action['agent_name']} (branch: {workspace_branch})")
            await asyncio.sleep(0.15)

        elif action["type"] == "report":
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "report",
                    "message": f"Generating summary for {action['completed_count']} completed tasks",
                }),
            }
            # Try LLM summary, fall back to static
            summary = _build_report(action)
            yield {
                "event": "action",
                "data": json.dumps({
                    "type": "report",
                    "summary": summary,
                }),
            }
            results.append("Report generated")
            await asyncio.sleep(0.1)

        elif action["type"] == "health":
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "health",
                    "message": "Running system health check",
                }),
            }
            yield {
                "event": "action",
                "data": json.dumps({
                    "type": "health",
                    **action,
                }),
            }
            results.append("Health check complete")

    # Final summary
    yield {
        "event": "done",
        "data": json.dumps({
            "action_count": len(actions),
            "results": results,
            "timestamp": _now(),
        }),
    }


def _build_report(action: dict) -> str:
    lines = [
        f"[REPORT] Execution Summary — {action['completed_count']} task(s) completed:",
    ]
    for title in action.get("tasks", []):
        lines.append(f"  ✓ {title}")
    lines.append("All objectives fulfilled. System standing by for new directives.")
    return "\n".join(lines)


# ─── Endpoint ───

@router.post("/stream")
async def invoke_stream(command: str | None = None):
    """SSE streaming invoke — analyses state, plans, executes, reports.

    Query param `command` is optional; if provided, it takes priority
    and is routed through the LangGraph pipeline.
    """
    state = _analyze_state()
    actions = _plan_actions(state, command)

    async def event_generator():
        # Opening event with analysis
        yield {
            "event": "analysis",
            "data": json.dumps({
                "agents_total": len(state["agents"]),
                "agents_idle": len(state["idle_agents"]),
                "agents_running": len(state["running_agents"]),
                "agents_error": len(state["error_agents"]),
                "tasks_unassigned": len(state["unassigned"]),
                "tasks_in_progress": len(state["in_progress"]),
                "tasks_completed": len(state["completed"]),
                "planned_actions": len(actions),
                "action_types": [a["type"] for a in actions],
            }),
        }
        await asyncio.sleep(0.05)

        # Execute actions
        async for event in _execute_actions(actions, state):
            yield event

    return EventSourceResponse(event_generator())


@router.post("")
async def invoke_sync(command: str | None = None):
    """Synchronous invoke — analyses, plans, executes, returns full result."""
    state = _analyze_state()
    actions = _plan_actions(state, command)
    results: list[dict] = []

    for action in actions:
        if action["type"] == "command":
            try:
                result = await run_graph(action["command"])
                results.append({
                    "type": "command",
                    "routed_to": result.routed_to,
                    "answer": result.answer,
                })
            except Exception as exc:
                results.append({"type": "command", "error": str(exc)})

        elif action["type"] == "retry":
            agent = _agents.get(action["agent_id"])
            if agent:
                agent.status = AgentStatus.running
                agent.thought_chain = "Auto-retry initiated by INVOKE sync."
                agent.progress.current = 0
            results.append({"type": "retry", **action})

        elif action["type"] == "assign":
            task = _tasks.get(action["task_id"])
            agent = _agents.get(action["agent_id"])
            ws_branch = None
            if task and agent:
                task.status = TaskStatus.assigned
                task.assigned_agent_id = action["agent_id"]
                agent.status = AgentStatus.running
                try:
                    ws_info = await ws_provision(action["agent_id"], action["task_id"])
                    agent.workspace = AgentWorkspace(
                        branch=ws_info.branch, path=str(ws_info.path),
                        status="active", task_id=ws_info.task_id,
                    )
                    ws_branch = ws_info.branch
                    agent.thought_chain = f"Workspace ready: {ws_info.branch}. Processing..."
                except Exception:
                    agent.thought_chain = f"Task assigned: {task.title}. Processing..."
            results.append({**action, "type": "assign", "workspace_branch": ws_branch})

        elif action["type"] == "report":
            results.append({"type": "report", "summary": _build_report(action)})

        elif action["type"] == "health":
            results.append({"type": "health", **action})

    return {
        "action_count": len(actions),
        "results": results,
        "timestamp": _now(),
    }
