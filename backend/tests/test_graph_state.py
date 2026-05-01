"""Schema-level unit tests for ``backend.agents.state.GraphState``.

These tests only cover the Pydantic field shape — they do **not**
exercise the LangGraph topology selection logic (that lives in
``test_topology_smxl.py`` per BP.C.7). The split keeps schema-shape
regressions surfaced here isolated from topology-behaviour churn.

Currently the file focuses on BP.C.5 — the ``size`` field. Add new
schema-only tests here when other ``GraphState`` fields land.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.agents.state import GraphState


class TestGraphStateSizeField:
    """BP.C.5: ``size: Literal["S","M","XL"]`` with default ``"M"``."""

    def test_default_is_M(self) -> None:
        """Unset ``size`` must default to ``"M"`` (standard DAG).

        Rationale: pre-BP.C call sites and tests don't populate this
        field; their behaviour must remain unchanged (i.e. the
        standard DAG topology) until BP.C.1's sizer wires the
        explicit value in.
        """
        state = GraphState()
        assert state.size == "M"

    @pytest.mark.parametrize("size", ["S", "M", "XL"])
    def test_accepts_each_literal(self, size: str) -> None:
        """Every member of the literal must round-trip without coercion."""
        state = GraphState(size=size)  # type: ignore[arg-type]
        assert state.size == size

    @pytest.mark.parametrize(
        "bad_value",
        [
            "L",        # reasonable typo — ensure it's rejected
            "s",        # case-sensitive
            "small",
            "",
            "M ",       # whitespace not stripped
            None,       # explicit None is not allowed (no Optional)
            123,        # int is not a string literal
        ],
    )
    def test_rejects_non_literal_values(self, bad_value: object) -> None:
        """Anything outside the closed literal set must raise."""
        with pytest.raises(ValidationError):
            GraphState(size=bad_value)  # type: ignore[arg-type]

    def test_size_is_orthogonal_to_other_fields(self) -> None:
        """Setting ``size`` must not perturb sibling defaults.

        Guards against accidental field reordering / shared-default
        regressions when future BP.C rows extend ``GraphState``.
        """
        state = GraphState(size="XL")
        assert state.size == "XL"
        assert state.user_command == ""
        assert state.routed_to == "general"
        assert state.sandbox_tier == "t1"
        assert state.user_role == "operator"
        assert state.retry_count == 0
        assert state.is_conversational is False
        assert state.soc_vendor == ""
        assert state.sdk_version == ""

    def test_existing_callers_unaffected(self) -> None:
        """Pre-BP.C construction patterns still work unchanged.

        Mirrors how ``backend.agents.nodes`` and the LangGraph
        pipeline build state today (no ``size=`` kwarg). The default
        guarantees backward compatibility.
        """
        state = GraphState(
            user_command="do the thing",
            routed_to="frontend",
            model_name="claude-haiku-4.5",
        )
        assert state.size == "M"
        assert state.user_command == "do the thing"
        assert state.routed_to == "frontend"
