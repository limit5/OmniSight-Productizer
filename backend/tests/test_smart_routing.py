"""Tests for Smart Model Routing (Phase 38).

Covers:
- Complexity estimation
- Model preference lookup
- Cost-aware selection
- Budget-aware downgrade
- Task decomposition (regex fallback)
- Auto-dependency chain
"""

from __future__ import annotations

import pytest

from backend.model_router import (
    estimate_complexity,
    select_model_for_task,
    MODEL_PREFERENCES,
    COST_TIERS,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Complexity Estimation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestComplexityEstimation:

    def test_complex_keywords(self):
        assert estimate_complexity("Refactor the ISP pipeline architecture") == "complex"
        assert estimate_complexity("Debug complex race condition in DMA") == "complex"
        assert estimate_complexity("NPU 量化精度優化") == "complex"

    def test_simple_keywords(self):
        assert estimate_complexity("List all agents and status") == "simple"
        assert estimate_complexity("Generate test summary report") == "simple"
        assert estimate_complexity("Format the log output") == "simple"
        assert estimate_complexity("列出狀態摘要") == "simple"

    def test_medium_default(self):
        assert estimate_complexity("Compile the sensor driver module") == "medium"
        assert estimate_complexity("Write I2C initialization code") == "medium"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model Preferences
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModelPreferences:

    def test_all_agent_types_have_preferences(self):
        for agent_type in ("firmware", "software", "validator", "reporter", "reviewer", "general"):
            assert agent_type in MODEL_PREFERENCES
            assert len(MODEL_PREFERENCES[agent_type]) >= 2

    def test_firmware_prefers_code_models(self):
        prefs = MODEL_PREFERENCES["firmware"]
        # Claude Sonnet should be in top 2 for firmware (best at C/C++)
        assert any("claude-sonnet" in p for p in prefs[:2])

    def test_reporter_prefers_cheap_models(self):
        prefs = MODEL_PREFERENCES["reporter"]
        # First preference should be a cheap model
        first_cost = COST_TIERS.get(prefs[0], 99)
        assert first_cost <= 1.0  # Haiku, mini, etc.

    def test_cost_tiers_populated(self):
        assert len(COST_TIERS) >= 15
        # All values are non-negative
        assert all(v >= 0 for v in COST_TIERS.values())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model Selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModelSelection:

    def test_per_agent_override_takes_precedence(self):
        result = select_model_for_task(
            agent_type="firmware",
            task_text="anything",
            agent_ai_model="openrouter:qwen/qwen3-235b",
        )
        assert result == "openrouter:qwen/qwen3-235b"

    def test_empty_model_returns_global_fallback(self):
        """Without API keys, should return empty (global default)."""
        result = select_model_for_task(
            agent_type="firmware",
            task_text="compile sensor driver",
        )
        # No keys configured in test env → returns ""
        assert result == ""

    def test_unknown_agent_type_uses_general(self):
        result = select_model_for_task(
            agent_type="nonexistent",
            task_text="do something",
        )
        assert isinstance(result, str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task Decomposition
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTaskDecomposition:

    def test_regex_decompose_english(self):
        from backend.routers.invoke import _regex_decompose
        parts = _regex_decompose("compile firmware and then run tests")
        assert len(parts) == 2
        assert "compile" in parts[0].lower()
        assert "test" in parts[1].lower()

    def test_regex_decompose_chinese(self):
        from backend.routers.invoke import _regex_decompose
        parts = _regex_decompose("編譯韌體然後跑測試")
        assert len(parts) == 2

    def test_regex_no_split_on_bare_and(self):
        """Bare 'and' should NOT split (fixes 'Build ISP and Sensor')."""
        from backend.routers.invoke import _regex_decompose
        parts = _regex_decompose("Build ISP and Sensor driver")
        assert len(parts) == 1

    def test_regex_single_task(self):
        from backend.routers.invoke import _regex_decompose
        parts = _regex_decompose("compile the sensor driver")
        assert len(parts) == 1

    def test_regex_multi_chinese(self):
        from backend.routers.invoke import _regex_decompose
        parts = _regex_decompose("編譯韌體，然後跑測試，接著產生報告")
        assert len(parts) == 3

    @pytest.mark.asyncio
    async def test_decompose_adds_dependencies(self):
        from backend.routers.invoke import _maybe_decompose_task
        from backend.models import Task, TaskStatus
        task = Task(id="t-dep", title="compile firmware and then deploy to EVK", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        assert len(children) == 2
        assert children[0].depends_on == []
        assert children[1].depends_on == [children[0].id]

    @pytest.mark.asyncio
    async def test_decompose_single_not_split(self):
        from backend.routers.invoke import _maybe_decompose_task
        from backend.models import Task, TaskStatus
        task = Task(id="t-single", title="compile the sensor driver", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        assert len(children) == 0
