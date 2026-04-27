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

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from backend import auth as _auth
from backend.models import InvokeHaltResponse

from backend.agents.graph import run_graph
from backend.routers.agents import _agents, _persist as _persist_agent
from backend.routers.tasks import _tasks, _persist as _persist_task
from backend.models import AgentStatus, AgentWorkspace, Task, TaskStatus
from backend.workspace import provision as ws_provision
from backend.handoff import load_handoff_for_task
from fastapi.responses import JSONResponse

from backend.events import emit_agent_update, emit_invoke

router = APIRouter(prefix="/invoke", tags=["invoke"])

# Concurrency guard — Phase 47A replaces the single-slot lock with a
# mode-aware semaphore owned by decision_engine (full_auto=4, turbo=8).
# Removed in M-Cluster 5 audit fix (R2 #29): `_invoke_lock` had no
# remaining callers — grep confirmed only the declaration referenced
# it. Real concurrency is owned by `_invoke_slot()` below.


# Phase 67-E follow-up: platform-aware RAG gate.
def _resolve_platform_tags(workspace_path: str | None) -> tuple[str, str]:
    """Return (soc_vendor, sdk_version) for the task's workspace.

    Reads `.omnisight/platform` (the hint file agents already honour
    via get_platform_config), then pulls `vendor_id` / `sdk_version`
    from the matching platform profile YAML. Any failure path returns
    ("", "") so the downstream SDK hard-lock stays permissive — the
    gate is strictly opt-in per workspace.
    """
    if not workspace_path:
        return "", ""
    try:
        from pathlib import Path
        import yaml
        hint = Path(workspace_path) / ".omnisight" / "platform"
        if not hint.exists():
            return "", ""
        platform = hint.read_text().strip()
        if not platform:
            return "", ""
        from backend.sdk_provisioner import _validate_platform_name, _platform_profile
        if not _validate_platform_name(platform):
            return "", ""
        profile = _platform_profile(platform)
        if profile is None or not profile.exists():
            return "", ""
        data = yaml.safe_load(profile.read_text()) or {}
        return (
            str(data.get("vendor_id") or ""),
            str(data.get("sdk_version") or ""),
        )
    except Exception:
        return "", ""


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
# Phase 52: last successful watchdog pass (epoch seconds). The
# /healthz endpoint reads this to surface "is the watchdog stuck?".
_watchdog_last_tick: float = 0.0
# R2-#20: ring buffer is mutated by sync error-publisher callbacks and
# iterated by the async watchdog; guard with a threading.Lock so we don't
# race on list length during trim-and-append.
import threading as _threading
_agent_error_history_lock = _threading.Lock()


def record_agent_error(agent_id: str, error_key: str) -> None:
    """Called by graph/nodes when an agent hits an error. Trim to window."""
    if not agent_id or not error_key:
        return
    with _agent_error_history_lock:
        buf = _agent_error_history.setdefault(agent_id, [])
        buf.append(error_key)
        if len(buf) > _AGENT_ERR_HIST_MAX:
            del buf[: len(buf) - _AGENT_ERR_HIST_MAX]


def clear_agent_error_history(agent_id: str) -> None:
    with _agent_error_history_lock:
        _agent_error_history.pop(agent_id, None)


