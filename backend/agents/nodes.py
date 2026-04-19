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

from backend.llm_adapter import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from backend.agents.state import AgentAction, GraphState, ToolCall, ToolResult
from backend.agents.tools import AGENT_TOOLS, TOOL_MAP, set_active_workspace
from backend.agents.llm import get_llm
from backend.events import emit_tool_progress, emit_pipeline_phase
from backend.prompt_loader import (
    build_system_prompt,
    build_skill_injection,
    extract_load_skill_requests,
    _resolve_skill_loading_mode,
)

logger = logging.getLogger(__name__)


def _parse_model_spec(model_name: str) -> tuple[str | None, str | None]:
    """Parse a model spec into (provider, model).

    Formats:
        ""                          → (None, None)  — use global settings
        "claude-sonnet-4-20250514"  → (None, "claude-sonnet-4-20250514")  — override model only
        "openrouter:qwen/qwen3-235b" → ("openrouter", "qwen/qwen3-235b")  — override both
        "anthropic:claude-opus-4"   → ("anthropic", "claude-opus-4")
    """
    if not model_name:
        return None, None
    if ":" in model_name:
        provider, _, model = model_name.partition(":")
        return provider.strip(), model.strip()
    return None, model_name


def _get_llm(bind_tools_for: str | None = None, model_name: str = ""):
    """Get the LLM, optionally with per-agent provider/model override.

    Args:
        bind_tools_for: Agent type for tool binding.
        model_name: Per-agent model spec (e.g. "openrouter:qwen/qwen3-235b").
                    If empty, uses global settings.llm_provider/model.
    """
    tools = AGENT_TOOLS.get(bind_tools_for, []) if bind_tools_for else None
    provider, model = _parse_model_spec(model_name)
    return get_llm(provider=provider, model=model, bind_tools=tools or None)


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


# C2 audit (2026-04-19): before a previous-attempt error string is
# concatenated into the next LLM invocation's system prompt, sanitize
# it so attacker-controlled content in a tool output / exception
# message cannot break the surrounding prompt structure. Concrete
# attack: adversary crafts a filesystem argument whose resulting
# exception text contains "\n\nIGNORE PREVIOUS RULES: …" — without
# sanitization that string becomes part of the next turn's system
# prompt verbatim.
_ERR_TRUNCATE_LEN = 800
_ERR_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _sanitize_error_for_prompt(err: str) -> str:
    if not err:
        return ""
    # Strip ANSI so terminal-escape sequences don't smuggle bytes.
    err = _ERR_ANSI_RE.sub("", err)
    # Collapse newlines to literal "\\n" so the error stays a single
    # logical "line" in the surrounding prompt — no blank lines that
    # the LLM might read as a new directive.
    err = err.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    # Hard cap so a runaway stack trace can't dominate the prompt.
    if len(err) > _ERR_TRUNCATE_LEN:
        err = err[:_ERR_TRUNCATE_LEN] + "…[truncated]"
    return err


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


