"""Smart Model Router — selects the best provider:model for each task.

Uses three signals to choose:
  1. Agent type preferences (firmware prefers strong code models)
  2. Task complexity (simple → cheap/fast, complex → powerful)
  3. Token budget awareness (high usage → downgrade to cheaper model)

The router only suggests — it never overrides an explicit per-agent ai_model.
"""

from __future__ import annotations

import logging
import re

from backend.config import settings
from backend.events import emit_pipeline_phase

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cost tiers (approximate USD per 1M tokens, input+output average)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COST_TIERS: dict[str, float] = {
    # Tier 1: Premium ($5-30/1M)
    "anthropic:claude-opus-4-20250514": 25.0,
    "openai:gpt-4o": 7.5,
    "openrouter:anthropic/claude-opus-4": 25.0,
    "openrouter:openai/gpt-4o": 7.5,
    # Tier 2: Standard ($1-5/1M)
    "anthropic:claude-sonnet-4-20250514": 4.5,
    "openrouter:anthropic/claude-sonnet-4": 4.5,
    "google:gemini-1.5-pro": 3.5,
    "openrouter:google/gemini-2.5-pro-preview": 3.5,
    "openrouter:mistralai/mistral-large": 3.0,
    "openrouter:cohere/command-r-plus": 3.0,
    "openrouter:qwen/qwen3-235b-a22b": 2.0,
    "deepseek:deepseek-chat": 1.0,
    # Tier 3: Budget ($0-1/1M)
    "anthropic:claude-haiku-4-20250506": 0.5,
    "openai:gpt-4o-mini": 0.3,
    "groq:llama-3.3-70b-versatile": 0.3,
    "openrouter:google/gemini-2.5-flash-preview": 0.3,
    "openrouter:qwen/qwen3-32b": 0.2,
    "openrouter:meta-llama/llama-4-scout": 0.2,
    # Tier 4: Free/Local
    "ollama:llama3.1": 0.0,
    "ollama:qwen2.5": 0.0,
    "ollama:deepseek-r1": 0.0,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent type → model preferences (ordered by priority)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MODEL_PREFERENCES: dict[str, list[str]] = {
    "firmware": [
        "anthropic:claude-sonnet-4-20250514",   # Best at C/C++ code
        "openrouter:anthropic/claude-sonnet-4",
        "openai:gpt-4o",
        "openrouter:qwen/qwen3-235b-a22b",
        "deepseek:deepseek-chat",
        "groq:llama-3.3-70b-versatile",
    ],
    "software": [
        "anthropic:claude-sonnet-4-20250514",
        "openrouter:anthropic/claude-sonnet-4",
        "openai:gpt-4o",
        "openrouter:qwen/qwen3-235b-a22b",
        "deepseek:deepseek-chat",
        "groq:llama-3.3-70b-versatile",
    ],
    "validator": [
        "openai:gpt-4o",                        # Strong reasoning
        "anthropic:claude-sonnet-4-20250514",
        "openrouter:qwen/qwen3-235b-a22b",
        "groq:llama-3.3-70b-versatile",         # Fast for test execution
    ],
    "reporter": [
        "anthropic:claude-haiku-4-20250506",     # Fast + cheap for text generation
        "openai:gpt-4o-mini",
        "openrouter:google/gemini-2.5-flash-preview",
        "groq:llama-3.3-70b-versatile",
    ],
    "reviewer": [
        "anthropic:claude-sonnet-4-20250514",    # Deep code understanding
        "openrouter:anthropic/claude-sonnet-4",
        "openai:gpt-4o",
    ],
    "general": [
        "anthropic:claude-sonnet-4-20250514",
        "openai:gpt-4o",
        "openrouter:qwen/qwen3-235b-a22b",
        "groq:llama-3.3-70b-versatile",
        "ollama:llama3.1",
    ],
}

# Complexity keywords
_COMPLEX_KEYWORDS = re.compile(
    r"(architect|refactor|design|optimize|security|audit|migration|integration|"
    r"debug.*complex|race.?condition|memory.?leak|performance|NPU|量化|架構|重構|效能)",
    re.IGNORECASE,
)
_SIMPLE_KEYWORDS = re.compile(
    r"(rename|format|log|comment|typo|simple|status|list|report|summary|"
    r"摘要|報告|列出|狀態|格式)",
    re.IGNORECASE,
)


def estimate_complexity(task_text: str) -> str:
    """Estimate task complexity from text.

    Returns: "simple", "medium", or "complex"
    """
    complex_hits = len(_COMPLEX_KEYWORDS.findall(task_text))
    simple_hits = len(_SIMPLE_KEYWORDS.findall(task_text))

    if complex_hits >= 2:
        return "complex"
    if complex_hits >= 1 and simple_hits == 0:
        return "complex"
    if simple_hits >= 2:
        return "simple"
    if simple_hits >= 1 and complex_hits == 0:
        return "simple"
    return "medium"


def _get_budget_ratio() -> float:
    """Get current token budget usage ratio (0.0 - 1.0+)."""
    try:
        from backend.routers.system import get_daily_cost
        budget = settings.token_budget_daily
        if budget <= 0:
            return 0.0  # Unlimited
        return get_daily_cost() / budget
    except Exception:
        return 0.0


def _is_provider_available(model_spec: str) -> bool:
    """Check if a provider:model is available (has API key and not in cooldown)."""
    try:
        from backend.agents.llm import validate_model_spec
        result = validate_model_spec(model_spec)
        if not result.get("valid", False):
            return False
    except Exception:
        return False
    # Also check cooldown — prefer the per-tenant per-key breaker; fall
    # back to the legacy global one for safety.
    provider = model_spec.split(":")[0] if ":" in model_spec else ""
    if provider:
        try:
            from backend import circuit_breaker
            from backend.db_context import current_tenant_id
            tid = current_tenant_id() or "t-default"
            fp = circuit_breaker.active_fingerprint(provider)
            if circuit_breaker.is_open(tid, provider, fp):
                return False
        except Exception:
            pass
        try:
            from backend.agents.llm import _provider_failures, PROVIDER_COOLDOWN
            import time
            if time.time() - _provider_failures.get(provider, 0) < PROVIDER_COOLDOWN:
                return False
        except Exception:
            pass
    return True


def select_model_for_task(
    agent_type: str,
    task_text: str,
    agent_ai_model: str = "",
) -> str:
    """Select the best model for a task based on agent type, complexity, and budget.

    Args:
        agent_type: The agent's type (firmware, software, validator, etc.)
        task_text: Task title + description for complexity estimation
        agent_ai_model: Per-agent model override — if set, returns it as-is

    Returns:
        Model spec in "provider:model" format, or "" for global default
    """
    # 1. Per-agent override takes absolute precedence
    if agent_ai_model:
        return agent_ai_model

    # 2. Get preferences for this agent type
    preferences = MODEL_PREFERENCES.get(agent_type, MODEL_PREFERENCES["general"])

    # 3. Complexity-based filtering
    complexity = estimate_complexity(task_text)

    # 4. Budget awareness — Phase 47C fix ①: honour BudgetStrategy.
    budget_ratio = _get_budget_ratio()
    try:
        from backend.budget_strategy import get_tuning as _get_tuning
        tuning = _get_tuning()
        tier = tuning.model_tier          # "premium" | "default" | "budget"
        downgrade_pct = tuning.downgrade_at_usage_pct / 100.0
    except Exception:
        tier, downgrade_pct = "default", 0.9

    budget_constrained = False
    if budget_ratio >= downgrade_pct:
        # Stratgy-aware cap when the token budget crosses the threshold.
        if tier == "budget":
            max_cost = 0.5
        elif tier == "premium":
            max_cost = 20.0
        else:
            max_cost = 5.0
        budget_constrained = True
    else:
        if tier == "budget":
            base = {"simple": 0.5, "complex": 5.0}.get(complexity, 2.0)
        elif tier == "premium":
            base = {"simple": 10.0, "complex": 100.0}.get(complexity, 30.0)
        else:
            base = {"simple": 2.0, "complex": 50.0}.get(complexity, 10.0)
        max_cost = base

    # Emit SSE when budget forces a downgrade
    if budget_constrained:
        try:
            emit_pipeline_phase(
                "smart_route",
                f"Budget {budget_ratio:.0%} — limiting {agent_type} to ≤${max_cost:.1f}/1M models",
            )
        except Exception:
            pass

    # 5. Find first available model within budget
    top_pref = preferences[0] if preferences else "global"
    for model_spec in preferences:
        cost = COST_TIERS.get(model_spec, 1.0)
        if cost > max_cost:
            continue
        if _is_provider_available(model_spec):
            # Emit SSE notification showing the routing decision
            short_model = model_spec.split(":")[-1] if ":" in model_spec else model_spec
            short_task = task_text[:50].replace("\n", " ")
            downgrade_note = ""
            if model_spec != top_pref and budget_constrained:
                top_short = top_pref.split(":")[-1] if ":" in top_pref else top_pref
                downgrade_note = f" (downgraded from {top_short})"
            try:
                emit_pipeline_phase(
                    "smart_route",
                    f"{agent_type} [{complexity}] → {short_model}{downgrade_note} | \"{short_task}\"",
                )
            except Exception:
                pass
            logger.info(
                "Smart routing: %s task [%s] → %s (cost=$%.1f/1M, budget=%.0f%%)",
                agent_type, complexity, model_spec, cost, budget_ratio * 100,
            )
            return model_spec

    # 6. Nothing available in preferences — return "" (use global default)
    logger.info("Smart routing: no preferred model available for %s, using global default", agent_type)
    return ""
