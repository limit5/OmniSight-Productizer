"""INVOKE (Singularity Sync) — context-aware global orchestration endpoint.

Analyses current system state (agents, tasks) and automatically determines
the highest-value action to perform. Streams progress via SSE.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from backend.models import InvokeHaltResponse

from backend.agents.graph import run_graph
from backend.routers.agents import _agents, _persist as _persist_agent
from backend.routers.tasks import _tasks, _persist as _persist_task
from backend.models import AgentStatus, AgentWorkspace, Task, TaskStatus
from backend.workspace import provision as ws_provision, get_workspace
from backend.handoff import load_handoff_for_task
from fastapi.responses import JSONResponse

from backend.events import emit_agent_update, emit_invoke

router = APIRouter(prefix="/invoke", tags=["invoke"])

# Concurrency guard — Phase 47A replaces the single-slot lock with a
# mode-aware semaphore owned by decision_engine. In Manual/Supervised mode
# the cap is still 1-2 (legacy behavior); FullAuto=4 / Turbo=8 unlock real
# parallelism. The module-level `_invoke_lock` remains for backward compat
# (tests and `.locked()` polling) but is now a degenerate semaphore-backed
# facade that checks the real budget lazily.
_invoke_lock = asyncio.Lock()  # kept for legacy `.locked()` callers


def _invoke_slot():
    """Acquire an INVOKE concurrency slot scaled by OperationMode."""
    from backend import decision_engine as _de
    return _de.parallel_slot()

# Halt flag — checked between actions to support emergency stop
# _running: set() = system running (not halted), clear() = halted
_running = asyncio.Event()
_running.set()  # starts in running state

logger = logging.getLogger(__name__)

# Background task registry for watchdog
_running_tasks: dict[str, tuple[asyncio.Task, float]] = {}  # agent_id → (task_handle, start_time)
TASK_TIMEOUT = 1800  # 30 minutes

# Phase 47B fix ③: per-agent ring buffer of recent error keys the watchdog
# can inspect. LangGraph GraphState isn't reachable from outside the node,
# so error_check_node publishes here. Capped at 10 entries per agent.
_agent_error_history: dict[str, list[str]] = {}
_AGENT_ERR_HIST_MAX = 10


def record_agent_error(agent_id: str, error_key: str) -> None:
    """Called by graph/nodes when an agent hits an error. Trim to window."""
    if not agent_id or not error_key:
        return
    buf = _agent_error_history.setdefault(agent_id, [])
    buf.append(error_key)
    if len(buf) > _AGENT_ERR_HIST_MAX:
        del buf[: len(buf) - _AGENT_ERR_HIST_MAX]


def clear_agent_error_history(agent_id: str) -> None:
    _agent_error_history.pop(agent_id, None)

# Lock to prevent watchdog and request handlers from modifying _agents/_tasks concurrently
_state_lock = asyncio.Lock()


async def run_watchdog():
    """Periodic scan for stuck background tasks and stale assignments."""
    import time as _time
    from backend import stuck_detector as _stuck
    from backend import decision_engine as _de
    # De-dupe: don't propose the same (agent_id, reason) again while an
    # earlier proposal is still pending.
    _open_proposals: dict[tuple[str, str], str] = {}
    _executed_proposals: set[str] = set()  # N9/②: avoid double-executing
    while True:
        await asyncio.sleep(60)
        # N9: when the system is halted, skip the stuck pass entirely —
        # proposals pile up with no executor able to act on them.
        if not _running.is_set():
            continue
        now = _time.time()
        async with _state_lock:
            # Phase 47B: stuck-agent detection BEFORE hard cancellation, so
            # full_auto/turbo modes can try a switch_model / spawn_alternate
            # remediation ahead of the 30-min timeout axe.
            for agent_id, (_, started) in list(_running_tasks.items()):
                if now - started <= 120:
                    continue  # too young to be stuck
                agent = _agents.get(agent_id)
                # Fix ③: read the ring buffer published by error_check_node
                err_hist = list(_agent_error_history.get(agent_id, []))
                signal = _stuck.analyze_agent(
                    agent_id,
                    error_history=err_hist,
                    retry_count=0,
                    started_at=started,
                    task_id=getattr(agent, "current_task_id", None) if agent else None,
                    now=now,
                )
                if signal is None:
                    continue
                key = (agent_id, signal.reason.value)
                # Skip if a prior proposal is still pending
                prior_id = _open_proposals.get(key)
                if prior_id:
                    try:
                        from backend import decision_engine as _de
                        prior = _de.get(prior_id)
                        if prior and prior.status.value == "pending":
                            continue
                    except Exception:
                        pass
                try:
                    dec = _stuck.propose_remediation(signal)
                    _open_proposals[key] = dec.id
                    # N11: log only identifiers + enums; the decision `title`
                    # / `detail` can carry task text (not a secret, but still
                    # noisy in centralised log stores). Structured fields
                    # only.
                    logger.info(
                        "[STUCK] agent=%s reason=%s strategy=%s decision=%s status=%s",
                        agent_id, signal.reason.value,
                        signal.suggested_strategy.value,
                        dec.id, dec.status.value,
                    )
                    # Fix ②: if DecisionEngine auto-executed in full_auto /
                    # turbo, actually apply the remediation. Otherwise the
                    # decision sits logged but inert.
                    if dec.status == _de.DecisionStatus.auto_executed and dec.id not in _executed_proposals:
                        _executed_proposals.add(dec.id)
                        try:
                            await _apply_stuck_remediation(agent_id, signal, dec.chosen_option_id or "")
                        except Exception as exc_e:
                            logger.warning("[STUCK] apply remediation failed: %s", exc_e)
                except Exception as exc:
                    logger.warning("[STUCK] proposal failed: %s", exc)

            # Catch up on decisions the user approved manually since last tick
            # and apply their remediation (fix ② for manual-mode path).
            try:
                from backend import decision_engine as _de_mod
                for d in _de_mod.list_history(limit=50):
                    if (d.kind.startswith("stuck/")
                        and d.id not in _executed_proposals
                        and d.status in (_de_mod.DecisionStatus.approved, _de_mod.DecisionStatus.auto_executed)):
                        _executed_proposals.add(d.id)
                        src_agent = d.source.get("agent_id") or ""
                        src_reason = d.source.get("reason") or ""
                        if not src_agent:
                            continue
                        # Reconstruct a minimal signal for the executor
                        fake_sig = _stuck.StuckSignal(
                            agent_id=src_agent, task_id=d.source.get("task_id"),
                            reason=_stuck.StuckReason(src_reason) if src_reason else _stuck.StuckReason.repeat_error,
                            suggested_strategy=_stuck.Strategy(d.chosen_option_id)
                                if d.chosen_option_id in {s.value for s in _stuck.Strategy}
                                else _stuck.Strategy.retry_same,
                            detail="", source=d.source,
                        )
                        try:
                            await _apply_stuck_remediation(src_agent, fake_sig, d.chosen_option_id or "")
                        except Exception as exc_e:
                            logger.warning("[STUCK] apply remediation (approved) failed: %s", exc_e)
            except Exception as exc:
                logger.debug("[STUCK] history scan failed: %s", exc)

            # Check background tasks
            for agent_id, (task_handle, started) in list(_running_tasks.items()):
                if now - started > TASK_TIMEOUT:
                    logger.warning("[WATCHDOG] Agent %s timed out after %ds — cancelling", agent_id, TASK_TIMEOUT)
                    task_handle.cancel()
                    agent = _agents.get(agent_id)
                    if agent:
                        agent.status = AgentStatus.error
                        agent.thought_chain = f"[WATCHDOG] Task timed out after {TASK_TIMEOUT}s"
                        try:
                            await _persist_agent(agent)
                        except Exception as exc:
                            logger.warning("[WATCHDOG] persist agent %s failed: %s", agent_id, exc)
                        emit_agent_update(agent_id, "error", agent.thought_chain)
                    _running_tasks.pop(agent_id, None)
            # Check tasks stuck in assigned/in_progress > 2 hours
            for t in list(_tasks.values()):
                if t.status in (TaskStatus.assigned, TaskStatus.in_progress):
                    try:
                        # Note: uses created_at as proxy since there is no assigned_at field.
                        # Using 4h timeout to compensate for potential delay between creation and assignment.
                        created = datetime.fromisoformat(t.created_at)
                        if (datetime.now() - created).total_seconds() > 14400:
                            t.status = TaskStatus.blocked
                            await _persist_task(t)
                            logger.warning("[WATCHDOG] Task %s stuck > 4h, set to blocked", t.id)
                    except (ValueError, TypeError) as exc:
                        # malformed timestamp — log, don't crash watchdog
                        logger.debug("[WATCHDOG] Bad timestamp on task %s: %s", t.id, exc)
                    except Exception as exc:
                        logger.warning("[WATCHDOG] Stuck-task check failed for %s: %s", t.id, exc)

            # Dynamic reallocation: blocked tasks → reassign to better idle agent
            idle_agents = [a for a in _agents.values() if a.status == AgentStatus.idle]
            if idle_agents:
                for t in list(_tasks.values()):
                    if t.status != TaskStatus.blocked or not t.assigned_agent_id:
                        continue
                    if not idle_agents:
                        break
                    scored = [(a, _score_agent_for_task(a, t)) for a in idle_agents]
                    scored.sort(key=lambda x: -x[1])
                    best_agent, best_score = scored[0]
                    if best_score > 2:  # Must be better than base score
                        t.status = TaskStatus.backlog
                        t.assigned_agent_id = None
                        await _persist_task(t)
                        idle_agents.remove(best_agent)
                        logger.info("[WATCHDOG] Reallocated blocked task %s to backlog for reassignment", t.id)


async def _apply_stuck_remediation(agent_id: str, signal, chosen: str) -> None:
    """Execute the strategy chosen by DecisionEngine for a stuck agent.

    - switch_model: bump the provider failure mark for the agent's current
      model so the fallback chain picks a different one, then clear the
      agent's error ring buffer so the new attempt is judged on its own.
    - spawn_alternate: create a new backlog task duplicating the stuck
      agent's current task, targeted at a different agent_type so
      `select_model_for_task` picks a different route.
    - escalate: mark the agent's status=warning + emit a notification so
      a human can pick it up. No code action beyond that.
    - retry_same: clear the ring buffer so the next retry isn't flagged
      as "still stuck".
    """
    from backend.stuck_detector import Strategy
    agent = _agents.get(agent_id)
    if chosen == Strategy.switch_model.value:
        try:
            from backend.agents.llm import _record_provider_failure
            if agent and agent.ai_model and ":" in agent.ai_model:
                provider = agent.ai_model.split(":")[0]
                _record_provider_failure(provider)
        except Exception as exc:
            logger.debug("[STUCK-exec] switch_model record failure: %s", exc)
        clear_agent_error_history(agent_id)
        emit_invoke("stuck_switch_model", f"[{agent_id}] model downgraded; retry with fallback chain")
        return
    if chosen == Strategy.spawn_alternate.value:
        task_id = signal.task_id or (agent.current_task_id if agent and hasattr(agent, "current_task_id") else None)
        src = _tasks.get(task_id) if task_id else None
        if src is None:
            emit_invoke("stuck_spawn_alt", f"[{agent_id}] spawn_alternate: no source task to duplicate")
            return
        alt_id = f"alt-{_uid()}"
        try:
            from backend.models import Task, TaskStatus, TaskPriority
            alt = Task(
                id=alt_id, title=f"[ALT] {src.title}",
                description=(src.description or "") + "\n\n[spawned by stuck-detector]",
                priority=TaskPriority.high, status=TaskStatus.backlog,
                suggested_agent_type=getattr(src, "suggested_agent_type", None),
                npi_phase_id=getattr(src, "npi_phase_id", None),
                parent_task_id=src.id,
            )
            _tasks[alt_id] = alt
            await _persist_task(alt)
            emit_invoke("stuck_spawn_alt", f"[{agent_id}] spawned alt task {alt_id}")
        except Exception as exc:
            logger.warning("[STUCK-exec] spawn_alternate failed: %s", exc)
        return
    if chosen == Strategy.escalate.value:
        if agent:
            agent.status = AgentStatus.warning
            agent.thought_chain = "[STUCK] escalated to human — awaiting intervention"
            try:
                await _persist_agent(agent)
            except Exception:
                pass
            emit_agent_update(agent_id, "warning", agent.thought_chain)
        try:
            from backend.notifications import notify
            await notify("action", f"Agent {agent_id} stuck — manual intervention needed",
                         source="stuck-detector")
        except Exception:
            pass
        return
    # retry_same (or unknown): clear buffer so the retry is judged fresh
    clear_agent_error_history(agent_id)


def _now() -> str:
    return datetime.now().isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6]


# ─── Pre-fetch retrieval ───

_PREFETCH_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "for", "to", "and", "or", "in", "of", "on", "with",
    "this", "that", "it", "be", "do", "not", "can", "will", "should", "must",
})
_PREFETCH_SUFFIXES = frozenset({".c", ".h", ".cpp", ".py", ".yaml", ".yml", ".md", ".json"})
_PREFETCH_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".next", "build", ".agent_workspaces"})


async def _prefetch_codebase_context(task_text: str, workspace_path: str | None) -> str:
    """Search the codebase for files relevant to the task (retrieval subagent).

    Runs filesystem I/O in a thread to avoid blocking the event loop.
    """
    import re as _re
    from pathlib import Path

    words = _re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", task_text)
    keywords = [w.lower() for w in words if w.lower() not in _PREFETCH_STOP_WORDS][:8]
    if not keywords:
        return ""

    search_root = Path(workspace_path) if workspace_path else Path(".")
    if not search_root.is_dir():
        return ""

    pattern = _re.compile("|".join(_re.escape(kw) for kw in keywords), _re.IGNORECASE)

    def _search_sync() -> str:
        """Synchronous search — runs in thread pool."""
        matches: list[str] = []
        for fpath in search_root.rglob("*"):
            if fpath.suffix not in _PREFETCH_SUFFIXES:
                continue
            if not fpath.is_file() or fpath.stat().st_size > 256_000:
                continue
            if any(part in _PREFETCH_SKIP_DIRS for part in fpath.parts):
                continue
            try:
                text = fpath.read_text(errors="replace")
                hit_lines = [
                    (i, line.strip())
                    for i, line in enumerate(text.splitlines(), 1)
                    if pattern.search(line)
                ]
                if hit_lines:
                    rel = fpath.relative_to(search_root)
                    for line_no, line_text in hit_lines[:3]:
                        matches.append(f"{rel}:{line_no}: {line_text[:120]}")
                if len(matches) >= 30:
                    break
            except Exception:
                continue
        if not matches:
            return ""
        return f"Found {len(matches)} relevant code references:\n" + "\n".join(matches)

    # Run sync search in thread pool to avoid blocking the event loop
    return await asyncio.to_thread(_search_sync)


# ─── Background task execution ───


async def _run_agent_task(agent, task, workspace_path: str | None) -> None:
    """Execute a task through LangGraph in the background.

    Updates agent status and sub_tasks via SSE events as work progresses.
    Registered in _running_tasks for watchdog monitoring.
    """
    import time as _time
    from backend.models import SubTask

    _running_tasks[agent.id] = (asyncio.current_task(), _time.time())
    try:
        handoff_ctx = ""
        try:
            handoff_ctx = await load_handoff_for_task(task.id)
        except Exception as exc:
            logger.debug("handoff load failed for %s: %s", task.id, exc)

        task_command = f"{task.title}. {task.description or ''}"
        task_skill = ""
        try:
            from backend.prompt_loader import match_task_skill, load_task_skill
            matched = match_task_skill(task_command)
            if matched:
                task_skill = load_task_skill(matched)
                logger.info("Task skill matched: %s for task %s", matched, task.id)
        except Exception as exc:
            logger.debug("task skill match failed for %s: %s", task.id, exc)
        try:
            pre_ctx = await _prefetch_codebase_context(task_command, workspace_path)
            if pre_ctx:
                handoff_ctx = f"## Pre-Fetched Codebase Context\n\n{pre_ctx}\n\n{handoff_ctx}"
        except Exception as exc:
            logger.debug("codebase prefetch failed for %s: %s", task.id, exc)
        # Smart model routing: select best model for this task
        from backend.model_router import select_model_for_task
        agent_type_str = agent.type.value if hasattr(agent.type, "value") else str(agent.type)
        selected_model = select_model_for_task(
            agent_type=agent_type_str,
            task_text=task_command,
            agent_ai_model=agent.ai_model or "",
        )
        # Validate the selected model has API key
        if selected_model:
            from backend.agents.llm import validate_model_spec
            _v = validate_model_spec(selected_model)
            if not _v["valid"]:
                from backend.events import emit_token_warning
                emit_token_warning(
                    "warn",
                    f"Agent {agent.id} model '{selected_model}': {_v['warning']} — falling back to global provider",
                )
                logger.warning("INVOKE: %s model '%s' not available: %s", agent.id, selected_model, _v["warning"])
                selected_model = ""  # Fall back to global
        try:
            graph_result = await run_graph(
                task_command,
                workspace_path=workspace_path,
                model_name=selected_model,
                agent_sub_type=agent.sub_type or "",
                handoff_context=handoff_ctx,
                task_skill_context=task_skill,
                task_id=task.id,
            )
            agent.thought_chain = graph_result.answer[:300] if graph_result.answer else "Task complete."
            agent.status = AgentStatus.success

            # Extract sub_tasks and check for escalation actions
            if graph_result.actions:
                for act in graph_result.actions:
                    # Sub-task extraction
                    if act.detail and act.detail.startswith("{"):
                        try:
                            detail_data = json.loads(act.detail)
                            if "sub_tasks" in detail_data:
                                agent.sub_tasks = [SubTask(**st) for st in detail_data["sub_tasks"]]
                                for st in agent.sub_tasks:
                                    matching = [tr for tr in graph_result.tool_results if st.label.startswith(tr.tool_name)]
                                    if matching:
                                        st.status = "completed" if matching[0].success else "error"
                        except (json.JSONDecodeError, Exception):
                            pass
                    # Escalation: retries exhausted → notify L3
                    if act.status == "awaiting_confirmation":
                        agent.status = AgentStatus.awaiting_confirmation
                        from backend.notifications import notify as _notify
                        await _notify(
                            "action", f"Agent {agent.id} frozen — retries exhausted",
                            message=act.detail[:200] if act.detail else "Human review required.",
                            source=f"agent:{agent.id}",
                        )
        except Exception as exc:
            agent.thought_chain = f"Execution error: {exc}"
            agent.status = AgentStatus.error
            logger.error("Agent task failed: agent=%s task=%s error=%s", agent.id, task.id, exc)
            # L3 notification: agent error
            from backend.notifications import notify as _notify
            await _notify("action", f"Agent {agent.id} failed on task {task.id}",
                           message=str(exc)[:200], source=f"agent:{agent.id}")

        # Auto-finalize workspace on success (commit + collect artifacts)
        if agent.status == AgentStatus.success and workspace_path:
            try:
                from backend.workspace import finalize, get_workspace
                ws_info = get_workspace(agent.id)
                if ws_info and ws_info.status == "active":
                    await finalize(agent.id)
                    logger.info("Auto-finalized workspace for %s", agent.id)
                    # Auto-push to Gerrit if enabled
                    from backend.config import settings as _cfg
                    if _cfg.gerrit_enabled and _cfg.gerrit_ssh_host:
                        try:
                            from backend.workspace import _run
                            from pathlib import Path
                            gerrit_url = f"ssh://{_cfg.gerrit_ssh_host}:{_cfg.gerrit_ssh_port}/{_cfg.gerrit_project}"
                            rc, out, err = await _run(
                                f'git push "{gerrit_url}" HEAD:refs/for/main',
                                cwd=Path(workspace_path),
                            )
                            if rc == 0:
                                emit_invoke("gerrit_push", f"Agent {agent.id} pushed to Gerrit for review")
                            else:
                                logger.warning("Gerrit push failed for %s: %s", agent.id, err[:100])
                        except Exception as exc:
                            logger.warning("Gerrit push error: %s", exc)
            except Exception as exc:
                logger.warning("Auto-finalize failed for %s: %s", agent.id, exc)

        await _persist_agent(agent)
        # Update task status based on agent outcome
        if agent.status == AgentStatus.success:
            task.status = TaskStatus.completed
            from datetime import datetime as _dt
            task.completed_at = _dt.now().isoformat()
        elif agent.status == AgentStatus.error:
            task.status = TaskStatus.blocked
        elif agent.status == AgentStatus.awaiting_confirmation:
            task.status = TaskStatus.blocked
        await _persist_task(task)
        await _check_parent_completion(task.id)
        # Pipeline auto-advance: notify pipeline when task completes
        if agent.status == AgentStatus.success:
            try:
                from backend.pipeline import on_task_completed
                await on_task_completed(task.id, getattr(task, "npi_phase_id", None))
            except Exception:
                pass
        emit_invoke("task_complete", f"Agent {agent.id} finished task {task.id}")
    finally:
        _running_tasks.pop(agent.id, None)


# ─── Task decomposition ───

_SPLIT_PATTERNS = re.compile(
    r"(?:\band then\b|\bthen\b|之後|然後|並且|以及|接著|，然後|，接著)", re.IGNORECASE,
)

# Conjunctions that should NOT trigger a split (too ambiguous)


async def _maybe_decompose_task(task) -> list:
    """Split a compound task into sub-tasks.

    Strategy:
      1. Try LLM-based decomposition (semantic understanding)
      2. Fall back to regex splitting if LLM unavailable

    Returns a list of new child Task objects (empty if no decomposition needed).
    """
    text = f"{task.title}. {task.description or ''}"

    # Try LLM decomposition first
    parts = await _llm_decompose(text)

    # Fallback: regex splitting
    if parts is None:
        parts = _regex_decompose(text)

    if len(parts) <= 1:
        return []

    from backend.agents.nodes import _rule_based_route

    children = []
    prev_id = None
    for i, part in enumerate(parts):
        route, _ = _rule_based_route(part)
        child_id = f"{task.id}-sub{i + 1}"
        # Auto-dependency: each sub-task depends on the previous one (sequential chain)
        depends = [prev_id] if prev_id else []
        child = Task(
            id=child_id,
            title=part,
            description=f"Sub-task {i + 1}/{len(parts)} of: {task.title}",
            priority=task.priority,
            status=TaskStatus.backlog,
            suggested_agent_type=route if route != "general" else task.suggested_agent_type,
            parent_task_id=task.id,
            depends_on=depends,
        )
        children.append(child)
        prev_id = child_id

    return children


async def _llm_decompose(text: str) -> list[str] | None:
    """Use LLM to decompose a compound task into atomic sub-tasks.

    Returns:
        list[str]: sub-task titles (2+), or [] if LLM says ATOMIC (no split),
                   or None if LLM unavailable (triggers regex fallback).
    """
    try:
        from backend.agents.llm import get_llm
        llm = get_llm()
        if not llm:
            return None

        from langchain_core.messages import SystemMessage, HumanMessage
        resp = llm.invoke([
            SystemMessage(content=(
                "You are a task decomposition assistant. Given a compound task, "
                "split it into 2-5 atomic sub-tasks that can each be assigned to "
                "one specialist agent. Rules:\n"
                "- Each sub-task must be a complete, self-contained instruction\n"
                "- Preserve the original intent — do NOT add extra steps\n"
                "- If the task is already atomic (single action), return ONLY: ATOMIC\n"
                "- Output each sub-task on a separate line, numbered: 1. ... 2. ...\n"
                "- Do NOT output anything else (no explanation, no prefix)"
            )),
            HumanMessage(content=text),
        ])
        content = resp.content.strip()  # type: ignore[union-attr]

        if "ATOMIC" in content:
            return []  # LLM explicitly says task is atomic — skip regex fallback

        # Parse numbered lines
        lines = []
        for line in content.split("\n"):
            line = line.strip()
            # Match "1. ...", "2. ..." etc
            m = re.match(r"^\d+[\.\)]\s*(.+)", line)
            if m:
                lines.append(m.group(1).strip())
        return lines if len(lines) >= 2 else None

    except Exception as exc:
        logger.debug("LLM decomposition failed (falling back to regex): %s", exc)
        return None


def _regex_decompose(text: str) -> list[str]:
    """Regex-based fallback decomposition. Splits on sequential conjunctions."""
    parts = _SPLIT_PATTERNS.split(text)
    parts = [p.strip().rstrip(".").strip() for p in parts if p.strip() and len(p.strip()) > 1]
    return parts


async def _check_parent_completion(task_id: str) -> None:
    """If task has a parent, check if all siblings are done → update parent."""
    task = _tasks.get(task_id)
    if not task or not task.parent_task_id:
        return
    parent = _tasks.get(task.parent_task_id)
    if not parent:
        return

    siblings = [t for t in _tasks.values() if t.parent_task_id == parent.id]
    if not siblings:
        return
    if all(s.status == TaskStatus.completed for s in siblings):
        parent.status = TaskStatus.completed
        await _persist_task(parent)
    elif any(s.status == TaskStatus.blocked for s in siblings):
        parent.status = TaskStatus.blocked
        await _persist_task(parent)
    elif any(s.status in (TaskStatus.in_review, TaskStatus.in_progress, TaskStatus.assigned) for s in siblings):
        # At least one child still in progress — parent stays in_progress
        if parent.status != TaskStatus.in_progress:
            parent.status = TaskStatus.in_progress
            await _persist_task(parent)


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

    # Priority 2: Unassigned tasks → auto-assign to best matching idle agent
    remaining_idle = list(state["idle_agents"])

    for task in sorted(state["unassigned"], key=lambda t: _priority_rank(t.priority)):
        if not remaining_idle:
            break
        # Dependency check: skip tasks whose dependencies haven't completed
        if task.depends_on:
            deps = [(dep_id, _tasks.get(dep_id)) for dep_id in task.depends_on]
            # Missing dependency (deleted/typo) blocks the task (safe default)
            if any(t is None for _, t in deps):
                continue
            if any(t.status != TaskStatus.completed for _, t in deps if t):
                continue
        # Score all idle agents for this task, pick best
        scored = [(a, _score_agent_for_task(a, task)) for a in remaining_idle]
        scored.sort(key=lambda x: -x[1])
        best_agent, best_score = scored[0]
        if best_score > 0:
            remaining_idle.remove(best_agent)
            actions.append({
                "type": "assign",
                "task_id": task.id,
                "task_title": task.title,
                "agent_id": best_agent.id,
                "agent_name": best_agent.name,
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


def _priority_rank(priority) -> int:
    p = priority.value if hasattr(priority, "value") else str(priority)
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(p, 4)


def _score_agent_for_task(agent, task) -> int:
    """Score how well an agent matches a task. Higher is better.

    Scoring:
      - type match (10): agent.type == task.suggested_agent_type
      - sub_type keyword match (up to 5): agent's role keywords appear in task text
      - ai_model bonus (1): agent has a specific LLM configured
      - fallback (2): any agent can do any task at low priority
    """
    score = 2  # Base: any idle agent is better than none

    task_type = task.suggested_agent_type
    if task_type:
        task_type_str = task_type.value if hasattr(task_type, "value") else str(task_type)
        agent_type_str = agent.type.value if hasattr(agent.type, "value") else str(agent.type)
        if agent_type_str == task_type_str:
            score += 10

    # sub_type keyword matching
    if agent.sub_type:
        from backend.prompt_loader import get_role_keywords
        agent_type_str = agent.type.value if hasattr(agent.type, "value") else str(agent.type)
        keywords = get_role_keywords(agent_type_str, agent.sub_type)
        task_text = f"{task.title} {task.description or ''}".lower()
        hits = sum(1 for kw in keywords if kw in task_text)
        score += min(hits * 2, 5)

    # Prefer agents with explicit model configured
    if agent.ai_model:
        score += 1

    return score


# ─── Action execution ───

async def _execute_actions(actions: list[dict], state: dict):
    """Execute planned actions and yield SSE events."""
    emit_invoke("start", f"Executing {len(actions)} action(s)")
    results = []

    for action in actions:
        # Check halt flag between actions
        if not _running.is_set():
            yield {
                "event": "done",
                "data": json.dumps({
                    "action_count": len(results),
                    "results": results + ["HALTED by emergency stop"],
                    "timestamp": _now(),
                }),
            }
            return

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
                # Find a running agent to use its model/role config + handoff
                running = state.get("running_agents", [])
                _agent_ctx = running[0] if running else None
                _handoff = ""
                if _agent_ctx and _agent_ctx.workspace and _agent_ctx.workspace.task_id:
                    try:
                        _handoff = await load_handoff_for_task(_agent_ctx.workspace.task_id)
                    except Exception:
                        pass
                # Auto-match task skill for command
                _task_skill = ""
                try:
                    from backend.prompt_loader import match_task_skill, load_task_skill
                    _matched = match_task_skill(action["command"])
                    if _matched:
                        _task_skill = load_task_skill(_matched)
                except Exception:
                    pass
                # Smart model routing for stream commands
                _stream_model = ""
                if _agent_ctx:
                    from backend.model_router import select_model_for_task as _sel
                    _at = _agent_ctx.type.value if hasattr(_agent_ctx.type, "value") else str(_agent_ctx.type)
                    _stream_model = _sel(_at, action["command"], _agent_ctx.ai_model or "")
                result = await run_graph(
                    action["command"],
                    model_name=_stream_model,
                    agent_sub_type=(_agent_ctx.sub_type or "") if _agent_ctx else "",
                    handoff_context=_handoff,
                    task_skill_context=_task_skill,
                )
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
                await _persist_agent(agent)
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

                await _persist_agent(agent)
                await _persist_task(task)

            # Launch task execution in background (non-blocking SSE)
            if task and agent:
                asyncio.create_task(_run_agent_task(
                    agent=agent,
                    task=task,
                    workspace_path=workspace_path,
                ))

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
    # Phase 47A: parallelism is capped by OperationMode via a Semaphore.
    # Reject at the door only when every slot is taken AND we're in Manual
    # (preserve the old "one-at-a-time" UX for Manual). Other modes block
    # inside the generator until a slot frees up.
    from backend import decision_engine as _de
    sema = _de.parallel_slot()
    if _de.get_mode() == _de.OperationMode.manual and sema.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": "Invoke already in progress (Manual mode)"},
        )

    # Pre-step: decompose compound tasks before planning
    async with _state_lock:
        for task in list(_tasks.values()):
            if task.status == TaskStatus.backlog and not task.child_task_ids:
                children = await _maybe_decompose_task(task)
                if children:
                    for child in children:
                        _tasks[child.id] = child
                        await _persist_task(child)
                    task.child_task_ids = [c.id for c in children]
                    task.status = TaskStatus.in_progress  # Parent waits for children
                    await _persist_task(task)

    state = _analyze_state()
    actions = _plan_actions(state, command)

    async def event_generator():
        async with sema:
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


@router.post("/halt", response_model=InvokeHaltResponse)
async def invoke_halt():
    """Emergency stop — cancel background tasks, stop containers, halt INVOKE."""
    _running.clear()
    # Cancel all tracked background tasks
    cancelled = 0
    for agent_id, (task_handle, _) in list(_running_tasks.items()):
        task_handle.cancel()
        cancelled += 1
        agent = _agents.get(agent_id)
        if agent and agent.status == AgentStatus.running:
            agent.status = AgentStatus.warning
            agent.thought_chain = "[HALT] Emergency stop activated"
            try:
                await _persist_agent(agent)
            except Exception:
                pass
    _running_tasks.clear()
    # Stop all Docker containers
    containers_stopped = 0
    try:
        from backend.container import stop_all_containers
        containers_stopped = await stop_all_containers()
    except Exception as exc:
        logger.warning("[HALT] stop_all_containers failed: %s", exc)
    # Mark active pipeline as halted so it does not auto-advance after resume.
    # Without this, the in-memory pipeline state stays "running" and new task
    # completions (race during halt) silently advance phases.
    try:
        from backend import pipeline as _pipeline_mod
        if _pipeline_mod._active_pipeline and _pipeline_mod._active_pipeline.get("status") == "running":
            _pipeline_mod._active_pipeline["status"] = "halted"
            _pipeline_mod._active_pipeline["halted_at"] = datetime.now().isoformat()
            from backend.events import emit_pipeline_phase as _epp
            _epp("pipeline_halt", "Pipeline halted by /invoke/halt")
    except Exception as exc:
        logger.debug("[HALT] pipeline state update failed: %s", exc)
    emit_invoke("halt", f"INVOKE halted: {cancelled} tasks cancelled, {containers_stopped} containers stopped")
    return {"status": "halted", "tasks_cancelled": cancelled, "containers_stopped": containers_stopped}


@router.post("/resume")
async def invoke_resume():
    """Resume INVOKE after emergency stop — restores halted agents to idle."""
    _running.set()
    # Restore agents that were set to warning during halt
    restored = 0
    for agent in _agents.values():
        if agent.status == AgentStatus.warning:
            agent.status = AgentStatus.idle
            agent.thought_chain = "Resumed from emergency halt."
            emit_agent_update(agent.id, agent.status, agent.thought_chain)
            restored += 1
    emit_invoke("resume", f"INVOKE resumed, {restored} agent(s) restored to idle")
    return {"status": "resumed", "agents_restored": restored}


@router.post("")
async def invoke_sync(command: str | None = None):
    """Synchronous invoke — analyses, plans, executes, returns full result."""
    from backend import decision_engine as _de
    sema = _de.parallel_slot()
    if _de.get_mode() == _de.OperationMode.manual and sema.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": "Invoke already in progress (Manual mode)"},
        )

    async with sema:
        state = _analyze_state()
        actions = _plan_actions(state, command)
        results: list[dict] = []

        for action in actions:
            if action["type"] == "command":
                try:
                    running = state.get("running_agents", [])
                    _agent_ctx = running[0] if running else None
                    _handoff = ""
                    if _agent_ctx and _agent_ctx.workspace and _agent_ctx.workspace.task_id:
                        try:
                            _handoff = await load_handoff_for_task(_agent_ctx.workspace.task_id)
                        except Exception:
                            pass
                    _task_skill = ""
                    try:
                        from backend.prompt_loader import match_task_skill as _mts, load_task_skill as _lts
                        _m = _mts(action["command"])
                        if _m:
                            _task_skill = _lts(_m)
                    except Exception:
                        pass
                    # Smart model routing for sync commands
                    _sync_model = ""
                    if _agent_ctx:
                        from backend.model_router import select_model_for_task as _sel_sync
                        _at_sync = _agent_ctx.type.value if hasattr(_agent_ctx.type, "value") else str(_agent_ctx.type)
                        _sync_model = _sel_sync(_at_sync, action["command"], _agent_ctx.ai_model or "")
                    result = await run_graph(
                        action["command"],
                        model_name=_sync_model,
                        agent_sub_type=(_agent_ctx.sub_type or "") if _agent_ctx else "",
                        handoff_context=_handoff,
                        task_skill_context=_task_skill,
                    )
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
                    await _persist_agent(agent)
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
                    await _persist_agent(agent)
                    await _persist_task(task)
                # Launch execution in background
                if task and agent:
                    asyncio.create_task(_run_agent_task(
                        agent=agent,
                        task=task,
                        workspace_path=str(agent.workspace.path) if agent.workspace.path else None,
                    ))
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
