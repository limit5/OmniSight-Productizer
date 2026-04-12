"""Agent nodes for the LangGraph topology.

Each node is a plain function that receives GraphState, does its work,
and returns a partial state update.  When an LLM is not configured the
nodes fall back to rule-based logic so the system stays functional.

Tool integration:
 - Specialist nodes can request tool calls via state.tool_calls
 - The tool_executor node runs them and writes results to state.tool_results
 - The summarizer node reads tool_results and produces the final answer
"""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from backend.agents.state import AgentAction, GraphState, ToolCall, ToolResult
from backend.agents.tools import AGENT_TOOLS, TOOL_MAP, set_active_workspace
from backend.agents.llm import get_llm
from backend.events import emit_tool_progress, emit_pipeline_phase, emit_agent_update
from backend.prompt_loader import build_system_prompt

logger = logging.getLogger(__name__)


def _get_llm(bind_tools_for: str | None = None):
    """Get the configured LLM, optionally with agent-specific tools bound."""
    tools = AGENT_TOOLS.get(bind_tools_for, []) if bind_tools_for else None
    return get_llm(bind_tools=tools or None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator (router)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ROUTE_KEYWORDS = {
    "firmware": [
        "firmware", "driver", "sensor", "i2c", "spi", "uvc", "isp",
        "flash", "embedded", "makefile", "cross-compile", "kernel",
    ],
    "software": [
        "software", "code", "algorithm", "build", "compile", "library",
        "sdk", "api", "function", "module", "refactor",
    ],
    "validator": [
        "test", "validate", "verify", "check", "qa", "coverage",
        "benchmark", "regression", "assert", "lint",
    ],
    "reporter": [
        "report", "document", "summary", "cert", "compliance",
        "fcc", "ce", "log", "export", "pdf", "markdown",
    ],
    "reviewer": [
        "review", "code-review", "patch", "patchset", "gerrit",
        "diff", "comment", "approve", "reject", "inline",
    ],
}


def _rule_based_route(text: str) -> tuple[str, list[str]]:
    """Route to best specialist(s) based on keyword scoring.

    Returns ``(primary_route, secondary_routes)`` where secondary_routes
    lists other specialists that also scored > 0 (for compound commands).
    """
    text_lower = text.lower()

    # Merge built-in keywords with skill file keywords
    all_keywords: dict[str, list[str]] = dict(_ROUTE_KEYWORDS)
    try:
        from backend.prompt_loader import list_available_roles
        for role in list_available_roles():
            cat = role["category"]
            kws = role.get("keywords", [])
            if cat in all_keywords:
                all_keywords[cat] = list(set(all_keywords[cat] + kws))
    except Exception:
        pass

    scores = {
        agent: sum(1 for kw in keywords if kw in text_lower)
        for agent, keywords in all_keywords.items()
    }
    sorted_agents = sorted(scores.items(), key=lambda x: -x[1])
    if not sorted_agents or sorted_agents[0][1] == 0:
        return "general", []

    primary = sorted_agents[0][0]
    secondary = [a for a, s in sorted_agents[1:] if s > 0]
    return primary, secondary


_QUESTION_PATTERNS = re.compile(
    r"(\?|什麼|怎麼|如何|為什麼|為何|哪|嗎|呢|建議|介紹|說明|解釋"
    r"|^what\b|^how\b|^why\b|^when\b|^where\b|^which\b|^can\b|^could\b"
    r"|^is\b|^are\b|^do\b|^does\b|^tell\b|^explain\b|^describe\b|^suggest\b)",
    re.IGNORECASE,
)


def _is_question(text: str) -> bool:
    """Heuristic: detect if text is a question/inquiry rather than a task command."""
    return bool(_QUESTION_PATTERNS.search(text))


def orchestrator_node(state: GraphState) -> dict:
    """Parse the user command: conversation vs task, then route accordingly."""
    cmd = state.user_command

    secondary: list[str] = []
    is_conv = False
    route = "general"

    llm = _get_llm()
    if llm:
        sys = SystemMessage(content=(
            "You are the OmniSight Orchestrator. Determine the user's intent:\n"
            "1. If the user is asking a QUESTION, requesting advice, or inquiring about "
            "status (NOT asking to execute/build/compile/test/deploy), respond ONLY with: CONVERSATIONAL\n"
            "2. Otherwise, decide which specialist agent should handle the task. "
            "Valid agents: firmware, software, validator, reporter, reviewer, general. "
            "Respond with agent name(s) comma-separated (primary first).\n"
            "Examples:\n"
            "- 'What is ISP tuning?' → CONVERSATIONAL\n"
            "- 'How many agents are running?' → CONVERSATIONAL\n"
            "- 'Compile the firmware driver' → firmware\n"
            "- 'Run tests and generate report' → validator,reporter"
        ))
        try:
            resp = llm.invoke([sys, *state.messages])
            raw = resp.content.strip().lower()  # type: ignore[union-attr]
            if "conversational" in raw:
                is_conv = True
            else:
                parts = [p.strip() for p in raw.split(",")]
                valid = {"firmware", "software", "validator", "reporter", "reviewer", "general"}
                valid_parts = [p for p in parts if p in valid]
                if valid_parts:
                    route = valid_parts[0]
                    secondary = valid_parts[1:]
                else:
                    route, secondary = _rule_based_route(cmd)
        except Exception as exc:
            logger.warning("LLM routing failed: %s — falling back", exc)
            if _is_question(cmd):
                is_conv = True
            else:
                route, secondary = _rule_based_route(cmd)
    else:
        # Rule-based: detect questions first, then route tasks
        if _is_question(cmd):
            is_conv = True
        else:
            route, secondary = _rule_based_route(cmd)

    if is_conv:
        emit_pipeline_phase("routing", "Conversational mode — answering directly")
        return {
            "is_conversational": True,
            "messages": [AIMessage(content="[ORCHESTRATOR] Entering conversational mode")],
        }

    detail = f"Routing to {route.upper()} specialist"
    if secondary:
        detail += f" (also relevant: {', '.join(s.upper() for s in secondary)})"
    emit_pipeline_phase("routing", detail)
    return {
        "routed_to": route,
        "secondary_routes": secondary,
        "messages": [AIMessage(content=f"[ORCHESTRATOR] {detail}")],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Specialist nodes — plan & request tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Rule-based tool selection when no LLM is available
_RULE_TOOL_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    # File reading patterns — path must look like a file path (with / or .)
    (re.compile(r"read\s+(?:file\s+)?([a-zA-Z0-9_./-]+\.\w+)", re.I), "read_file", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"cat\s+([a-zA-Z0-9_./-]+\.\w+)", re.I), "read_file", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"show\s+(?:file\s+)?([a-zA-Z0-9_./-]+\.\w+)", re.I), "read_file", lambda m: {"path": m.group(1).strip()}),
    # YAML
    (re.compile(r"(parse|load)\s+(.+\.ya?ml)", re.I), "read_yaml", lambda m: {"path": m.group(2).strip()}),
    # Directory listing
    (re.compile(r"(ls|list|dir)\b\s*(.*)", re.I), "list_directory", lambda m: {"path": m.group(2).strip() or "."}),
    # Search
    (re.compile(r"(search|find|grep)\s+['\"]?(.+?)['\"]?\s+(in\s+)?(.+)?", re.I), "search_in_files",
     lambda m: {"pattern": m.group(2), "path": (m.group(4) or ".").strip()}),
    # Git
    (re.compile(r"git\s+status", re.I), "git_status", lambda m: {}),
    (re.compile(r"git\s+log", re.I), "git_log", lambda m: {}),
    (re.compile(r"git\s+diff\s*(.*)", re.I), "git_diff", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"git\s+branch", re.I), "git_branch", lambda m: {}),
    (re.compile(r"git\s+add\s+(.+)", re.I), "git_add", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"git\s+commit\s+(.+)", re.I), "git_commit", lambda m: {"message": m.group(1).strip()}),
    # Bash / make / compile
    (re.compile(r"^(make|cmake|gcc|g\+\+|python3?|pip|npm)\b(.+)?", re.I), "run_bash",
     lambda m: {"command": m.group(0).strip()}),
    (re.compile(r"(run|exec|execute)\s+(.+)", re.I), "run_bash",
     lambda m: {"command": m.group(2).strip()}),
    # Bare bash command patterns
    (re.compile(r"^(which|whoami|uname|cat|echo|pwd|env)\b(.+)?", re.I), "run_bash",
     lambda m: {"command": m.group(0).strip()}),
    # Report generation
    (re.compile(r"(?:generate|create)\s+(?:a\s+)?(\w+)\s+report", re.I), "generate_artifact_report",
     lambda m: {"template": m.group(1).strip().lower(), "title": f"{m.group(1).strip()} Report"}),
    # Simulation — require "simulation/sim" keyword + module name
    (re.compile(r"(?:run|execute|start)\s+(?:a\s+)?(?:simulation|sim)\s+(?:for\s+)?(\w+)", re.I), "run_simulation",
     lambda m: {"track": "algo", "module": m.group(1).strip()}),
    (re.compile(r"simulate\s+(?:module\s+)?(\w+)\s+(?:algo|hw|module)", re.I), "run_simulation",
     lambda m: {"track": "algo", "module": m.group(1).strip()}),
]


