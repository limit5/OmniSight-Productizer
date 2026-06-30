"""Agent nodes for the LangGraph topology.

Each node is a plain function that receives ``GraphState``, does its
work, and returns a *partial* state update (a dict that LangGraph
merges via its reducers — never mutate ``state`` directly). When an
LLM is not configured the nodes fall back to rule-based logic so the
system stays functional in offline / dev environments.

Graph topology (high level)
---------------------------
1. ``orchestrator_node`` — decides conversational vs task and, for
   tasks, picks the primary specialist (firmware / software /
   validator / reporter / reviewer / general).
2. Guild nodes (built by :func:`_guild_node_factory` and
   exported as ``firmware_node``, ``software_node``, …) — plan the
   work and either answer directly or emit ``tool_calls``.
3. ``tool_executor_node`` — runs the requested tools (in the agent's
   isolated workspace if one is set) and records ``tool_results``.
4. ``error_check_node`` — self-healing gate: classifies tool errors
   vs verification ``[FAIL]`` outputs, drives retry / loop-breaker /
   auto-fix / RAG pre-fetch hint logic.
5. ``context_compression_gate`` — L2 memory: compresses the message
   history when it nears the model's context window.
6. ``summarizer_node`` — synthesises ``tool_results`` into the final
   answer that the UI shows.
7. ``conversation_node`` — parallel path for direct Q&A without
   tool execution; runs the chat-layer security stack (R20).

The factory :func:`external_agent_node_factory` produces an A2A
"bring-your-own-agent" node that invokes an operator-registered
external endpoint instead of an in-process specialist.

State conventions
-----------------
* Each node returns a partial dict; LangGraph's ``add_messages``
  reducer (for ``messages``) processes ``RemoveMessage`` and append
  semantics for callers.
* ``actions`` carry status updates that the runtime relays to the
  UI / DB; nodes never write to the DB directly.
* ``tool_calls`` (request) and ``tool_results`` (response) are the
  hand-off contract between specialists and ``tool_executor_node``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.agents.cognee_integration import build_repo_map_via_cognee
from backend.llm_adapter import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from backend.agents.state import AgentAction, GraphState, ToolCall, ToolResult
from backend.agents.tools import AGENT_TOOLS, GUILD_TOOLS, ORCHESTRATION_TOOLS, TOOL_MAP, set_active_workspace
from backend.agents.llm import get_llm
from backend.events import emit_tool_progress, emit_pipeline_phase, emit_turn_tool_stats
from backend.prompt_loader import (
    build_system_prompt,
    build_skill_injection,
    extract_load_skill_requests,
    _resolve_skill_loading_mode,
)
from backend.rtk_fallback import update_rtk_fallback_history
from backend.web.vite_error_prompt import build_last_vite_error_banner
from backend.web.vite_retry_budget import (
    emit_vite_pattern_escalation,
    should_escalate_vite_pattern,
)

logger = logging.getLogger(__name__)

_TOOL_ERROR_PREFIXES = (
    "[ERROR]",
    "[BLOCKED]",
    "[TIMEOUT]",
    "[PATCH-FAILED]",
    "[REJECTED]",
)

ExternalAgentPayloadBuilder = Callable[[GraphState], dict[str, Any]]
ExternalAgentRegistryLike = Any


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


def _get_llm(bind_tools_for: str | None = None, model_name: str = "", extra_tools=None):
    """Get the LLM, optionally with per-agent provider/model override.

    Args:
        bind_tools_for: Agent type for tool binding.
        model_name: Per-agent model spec (e.g. "openrouter:qwen/qwen3-235b").
                    If empty, uses global settings.llm_provider/model.
        extra_tools: Optional explicit tool list to bind ON TOP of (or
                    instead of) the guild tools. Used by the conversation
                    node to expose just ``create_task`` on the otherwise
                    tool-free chat path (Gap-② routing fix, 2026-06-30).

    Z.6.3: ollama is not short-circuited here — GUILD_TOOLS is provider-agnostic
    (keyed by agent_type, not provider).  ``get_llm()`` applies
    ``llm.bind_tools()`` uniformly for every provider including ollama, using
    the path established in Z.6.2.
    """
    tools = (
        (GUILD_TOOLS.get(bind_tools_for) or AGENT_TOOLS.get(bind_tools_for, []))
        if bind_tools_for
        else None
    )
    if extra_tools:
        tools = list(tools or []) + list(extra_tools)
    provider, model = _parse_model_spec(model_name)
    guild = bind_tools_for if bind_tools_for in GUILD_TOOLS else None
    return get_llm(
        provider=provider,
        model=model,
        bind_tools=tools or None,
        guild=guild,
        guild_id=bind_tools_for,
    )


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


# Deterministic "file a task" intent (dogfood 2026-06-30). create_task is
# only reachable — and only surfaces its REAL result — on the conversational
# path (conversation_node). Letting the LLM router decide was
# non-deterministic and once landed on the heavy general-specialist path,
# which hallucinated a fake ticket id (SORA-28) and timed out. A request
# that pairs a create-verb with a task-noun (either order, EN or CJK) is
# pinned to the conversational path here. Over-matching is harmless:
# conversation_node only actually files when the model judges the user has
# confirmed concrete scope.
_TASK_CREATE_VERB = re.compile(
    r"建立|建個|建成|新增|開票|開單|開個|開一[張個]|安排|登記|提交|"
    r"create|file|open|raise|submit|log",
    re.IGNORECASE,
)
_TASK_CREATE_NOUN = re.compile(
    r"task|工作|任務|工單|票|ticket|story|issue|backlog",
    re.IGNORECASE,
)


def _is_task_creation_intent(text: str) -> bool:
    """True when the user is asking to create/file/arrange a task."""
    if not text:
        return False
    return bool(_TASK_CREATE_VERB.search(text) and _TASK_CREATE_NOUN.search(text))


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
    """Defang an error string before splicing it into a prompt.

    Strips ANSI escapes, collapses real newlines into the literal
    ``\\n`` (so the error stays a single logical line and can't
    introduce blank-line breaks the LLM might read as a new
    instruction block), and truncates at ``_ERR_TRUNCATE_LEN`` so a
    runaway stack trace cannot dominate the prompt. Empty input
    returns ``""`` so the caller can splice unconditionally.
    """
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
    """Decide conversational vs task and pick the primary specialist.

    The orchestrator is the entry node of the graph. It inspects
    ``state.user_command`` (and the message history) and chooses
    between two mutually exclusive paths:

    * **Conversational** — the user is asking a question or seeking
      advice. ``is_conversational=True`` is set on the returned
      partial state and the graph routes to :func:`conversation_node`.
    * **Task** — the user wants something executed. ``routed_to`` is
      set to the chosen specialist (one of ``firmware``, ``software``,
      ``validator``, ``reporter``, ``reviewer``, ``general``) and
      ``secondary_routes`` lists any other specialists whose keywords
      also fired (used for compound commands and the
      ``[RECOMMENDATION]`` line emitted by the summarizer).

    Routing strategy:

    1. If an LLM is available, ask it to classify (``CONVERSATIONAL``
       or comma-separated agent names). Invalid replies fall through
       to the rule-based router.
    2. Otherwise, use :func:`_is_question` (regex on EN + CJK question
       markers) to detect questions, then :func:`_rule_based_route`
       for keyword scoring against ``_ROUTE_KEYWORDS`` merged with
       any role-skill keywords declared by ``prompt_loader``.

    Emits a ``routing`` pipeline phase event for the UI.
    """
    cmd = state.user_command

    secondary: list[str] = []
    is_conv = False
    route = "general"

    # Deterministic short-circuit: a clear task-filing request always goes
    # to the conversational path (the only reliable create_task route),
    # bypassing the non-deterministic LLM router that once mis-routed it to
    # the hallucinating general-specialist pipeline.
    if _is_task_creation_intent(cmd):
        emit_pipeline_phase("routing", "Conversational mode — task-filing request")
        return {
            "is_conversational": True,
            "messages": [AIMessage(content="[ORCHESTRATOR] Entering conversational mode")],
        }

    llm = _get_llm()
    if llm:
        sys = SystemMessage(content=(
            "You are the OmniSight Orchestrator. Determine the user's intent:\n"
            "1. If the user is asking a QUESTION, requesting advice, inquiring about "
            "status, OR discussing/planning what to build and asking you to file or "
            "arrange a task (the conversational orchestrator handles task-filing "
            "itself), respond ONLY with: CONVERSATIONAL\n"
            "2. Otherwise, when the user gives a direct one-shot execution command "
            "(build/compile/test/deploy now), decide which specialist agent should "
            "handle it. Valid agents: firmware, software, validator, reporter, "
            "reviewer, general. Respond with agent name(s) comma-separated (primary first).\n"
            "Examples:\n"
            "- 'What is ISP tuning?' → CONVERSATIONAL\n"
            "- 'How many agents are running?' → CONVERSATIONAL\n"
            "- '幫我建立對應的 OmniSight Task / file this as a task' → CONVERSATIONAL\n"
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


def _maybe_emit_vite_retry_budget(state: GraphState) -> dict:
    """W15.4 #XXX — Detect a 3-strike Vite-pattern repeat in
    ``state.error_history`` and, if found, emit the operator escalation
    once per (graph run, signature) tuple.

    Returns a partial state-update dict shaped for direct
    ``**spread`` into the specialist node's return value:

      * ``{}`` — no escalation fired this turn (the common case).
      * ``{"vite_escalated_signatures": [...]}`` — the freshly-escalated
        signature is appended; the caller spreads it into its return so
        the next LLM turn observes the gate.

    The detection delegates entirely to
    :func:`backend.web.vite_retry_budget.should_escalate_vite_pattern`
    so the threshold + idempotency semantics live in one place — this
    helper only handles the LangGraph-state plumbing and the call to
    :func:`backend.web.vite_retry_budget.emit_vite_pattern_escalation`.
    """

    decision = should_escalate_vite_pattern(
        state.error_history,
        already_escalated=tuple(state.vite_escalated_signatures),
    )
    if decision is None:
        return {}
    emit_vite_pattern_escalation(
        task_id=state.task_id or "",
        agent_id=state.routed_to or "",
        decision=decision,
    )
    return {
        "vite_escalated_signatures": (
            list(state.vite_escalated_signatures) + [decision.pattern]
        ),
    }


def _guild_node_factory(
    guild: str | None = None,
    *,
    agent_type: str | None = None,
):
    """Build a Guild node bound to ``guild``.

    Produces the async coroutine exported as e.g. ``firmware_node``
    or ``software_node``. The returned node:

    * Builds the agent-specific system prompt via
      :func:`build_system_prompt` (which folds in handoff context,
      task skill context, the W11 clone-spec block when present,
      the last Vite-error banner, and the Cognee KG / B8 repo-map
      preamble).
    * Prepends a sanitised ``<previous_error>`` or
      ``<verification_failure>`` block on retry turns so jailbreak
      markers smuggled inside error text stay inside an
      XML-tagged untrusted-content envelope (paired with the
      Security Guardrails preamble — C2/M3 audit, 2026-04-19).
    * Supports the B15 #350 lazy-skill-loading inner loop: if the
      LLM emits ``[LOAD_SKILL: <name>]`` markers, fetches the skill
      body and re-invokes up to ``_MAX_SKILL_LOAD_ITERATIONS`` times
      before forcing a decision.
    * Calls :func:`_handle_llm_error` on LLM exceptions for
      classify / backoff / failover, then falls through to
      :func:`_rule_based_tool_calls` and finally to a static
      per-agent answer from ``_FALLBACK_ANSWERS``.

    The returned function has ``__name__`` set to
    ``<guild>_node`` so LangGraph debugging output is meaningful.

    ``agent_type`` remains accepted as the legacy keyword alias while
    downstream prompt/tool/action registries still use those slug
    values as their lookup keys.
    """
    if guild is None:
        if agent_type is None:
            raise TypeError("_guild_node_factory() missing required argument: 'guild'")
        guild = agent_type
    elif agent_type is not None and agent_type != guild:
        raise ValueError("guild and legacy agent_type alias must match")

    agent_type = guild

    async def node(state: GraphState) -> dict:
        cmd = state.user_command
        llm = _get_llm(bind_tools_for=agent_type, model_name=state.model_name)
        # W15.4 #XXX — detect 3-strike same-pattern Vite errors and
        # escalate to the operator once per (graph run, signature)
        # before the LLM call.  The merged dict carries
        # ``vite_escalated_signatures`` only when a fresh emission
        # fired; legacy / non-Vite graph runs see ``{}`` and the
        # spread is a no-op.
        vite_extra = _maybe_emit_vite_retry_budget(state)

        # ── LLM mode: let the model decide which tools to call ──
        if llm:
            prompt = build_system_prompt(
                model_name=state.model_name,
                agent_type=agent_type,
                sub_type=state.agent_sub_type,
                handoff_context=state.handoff_context,
                task_skill_context=state.task_skill_context,
                # W11.10 (#XXX): when the router populated ``clone_spec_context``
                # from a W11.6 TransformedSpec + W11.7 CloneManifest, append the
                # block to the frontend agent's role prompt so scaffolding
                # respects W11 invariants (no copied bytes / placeholder images
                # only / attribution mandatory / manifest fingerprint echoed).
                # Empty string for non-W11 runs is a no-op in build_system_prompt.
                clone_spec_context=state.clone_spec_context,
                # W15.3 (#XXX): pick the most recent ``omnisight-vite-plugin``
                # error from ``state.error_history`` (W15.2 folded it in) and
                # render it as the Chinese-localised banner ("上次 build 有
                # error: [file:line] [message]") so the agent's next turn opens
                # with a structured reminder of the last build / runtime
                # failure without waiting for a tool-error round trip. Empty
                # string for graphs that have not seen a Vite error yet (the
                # common case — most graphs never touch a sandboxed Vite
                # preview) is a no-op in build_system_prompt.
                last_vite_error_banner=build_last_vite_error_banner(
                    state.error_history
                ),
                # OP-852: Cognee KG (C3) replaces the B8 PageRank repo-map.
                # The helper silently falls back to B8's
                # ``build_repo_map_system_prefix`` when Cognee is
                # unreachable / not installed / times out, so this swap
                # is a runtime upgrade with no behaviour change for
                # operators who have not enabled the optional bundle.
                repo_map_preamble=build_repo_map_via_cognee(
                    Path.cwd(),
                    ticket_text="\n\n".join(
                        part for part in (state.task_id or "", state.user_command) if part
                    ),
                ),
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
                        **vite_extra,
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
                    **vite_extra,
                }

            except Exception as exc:
                # Classify the error and attempt intelligent recovery
                resp = await _handle_llm_error(exc, agent_type, state.model_name)
                if resp is not None:
                    if vite_extra:
                        merged = dict(resp)
                        merged.update(vite_extra)
                        return merged
                    return resp
                # Fall through to rule-based

        # ── Rule-based mode: pattern-match tool calls from the command ──
        tool_calls = _rule_based_tool_calls(cmd)
        if tool_calls:
            # Filter to only tools this agent has access to
            allowed_tools = GUILD_TOOLS.get(agent_type) or AGENT_TOOLS.get(agent_type, [])
            allowed = {t.name for t in allowed_tools}
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
                **vite_extra,
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
            **vite_extra,
        }

    node.__name__ = f"{agent_type}_node"
    return node


def _specialist_node_factory(agent_type: str):
    """Backward-compatible alias for :func:`_guild_node_factory`."""
    return _guild_node_factory(agent_type=agent_type)


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
    """Execute every pending ``ToolCall`` in ``state.tool_calls``.

    For each call this node:

    1. Looks up the tool in ``TOOL_MAP`` and emits a ``start``
       progress event. Unknown tool names produce a ``[ERROR]``
       ``ToolResult`` and skip to the next call.
    2. Optionally injects ``task_id`` (for report tools).
    3. Submits the call to the **PEP gateway** (#306) for tier-aware
       policy evaluation. A ``deny`` short-circuits with a
       ``[BLOCKED]`` result; unexpected gateway exceptions fall
       through conservatively (the gateway's own circuit breaker
       will fail-closed on the next call).
    4. Runs the tool via ``ainvoke``, optionally compressing the
       output via :func:`backend.output_compressor.compress_output`
       (skipped when ``state.rtk_bypass`` is set), and classifies
       success by checking for any ``_TOOL_ERROR_PREFIXES`` prefix.
    5. Best-effort: flushes a scratchpad checkpoint after each
       successful tool call so a crash mid-turn leaves a recoverable
       progress trail (#309). Scratchpad failures are swallowed.

    Workspace handling: when ``state.workspace_path`` is set, the
    active workspace is switched for the duration of the loop so
    tools see the agent's isolated tree (and container routing
    activates if applicable). The ``finally`` block guarantees the
    workspace is reset to ``None`` even on cancellation, which
    matters because ``set_active_workspace`` writes to a
    module-global used by every tool.

    Returns a partial state update with ``tool_results`` (one per
    call), an empty ``tool_calls`` list (consumed), and one
    ``ToolMessage`` per call appended to ``messages`` so the LLM
    sees the tool output on the next turn.
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
                success = not any(output.startswith(p) for p in _TOOL_ERROR_PREFIXES)
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


def external_agent_node_factory(
    agent_id: str,
    *,
    registry: ExternalAgentRegistryLike,
    tenant_id: str,
    bearer_token: str = "",
    payload_builder: ExternalAgentPayloadBuilder | None = None,
) -> Callable[[GraphState], Awaitable[dict]]:
    """Build a LangGraph node that invokes an operator-registered A2A agent.

    The returned coroutine resolves the endpoint and builds a client
    via the injected ``registry`` (which owns durable cross-worker
    state — endpoint enablement, tenant scoping, token rotation),
    sends the payload built by ``payload_builder`` (defaulting to
    :func:`_default_external_agent_payload`), and converts the A2A
    response into a ``ToolResult`` + ``ToolMessage`` pair so the
    rest of the graph (summarizer, error_check) treats it like any
    other tool invocation.

    Success is determined by :func:`_a2a_payload_success` — a status
    field of ``failed`` / ``error`` / ``cancelled`` or a non-empty
    ``last_error`` flips the result to ``success=False``, which lets
    ``error_check_node`` apply the standard retry policy.

    Args:
        agent_id: External agent identifier registered with the
            ``registry``. Leading / trailing whitespace is stripped.
            An empty value raises ``ValueError`` at factory time
            (fail-fast rather than at first invocation).
        registry: Object exposing ``get_endpoint`` and
            ``build_client``; typically
            ``backend.agents.external_agent_registry.ExternalAgentRegistry``.
        tenant_id: Tenant scope passed to ``build_client``. Empty
            raises ``ValueError``.
        bearer_token: Optional bearer token override. Empty string
            means the registry-stored token is used.
        payload_builder: Optional callable that maps ``GraphState``
            to the JSON-serialisable request body. Defaults to
            :func:`_default_external_agent_payload`.

    Returns:
        An async LangGraph node. Its ``__name__`` is set to
        ``external_agent_<agent_id>_node`` so LangGraph's debug
        output and DAG visualisation show a meaningful label.

    Module-global state audit (SOP Step 1): the factory stores no
    module-level mutable state. Each node closes over its workflow
    configuration and the injected registry; durable cross-worker
    consistency remains owned by that registry implementation,
    matching ``ExternalAgentRegistry``.
    """
    clean_agent_id = agent_id.strip()
    if not clean_agent_id:
        raise ValueError("agent_id is required")
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")

    async def external_agent_node(state: GraphState) -> dict:
        tool_name = f"external_agent:{clean_agent_id}"
        emit_tool_progress(
            tool_name,
            "start",
            f"Invoking external A2A agent {clean_agent_id}",
        )
        try:
            endpoint = await registry.get_endpoint(
                clean_agent_id,
                require_enabled=True,
            )
            client = await registry.build_client(
                clean_agent_id,
                tenant_id=tenant_id,
                bearer_token=bearer_token,
            )
            payload_fn = payload_builder or _default_external_agent_payload
            result = await client.invoke(endpoint.agent_name, payload_fn(state))
            output = json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
            success = _a2a_payload_success(result.payload)
            emit_tool_progress(
                tool_name,
                "done" if success else "error",
                output,
                success=success,
            )
            return {
                "tool_results": [
                    ToolResult(
                        tool_name=tool_name,
                        output=output,
                        success=success,
                    )
                ],
                "messages": [ToolMessage(content=output, tool_call_id=tool_name)],
            }
        except Exception as exc:
            output = f"[ERROR] {tool_name} failed: {exc}"
            emit_tool_progress(tool_name, "error", output, success=False)
            return {
                "tool_results": [
                    ToolResult(
                        tool_name=tool_name,
                        output=output,
                        success=False,
                    )
                ],
                "messages": [ToolMessage(content=output, tool_call_id=tool_name)],
            }

    external_agent_node.__name__ = f"external_agent_{clean_agent_id.replace('-', '_')}_node"
    return external_agent_node


def _default_external_agent_payload(state: GraphState) -> dict[str, Any]:
    """Default A2A request body when no ``payload_builder`` was injected.

    Carries the minimum context an external agent needs to act on a
    handoff: the user command, ticket id, routing decision, the
    workspace path it should target, and a serialised view of any
    ``tool_results`` accumulated earlier in the graph.
    """
    return {
        "command": state.user_command,
        "task_id": state.task_id,
        "routed_to": state.routed_to,
        "secondary_routes": list(state.secondary_routes),
        "workspace_path": state.workspace_path,
        "tool_results": [
            result.model_dump()
            for result in state.tool_results
        ],
    }


def _a2a_payload_success(payload: dict[str, Any]) -> bool:
    """Decide whether an A2A response should count as success.

    Treats a ``status`` of ``failed`` / ``error`` / ``cancelled`` /
    ``canceled`` (US + UK spelling) or any non-empty ``last_error``
    field as failure; anything else is success. Returning ``False``
    here funnels the call into the standard retry policy in
    :func:`error_check_node`.
    """
    status = str(payload.get("status", "")).lower()
    if status in {"failed", "error", "cancelled", "canceled"}:
        return False
    if payload.get("last_error"):
        return False
    return True


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
    """Self-healing gate that classifies failures and drives retry.

    Two independent retry loops are tracked, with tool-execution
    errors taking priority over verification failures:

    1. **Tool execution errors** (``retry_count`` / ``max_retries``)
       — the tool crashed, was denied by PEP, or timed out. Each
       failure increments ``retry_count`` and pushes the error key
       onto ``error_history`` (capped at 50 entries to bound state
       size during long runs).
    2. **Verification failures** (``verification_loop_iteration`` /
       ``max_verification_iterations``) — the tool ran cleanly but
       its output starts with ``[FAIL]`` (e.g. a simulation that
       compiled but failed an assertion). Only counted when no tool
       errors are present in the same batch.

    Side-effects layered on top of the basic retry counter:

    * **Permission auto-fix** (``permission_errors``) — recognised
      env errors (permission denied, missing dir, etc.) are
      auto-fixed in place, and the H8 loop guard caps each category
      at 2 attempts per run so a re-occurring external issue
      (file mode reset by another process) escalates instead of
      looping forever.
    * **RTK fallback** (:func:`update_rtk_fallback_history`) — for
      ``run_bash`` / ``Bash`` failures, decides whether to bypass
      the retry tool kit on the next turn and folds a human-readable
      message into the error summary.
    * **Stuck-loop detection** — when the same error key repeats
      ≥ 2 times the loop breaker fires (``loop_breaker_triggered``)
      and the conditional edge :func:`_should_retry` will route to
      the summarizer instead of looping forever. A
      ``stuck_loop`` debug finding is emitted for the UI.
    * **RAG pre-fetch (#67-E)** — on the first retry only, a
      similar past sandbox-error fix may be pre-fetched (cosine
      > 0.85, SDK-version hard-lock, 1000-token cap) and rendered
      as a ``<system_auto_prefetch>`` block on the next turn so the
      agent's retry prompt opens with a structured hint.

    Returns a partial state update encoding the next-turn decision:
    incremented counters and a populated ``last_error`` /
    ``last_verification_failure`` if the graph should retry, or
    cleared fields plus an ``actions=[update_status:
    awaiting_confirmation]`` when retries are exhausted (which the
    runtime renders as a human-review handoff).
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
    fallback_command = state.user_command if failed and failed[0].tool_name in {"run_bash", "Bash"} else ""
    rtk_history, rtk_decision = update_rtk_fallback_history(
        task_id=state.task_id,
        failed_tool_name=failed[0].tool_name if failed else "",
        failed_output=failed[0].output if failed else error_summary,
        prior_history=state.rtk_fallback_history,
        command=fallback_command,
    )
    if rtk_decision:
        try:
            from backend import metrics as _m
            _m.rtk_fallback_total.inc()
        except Exception:
            logger.debug("RTK fallback metric publish failed", exc_info=True)
        emit_pipeline_phase("rtk_fallback", rtk_decision.message[:200])
        error_summary = f"{error_summary}; {rtk_decision.message}"

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
        "rtk_bypass": rtk_decision is not None,
        "rtk_fallback_history": rtk_history,
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

    R20 Phase 0 (2026-04-25): now wraps the LLM call with three layers
    of chat-layer security:

      1. Prompt hardening — every system prompt prepends
         ``INJECTION_GUARD_PRELUDE`` so the LLM is told (in operator's
         language) not to disclose system prompts / internal docs /
         secrets, and not to execute instructions found in retrieved
         doc content or user-provided text.
      2. RAG with classification gate — the user's last message is
         used as a retrieval query against ``backend.rag``. Only docs
         whose audience matches the operator's role appear in the LLM
         context; ``internal``-tagged docs are unreachable from chat.
      3. Output redaction — ``secret_filter.redact()`` runs over the
         LLM response before we hand it back; any leaked credential
         shape (gh_pat, sk-ant-*, AWS keys, JWT, internal hostnames,
         etc.) is replaced with ``[REDACTED:<kind>]``.
    """
    state_summary = _build_state_summary()
    llm = _get_llm(bind_tools_for=None, model_name=state.model_name)
    # Gap-② routing fix (2026-06-30): the conversational path is where the
    # user actually talks to the orchestrator, so it — not just the
    # specialist task nodes — must be able to FILE work. Bind just
    # ``create_task`` here (the gated Story filer); everything else stays
    # tool-free. ``llm`` (no tools) is still used for the offline fallback
    # and for summarising a tool result without re-triggering the tool.
    llm_tools = _get_llm(
        bind_tools_for=None, model_name=state.model_name,
        extra_tools=ORCHESTRATION_TOOLS,
    ) if llm else None

    # R20 Phase 0: pull last user message (if any) for RAG + injection
    # detection. If there's no user message, skip retrieval and run
    # plain — the coach path can call this with only an AI/system
    # message and we don't want to retrieve on it.
    last_user_text = ""
    for msg in reversed(state.messages):
        # Use class name string check to avoid importing all message types.
        if msg.__class__.__name__ == "HumanMessage":
            last_user_text = (msg.content or "") if hasattr(msg, "content") else ""
            break
    last_user_text = str(last_user_text) if last_user_text else ""

    # Retrieve relevant docs (classification-gated) — runs even without
    # an LLM so the offline fallback can still cite something useful.
    retrieved_block = ""
    try:
        from backend import rag as _rag
        hits = _rag.retrieve(
            last_user_text, role=state.user_role, top_k=4,
        ) if last_user_text else []
        if hits:
            retrieved_block = _rag.format_hits_for_prompt(hits)
    except Exception as _rag_exc:
        logger.debug("RAG retrieve skipped (%s) — proceeding without context", _rag_exc)

    if not llm:
        # Offline fallback: return state summary + retrieved doc cites.
        emit_pipeline_phase("conversation", "Offline mode — returning state summary")
        offline = (
            "[OFFLINE] I can't process your question without an LLM "
            f"provider.\n\nCurrent state:\n{state_summary}"
        )
        if retrieved_block:
            offline += (
                "\n\nThese docs may help — open them directly:\n"
                + retrieved_block
            )
        return {
            "answer": offline,
            "messages": [AIMessage(content=offline)],
        }

    # R20 Phase 0: chat-layer security imports (kept local to avoid
    # cold-import cost during graph construction).
    from backend.security import (
        INJECTION_GUARD_PRELUDE,
        harden_user_message,
        redact,
    )

    persona = (
        "You are the OmniSight Conversational Assistant — an expert in "
        "embedded AI camera development AND in operating the OmniSight "
        "platform itself (agents, tasks, settings, integrations, "
        "operational SOPs). Answer questions about both domains.\n\n"
        f"Current System State:\n{state_summary}\n\n"
    )
    if retrieved_block:
        persona += (
            "Retrieved docs (classification-gated to the user's role; "
            "any doc shown here is approved for this user):\n"
            f"{retrieved_block}\n\n"
        )
    persona += (
        "Guidelines:\n"
        "- Be conversational, helpful, and concise.\n"
        "- Use markdown for formatting when appropriate.\n"
        "- If a retrieved doc covers the question, cite it inline as "
        "[source: <path>] so the operator can open the original.\n"
        "- If retrieved docs don't answer the question, say so and "
        "suggest where the operator might look (without inventing a "
        "doc path).\n"
        "- You are the user's orchestrator. When the user wants real work "
        "done (build / fix / implement / a tool or feature) AND has "
        "confirmed the scope, CALL the create_task tool ONCE to file it as "
        "a runner Story — pick the closest area and write crisp acceptance "
        "criteria. Refer back to earlier turns in this conversation for the "
        "details instead of re-asking what the user already told you. The "
        "Story is filed GATED (it will not dispatch until the operator "
        "releases it), so report the ticket key and that it awaits their "
        "approval — never claim the work has started. Do not file before "
        "the user agrees on scope; for a quick one-off command (compile / "
        "test / deploy) you may instead suggest typing it directly.\n"
        "- Answer in the same language as the user's question."
    )

    sys_prompt = SystemMessage(content=INJECTION_GUARD_PRELUDE + "\n\n" + persona)

    # Wrap a likely-injection user message with a spotlighting hint
    # before the LLM sees it. The original text is preserved inside
    # the wrapper so the LLM still has full context.
    if last_user_text and state.messages:
        last_idx = -1
        for i in range(len(state.messages) - 1, -1, -1):
            if state.messages[i].__class__.__name__ == "HumanMessage":
                last_idx = i
                break
        if last_idx >= 0:
            wrapped = harden_user_message(last_user_text)
            if wrapped is not last_user_text:
                # Build a copy of messages with the last user message
                # replaced by the hardened version. Don't mutate state
                # — LangGraph's add_messages reducer would re-merge.
                from langchain_core.messages import HumanMessage
                new_messages = list(state.messages)
                new_messages[last_idx] = HumanMessage(content=wrapped)
                send_messages = new_messages
            else:
                send_messages = list(state.messages)
        else:
            send_messages = list(state.messages)
    else:
        send_messages = list(state.messages)

    emit_pipeline_phase("conversation", "Generating conversational response")
    try:
        resp = (llm_tools or llm).invoke([sys_prompt, *send_messages])
        # Gap-② routing fix: if the model decided to file work, run the
        # tool(s) it requested (only create_task is bound here), then let
        # the plain (tool-free) LLM turn the result into a natural-language
        # reply. Bounded to a single tool round — the summary LLM has no
        # tools, so it cannot re-trigger create_task into a loop.
        tool_calls = getattr(resp, "tool_calls", None) or []
        if tool_calls:
            from langchain_core.messages import ToolMessage
            emit_pipeline_phase("conversation", f"Filing {len(tool_calls)} task(s)")
            followup = [sys_prompt, *send_messages, resp]
            for call in tool_calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = (call.get("args") if isinstance(call, dict) else getattr(call, "args", {})) or {}
                cid = (call.get("id") if isinstance(call, dict) else getattr(call, "id", "")) or name
                fn = TOOL_MAP.get(name)
                if fn is None:
                    out = f"[ERROR] Unknown tool: {name}"
                else:
                    try:
                        out = await fn.ainvoke(args)
                    except Exception as tool_exc:  # noqa: BLE001
                        out = f"[ERROR] {name} failed: {tool_exc}"
                emit_tool_progress(
                    name, "done" if str(out).startswith("[OK]") else "error", str(out),
                )
                followup.append(ToolMessage(content=str(out), tool_call_id=cid))
            resp = llm.invoke(followup)
        answer = resp.content  # type: ignore[union-attr]
        # R20 Phase 0: redact accidentally-leaked secrets from the
        # LLM's output BEFORE it reaches the chat / SSE / audit log.
        if isinstance(answer, str):
            redacted, fired = redact(answer)
            if fired:
                logger.warning(
                    "[R20-SEC] secret_filter redacted %s in conversation reply",
                    ",".join(fired),
                )
            answer = redacted
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
    """L2 memory gate: compress message history when it nears the
    model's context window.

    Estimates current usage via :func:`_estimate_context_tokens`
    (``len(content) // _CHARS_PER_TOKEN``, conservative for mixed
    EN / CJK) against the per-model window from
    :func:`_get_context_window`. Two thresholds:

    * ``_L2_WARN_THRESHOLD`` (80 %) — logs a warning event but takes
      no action; visible in the UI so operators can shorten the
      conversation.
    * ``_L2_COMPRESS_THRESHOLD`` (90 %) — collapses every message
      *except the last 4* into a single ``[L2 COMPRESSED HISTORY]``
      digest. The digest is built by an LLM when one is available
      and by a rule-based extractor (lines containing ``[OK]``,
      ``[FAIL]``, ``[ERROR]``, ``AGENT]``, etc.) when not.

    Compression is expressed as ``RemoveMessage`` ops + one
    ``AIMessage`` so the ``add_messages`` reducer rewrites history
    by id (otherwise the reducer would *append* the digest while
    keeping the originals, defeating compression). Messages without
    a stable ``id`` are left in place — better to undercount than
    to corrupt history.

    Returns an empty dict (no-op) when usage is below the compress
    threshold or there are fewer than 7 messages total.
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
    """Synthesise ``tool_results`` into the final answer for the user.

    Terminal node of the task path. Behaviour:

    * Always emits a ``emit_turn_tool_stats`` event so the UI can
      clear any stale "failed N" badge from the agent card — even
      on the pass-through case where the specialist answered
      directly without calling any tools.
    * If ``state.answer`` is already populated and there are no
      ``tool_results``, returns ``{}`` (the specialist's direct
      answer is already in ``state``).
    * If an LLM is available and there *are* tool results, asks it
      to summarise the results in the specialist's voice and
      ensures the response is prefixed with ``[<AGENT> AGENT]``.
    * Otherwise falls back to a deterministic ``[OK] / [FAILED]``
      bullet list per tool, truncated at 500 characters per output.

    When ``state.secondary_routes`` is non-empty, appends a
    ``[RECOMMENDATION]`` line suggesting related specialists — this
    surfaces the compound-command information that the orchestrator
    picked up but didn't act on (we only execute the primary route
    in a single turn).
    """
    agent_type = state.routed_to
    prefix = f"[{agent_type.upper()} AGENT]"

    # ZZ.A3 #303-3: emit per-turn tool-execution summary before we
    # branch into the answer-synthesis paths. Runs at every turn end
    # (even the "no tools were called" pass-through below) so the UI
    # sees a zeroed snapshot and can clear any stale "failed N" badge
    # from a previous turn on the same agent card.
    try:
        tool_call_count = len(state.tool_results)
        failed_tools = [r.tool_name for r in state.tool_results if not r.success]
        emit_turn_tool_stats(
            agent_type,
            tool_call_count,
            len(failed_tools),
            failed_tools=failed_tools,
            task_id=state.task_id,
            broadcast_scope="global",
        )
    except Exception as exc:  # pragma: no cover — best-effort telemetry
        logger.debug("emit_turn_tool_stats skipped: %s", exc)

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

firmware_node = _guild_node_factory("firmware")
software_node = _guild_node_factory("software")
validator_node = _guild_node_factory("validator")
reporter_node = _guild_node_factory("reporter")
reviewer_node = _guild_node_factory("reviewer")
general_node = _guild_node_factory("general")