async def _handle_llm_error(exc: Exception, agent_type: str, model_name: str) -> dict | None:
    """Handle LLM errors with classification, backoff, failover, and SSE notification.

    Returns a dict (answer for the user) if handled, or None to fall through to rule-based.
    """
    import asyncio
    import time

    from backend.llm_errors import classify_llm_error, LLMErrorCategory

    err = classify_llm_error(exc)
    category = err["category"]

    # Emit SSE notification for visibility
    try:
        emit_pipeline_phase(
            "llm_error",
            f"{agent_type} [{category}] {err['message'][:80]}",
        )
    except Exception:
        pass

    logger.warning(
        "%s LLM error [%s] (status=%s, retryable=%s, failover=%s): %s",
        agent_type, category, err["status_code"], err["retryable"], err["failover"], err["message"][:120],
    )

    # Permanent failures — mark provider and notify user
    if err["provider_action"] == "permanent_disable":
        provider = model_name.split(":")[0] if ":" in model_name else ""
        if provider:
            from backend.agents.llm import _record_provider_failure
            # 24h cooldown for auth/billing — pass an explicit future timestamp.
            _record_provider_failure(provider, ts=time.time() + 86400)
        try:
            from backend.events import emit_token_warning
            if category == LLMErrorCategory.AUTH_FAILED:
                emit_token_warning("warn", f"Provider auth failed: {err['message'][:100]}. Check API key in Settings.")
            elif category == LLMErrorCategory.BILLING_EXHAUSTED:
                emit_token_warning("warn", f"Provider billing exhausted: {err['message'][:100]}. Add credits or switch provider.")
            # Also emit pipeline_phase warning so the frontend pipeline panel
            # surfaces the permanent disable (not just the LLM panel).
            emit_pipeline_phase(
                "provider_disabled",
                f"Provider {provider or model_name} disabled for 24h ({category})",
            )
        except Exception:
            pass
        return None  # Fall through to failover/rule-based

    # Context overflow — trigger L2 compression and signal retry
    if category == LLMErrorCategory.CONTEXT_OVERFLOW:
        try:
            emit_pipeline_phase("l2_compress", f"Context overflow detected — triggering auto-compression for {agent_type}")
            from backend.events import emit_token_warning
            emit_token_warning("warn", f"Context too long for {model_name or 'default model'} — auto-compressing conversation history")
        except Exception:
            pass
        # Return a special signal that the graph can use to compress and retry
        # The context_compression_gate will handle the actual compression
        return {
            "answer": "",
            "messages": [AIMessage(content=f"[CONTEXT_OVERFLOW] {err['message'][:200]}")],
        }

    # Retryable with backoff — attempt retry with exponential delay
    # Phase 47C fix ①: BudgetStrategy tuning overrides classifier default
    # when the strategy caps retries lower (cost_saver) or higher (quality).
    _classifier_max = err["max_retries"]
    try:
        from backend.budget_strategy import get_tuning as _get_budget_tuning
        strat_cap = _get_budget_tuning().max_retries
        effective_max_retries = min(_classifier_max, strat_cap) if _classifier_max > 0 else 0
    except Exception:
        effective_max_retries = _classifier_max

    if err["retryable"] and effective_max_retries > 0:
        base_delay = err["retry_after"] or err["base_delay"]
        for attempt in range(1, effective_max_retries + 1):
            delay = base_delay * (2 ** (attempt - 1))
            delay = min(delay, 30)  # Cap at 30 seconds
            logger.info("LLM retry %d/%d for %s (waiting %.1fs)", attempt, effective_max_retries, category, delay)
            try:
                emit_pipeline_phase("llm_retry", f"{agent_type} retry {attempt}/{effective_max_retries} in {delay:.0f}s ({category})")
            except Exception:
                pass
            # Async sleep so the LangGraph node yields to the event loop
            # during retry backoff, instead of starving every other coroutine
            # for up to 30 s. Token-budget freeze is also re-checked between
            # retries so we don't keep retrying after global cutoff.
            await asyncio.sleep(delay)
            try:
                from backend.routers import system as _sys_mod
                if getattr(_sys_mod, "is_token_frozen", lambda: getattr(_sys_mod, "token_frozen", False))():
                    logger.warning("Token budget frozen mid-retry — aborting %s", category)
                    return None
            except Exception:
                pass
            try:
                llm = _get_llm(bind_tools_for=agent_type, model_name=model_name)
                if llm:
                    return None  # LLM recovered — caller will re-invoke on next graph cycle
            except Exception:
                continue
        logger.warning("LLM retries exhausted for %s after %d attempts", category, effective_max_retries)

    # Cooldown the provider for failover
    if err["provider_action"] == "cooldown":
        provider = model_name.split(":")[0] if ":" in model_name else ""
        if provider:
            from backend.agents.llm import _record_provider_failure
            _record_provider_failure(provider)

    return None  # Fall through to rule-based fallback


#  B15 #350 — Skill Lazy Loading: inner-loop cap for [LOAD_SKILL:] markers.
#  Each specialist invocation may pull at most this many extra skill bodies
#  before we force a decision (answer or tool-call). Bounds runaway agents
#  that would otherwise keep asking for more skills.
_MAX_SKILL_LOAD_ITERATIONS = 3