def _rule_based_tool_calls(cmd: str) -> list[ToolCall]:
    """Extract tool calls from the user command using regex patterns."""
    calls: list[ToolCall] = []
    for pattern, tool_name, arg_fn in _RULE_TOOL_PATTERNS:
        m = pattern.search(cmd)
        if m:
            try:
                args = arg_fn(m)
                calls.append(ToolCall(tool_name=tool_name, arguments=args))
            except Exception:
                continue
    return calls


def _build_sub_tasks(tool_calls: list[ToolCall]) -> list[dict]:
    """Generate sub-task breakdown from tool calls for UI display."""
    return [
        {"id": f"st-{i}", "label": f"{tc.tool_name}({', '.join(f'{k}={v}' for k, v in list(tc.arguments.items())[:2])})", "status": "pending"}
        for i, tc in enumerate(tool_calls)
    ]


def _specialist_node_factory(agent_type: str):
    """Create a specialist node that can request tool calls."""

    def node(state: GraphState) -> dict:
        cmd = state.user_command
        llm = _get_llm(bind_tools_for=agent_type)

        # ── LLM mode: let the model decide which tools to call ──
        if llm:
            prompt = build_system_prompt(
                model_name=state.model_name,
                agent_type=agent_type,
                sub_type=state.agent_sub_type,
                handoff_context=state.handoff_context,
                task_skill_context=state.task_skill_context,
            )
            if state.last_error:
                prompt = (
                    f"PREVIOUS ATTEMPT FAILED (retry {state.retry_count}/{state.max_retries}):\n"
                    f"{state.last_error}\n\n"
                    "Adjust your approach to avoid the same error.\n\n"
                    + prompt
                )
            sys = SystemMessage(content=prompt)
            try:
                resp = llm.invoke([sys, *state.messages])

                # Check if LLM requested tool calls
                if hasattr(resp, "tool_calls") and resp.tool_calls:
                    tool_calls = [
                        ToolCall(
                            tool_name=tc["name"],
                            arguments=tc.get("args", {}),
                        )
                        for tc in resp.tool_calls
                    ]
                    return {
                        "tool_calls": tool_calls,
                        "messages": [resp],
                        "actions": [
                            AgentAction(
                                type="update_status",
                                agent_type=agent_type,
                                status="running",
                                detail=json.dumps({"sub_tasks": _build_sub_tasks(tool_calls)}),
                            )
                        ],
                    }

                # No tool calls — LLM gave a direct answer
                answer = resp.content  # type: ignore[union-attr]
                prefix = f"[{agent_type.upper()} AGENT] "
                if not answer.startswith(prefix):
                    answer = prefix + answer

                return {
                    "answer": answer,
                    "actions": [
                        AgentAction(
                            type="update_status",
                            agent_type=agent_type,
                            status="running",
                            detail=f"Processing: {cmd}",
                        )
                    ],
                    "messages": [AIMessage(content=answer)],
                }

            except Exception as exc:
                logger.warning("%s LLM failed: %s", agent_type, exc)
                # Fall through to rule-based

        # ── Rule-based mode: pattern-match tool calls from the command ──
        tool_calls = _rule_based_tool_calls(cmd)
        if tool_calls:
            # Filter to only tools this agent has access to
            allowed = {t.name for t in AGENT_TOOLS.get(agent_type, [])}
            tool_calls = [tc for tc in tool_calls if tc.tool_name in allowed]

        if tool_calls:
            return {
                "tool_calls": tool_calls,
                "messages": [
                    AIMessage(
                        content=f"[{agent_type.upper()} AGENT] Executing {len(tool_calls)} tool(s): "
                        + ", ".join(tc.tool_name for tc in tool_calls)
                    )
                ],
                "actions": [
                    AgentAction(
                        type="update_status",
                        agent_type=agent_type,
                        status="running",
                        detail=json.dumps({"sub_tasks": _build_sub_tasks(tool_calls)}),
                    )
                ],
            }

        # No tools matched — produce a static answer
        answer = _FALLBACK_ANSWERS.get(agent_type, _FALLBACK_ANSWERS["general"])
        return {
            "answer": answer,
            "actions": [
                AgentAction(
                    type="update_status",
                    agent_type=agent_type,
                    status="running",
                    detail=f"Processing: {cmd}",
                )
            ],
            "messages": [AIMessage(content=answer)],
        }

    node.__name__ = f"{agent_type}_node"
    return node