def _snapshot_agent_errors(agent_id: str) -> list[str]:
    """Thread-safe snapshot for watchdog iteration."""
    with _agent_error_history_lock:
        return list(_agent_error_history.get(agent_id, []))

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
    global _watchdog_last_tick
    while True:
        await asyncio.sleep(60)
        # Fix-A S7: only publish the tick AFTER the stuck-detection pass
        # completes, so a hung detector shows up as watchdog-age growth
        # in /healthz instead of being masked by a fresh tick at loop top.
        # Halted state still bumps the tick (idle ≠ stuck).
        if not _running.is_set():
            _watchdog_last_tick = _time.time()
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
                err_hist = _snapshot_agent_errors(agent_id)
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

        # Fix-A S7: publish tick only after a full pass completes so
        # /healthz watchdog-age reflects real liveness, not loop entry.
        _watchdog_last_tick = _time.time()


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
    if chosen == Strategy.hibernate_and_wait.value:
        # Phase 47-Fix Batch E: docker pause the container; preserve
        # worktree state. Operator (or auto-resume in higher modes)
        # can `docker unpause` to continue.
        try:
            from backend.container import pause_container
            paused = await pause_container(agent_id)
        except Exception as exc:
            logger.warning("[STUCK-exec] hibernate failed for %s: %s", agent_id, exc)
            paused = False
        if agent:
            agent.status = AgentStatus.idle
            agent.thought_chain = (
                "[STUCK] hibernated (container paused) — resume any time"
                if paused
                else "[STUCK] hibernate requested but container not paused"
            )
            try:
                await _persist_agent(agent)
            except Exception:
                pass
            emit_agent_update(agent_id, "idle", agent.thought_chain)
        emit_invoke("stuck_hibernate", f"[{agent_id}] container paused={paused}")
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
        # Phase 67-E follow-up: resolve platform tags for the sandbox
        # RAG pre-fetch SDK hard-lock. Reads the workspace's
        # `.omnisight/platform` hint and pulls vendor/sdk from the
        # profile YAML. Empty strings when the workspace is not
        # platform-tagged — downstream gate stays permissive.
        soc_vendor, sdk_version = _resolve_platform_tags(workspace_path)
        try:
            graph_result = await run_graph(
                task_command,
                workspace_path=workspace_path,
                model_name=selected_model,
                agent_sub_type=agent.sub_type or "",
                handoff_context=handoff_ctx,
                task_skill_context=task_skill,
                task_id=task.id,
                soc_vendor=soc_vendor,
                sdk_version=sdk_version,
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
                    # Auto-push to Gerrit if enabled.
                    #
                    # Phase 5-7 (#multi-account-forge): the SSH host /
                    # port / project come from the resolved
                    # ``git_accounts(platform='gerrit')`` row instead of
                    # ``settings.gerrit_*`` scalars, so operator-added
                    # accounts are honoured. Falls back to the legacy
                    # shim's ``default-gerrit`` virtual row when the
                    # table is empty (preserves single-instance
                    # behaviour pre-5-5 auto-migration).
                    from backend.config import settings as _cfg
                    if _cfg.gerrit_enabled:
                        try:
                            from backend.git_credentials import pick_default
                            account = await pick_default("gerrit")
                            ssh_host = (account or {}).get("ssh_host") or ""
                            ssh_port = int((account or {}).get("ssh_port") or 0) or 29418
                            project = ((account or {}).get("project") or "").strip()
                            if ssh_host and project:
                                from backend.workspace import _run
                                from pathlib import Path
                                gerrit_url = f"ssh://{ssh_host}:{ssh_port}/{project}"
                                rc, out, err = await _run(
                                    f'git push "{gerrit_url}" HEAD:refs/for/main',
                                    cwd=Path(workspace_path),
                                )
                                if rc == 0:
                                    emit_invoke("gerrit_push", f"Agent {agent.id} pushed to Gerrit for review")
                                else:
                                    logger.warning("Gerrit push failed for %s: %s", agent.id, err[:100])
                            else:
                                logger.debug(
                                    "Gerrit auto-push skipped for %s: no ssh_host/project on default account",
                                    agent.id,
                                )
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

        from backend.llm_adapter import SystemMessage, HumanMessage
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


# R20-B (2026-04-25): orchestrator coaching for empty / pending-only
# states. When INVOKE fires with no command and the planner has nothing
# real to do (priorities 1-3 yield zero actions), we used to fall back
# to a `[health]` action that just echoed agent/task counts. Operators
# reported that when the workspace is empty (or there are stale PEP
# HOLDs and nothing else), the system should *coach* them on what to
# do next — using the lead-architect/orchestrator persona that already
# exists in the LangGraph pipeline. Coach replaces health in the
# priority-4 slot. If priorities 1-3 produced real work (assigns,
# retries, reports), we don't coach — the operator can see what's
# happening from the action stream itself.

# BS.10.1 (2026-04-27): lazy install coach hook. When the operator's
# command (or any backlog/in-progress task text) hints at a toolchain
# the tenant hasn't installed yet, the planner emits one
# ``missing_toolchain:<slug>`` trigger per missing entry so the coach
# can deeplink to ``Settings → Platforms?entry=<slug>`` and prompt the
# operator to install it instead of letting the agent fail at compile
# time.
#
# Keyword map is keyed by the canonical catalog ``entry_id`` (locked to
# ``BOOTSTRAP_VERTICAL_PRIMARY_ENTRY`` in ``lib/api.ts`` so frontend +
# backend stay in lock-step). Keywords are matched case-insensitively
# as substrings against the combined text corpus (recent command +
# pending-task title/description + running-agent thought_chain). Adding
# a new toolchain row is a code change — we deliberately avoid pulling
# the catalog at runtime so the coach doesn't gain a hard dependency on
# PG availability for the "what could the operator install?" half of
# the trigger (the "what HAS the operator installed?" half still needs
# PG, but failure there degrades to "no missing_toolchain triggers
# emitted" rather than crashing the whole planner).
#
# Module-global state audit (per docs/sop/implement_phase_step.md
# Step 1): ``_TOOLCHAIN_KEYWORD_MAP`` is a module-level frozen mapping
# — every uvicorn worker derives the same value from source code, no
# cross-worker coordination needed (Answer #1).
_TOOLCHAIN_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "android-sdk-platform-tools": (
        "android", "adb", "fastboot", "apk", "aab", "android sdk",
        "android studio", "google play",
    ),
    "espressif-esp-idf-v5": (
        "esp32", "esp-idf", "espressif", "esp idf", "xtensa", "esp8266",
        "esp32-s3", "esp32-c3",
    ),
    "nodejs-lts-20": (
        "node.js", "nodejs", "node js", "npm", "yarn", "pnpm",
        "react", "next.js", "nextjs", "vite", "typescript",
    ),
    "python-uv": (
        "python", "pip ", "pip install", "uv pip", "venv", "pyproject",
        "poetry", "pytest",
    ),
    "arm-gnu-toolchain-13": (
        "arm-none-eabi", "cross-compile arm", "cross compile arm",
        "stm32", "cortex-m", "cortex m", "gcc-arm", "gcc arm",
        "arm gnu toolchain",
    ),
}