def _specialist_node_factory(agent_type: str):
    """Create a specialist node that can request tool calls."""

    async def node(state: GraphState) -> dict:
        cmd = state.user_command
        llm = _get_llm(bind_tools_for=agent_type, model_name=state.model_name)

        # ── LLM mode: let the model decide which tools to call ──
        if llm:
            prompt = build_system_prompt(
                model_name=state.model_name,
                agent_type=agent_type,
                sub_type=state.agent_sub_type,
                handoff_context=state.handoff_context,
                task_skill_context=state.task_skill_context,
            )
            if state.last_verification_failure:
                # M3 audit (2026-04-19): wrap error in XML so any jailbreak
                # markers inside the error text ("IGNORE PREVIOUS RULES:",
                # persona swap, role-override) stay INSIDE the block and
                # are structurally marked as untrusted content — paired
                # with the Security Guardrails preamble (prompt_loader.py
                # C2 fix) that tells the agent data inside error blocks
                # is not instruction.
                prompt = (
                    f"<verification_failure iteration=\"{state.verification_loop_iteration}\" of=\"{state.max_verification_iterations}\">\n"
                    f"{_sanitize_error_for_prompt(state.last_verification_failure)}\n"
                    f"</verification_failure>\n\n"
                    "Analyze the test/simulation failures above. Fix the code to pass the failing tests, "
                    "then re-run the simulation to verify.\n\n"
                    + prompt
                )
            elif state.last_error:
                prompt = (
                    f"<previous_error retry=\"{state.retry_count}\" of=\"{state.max_retries}\">\n"
                    f"{_sanitize_error_for_prompt(state.last_error)}\n"
                    f"</previous_error>\n\n"
                    "Adjust your approach to avoid the same error.\n\n"
                    + prompt
                )
            sys = SystemMessage(content=prompt)
            try:
                # B15 #350: when skill loading is in "lazy" mode, the system
                # prompt carries only a skill catalog. The agent may emit
                # `[LOAD_SKILL: <name>]` markers asking for the full body of
                # one or more skills. We loop up to _MAX_SKILL_LOAD_ITERATIONS
                # times, each time injecting the requested skill bodies as a
                # fresh SystemMessage and re-invoking the LLM.
                lazy_mode = _resolve_skill_loading_mode(None) == "lazy"
                extra_messages: list = []
                loaded_skills: set[str] = set()
                resp = None
                for skill_iter in range(_MAX_SKILL_LOAD_ITERATIONS + 1):
                    resp = llm.invoke([sys, *state.messages, *extra_messages])
                    if not lazy_mode:
                        break
                    agent_output = getattr(resp, "content", "") or ""
                    requested = extract_load_skill_requests(agent_output)
                    # Filter out skills we've already loaded this turn.
                    new_requests = [s for s in requested if s not in loaded_skills]
                    if not new_requests:
                        break
                    if skill_iter >= _MAX_SKILL_LOAD_ITERATIONS:
                        emit_pipeline_phase(
                            "skill_load_capped",
                            f"{agent_type} reached skill-load cap "
                            f"({_MAX_SKILL_LOAD_ITERATIONS}); ignoring "
                            f"{', '.join(new_requests)}",
                        )
                        break
                    injection = build_skill_injection(
                        explicit_skills=new_requests,
                        domain_context="",
                        user_prompt=cmd,
                    )
                    if not injection:
                        emit_pipeline_phase(
                            "skill_load_miss",
                            f"{agent_type} requested skills not found: "
                            f"{', '.join(new_requests)}",
                        )
                        # Record as "loaded" so we don't loop forever asking
                        # for a name that doesn't resolve.
                        loaded_skills.update(new_requests)
                        continue
                    loaded_skills.update(new_requests)
                    emit_pipeline_phase(
                        "skill_loaded",
                        f"{agent_type} loaded skill(s): "
                        f"{', '.join(new_requests)} "
                        f"({len(injection)} chars)",
                    )
                    # Keep the agent's request visible and append the
                    # injected skill body so the next LLM call sees both.
                    extra_messages.append(AIMessage(content=agent_output))
                    extra_messages.append(SystemMessage(
                        content=(
                            "[LOADED_SKILL] The following skill body was "
                            "loaded at your request. Use it to continue your "
                            "reasoning and then produce tool calls or a "
                            "final answer.\n\n" + injection
                        )
                    ))

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
                        "messages": [*extra_messages, resp],
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
                    "messages": [*extra_messages, AIMessage(content=answer)],
                }

            except Exception as exc:
                # Classify the error and attempt intelligent recovery
                resp = await _handle_llm_error(exc, agent_type, state.model_name)
                if resp is not None:
                    return resp
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

            # R0 (#306) — PEP Gateway: classify before exec.
            try:
                from backend import pep_gateway as _pep
                pep_dec = await _pep.evaluate(
                    tool=tc.tool_name,
                    arguments=args,
                    agent_id=agent_id or "",
                    tier=state.sandbox_tier or "t1",
                )
                if pep_dec.action is _pep.PepAction.deny:
                    output = f"[BLOCKED] PEP denied {tc.tool_name}: {pep_dec.reason}"
                    emit_tool_progress(tc.tool_name, "error", output, index=i, success=False)
                    results.append(ToolResult(tool_name=tc.tool_name, output=output, success=False))
                    tool_messages.append(ToolMessage(content=output, tool_call_id=tc.tool_name))
                    continue
            except Exception as pep_exc:
                # PEP evaluate raised unexpectedly — stay conservative:
                # let the tool run (circuit breaker inside evaluate() will
                # have tripped already so the next call fails closed).
                logger.warning("PEP evaluate raised: %s — proceeding", pep_exc)

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
                # R3 (#309) — opportunistic scratchpad flush after each
                # successful tool call. Best-effort; a scratchpad write
                # must never block tool execution.
                if success and agent_id:
                    try:
                        from backend import scratchpad as _sp
                        tracker = _sp.get_tracker(agent_id)
                        if tracker.note_tool_done():
                            prior = _sp.reload_latest(agent_id) or _sp.ScratchpadState(agent_id=agent_id)
                            prior.current_task = prior.current_task or (state.task_id or "")
                            prior.progress = (
                                f"{prior.progress}\n- {tc.tool_name} ✓".strip()
                                if prior.progress else f"- {tc.tool_name} ✓"
                            )[:4000]
                            prior.turn = (prior.turn or 0) + 1
                            _sp.save(prior, trigger="tool_done", task_id=state.task_id)
                    except Exception as _sp_exc:
                        logger.debug("scratchpad tool_done flush skipped: %s", _sp_exc)
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

