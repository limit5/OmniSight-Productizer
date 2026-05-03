"""BP.A.1 — Contract tests for ``backend/templates/spec.py``.

These tests pin the SpecTemplate validation surface so that downstream
template work (BP.A.2 TaskTemplate / BP.A.6 template_validator middleware)
can rely on the boundaries being enforced *at the model layer*, not in
ad-hoc if-checks inside FastAPI handlers.

BP.A.7 will later fold a superset of these checks into the unified
~150-test ``test_templates.py`` suite. Until then this file is the
authoritative regression for SpecTemplate alone — keep it green.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.templates.spec import SCHEMA_VERSION, SpecTemplate


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        system_boundaries=[
            "does NOT touch billing service",
            "does NOT touch auth service",
            "does NOT mutate user storage",
        ],
        hardware_constraints=[
            "arm64 cortex-a55 SoC",
            "512 MB DDR4 RAM ceiling",
            "5 W TDP power budget",
        ],
        api_idl_schema="openapi: 3.0.0\ninfo:\n  title: example\n  version: 0.1\n",
        bdd_executable_specs=(
            "Feature: example\n"
            "  Scenario: minimal\n"
            "    Given a precondition\n"
            "    When an event\n"
            "    Then an outcome\n"
        ),
        edge_cases_handled=[
            "power-loss mid-write",
            "network partition during sync",
            "wall-clock skew across nodes",
        ],
    )
    base.update(overrides)
    return base


class TestSpecTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = SpecTemplate(**_valid_payload())
        assert t.schema_version == SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = SpecTemplate(**_valid_payload())
        with pytest.raises(ValidationError):
            t.system_boundaries = []  # type: ignore[misc]

    def test_strips_whitespace(self) -> None:
        payload = _valid_payload()
        payload["api_idl_schema"] = "  openapi: 3.0.0  "
        t = SpecTemplate(**payload)
        assert t.api_idl_schema == "openapi: 3.0.0"

    def test_json_round_trip(self) -> None:
        original = SpecTemplate(**_valid_payload())
        rebuilt = SpecTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original


class TestSpecTemplateMinLengthGuards:
    @pytest.mark.parametrize(
        "field",
        ["system_boundaries", "hardware_constraints", "edge_cases_handled"],
    )
    def test_three_item_lists_reject_two_entries(self, field: str) -> None:
        payload = _valid_payload(**{field: ["only-a", "only-b"]})
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        errs = exc.value.errors()
        assert any(
            e["type"] == "too_short" and e["loc"][0] == field for e in errs
        ), errs

    @pytest.mark.parametrize(
        "field",
        ["system_boundaries", "hardware_constraints", "edge_cases_handled"],
    )
    def test_three_item_lists_accept_exactly_three(self, field: str) -> None:
        payload = _valid_payload(**{field: ["x", "y", "z"]})
        t = SpecTemplate(**payload)
        assert len(getattr(t, field)) == 3

    @pytest.mark.parametrize("field", ["api_idl_schema", "bdd_executable_specs"])
    def test_string_fields_reject_empty(self, field: str) -> None:
        payload = _valid_payload(**{field: ""})
        with pytest.raises(ValidationError):
            SpecTemplate(**payload)


class TestSpecTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _valid_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _valid_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_required_field_reports_clear_error(self) -> None:
        payload = _valid_payload()
        del payload["edge_cases_handled"]
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("edge_cases_handled",) in locs