def _collect_toolchain_hints(text: str) -> set[str]:
    """Return catalog ``entry_id`` slugs hinted at by *text*.

    Pure helper — case-insensitive substring scan over
    :data:`_TOOLCHAIN_KEYWORD_MAP`. Empty / whitespace-only input
    returns an empty set so callers can pipe arbitrary corpora through
    without pre-filtering.
    """
    if not text:
        return set()
    haystack = text.lower()
    hits: set[str] = set()
    for slug, keywords in _TOOLCHAIN_KEYWORD_MAP.items():
        for kw in keywords:
            if kw in haystack:
                hits.add(slug)
                break
    return hits


def _build_coach_text_corpus(state: dict, command: str | None) -> str:
    """Concatenate the operator's *current conversation* + *expected to
    run* task surfaces into one lower-cased string for keyword scoring.

    Sources, in priority order:
      * the live INVOKE ``command`` (if any) — direct operator intent
      * backlog / assigned / in-progress task titles + descriptions
      * running agents' ``thought_chain`` (often quotes the task command
        or describes the next planned step)

    Completed / blocked tasks are excluded — the trigger is for work the
    operator is *about to* run, not historical work. Truncated to a
    sensible cap so a runaway description can't hog the keyword scan.
    """
    parts: list[str] = []
    if command:
        parts.append(command)
    for t in state.get("tasks") or []:
        try:
            status = t.status
            status_val = status.value if hasattr(status, "value") else str(status)
        except Exception:
            status_val = ""
        if status_val not in {"backlog", "assigned", "in_progress"}:
            continue
        title = getattr(t, "title", "") or ""
        desc = getattr(t, "description", "") or ""
        if title:
            parts.append(title)
        if desc:
            parts.append(desc)
    for agent in state.get("running_agents") or []:
        chain = getattr(agent, "thought_chain", "") or ""
        if chain:
            parts.append(chain)
    blob = " ".join(parts)
    if len(blob) > 8_000:
        blob = blob[:8_000]
    return blob