def _extract_error_key(error_summary: str) -> str:
    """Extract the tool name from an error summary for loop detection."""
    if not error_summary or not error_summary.strip():
        return "_empty_error"
    key = error_summary.split(":")[0].strip() if ":" in error_summary else ""
    return key if key else error_summary[:50] or "_unknown"


async def error_check_node(state: GraphState) -> dict:
    """Check tool results for failures with loop detection.

    Two separate loops:
    1. Tool execution errors (retry_count) — tool crashed or timed out
    2. Verification failures (verification_loop_iteration) — tool ran OK but
       returned [FAIL] (e.g., simulation tests failed)

    Detects stuck loops via error_history comparison.
    """
    # Separate tool execution errors from verification failures
    tool_errors = [r for r in state.tool_results if not r.success]
    verification_failed = [
        r for r in state.tool_results
        if r.success and r.output.strip().startswith("[FAIL]")
    ]

    # Verification failures only processed if there are NO tool errors
    # (tool errors take priority — fix the crash first, then verify)
    if verification_failed and not tool_errors:
        v_iter = state.verification_loop_iteration + 1
        if v_iter > state.max_verification_iterations:
            emit_pipeline_phase(
                "verification_exhausted",
                f"Verification failed {v_iter} times — escalating to human",
            )
            from backend.events import emit_debug_finding
            emit_debug_finding(
                task_id=state.task_id or "", agent_id=state.routed_to or "",
                finding_type="verification_exhausted", severity="error",
                message=f"Verification loop exhausted after {v_iter} iterations",
            )
            return {
                "last_verification_failure": "",
                "tool_calls": [], "tool_results": [],
            }
        v_msg = "; ".join(f"{r.tool_name}: {r.output[:200]}" for r in verification_failed)
        emit_pipeline_phase(
            "verification_failure",
            f"Verification failed (iteration {v_iter}/{state.max_verification_iterations}): {v_msg[:120]}",
        )
        return {
            "verification_loop_iteration": v_iter,
            "last_verification_failure": v_msg,
            "tool_calls": [], "tool_results": [],
        }

    # Process tool execution errors (existing retry logic)
    failed = tool_errors

    if not failed or state.retry_count >= state.max_retries:
        if failed and state.retry_count >= state.max_retries:
            agent_type = state.routed_to
            emit_pipeline_phase(
                "escalation",
                f"Max retries ({state.max_retries}) exhausted. Freezing agent for human review.",
            )
            from backend.events import emit_debug_finding
            emit_debug_finding(
                task_id=state.task_id or "", agent_id=state.routed_to or "",
                finding_type="retries_exhausted", severity="error",
                message=f"Max retries exhausted after {state.max_retries} attempts",
            )
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
        return {"last_error": "", "last_verification_failure": "", "rtk_bypass": False}

    error_summary = "; ".join(
        f"{r.tool_name}: {r.output[:200]}" for r in failed
    )

    # Permission/environment auto-fix — attempt before counting as retry.
    # Loop guard (H8): if we've already auto-fixed the same category twice in
    # this graph run, stop trying and let the error propagate to the human.
    # Without this, fix→same-error→fix can loop indefinitely (e.g. chmod
    # restored to 644 by an external process between every retry).
    try:
        from backend.permission_errors import classify_permission_error, attempt_auto_fix
        prior_fixes = list(getattr(state, "auto_fix_history", []) or [])
        for r in failed:
            perm_err = classify_permission_error(r.output)
            if perm_err:
                emit_pipeline_phase(
                    "env_error",
                    f"{perm_err['category']}: {perm_err['matched_text'][:60]}",
                )
                same_cat_attempts = sum(1 for c in prior_fixes if c == perm_err["category"])
                if perm_err["auto_fixable"] and same_cat_attempts < 2:
                    fix_result = await attempt_auto_fix(
                        perm_err["category"], r.output, state.workspace_path or ""
                    )
                    if fix_result.get("fixed"):
                        emit_pipeline_phase(
                            "env_fix",
                            f"Auto-fixed {perm_err['category']}: {fix_result.get('action', '')}",
                        )
                        logger.info("Permission auto-fix: %s → %s", perm_err["category"], fix_result)
                        return {
                            "last_error": f"[AUTO-FIXED] {perm_err['category']}: {fix_result.get('action', '')}. Retrying...",
                            "tool_calls": [], "tool_results": [],
                            "auto_fix_history": (prior_fixes + [perm_err["category"]])[-20:],
                        }
                elif same_cat_attempts >= 2:
                    logger.warning(
                        "Auto-fix loop guard: %s tried %d times, escalating",
                        perm_err["category"], same_cat_attempts,
                    )
                    emit_pipeline_phase(
                        "env_fix_escalated",
                        f"Auto-fix giving up on {perm_err['category']} after {same_cat_attempts} attempts",
                    )
                else:
                    # Non-fixable — emit specific user guidance
                    try:
                        from backend.events import emit_token_warning
                        emit_token_warning(
                            "warn",
                            f"Environment issue: {perm_err['fix_description']}",
                        )
                    except Exception:
                        pass
    except Exception as exc:
        logger.debug("Permission check failed (non-critical): %s", exc)

    # Loop detection: compare error key with previous errors.
    # Cap history length to bound LangGraph state size during long retry loops.
    error_key = _extract_error_key(error_summary)
    _ERROR_HISTORY_MAX = 50
    _new_history = list(state.error_history) + [error_key]
    updated_history = _new_history[-_ERROR_HISTORY_MAX:]

    # Phase 47B fix ③: publish to the invoke-side ring buffer so the
    # watchdog's stuck-detector can see real error keys. Safe best-effort.
    try:
        agent_id_for_hist = getattr(state, "agent_id", "") or ""
        if agent_id_for_hist:
            from backend.routers.invoke import record_agent_error
            record_agent_error(agent_id_for_hist, error_key)
    except Exception:
        pass
    same_count = state.same_error_count
    loop_triggered = state.loop_breaker_triggered

    if len(updated_history) >= 2 and updated_history[-1] == updated_history[-2]:
        same_count += 1
    else:
        same_count = 0

    # If same error repeated 2+ times → trigger loop breaker
    if same_count >= 2 and not loop_triggered:
        loop_triggered = True
        emit_pipeline_phase(
            "loop_detection",
            f"Same error repeated {same_count + 1}x — loop breaker triggered",
        )
        from backend.events import emit_debug_finding
        emit_debug_finding(
            task_id=state.task_id or "", agent_id=state.routed_to or "",
            finding_type="stuck_loop", severity="error",
            message=f"Tool '{failed[0].tool_name}' failed {same_count + 1} consecutive times — stuck loop detected",
        )

    new_retry = state.retry_count + 1
    emit_pipeline_phase(
        "retry",
        f"Tool error (attempt {new_retry}/{state.max_retries}): {error_summary[:120]}",
    )

    # Phase 67-E: RAG pre-fetch on first retry. Replaces the previous
    # inline `[L3 HINT]` query. The new path routes through
    # `prefetch_for_sandbox_error` which enforces cosine > 0.85,
    # SDK-version hard-lock, and 1000-token block budget per
    # docs/design/dag-pre-fetching.md, and emits a structured
    # <system_auto_prefetch> block the agent's retry prompt can
    # consume. Same surface — an AIMessage appended on hit; None
    # on miss keeps the retry prompt clean.
    l3_hint_messages = []
    if new_retry == 1:
        try:
            from backend import rag_prefetch as _rp
            error_log = failed[0].output if failed else error_summary
            block = await _rp.prefetch_for_sandbox_error(
                error_log, rc=1,
                # Phase 67-E follow-up: state now carries platform tags.
                # Empty strings still map to "unknown, permissive" in
                # `_version_hard_lock_rejects`, so non-platform-aware
                # callers degrade gracefully.
                soc_vendor=state.soc_vendor,
                sdk_version=state.sdk_version,
            )
            if block:
                l3_hint_messages = [AIMessage(content=block)]
                emit_pipeline_phase(
                    "l3_query", "Pre-fetched past solution(s) for retry hint",
                )
        except Exception as exc:
            logger.debug("rag_prefetch in error_check failed (non-critical): %s", exc)

    return {
        "retry_count": new_retry,
        "last_error": error_summary,
        "error_history": updated_history,
        "same_error_count": same_count,
        "loop_breaker_triggered": loop_triggered,
        "tool_calls": [],
        "tool_results": [],
        "rtk_bypass": new_retry >= 2,
        **({"messages": l3_hint_messages} if l3_hint_messages else {}),
    }


