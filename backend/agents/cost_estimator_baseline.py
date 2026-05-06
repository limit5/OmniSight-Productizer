"""MP.W2.2 -- initial cost-estimator baseline data.

The MP.W2 cost estimator starts from ADR-0007's session-level
13-epic averages, keyed by ``agent_class``.  Later MP.W2 tickets own
prediction features, tenant calibration, and API exposure; this module
only supplies the immutable seed rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentClassCostBaseline:
    """Initial average per task for one ``agent_class``."""

    agent_class: str
    avg_tokens_per_item: int
    avg_wall_time_seconds: int
    api_rate_cost_usd: float
    source_epic_count: int
    predicted: bool = False


BASELINE_SOURCE = "ADR-0007 cost prediction model, 13-epic session average"

AGENT_CLASS_COST_BASELINES: dict[str, AgentClassCostBaseline] = {
    "subscription-codex": AgentClassCostBaseline(
        agent_class="subscription-codex",
        avg_tokens_per_item=12_000,
        avg_wall_time_seconds=4 * 60,
        api_rate_cost_usd=0.05,
        source_epic_count=13,
    ),
    "api-anthropic": AgentClassCostBaseline(
        agent_class="api-anthropic",
        avg_tokens_per_item=25_000,
        avg_wall_time_seconds=6 * 60,
        api_rate_cost_usd=0.20,
        source_epic_count=13,
        predicted=True,
    ),
    "api-openai": AgentClassCostBaseline(
        agent_class="api-openai",
        avg_tokens_per_item=15_000,
        avg_wall_time_seconds=5 * 60,
        api_rate_cost_usd=0.08,
        source_epic_count=13,
        predicted=True,
    ),
}


def get_agent_class_cost_baseline(agent_class: str) -> AgentClassCostBaseline:
    """Return the seed baseline for ``agent_class``."""
    key = agent_class.strip()
    try:
        return AGENT_CLASS_COST_BASELINES[key]
    except KeyError as exc:
        raise KeyError(f"No MP.W2.2 cost baseline for agent_class {key!r}") from exc


def list_agent_class_cost_baselines() -> tuple[AgentClassCostBaseline, ...]:
    """Return all seed baselines in stable ``agent_class`` order."""
    return tuple(
        AGENT_CLASS_COST_BASELINES[key]
        for key in sorted(AGENT_CLASS_COST_BASELINES)
    )


__all__ = [
    "AGENT_CLASS_COST_BASELINES",
    "AgentClassCostBaseline",
    "BASELINE_SOURCE",
    "get_agent_class_cost_baseline",
    "list_agent_class_cost_baselines",
]