async def _load_installed_entry_ids(tenant_id: str) -> frozenset[str]:
    """Return the set of catalog ``entry_id`` values currently installed
    for *tenant_id* (latest install_jobs row per entry is
    ``state='completed'`` AND not an uninstall record).

    Mirrors the filter used by ``GET /installer/installed`` so the coach
    and the InstalledTab agree on what "installed" means. Errors (PG
    unreachable, missing column, schema drift) degrade to an empty set
    — the coach simply will not emit ``missing_toolchain`` triggers
    rather than 500-ing the whole planner.

    Module-global / cross-worker state audit: pure SELECT scoped by
    tenant_id; multi-worker safe via PG MVCC (Answer #2).

    Read-after-write timing: a freshly committed install_jobs row is
    visible to the next worker by PG snapshot isolation, so the operator
    finishing an install via the BS.7 drawer immediately stops seeing
    the matching ``missing_toolchain`` coaching card on the next INVOKE.
    """
    if not tenant_id:
        return frozenset()
    sql = """
        SELECT DISTINCT ON (entry_id)
            entry_id, state, result_json
        FROM install_jobs
        WHERE tenant_id = $1
        ORDER BY entry_id, queued_at DESC
    """
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(sql, tenant_id)
    except Exception as exc:
        logger.debug("[BS.10.1] installed-entries load failed: %s", exc)
        return frozenset()
    installed: set[str] = set()
    for row in rows:
        try:
            if row["state"] != "completed":
                continue
            payload = row["result_json"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    payload = None
            if isinstance(payload, dict) and payload.get("kind") == "uninstall":
                continue
            installed.add(str(row["entry_id"]))
        except Exception:
            continue
    return frozenset(installed)


def _detect_coaching_triggers(
    state: dict, suppress: frozenset[str],
    *, command: str | None = None,
) -> tuple[list[str], int]:
    """Return ``(trigger_keys, pending_count)`` for the orchestrator coach.

    Trigger keys are short tokens the prompt builder maps to operator-
    facing language. ``suppress`` lets the frontend tell the planner
    "I already showed coaching for X this session" so the operator
    isn't re-coached on every INVOKE press (frontend tracks via
    sessionStorage; see ``hooks/use-engine.ts`` invoke()).

    BS.10.1 — also emits one ``missing_toolchain:<slug>`` trigger per
    catalog entry the *current conversation* (``command``) or any
    *expected-to-run* task hints at but the tenant has not installed
    yet. ``state["installed_entries"]`` is the upstream-supplied
    ``frozenset[str]`` of installed catalog ``entry_id`` values
    (typically loaded via :func:`_load_installed_entry_ids`); when
    absent the missing-toolchain detection is a no-op so unit tests can
    drive the function without a PG round-trip.
    """
    triggers: list[str] = []
    if (not state["agents"] and not state["tasks"]
            and "empty_workspace" not in suppress):
        triggers.append("empty_workspace")
    pending_count = 0
    try:
        from backend import decision_engine as _de
        pending_count = len(_de.list_pending())
    except Exception:
        pass
    if pending_count > 0 and "stale_pep" not in suppress:
        triggers.append("stale_pep")

    # BS.10.1 — missing toolchain detection. ``installed_entries`` is
    # supplied by the caller (the INVOKE endpoint pre-loads it from
    # ``install_jobs`` so the planner stays sync-pure). Skipped entirely
    # when the upstream did not provide it (test isolation, sync code
    # paths) so existing behaviour is unchanged.
    installed_entries = state.get("installed_entries")
    if installed_entries is not None:
        corpus = _build_coach_text_corpus(state, command)
        hinted = _collect_toolchain_hints(corpus)
        for slug in sorted(hinted):
            if slug in installed_entries:
                continue
            key = f"missing_toolchain:{slug}"
            if key in suppress:
                continue
            triggers.append(key)
    return triggers, pending_count


def _plan_actions(
    state: dict, command: str | None,
    *, suppress_coach: frozenset[str] = frozenset(),
) -> list[dict]:
    """Decide what actions to take based on current state.

    Returns a list of action dicts, each with:
      - type: "assign" | "retry" | "report" | "health" | "command" | "coach"
      - detail fields depending on type

    R20-B: when the planner would otherwise return only ``[health]``
    (i.e. nothing real to do), it instead emits ``[coach]`` if the
    orchestrator has something to say (empty workspace / stale PEP
    HOLDs). ``suppress_coach`` is the set of trigger keys the operator
    has already been coached about in this session.
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

    # Priority 4: Nothing to do → coach (if orchestrator has something to
    # say) or fall back to a passive health check. R20-B replaced the
    # bare health echo with an orchestrator-led coaching step when the
    # workspace is empty / has stale PEP HOLDs. BS.10.1 forwards the
    # operator's ``command`` so the missing-toolchain detector can see
    # the live INVOKE intent in addition to backlog task text.
    if not actions:
        triggers, pending_count = _detect_coaching_triggers(
            state, suppress_coach, command=command,
        )
        if triggers:
            actions.append({
                "type": "coach",
                "triggers": triggers,
                "pending_count": pending_count,
                "agent_count": len(state["agents"]),
                "task_count": len(state["tasks"]),
            })
        else:
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
                        anchor_sha=ws_info.anchor_sha,
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

        elif action["type"] == "coach":
            # R20-B: orchestrator-led coaching for empty / stale-PEP states.
            yield {
                "event": "phase",
                "data": json.dumps({
                    "phase": "coach",
                    "message": "Orchestrator coaching mode",
                }),
            }
            coach_msg = await _generate_coach_message(action)
            yield {
                "event": "action",
                "data": json.dumps({
                    "type": "coach",
                    "message": coach_msg,
                    "triggers": list(action.get("triggers") or []),
                    "pending_count": int(action.get("pending_count") or 0),
                }),
            }
            results.append("Orchestrator coached operator")

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


# R20-B (2026-04-25): orchestrator coach prompt + content generation.
#
# When the planner emits a ``coach`` action (priority-4 fallback when
# the workspace is empty or there are stale PEP HOLDs), we route the
# trigger context through the orchestrator persona to produce a short,
# action-oriented message. The LLM call is bounded (single round-trip,
# no tool use) and falls back to a hard-coded templated message when
# the LLM is unavailable / fails / returns empty — operators in zero-
# credit / offline-LLM environments still get useful guidance instead
# of a bare [HEALTH] echo.
_COACH_SYSTEM_PROMPT = """You are the OmniSight Orchestrator — the lead-architect / coordinator persona of an embedded AI camera development platform.

The operator just pressed INVOKE but the system is in a state that needs guidance from you. Your job:
1. Acknowledge what you see in 1 short sentence (friendly, slightly playful, never condescending).
2. Offer 2-3 SPECIFIC, ACTIONABLE next steps as a tight markdown list (each item one short line).

Triggers tell you what to coach about. Translate them to operator-facing language — never repeat the trigger key verbatim:
- empty_workspace: 0 agents / 0 tasks. Suggest: ` + AGENT ` button, `/help`, `/tour`, or "tell me what you're building and I'll route it".
- stale_pep:N: there are N PEP HOLD decisions waiting from earlier. Suggest: review them via the bottom-right toasts (each has a WHY? button now), or APPROVE / REJECT in bulk.
- missing_toolchain:<entry-id>: the operator's INVOKE command (and/or the backlog tasks they queued) will need a vendor toolchain that this machine has not installed yet. The context block hands you the human display name (e.g. "Android SDK Platform Tools", "ESP-IDF v5", "Node.js LTS 20", "Python toolchain (uv)", "ARM GNU Toolchain 13"), a one-line hint about what the toolchain is for, and a one-click install URL of shape `/settings/platforms?entry=<entry-id>`. Surface each missing toolchain as its own bullet, render the install URL as a markdown link with a bilingual action label like `[安裝 / Install](url)` so a CJK or English operator both see a clear CTA, and ALWAYS use the display name — never paste the slug verbatim.

Trigger priority when several co-fire:
- `missing_toolchain` always leads. The operator already declared intent by typing the command, so install-first-then-run is the productive path; SKIP the `empty_workspace` framing entirely whenever any `missing_toolchain` is present.
- If `stale_pep` co-fires with `missing_toolchain`, mention pending PEPs as ONE short reminder line at the end (not a full sub-list) — the toolchain install is the headline.
- When only `empty_workspace` and `stale_pep` co-fire, lead with the PEP queue (it's already-started work) and offer the empty-workspace prompts as the secondary nudge.

Match the operator's recent message language (CJK or English; default CJK if no recent operator messages). Do not apologise, do not over-explain, do not repeat what's already in the toast — your job is meta-narration + action prompts. Keep total length under 6 lines."""


def _build_coach_context(triggers: list[str], pending_count: int) -> str:
    """LLM context block. Each trigger is translated to a one-line
    operator-facing description so the LLM never has to guess what the
    raw key means.

    BS.10.3 — ``missing_toolchain:<slug>`` triggers carry a human display
    name + hint + install URL, so the LLM can render the markdown link
    described in ``_COACH_SYSTEM_PROMPT`` without echoing the slug. Slugs
    not present in ``_TOOLCHAIN_DISPLAY`` (drift / future entries) fall
    back to the slug as both name and hint — the module-import-time
    drift assert at the bottom of this file pushes that case to CI red,
    so reaching it in prod implies an emergency hotfix.
    """
    parts = ["Triggers detected by the planner:"]
    for t in triggers:
        if t == "empty_workspace":
            parts.append("- empty_workspace: workspace has 0 agents and 0 tasks")
        elif t == "stale_pep":
            parts.append(
                f"- stale_pep: {pending_count} PEP HOLD "
                f"decision{'s' if pending_count != 1 else ''} "
                "waiting for operator approve/reject"
            )
        elif t.startswith("missing_toolchain:"):
            slug = t.split(":", 1)[1]
            name, hint = _TOOLCHAIN_DISPLAY.get(slug, (slug, "toolchain"))
            url = _toolchain_install_url(slug)
            parts.append(
                f"- missing_toolchain: operator's queued work needs "
                f"**{name}** ({hint}); not installed on this machine. "
                f"One-click install URL: {url}"
            )
        else:
            parts.append(f"- {t}")
    return "\n".join(parts)


# BS.10.2: human-friendly display labels per ``_TOOLCHAIN_KEYWORD_MAP``
# slug. Used by :func:`_build_templated_coach_message` to render an
# actionable bullet for each ``missing_toolchain:<slug>`` trigger. Tuple
# is ``(name, hint)`` where *name* is the headline label (mirrors the
# catalog display name; English so a Chinese-speaking operator can paste
# it straight into a search) and *hint* a one-line "what is this for"
# helper so the operator does not need to leave the chat to guess.
#
# Module-global state audit (per docs/sop/implement_phase_step.md
# Step 1): module-level frozen mapping — every uvicorn worker derives
# the same value from source code (Answer #1, per-worker stateless
# derivation). Keys must stay aligned with ``_TOOLCHAIN_KEYWORD_MAP``;
# the inline drift check at module bottom (BS.10.2) raises at import
# time if a slug is missing here so a future toolchain row added to
# ``_TOOLCHAIN_KEYWORD_MAP`` cannot silently render as "<slug> /
# toolchain".
_TOOLCHAIN_DISPLAY: dict[str, tuple[str, str]] = {
    "android-sdk-platform-tools": (
        "Android SDK Platform Tools",
        "adb / fastboot / Android API",
    ),
    "espressif-esp-idf-v5": (
        "ESP-IDF v5",
        "Espressif ESP32 / ESP8266 SDK",
    ),
    "nodejs-lts-20": (
        "Node.js LTS 20",
        "npm / pnpm / yarn / TypeScript",
    ),
    "python-uv": (
        "Python toolchain (uv)",
        "uv pip / venv / pytest",
    ),
    "arm-gnu-toolchain-13": (
        "ARM GNU Toolchain 13",
        "arm-none-eabi-gcc / Cortex-M cross-compile",
    ),
}


def _missing_toolchain_slugs(triggers: list[str]) -> list[str]:
    """Extract entry-id slugs from ``missing_toolchain:<slug>`` triggers.

    Pure helper. Order is preserved — the planner emits sorted triggers
    (see :func:`_detect_coaching_triggers`) so the rendered message reads
    identically across runs and is stable for the BS.10.5 contract test.
    """
    out: list[str] = []
    for t in triggers:
        if not t.startswith("missing_toolchain:"):
            continue
        slug = t.split(":", 1)[1]
        if slug:
            out.append(slug)
    return out


def _toolchain_install_url(slug: str) -> str:
    """BS.10.4 deeplink — `Settings → Platforms` with the ``entry`` query
    param pre-filled. Slug is locked to ``_TOOLCHAIN_KEYWORD_MAP`` keys
    (catalog ``entry_id`` values) so frontend / backend stay in lock-step.
    """
    return f"/settings/platforms?entry={slug}"


def _build_templated_coach_message(
    triggers: list[str], pending_count: int,
) -> str:
    """LLM-unavailable fallback. CJK-default to match the operator base
    with bilingual action labels (``安裝 / Install``) so an English-only
    operator still has a clear call-to-action.

    Hard-coded but still vastly better than ``[HEALTH] check complete``.
    Phrasing mirrors what the LLM would produce so the UX stays
    consistent across LLM-on / LLM-off environments.

    BS.10.2 — recognises ``missing_toolchain:<slug>`` triggers and emits
    one bullet per missing entry with a deeplink to
    ``/settings/platforms?entry=<slug>`` (handled by BS.10.4). The
    missing-toolchain banner takes priority over the legacy
    ``empty_workspace`` / ``stale_pep`` branches because a toolchain gap
    is the most specific blocker in front of the operator's intended
    work — install-first-then-run is the productive path.
    """
    has_empty = "empty_workspace" in triggers
    has_pep = "stale_pep" in triggers
    missing_slugs = _missing_toolchain_slugs(triggers)
    lines: list[str] = []
    if missing_slugs:
        # Banner phrasing differs slightly for single vs many — a 1-of-1
        # install gets a pointed sentence; an N-of-N install gets a
        # summary-then-list so the operator sees the full scope before
        # committing.
        if len(missing_slugs) == 1:
            slug = missing_slugs[0]
            name, hint = _TOOLCHAIN_DISPLAY.get(slug, (slug, "toolchain"))
            lines.append(
                "看起來你接下來要跑的工作會用到 "
                f"**{name}** ({hint})，但這台機器還沒裝過 — 先裝再跑會比較順。"
            )
            lines.append(
                f"- 一鍵安裝 / Install **{name}**: "
                f"[Settings → Platforms]({_toolchain_install_url(slug)})"
            )
        else:
            lines.append(
                f"接下來要跑的工作會用到 {len(missing_slugs)} 個 toolchain，"
                "但這台機器都還沒裝 — 先裝再跑會比較順。"
            )
            for slug in missing_slugs:
                name, hint = _TOOLCHAIN_DISPLAY.get(slug, (slug, "toolchain"))
                lines.append(
                    f"- {name} ({hint}): "
                    f"[安裝 / Install]({_toolchain_install_url(slug)})"
                )
        if has_pep:
            lines.append(
                f"- 順帶提醒：右下角還有 {pending_count} 個 PEP HOLD "
                "決定等你 APPROVE / REJECT"
            )
    elif has_empty and has_pep:
        lines.append(
            f"工作台目前是空的，但右下角還有 {pending_count} 個 PEP HOLD "
            "決定從之前留下來等你處理。"
        )
        lines.append(
            "- 處理待審決定：點 toast 上的 **WHY?** 看細節，再 APPROVE / REJECT"
        )
        lines.append("- 開始新工作：點右上角 ` + AGENT ` 建立第一個 agent")
        lines.append("- 或直接告訴我你想做什麼，我幫你 route 到對的 specialist")
    elif has_empty:
        lines.append("工作台是空的喔。要怎麼開始？")
        lines.append("- 試試 `/tour` 看一遍 5 步驟介紹")
        lines.append("- 點右上角 ` + AGENT ` 建立第一個 agent")
        lines.append("- 或直接打字告訴我你想做什麼，我幫你 routing")
    elif has_pep:
        lines.append(
            f"有 {pending_count} 個 PEP HOLD 決定從之前留下來還沒處理。"
        )
        lines.append(
            "- 點右下 toast 的 **WHY?** 看 What / Why / If approve / If reject"
        )
        lines.append("- 確認 OK 就 APPROVE，不確定就先 REJECT，agent 會走別的路徑")
    else:
        lines.append("一切看起來都正常 — 隨時告訴我你想做什麼。")
    return "\n".join(lines)


# BS.10.2 drift guard — ``_TOOLCHAIN_DISPLAY`` must cover every slug in
# ``_TOOLCHAIN_KEYWORD_MAP`` so a future toolchain row added to the
# detector cannot silently fall through to the ``(slug, "toolchain")``
# placeholder. Module-import-time check (single statement, no IO);
# raises ``AssertionError`` so ``import backend.routers.invoke`` fails
# loudly during CI rather than producing a degraded UX in prod.
assert set(_TOOLCHAIN_DISPLAY.keys()) == set(_TOOLCHAIN_KEYWORD_MAP.keys()), (
    "_TOOLCHAIN_DISPLAY drift vs _TOOLCHAIN_KEYWORD_MAP: "
    f"missing={set(_TOOLCHAIN_KEYWORD_MAP) - set(_TOOLCHAIN_DISPLAY)} "
    f"extra={set(_TOOLCHAIN_DISPLAY) - set(_TOOLCHAIN_KEYWORD_MAP)}"
)


async def _generate_coach_message(action: dict) -> str:
    """Compose the coach message: LLM-driven if available, templated fallback.

    R20 Phase 0 (2026-04-25): wraps the LLM call with the shared
    chat-layer security stack — ``INJECTION_GUARD_PRELUDE`` prepended
    to the persona prompt so the coach respects the same rules as
    ``conversation_node``, and ``secret_filter.redact()`` over the
    output. The coach prompt itself never includes user-controlled
    text in its system message (it's driven entirely by the planner-
    generated ``triggers`` list), so injection risk here is lower
    than ``conversation_node`` — but layering the same guards keeps
    the security model uniform across every chat-facing LLM call.
    """
    triggers = list(action.get("triggers") or [])
    pending = int(action.get("pending_count") or 0)
    fallback = _build_templated_coach_message(triggers, pending)
    try:
        from backend.agents.nodes import _get_llm
        from backend.security import INJECTION_GUARD_PRELUDE, redact
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = _get_llm(bind_tools_for=None)
        if not llm:
            return fallback
        sys = SystemMessage(
            content=INJECTION_GUARD_PRELUDE + "\n\n" + _COACH_SYSTEM_PROMPT,
        )
        ctx = HumanMessage(content=_build_coach_context(triggers, pending))
        resp = llm.invoke([sys, ctx])
        out = (resp.content or "").strip() if hasattr(resp, "content") else ""  # type: ignore[union-attr]
        if not out:
            return fallback
        # Redact any accidentally-leaked secrets/internal hosts before
        # the message reaches the operator's chat.
        redacted, fired = redact(out)
        if fired:
            logger.warning(
                "[R20-SEC] secret_filter redacted %s in coach reply",
                ",".join(fired),
            )
        return redacted
    except Exception as exc:
        logger.debug("coach LLM failed (%s) — using templated fallback", exc)
        return fallback


# ─── Endpoint ───

def _resolve_tenant_id(user) -> str:
    """Best-effort caller tenant id (mirrors ``installer._ensure_tenant``).

    BS.10.1 helper. The INVOKE router historically did not need a
    tenant context, but the missing-toolchain coach trigger queries
    ``install_jobs`` which is tenant-scoped. ``user`` may be a Pydantic
    ``User`` model or a plain mapping in degraded auth modes — both
    code paths converge on ``"t-default"`` when no tenant is
    advertised, matching every other tenant-aware router.
    """
    try:
        tid = getattr(user, "tenant_id", None)
        if tid is None and isinstance(user, dict):
            tid = user.get("tenant_id")
        return str(tid) if tid else "t-default"
    except Exception:
        return "t-default"


@router.post("/stream")
async def invoke_stream(
    command: str | None = None,
    suppress_coach: str | None = None,
    user=Depends(_auth.check_llm_quota),  # auth + M4 per-user LLM rate limit
):
    """SSE streaming invoke — analyses state, plans, executes, reports.

    Query param `command` is optional; if provided, it takes priority
    and is routed through the LangGraph pipeline.

    R20-B: ``suppress_coach`` is a comma-separated list of coaching
    trigger keys the frontend has already shown the operator this
    session (tracked in sessionStorage). Planner skips coaching for
    those triggers so the operator isn't re-coached on every INVOKE
    press. Recognised keys: ``empty_workspace`` / ``stale_pep``.
    """
    suppress_set: frozenset[str] = frozenset(
        t.strip() for t in (suppress_coach or "").split(",") if t.strip()
    )
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
    # BS.10.1 — pre-load the tenant's installed catalog so the planner
    # can emit ``missing_toolchain:<slug>`` coaching triggers when the
    # operator's command (or an unassigned task) hints at a toolchain
    # that has not been installed yet. Errors degrade silently — the
    # coach simply will not emit missing-toolchain triggers.
    state["installed_entries"] = await _load_installed_entry_ids(
        _resolve_tenant_id(user),
    )
    actions = _plan_actions(state, command, suppress_coach=suppress_set)

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
async def invoke_sync(
    command: str | None = None,
    user=Depends(_auth.check_llm_quota),  # auth + M4 per-user LLM rate limit
):
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
        # BS.10.1 — pre-load installed catalog so the planner can emit
        # missing-toolchain coach triggers (parity with /invoke/stream).
        state["installed_entries"] = await _load_installed_entry_ids(
            _resolve_tenant_id(user),
        )
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
                            anchor_sha=ws_info.anchor_sha,
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