def _should_retry(state: GraphState) -> str:
    """Conditional edge after error_check: retry specialist or summarize.

    Three paths:
    1. Loop breaker → summarizer (stuck pattern detected)
    2. Tool error + retries left → retry specialist
    3. Verification [FAIL] + iterations left → retry specialist (fix code)
    4. Otherwise → summarizer
    """
    if state.loop_breaker_triggered:
        return "summarizer"
    if state.last_error and state.retry_count < state.max_retries:
        return state.routed_to
    if state.last_verification_failure and state.verification_loop_iteration < state.max_verification_iterations:
        return state.routed_to
    return "summarizer"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conversation node — direct Q&A without tool execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_state_summary() -> str:
    """Build a concise system state summary including debug findings."""
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
        summary = (
            f"Agents: {len(agents_list)} total ({running} running, {idle} idle, {errors} error)\n"
            f"Tasks: {len(tasks_list)} total ({pending} pending, {in_prog} in progress, "
            f"{completed} completed, {blocked} blocked)"
        )
        # Append recent debug entries from system log (no async DB needed)
        if errors > 0 or blocked > 0:
            try:
                from backend.routers.system import get_recent_logs
                debug_lines = [
                    log["message"] for log in get_recent_logs(20)
                    if "[DEBUG]" in log.get("message", "")
                ]
                if debug_lines:
                    summary += "\n\nRecent Debug Alerts:"
                    for line in debug_lines[:5]:
                        summary += f"\n  {line[:100]}"
            except Exception:
                pass
        return summary
    except Exception:
        return "System state unavailable."


