"""Sora orchestrator model auto-routing (P2, supervisor roadmap).

Covers `_resolve_orchestrator_model`: manual pin wins, auto-route maps
complexity → Sora's model tier, env kill-switch, and fail-open.
See backend/agents/nodes.py + docs/design/rpg/sora-supervisor-roadmap.md.
"""

import os

import pytest

from backend.agents.nodes import _resolve_orchestrator_model, _SORA_ROUTE_MODEL


def test_manual_pin_wins_over_auto():
    model, reason = _resolve_orchestrator_model("anthropic:claude-opus-4-8", "hi there")
    assert model == "anthropic:claude-opus-4-8"
    assert reason == "manual_override"


def test_auto_route_short_chat_goes_small():
    model, reason = _resolve_orchestrator_model("", "hi")
    assert model == _SORA_ROUTE_MODEL["small"]
    assert reason == "auto:small"


def test_auto_route_heavy_refactor_goes_large():
    prompt = "幫我重構整個模組並拆解成多個子任務，跨多個檔案設計新的架構"
    model, reason = _resolve_orchestrator_model("auto", prompt)
    assert model == _SORA_ROUTE_MODEL["large"]
    assert reason == "auto:large"


def test_env_kill_switch_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_ORCHESTRATOR_AUTO_ROUTE", "0")
    model, reason = _resolve_orchestrator_model("", "anything")
    assert model == ""  # empty → _get_llm uses the configured default
    assert reason == "auto_route_disabled"


def test_tiers_are_distinct_anthropic_specs():
    # Each tier is an explicit provider:model spec, all distinct.
    vals = set(_SORA_ROUTE_MODEL.values())
    assert len(vals) == 3
    assert all(v.startswith("anthropic:") for v in vals)
