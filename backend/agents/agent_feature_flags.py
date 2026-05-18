"""AUDIT-29b agent/runtime feature flags.

The runner reads these flags synchronously at pickup time. Values are not
cached so an operator env change is picked up on the next runner tick.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

EnvMode = Literal["enabled_values", "disabled_values"]


@dataclass(frozen=True)
class AgentFeatureFlag:
    """One env-backed agent feature flag."""

    name: str
    env_name: str
    default: bool
    env_mode: EnvMode = "enabled_values"

    def enabled(self) -> bool:
        raw = os.environ.get(self.env_name)
        if raw is None:
            return self.default
        value = raw.strip().lower()
        if self.env_mode == "disabled_values":
            return value not in _TRUE_VALUES
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
        return self.default


failure_graph = AgentFeatureFlag(
    name="failure_graph",
    env_name="OMNISIGHT_FAILURE_GRAPH_ENABLED",
    default=False,
)
project_state = AgentFeatureFlag(
    name="project_state",
    env_name="OMNISIGHT_PROJECT_STATE_INJECT",
    default=False,
)
ops_only = AgentFeatureFlag(
    name="ops_only",
    env_name="OMNISIGHT_RUNNER_OPS_ONLY_DISABLED",
    default=True,
    env_mode="disabled_values",
)
coord_skip = AgentFeatureFlag(
    name="coord_skip",
    env_name="OMNISIGHT_COORD_SKIP",
    default=False,
)
cognee_recall = AgentFeatureFlag(
    name="cognee_recall",
    env_name="OMNISIGHT_COGNEE_RECALL",
    default=False,
)
antipattern_inject = AgentFeatureFlag(
    name="antipattern_inject",
    env_name="OMNISIGHT_ANTIPATTERN_INJECT",
    default=False,
)
reflection_rag_prompt = AgentFeatureFlag(
    name="reflection_rag_prompt",
    env_name="OMNISIGHT_REFLECTION_RAG_PROMPT",
    default=False,
)

ALL_FLAGS = (
    failure_graph,
    project_state,
    ops_only,
    coord_skip,
    cognee_recall,
    antipattern_inject,
    reflection_rag_prompt,
)


def is_project_state_inject_enabled_sync() -> bool:
    """Compatibility helper for OP-905 runner project-state injection."""
    return project_state.enabled()


__all__ = [
    "ALL_FLAGS",
    "AgentFeatureFlag",
    "antipattern_inject",
    "cognee_recall",
    "coord_skip",
    "failure_graph",
    "is_project_state_inject_enabled_sync",
    "ops_only",
    "project_state",
    "reflection_rag_prompt",
]