_FALLBACK_ANSWERS = {
    "firmware": (
        "[FIRMWARE AGENT] Acknowledged. Analyzing firmware requirements:\n"
        "1. Parse hardware_manifest.yaml for sensor/ISP config\n"
        "2. Generate Linux kernel module skeleton\n"
        "3. Configure Makefile for cross-compilation\n"
        "4. Prepare I2C/SPI initialization sequence\n"
        "Ready to execute when confirmed."
    ),
    "software": (
        "[SOFTWARE AGENT] Acknowledged. Planning software pipeline:\n"
        "1. Analyze algorithm requirements\n"
        "2. Set up build environment and dependencies\n"
        "3. Implement core modules\n"
        "4. Run static analysis\n"
        "Ready to proceed."
    ),
    "validator": (
        "[VALIDATOR AGENT] Acknowledged. Preparing validation suite:\n"
        "1. Define test matrix from specifications\n"
        "2. Set up test harness\n"
        "3. Execute unit + integration tests\n"
        "4. Generate coverage report\n"
        "Standing by for test execution."
    ),
    "reporter": (
        "[REPORTER AGENT] Acknowledged. Report generation plan:\n"
        "1. Collect system metrics and test results\n"
        "2. Cross-reference compliance requirements\n"
        "3. Generate structured documentation\n"
        "4. Export in requested format\n"
        "Awaiting data sources."
    ),
    "reviewer": (
        "[REVIEWER AGENT] Acknowledged. Preparing code review:\n"
        "1. Fetch patchset diff from Gerrit\n"
        "2. Analyze for memory safety, pointer issues, thread safety\n"
        "3. Check coding style and conventions\n"
        "4. Post inline comments on findings\n"
        "5. Submit Code-Review score (+1 or -1)\n"
        "Standing by for patchset."
    ),
    "general": (
        "[ORCHESTRATOR] Command received. No specific specialist matched.\n"
        "Available specialists: firmware, software, validator, reporter, reviewer.\n"
        "Please refine your request or type 'help' for guidance."
    ),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool executor node
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def tool_executor_node(state: GraphState) -> dict:
    """Execute all pending tool calls and record their results.

    If the graph state has a workspace_path, tools operate inside that
    isolated workspace instead of the global project root.

    Emits real-time SSE events for each tool (start → done/error).
    """
    from pathlib import Path

    results: list[ToolResult] = []
    tool_messages: list[ToolMessage] = []

    # Activate isolated workspace if set (enables container routing too)
    agent_id = None
    if state.actions:
        agent_id = state.actions[0].agent_id or state.actions[0].agent_type
    if state.workspace_path:
        set_active_workspace(Path(state.workspace_path), agent_id=agent_id)
        emit_pipeline_phase("tool_execution", f"Executing {len(state.tool_calls)} tool(s) in workspace: {state.workspace_path}")
    else:
        set_active_workspace(None, agent_id=None)
        emit_pipeline_phase("tool_execution", f"Executing {len(state.tool_calls)} tool(s)")

    try:
        for i, tc in enumerate(state.tool_calls):
            tool_fn = TOOL_MAP.get(tc.tool_name)
            if not tool_fn:
                output = f"[ERROR] Unknown tool: {tc.tool_name}"
                emit_tool_progress(tc.tool_name, "error", output)
                results.append(ToolResult(tool_name=tc.tool_name, output=output, success=False))
                tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
                continue

            emit_tool_progress(tc.tool_name, "start", f"Running {tc.tool_name}({tc.arguments})", index=i)

            # Inject task_id from state for report tools if not already set
            args = tc.arguments
            if tc.tool_name == "generate_artifact_report" and not args.get("task_id") and state.task_id:
                args = {**args, "task_id": state.task_id}

            try:
                output = await tool_fn.ainvoke(args)
                # Compress output to save tokens (covers ALL tools)
                if not state.rtk_bypass:
                    try:
                        from backend.output_compressor import compress_output
                        output, _ = await compress_output(output, tc.tool_name)
                    except Exception:
                        pass  # Compression failure — use original output
                _ERROR_PREFIXES = ("[ERROR]", "[BLOCKED]", "[TIMEOUT]")
                success = not any(output.startswith(p) for p in _ERROR_PREFIXES)
                status_label = "done" if success else "error"
                emit_tool_progress(tc.tool_name, status_label, output, index=i, success=success)
                results.append(ToolResult(tool_name=tc.tool_name, output=output, success=success))
                tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
            except Exception as exc:
                output = f"[ERROR] {tc.tool_name} failed: {exc}"
                emit_tool_progress(tc.tool_name, "error", output, index=i, success=False)
                results.append(ToolResult(tool_name=tc.tool_name, output=output, success=False))
                tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
    finally:
        # Always reset workspace context, even if loop is interrupted
        set_active_workspace(None, agent_id=None)

    emit_pipeline_phase("tool_complete", f"{len(results)} tool(s) finished")

    return {
        "tool_results": results,
        "tool_calls": [],
        "messages": tool_messages,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error check node — self-healing loop gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def error_check_node(state: GraphState) -> dict:
    """Check tool results for failures and decide whether to retry.

    If any tool failed and retries remain, route back to the specialist
    with error context.  Otherwise, pass through to the summarizer.
    """
    failed = [r for r in state.tool_results if not r.success]
    if not failed or state.retry_count >= state.max_retries:
        # Retries exhausted with errors → escalate to human
        if failed and state.retry_count >= state.max_retries:
            agent_type = state.routed_to
            emit_pipeline_phase(
                "escalation",
                f"Max retries ({state.max_retries}) exhausted. Freezing agent for human review.",
            )
            # Signal escalation via action (notification sent by invoke.py when it sees this action)
            return {
                "last_error": "",
                "actions": [
                    AgentAction(
                        type="update_status",
                        agent_type=agent_type,
                        status="awaiting_confirmation",
                        detail=f"Frozen after {state.max_retries} failed retries. @Human intervention required.",
                    )
                ],
            }
        # No errors — proceed to summarizer, reset compression bypass
        return {"last_error": "", "rtk_bypass": False}

    error_summary = "; ".join(
        f"{r.tool_name}: {r.output[:200]}" for r in failed
    )
    emit_pipeline_phase(
        "retry",
        f"Tool error detected (attempt {state.retry_count + 1}/{state.max_retries}): {error_summary[:120]}",
    )
    new_retry = state.retry_count + 1
    return {
        "retry_count": new_retry,
        "last_error": error_summary,
        "tool_calls": [],
        "tool_results": [],
        # After 2 consecutive failures, bypass RTK compression to get full uncompressed output
        "rtk_bypass": new_retry >= 2,
    }


def _should_retry(state: GraphState) -> str:
    """Conditional edge after error_check: retry specialist or summarize.

    Uses ``last_error`` (set by error_check_node when errors found) and
    ``retry_count`` vs ``max_retries`` to decide.  Cannot rely on
    ``tool_results`` because error_check_node clears it before this runs.
    """
    if state.last_error and state.retry_count < state.max_retries:
        return state.routed_to
    return "summarizer"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conversation node — direct Q&A without tool execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_state_summary() -> str:
    """Build a concise system state summary for conversational context."""
    try:
        from backend.routers.invoke import _agents, _tasks
        agents_list = list(_agents.values())
        tasks_list = list(_tasks.values())
        running = sum(1 for a in agents_list if a.status.value == "running")
        idle = sum(1 for a in agents_list if a.status.value == "idle")
        errors = sum(1 for a in agents_list if a.status.value == "error")
        pending = sum(1 for t in tasks_list if t.status.value == "backlog")
        in_prog = sum(1 for t in tasks_list if t.status.value in ("assigned", "in_progress"))
        completed = sum(1 for t in tasks_list if t.status.value == "completed")
        blocked = sum(1 for t in tasks_list if t.status.value == "blocked")
        return (
            f"Agents: {len(agents_list)} total ({running} running, {idle} idle, {errors} error)\n"
            f"Tasks: {len(tasks_list)} total ({pending} pending, {in_prog} in progress, "
            f"{completed} completed, {blocked} blocked)"
        )
    except Exception:
        return "System state unavailable."


def conversation_node(state: GraphState) -> dict:
    """Answer general questions without tool execution.

    This node is the conversational path — parallel to specialist nodes.
    It injects system state context and calls LLM without tool bindings.
    """
    state_summary = _build_state_summary()
    llm = _get_llm(bind_tools_for=None)

    if not llm:
        # Offline fallback: return state summary directly
        emit_pipeline_phase("conversation", "Offline mode — returning state summary")
        return {
            "answer": f"[OFFLINE] I can't process your question without an LLM provider.\n\nCurrent state:\n{state_summary}",
            "messages": [AIMessage(content=state_summary)],
        }

    sys_prompt = SystemMessage(content=(
        "You are the OmniSight Conversational Assistant — an expert in embedded AI camera development. "
        "Answer questions about ISP tuning, sensor optimization, firmware architecture, Linux drivers, "
        "image processing pipelines, NPI lifecycle, and system status.\n\n"
        f"Current System State:\n{state_summary}\n\n"
        "Guidelines:\n"
        "- Be conversational, helpful, and concise.\n"
        "- Use markdown for formatting when appropriate.\n"
        "- If the user wants to execute a task (compile, test, deploy), suggest: "
        "'Try typing a command like \"compile firmware\" or create a task via the Task Backlog.'\n"
        "- Answer in the same language as the user's question."
    ))

    emit_pipeline_phase("conversation", "Generating conversational response")
    try:
        resp = llm.invoke([sys_prompt, *state.messages])
        answer = resp.content  # type: ignore[union-attr]
        return {
            "answer": answer,
            "messages": [AIMessage(content=answer)],
        }
    except Exception as exc:
        logger.warning("Conversation LLM failed: %s", exc)
        return {
            "answer": f"I'm having trouble responding right now.\n\nSystem state:\n{state_summary}",
            "messages": [AIMessage(content=f"Conversation error: {exc}")],
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Summarizer node — produces final answer from tool results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def summarizer_node(state: GraphState) -> dict:
    """Synthesize tool results into a final answer."""
    agent_type = state.routed_to
    prefix = f"[{agent_type.upper()} AGENT]"

    # If we already have an answer (no tools were called), pass through
    if state.answer and not state.tool_results:
        return {}

    llm = _get_llm()
    if llm and state.tool_results:
        sys = SystemMessage(content=(
            f"You are the {agent_type.title()} Agent. You just executed tools and got "
            "results. Summarize the results concisely for the user. "
            "Start your response with your agent prefix."
        ))
        try:
            resp = llm.invoke([sys, *state.messages])
            answer = resp.content  # type: ignore[union-attr]
            if not answer.startswith(prefix):
                answer = f"{prefix} {answer}"
            return {"answer": answer, "messages": [AIMessage(content=answer)]}
        except Exception as exc:
            logger.warning("Summarizer LLM failed: %s", exc)

    # Rule-based summary
    lines = [f"{prefix} Tool execution complete.\n"]
    for result in state.tool_results:
        status = "OK" if result.success else "FAILED"
        # Truncate long outputs for the summary
        output_preview = result.output[:500]
        if len(result.output) > 500:
            output_preview += "..."
        lines.append(f"  [{status}] {result.tool_name}:\n{output_preview}\n")

    # Append multi-agent recommendation if secondary routes exist
    if state.secondary_routes:
        others = ", ".join(s.upper() for s in state.secondary_routes)
        lines.append(f"\n[RECOMMENDATION] Related work may benefit from: {others} agent(s).")

    answer = "\n".join(lines)
    return {"answer": answer, "messages": [AIMessage(content=answer)]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exported node instances
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

firmware_node = _specialist_node_factory("firmware")
software_node = _specialist_node_factory("software")
validator_node = _specialist_node_factory("validator")
reporter_node = _specialist_node_factory("reporter")
reviewer_node = _specialist_node_factory("reviewer")
general_node = _specialist_node_factory("general")
