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

import logging
import re

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from backend.agents.state import AgentAction, GraphState, ToolCall, ToolResult
from backend.agents.tools import AGENT_TOOLS, TOOL_MAP, set_active_workspace
from backend.agents.llm import get_llm
from backend.events import emit_tool_progress, emit_pipeline_phase, emit_agent_update

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
}


def _rule_based_route(text: str) -> str:
    text_lower = text.lower()
    scores = {
        agent: sum(1 for kw in keywords if kw in text_lower)
        for agent, keywords in _ROUTE_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else "general"


def orchestrator_node(state: GraphState) -> dict:
    """Parse the user command, decide which specialist to route to."""
    cmd = state.user_command

    llm = _get_llm()
    if llm:
        sys = SystemMessage(content=(
            "You are the OmniSight Orchestrator. Given a user command about "
            "embedded AI camera development, decide which specialist agent "
            "should handle it. Reply with EXACTLY one word: firmware, software, "
            "validator, reporter, or general."
        ))
        try:
            resp = llm.invoke([sys, *state.messages])
            route = resp.content.strip().lower()  # type: ignore[union-attr]
            if route not in ("firmware", "software", "validator", "reporter", "general"):
                route = _rule_based_route(cmd)
        except Exception as exc:
            logger.warning("LLM routing failed: %s — falling back", exc)
            route = _rule_based_route(cmd)
    else:
        route = _rule_based_route(cmd)

    emit_pipeline_phase("routing", f"Routing to {route.upper()} specialist")
    return {
        "routed_to": route,
        "messages": [AIMessage(content=f"[ORCHESTRATOR] Routing to {route.upper()} specialist.")],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Specialist nodes — plan & request tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SPECIALIST_PROMPTS = {
    "firmware": (
        "You are the Firmware Agent for embedded AI cameras. "
        "You handle UVC/RTSP drivers, Linux kernel modules, I2C/SPI sensor "
        "initialization, ISP pipeline configuration, Makefile cross-compilation, "
        "and flash operations.\n\n"
        "You have access to tools for reading/writing files, running git commands, "
        "and executing bash commands. Use them when the user's request requires "
        "inspecting or modifying the project. Always check existing files before "
        "writing new ones."
    ),
    "software": (
        "You are the Software Agent. You handle algorithm implementation, "
        "SDK/API development, C/C++ library integration, code refactoring, "
        "and build system maintenance.\n\n"
        "You have access to tools for reading/writing files, running git commands, "
        "and executing bash commands. Use them to inspect and modify code."
    ),
    "validator": (
        "You are the Validator Agent. You design and run test suites, "
        "coverage analysis, regression checks, benchmarks, and QA processes "
        "for embedded camera systems.\n\n"
        "You have access to tools for reading files, checking git status, "
        "and running test commands via bash."
    ),
    "reporter": (
        "You are the Reporter Agent. You generate compliance documentation "
        "(FCC/CE), test summaries, project reports, and exportable artifacts.\n\n"
        "You have access to tools for reading files and checking git history."
    ),
}

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


def _specialist_node_factory(agent_type: str):
    """Create a specialist node that can request tool calls."""

    def node(state: GraphState) -> dict:
        cmd = state.user_command
        llm = _get_llm(bind_tools_for=agent_type)

        # ── LLM mode: let the model decide which tools to call ──
        if llm and agent_type in _SPECIALIST_PROMPTS:
            sys = SystemMessage(content=_SPECIALIST_PROMPTS[agent_type])
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
                                detail=f"Executing {len(tool_calls)} tool(s)...",
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
                        detail=f"Executing tools: {', '.join(tc.tool_name for tc in tool_calls)}",
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
    "general": (
        "[ORCHESTRATOR] Command received. No specific specialist matched.\n"
        "Available specialists: firmware, software, validator, reporter.\n"
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

    for i, tc in enumerate(state.tool_calls):
        tool_fn = TOOL_MAP.get(tc.tool_name)
        if not tool_fn:
            output = f"[ERROR] Unknown tool: {tc.tool_name}"
            emit_tool_progress(tc.tool_name, "error", output)
            results.append(ToolResult(tool_name=tc.tool_name, output=output, success=False))
            tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
            continue

        emit_tool_progress(tc.tool_name, "start", f"Running {tc.tool_name}({tc.arguments})", index=i)

        try:
            output = await tool_fn.ainvoke(tc.arguments)
            emit_tool_progress(tc.tool_name, "done", output, index=i, success=True)
            results.append(ToolResult(tool_name=tc.tool_name, output=output, success=True))
            tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
        except Exception as exc:
            output = f"[ERROR] {tc.tool_name} failed: {exc}"
            emit_tool_progress(tc.tool_name, "error", output, index=i, success=False)
            results.append(ToolResult(tool_name=tc.tool_name, output=output, success=False))
            tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))

    emit_pipeline_phase("tool_complete", f"{len(results)} tool(s) finished")

    # Reset workspace context
    set_active_workspace(None, agent_id=None)

    return {
        "tool_results": results,
        "tool_calls": [],
        "messages": tool_messages,
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

    answer = "\n".join(lines)
    return {"answer": answer, "messages": [AIMessage(content=answer)]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exported node instances
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

firmware_node = _specialist_node_factory("firmware")
software_node = _specialist_node_factory("software")
validator_node = _specialist_node_factory("validator")
reporter_node = _specialist_node_factory("reporter")
general_node = _specialist_node_factory("general")
