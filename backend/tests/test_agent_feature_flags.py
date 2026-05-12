"""AUDIT-29b agent runtime feature flags."""
from __future__ import annotations

import importlib

import pytest

from backend.agents import agent_feature_flags as flags


def test_canonical_agent_flags_are_defined() -> None:
    assert [flag.name for flag in flags.ALL_FLAGS] == [
        "failure_graph",
        "project_state",
        "ops_only",
        "coord_skip",
        "cognee_recall",
        "antipattern_inject",
    ]
    assert flags.failure_graph.env_name == "OMNISIGHT_FAILURE_GRAPH_ENABLED"
    assert flags.project_state.env_name == "OMNISIGHT_PROJECT_STATE_INJECT"
    assert flags.ops_only.env_name == "OMNISIGHT_RUNNER_OPS_ONLY_DISABLED"
    assert flags.coord_skip.env_name == "OMNISIGHT_COORD_SKIP"
    assert flags.cognee_recall.env_name == "OMNISIGHT_COGNEE_RECALL"
    assert flags.antipattern_inject.env_name == "OMNISIGHT_ANTIPATTERN_INJECT"


def test_agent_flags_default_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in flags.ALL_FLAGS:
        monkeypatch.delenv(flag.env_name, raising=False)

    assert flags.failure_graph.enabled() is False
    assert flags.project_state.enabled() is False
    assert flags.ops_only.enabled() is True
    assert flags.coord_skip.enabled() is False
    assert flags.cognee_recall.enabled() is False
    assert flags.antipattern_inject.enabled() is False


@pytest.mark.parametrize(
    "flag",
    [
        flags.failure_graph,
        flags.project_state,
        flags.coord_skip,
        flags.cognee_recall,
        flags.antipattern_inject,
    ],
)
def test_enabled_value_env_overrides_false_defaults(
    monkeypatch: pytest.MonkeyPatch,
    flag: flags.AgentFeatureFlag,
) -> None:
    monkeypatch.setenv(flag.env_name, "1")
    assert flag.enabled() is True

    monkeypatch.setenv(flag.env_name, "false")
    assert flag.enabled() is False


def test_ops_only_uses_existing_disabled_env_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(flags.ops_only.env_name, raising=False)
    assert flags.ops_only.enabled() is True

    monkeypatch.setenv(flags.ops_only.env_name, "1")
    assert flags.ops_only.enabled() is False

    monkeypatch.setenv(flags.ops_only.env_name, "0")
    assert flags.ops_only.enabled() is True


def test_env_reads_are_not_import_time_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(flags.cognee_recall.env_name, raising=False)
    assert flags.cognee_recall.enabled() is False

    monkeypatch.setenv(flags.cognee_recall.env_name, "yes")
    assert flags.cognee_recall.enabled() is True

    reloaded = importlib.reload(flags)
    assert reloaded.cognee_recall.enabled() is True
    monkeypatch.setenv(reloaded.cognee_recall.env_name, "no")
    assert reloaded.cognee_recall.enabled() is False


def test_project_state_compatibility_helper_reads_canonical_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(flags.project_state.env_name, "on")
    assert flags.is_project_state_inject_enabled_sync() is True

    monkeypatch.setenv(flags.project_state.env_name, "off")
    assert flags.is_project_state_inject_enabled_sync() is False