async def conversation_node(state: GraphState) -> dict:
    """Answer general questions without tool execution.

    This node is the conversational path — parallel to specialist nodes.
    It injects system state context and calls LLM without tool bindings.
    """
    state_summary = _build_state_summary()
    llm = _get_llm(bind_tools_for=None, model_name=state.model_name)

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
        await _handle_llm_error(exc, "conversation", state.model_name)
        return {
            "answer": f"I'm having trouble responding right now.\n\nSystem state:\n{state_summary}",
            "messages": [AIMessage(content=f"Conversation error: {exc}")],
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L2 Memory: Context compression gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Context budget thresholds (in estimated tokens)
_L2_WARN_THRESHOLD = 0.80   # 80% → log warning
_L2_COMPRESS_THRESHOLD = 0.90  # 90% → auto-compress old messages
_CHARS_PER_TOKEN = 3  # Conservative for mixed EN/CJK

# Default context windows per provider family (tokens)
_DEFAULT_CONTEXT_WINDOWS = {
    "claude": 200_000,
    "gpt": 128_000,
    "gemini": 1_000_000,
    "groq": 32_000,
    "deepseek": 64_000,
    "ollama": 8_000,
}


def _estimate_context_tokens(state: GraphState) -> int:
    """Estimate total tokens in the current message history."""
    total_chars = sum(len(m.content) for m in state.messages if hasattr(m, "content"))
    return total_chars // _CHARS_PER_TOKEN


def _get_context_window(model_name: str = "") -> int:
    """Get the context window size for the given model (or global default)."""
    try:
        from backend.config import settings
        # Prefer per-agent model_name over global setting
        model = (model_name or settings.llm_model or "").lower()
        for prefix, window in _DEFAULT_CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return window
    except Exception:
        pass
    return 128_000  # Safe default


def context_compression_gate(state: GraphState) -> dict:
    """L2 Memory gate: compress conversation history if context budget is exceeded.

    Runs before the summarizer. If messages exceed 90% of context window,
    compresses older messages (keeping the most recent 4) into a digest.
    """
    est_tokens = _estimate_context_tokens(state)
    ctx_window = _get_context_window(state.model_name)
    usage_ratio = est_tokens / ctx_window if ctx_window > 0 else 0

    if usage_ratio >= _L2_WARN_THRESHOLD:
        emit_pipeline_phase(
            "l2_memory",
            f"Context usage: {usage_ratio:.0%} ({est_tokens}/{ctx_window} tokens)",
        )

    if usage_ratio < _L2_COMPRESS_THRESHOLD:
        return {}  # No compression needed

    # Compress: keep last 4 messages, summarize the rest
    messages = list(state.messages)
    if len(messages) <= 6:
        return {}  # Too few messages to compress

    keep_recent = 4
    old_messages = messages[:-keep_recent]

    # Build text from old messages for summarization
    old_text_parts = []
    for m in old_messages:
        role = getattr(m, "type", "unknown")
        content = getattr(m, "content", "")
        if content:
            old_text_parts.append(f"[{role}] {content[:500]}")
    old_text = "\n".join(old_text_parts)

    # Try LLM summarization
    summary = ""
    llm = _get_llm()
    if llm:
        try:
            sys = SystemMessage(content=(
                "Compress this conversation history into a concise digest (max 300 tokens). "
                "Focus on: what was asked, what tools ran, what succeeded/failed, current status. "
                "Use terse technical language."
            ))
            resp = llm.invoke([sys, AIMessage(content=old_text[:6000])])
            summary = resp.content  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("L2 compression LLM failed: %s", exc)

    if not summary:
        # Rule-based fallback: extract key lines
        key_lines = [l for l in old_text.split("\n")
                     if any(k in l for k in ("[OK]", "[FAIL]", "[ERROR]", "AGENT]", "decided", "completed"))]
        summary = "\n".join(key_lines[:10]) if key_lines else old_text[:600]

    # Use RemoveMessage to delete old messages, then append compressed digest.
    # The add_messages reducer processes RemoveMessage by ID, so this correctly
    # removes old entries and appends the new compressed message.
    remove_ops = []
    for m in old_messages:
        if hasattr(m, "id") and m.id:
            remove_ops.append(RemoveMessage(id=m.id))

    compressed_msg = AIMessage(content=f"[L2 COMPRESSED HISTORY]\n{summary}")

    logger.info(
        "L2 context compressed: removing %d old messages, adding 1 digest (%d chars)",
        len(remove_ops), len(summary),
    )
    emit_pipeline_phase(
        "l2_compress",
        f"Compressed {len(old_messages)} old messages into digest ({len(summary)} chars)",
    )

    return {"messages": remove_ops + [compressed_msg]}


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
