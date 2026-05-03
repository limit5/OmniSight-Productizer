"""Phase 3 — Sub-agent dispatch (the `Agent` tool).

When a parent agentic loop hits a multi-step sub-task that benefits from
context isolation (a fresh transcript) or a specialized tool set (e.g.,
read-only exploration), it can call the ``Agent`` tool. This module
implements the handler: a nested
:meth:`AnthropicClient.run_with_tools` invocation with its own system
prompt, tool list, model, and iteration cap, returning the sub-agent's
final text back into the parent's tool_result block.

Why not just have the parent loop do everything directly?

  * **Transcript isolation** — long exploration burns parent's cache and
    fills its working memory. Sub-agent's full transcript is summarized
    into one text result.
  * **Tool scoping** — an ``Explore`` sub-agent should be read-only;
    a ``Plan`` sub-agent shouldn't write code. Forcing the parent to
    drop tools mid-loop is awkward; isolating in a sub-call is clean.
  * **Cost containment** — independent ``max_iterations`` per
    sub-agent prevents one open-ended exploration from eating the
    parent's whole budget.

What's intentionally NOT supported in v1:

  * ``isolation: "worktree"`` — would need git-worktree management; defer
    until a real use case demands it.
  * ``run_in_background`` — sub-agents are synchronous wrt parent; the
    parent's tool_result block must contain the return value.
  * **Sub-of-sub recursion** — ``DEFAULT_SUBAGENT_TYPES`` deliberately
    omits ``Agent`` from the tools list, so a sub-agent cannot itself
    spawn another sub-agent. Avoids fan-out budget surprises.

Shared with backend specialist agents (HD / BSP / HAL / etc.): any
agent built on top of :class:`AnthropicClient` can register the same
handler to expose Agent-tool semantics inside its own loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.agents.anthropic_native_client import (
    DEFAULT_MODEL_HAIKU,
    DEFAULT_MODEL_OPUS,
    DEFAULT_MODEL_SONNET,
    AnthropicClient,
)

logger = logging.getLogger(__name__)


_MODEL_ALIAS: dict[str, str] = {
    "opus": DEFAULT_MODEL_OPUS,
    "sonnet": DEFAULT_MODEL_SONNET,
    "haiku": DEFAULT_MODEL_HAIKU,
}


@dataclass(frozen=True)
class SubAgentTypeSpec:
    """Configuration for one named sub-agent type.

    The set of types defines the menu the parent LLM can pick from via
    the ``subagent_type`` field of the ``Agent`` tool input.
    """

    name: str
    """Identifier (e.g., ``"Explore"``, ``"Plan"``, ``"general-purpose"``)."""

    tools: tuple[str, ...]
    """Tools the sub-agent is allowed to use. **Should not include
    ``Agent``** — see module docstring for the no-recursion rationale."""

    system_prefix: str
    """Pre-prompt that frames the sub-agent's role. The user-supplied
    ``description`` and ``prompt`` are appended after this."""

    default_model: str = DEFAULT_MODEL_SONNET
    """Model used when the parent doesn't supply one. Sonnet by default
    so sub-agents are cheap; parent can override per call."""

    max_iterations: int = 25
    """Per-sub-agent tool-loop ceiling. Independent of parent's cap."""

    max_tokens: int = 8192
    """Output token cap per sub-call response."""

    enable_cache: bool = True


# ─── Default catalog ─────────────────────────────────────────────


_GENERAL_PROMPT = (
    "You are a sub-agent invoked from a parent agentic loop. Your job is "
    "to complete the brief below and return ONE concise summary of what "
    "you did + any findings the parent needs.\n\n"
    "Constraints:\n"
    "  * The parent will read ONLY your final text — your tool calls are "
    "    invisible to it.\n"
    "  * If the brief asks you to research / explore, return findings "
    "    bullet-pointed; the parent will decide what to do.\n"
    "  * Don't ask follow-up questions; you are autonomous within the "
    "    iteration cap.\n"
    "  * On finish, summarise plainly. Don't echo back tool output.\n"
)

_EXPLORE_PROMPT = (
    "You are an Explore sub-agent. Read-only — Read / Grep / Glob only. "
    "Do NOT modify files. Your output is a structured findings summary "
    "for the parent: which files, which symbols, which patterns matter. "
    "Concise bullet form. End with '== END FINDINGS =='."
)

_PLAN_PROMPT = (
    "You are a Plan sub-agent. Read-only research; produce a step-by-step "
    "implementation plan. Do NOT modify files. Output structure:\n"
    "  1. Goal restatement (1 sentence)\n"
    "  2. Files / modules touched\n"
    "  3. Step list (numbered, each step = 1 commit unit)\n"
    "  4. Key trade-offs / open questions\n"
    "End with '== END PLAN =='."
)


DEFAULT_SUBAGENT_TYPES: dict[str, SubAgentTypeSpec] = {
    "general-purpose": SubAgentTypeSpec(
        name="general-purpose",
        tools=("Read", "Write", "Edit", "Bash", "Grep", "Glob"),
        system_prefix=_GENERAL_PROMPT,
    ),
    "Explore": SubAgentTypeSpec(
        name="Explore",
        tools=("Read", "Grep", "Glob"),
        system_prefix=_EXPLORE_PROMPT,
        max_iterations=30,
    ),
    "Plan": SubAgentTypeSpec(
        name="Plan",
        tools=("Read", "Grep", "Glob"),
        system_prefix=_PLAN_PROMPT,
        max_iterations=20,
    ),
}


# ─── Handler factory ─────────────────────────────────────────────


SubAgentObserver = Callable[[dict[str, Any]], None]


def make_agent_tool_handler(
    *,
    client: AnthropicClient,
    types_map: dict[str, SubAgentTypeSpec] | None = None,
    parent_system_suffix: str = "",
    on_dispatch: SubAgentObserver | None = None,
):
    """Build an async ``Agent``-tool handler bound to ``client``.

    Args:
      client: AnthropicClient whose dispatcher already has the basic
        tool handlers (Read/Write/etc) registered. The sub-agent uses
        the SAME dispatcher — sandboxing rules are inherited.
      types_map: Override / extend the default subagent types.
        ``"general-purpose"`` MUST be present (fallback for unknown
        ``subagent_type`` values from the LLM).
      parent_system_suffix: Optional text the parent wants appended to
        every sub-agent's system prompt — e.g., the parent's PROJECT_ROOT
        block so paths resolve identically.
      on_dispatch: Optional observer fired on every Agent tool call;
        receives ``{description, subagent_type, prompt_chars, model,
        max_iterations}``. Useful for the parent to log / count sub-agents.

    Returns:
      An async callable suitable for ``ToolDispatcher.register("Agent", h)``.

    Contract:
      ``run_in_background`` is intentionally unsupported. Sub-agent output is
      the parent tool_result payload, so the handler rejects background
      requests before dispatching the observer or nested client call.
    """
    types = types_map or DEFAULT_SUBAGENT_TYPES
    if "general-purpose" not in types:
        raise ValueError("types_map must contain 'general-purpose' fallback")

    async def _handler(payload: dict[str, Any]) -> str:
        description = str(payload.get("description", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        if not description:
            raise ValueError("Agent tool requires 'description'")
        if not prompt:
            raise ValueError("Agent tool requires 'prompt'")

        requested_type = str(payload.get("subagent_type", "")).strip() or "general-purpose"
        spec = types.get(requested_type) or types["general-purpose"]
        if requested_type not in types:
            logger.info(
                "Agent: unknown subagent_type %r, falling back to general-purpose",
                requested_type,
            )

        # Model override (LLM gives "opus"/"sonnet"/"haiku" friendly aliases).
        requested_model = str(payload.get("model", "")).strip().lower()
        model = _MODEL_ALIAS.get(requested_model, spec.default_model)

        if payload.get("run_in_background"):
            # v1: refuse explicitly. Sub-agents must be sync to fit into a
            # parent tool_result block.
            raise NotImplementedError(
                "run_in_background not supported for sub-agents in v1; "
                "the parent loop needs the sub-agent's text synchronously."
            )
        if payload.get("isolation"):
            logger.warning(
                "Agent: isolation=%r requested but not implemented in v1; ignoring",
                payload["isolation"],
            )

        sub_system = spec.system_prefix
        if parent_system_suffix:
            sub_system = f"{sub_system}\n\n{parent_system_suffix}"

        sub_prompt = (
            f"## Brief from parent\n"
            f"**Description**: {description}\n\n"
            f"**Detailed prompt**:\n{prompt}"
        )

        if on_dispatch is not None:
            try:
                on_dispatch(
                    {
                        "description": description,
                        "subagent_type": spec.name,
                        "prompt_chars": len(prompt),
                        "model": model,
                        "max_iterations": spec.max_iterations,
                    }
                )
            except Exception:  # noqa: BLE001 - observer boundary
                logger.exception("Agent: on_dispatch raised, swallowing")

        result = await client.run_with_tools(
            prompt=sub_prompt,
            tools=list(spec.tools),
            system=sub_system,
            model=model,
            max_tokens=spec.max_tokens,
            max_iterations=spec.max_iterations,
            enable_cache=spec.enable_cache,
            on_tool_call="silent",  # don't pollute parent log with sub tools
        )

        text = result.final_text.strip() or "(sub-agent returned no text)"
        # Decorate so the parent LLM clearly sees this is sub-agent output,
        # plus a short metadata footer for transparency.
        usage = result.usage
        footer = (
            f"\n\n---\n_(sub-agent: {spec.name}; "
            f"iterations={result.iterations}; stop={result.stop_reason}; "
            f"input={usage.input_tokens} output={usage.output_tokens} "
            f"cache_read={usage.cache_read_input_tokens})_"
        )
        return f"## Sub-agent ({spec.name}) result\n\n{text}{footer}"

    return _handler


def list_default_subagent_types() -> list[str]:
    """Public list of subagent type names (for CLI / banner / tests)."""
    return sorted(DEFAULT_SUBAGENT_TYPES.keys())
