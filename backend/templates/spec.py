"""BP.A.1 — SpecTemplate Pydantic schema (Architect Guild output).

The first of the four Blueprint-v2 templates that gate every multi-agent
software-factory hand-off. A `SpecTemplate` is the contract emitted by the
Architect Guild and consumed downstream by the PM Guild (which produces
``TaskTemplate`` — BP.A.2). It pins the system surface so that downstream
guilds cannot silently expand scope.

Required fields (mirrors ``docs/design/blueprint-v2-implementation-plan.md``
Appendix B, with the lower bounds the appendix calls out as comments
promoted into Pydantic ``min_length`` constraints so a missing entry hard-
fails at the boundary instead of leaking into Cognitive Load scoring):

  - ``schema_version``       — pinned to ``"1.0.0"`` so future revisions
                               must add a discriminator instead of mutating.
  - ``system_boundaries``    — ≥ 3. Out-of-scope fences ("does NOT touch X").
  - ``hardware_constraints`` — ≥ 3. SoC / RAM / power / latency budgets.
  - ``api_idl_schema``       — single string holding OpenAPI 3.0 / Protobuf
                               / C++ header contents (validation is left to
                               BP.A.6 ``template_validator.py`` which can
                               sniff the dialect and dispatch).
  - ``bdd_executable_specs`` — Gherkin (``Feature: ... Scenario: ...``).
  - ``edge_cases_handled``   — ≥ 3 explicitly enumerated edge cases.

Cross-worker safety: this module declares no module-level mutable state,
no singletons, no in-memory cache. Every ``SpecTemplate`` instance is a
plain Pydantic value object — safe under ``uvicorn --workers N`` because
each worker constructs its own instances from the same JSON payload.

Cross-references:
  - BP.A.2 ``backend/templates/task.py``   — TaskTemplate
  - BP.A.3 ``backend/templates/impl.py``   — ImplTemplate
  - BP.A.4 ``backend/templates/review.py`` — ReviewTemplate
  - BP.A.6 ``backend/template_validator.py`` — FastAPI middleware that turns
    Pydantic ``ValidationError`` into the cognitive-penalty prompt.
  - BP.A.7 ``backend/tests/test_templates.py`` — unified ~150-test suite.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


class SpecTemplate(BaseModel):
    """Architect-Guild spec contract. Frozen, JSON-serialisable."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    schema_version: Literal["1.0.0"] = Field(
        default=SCHEMA_VERSION,
        description="Pinned schema version. Bump via discriminated union, never in-place.",
    )
    system_boundaries: list[str] = Field(
        ...,
        min_length=3,
        description=(
            "Out-of-scope fences. ≥ 3 entries; each entry is a single-line "
            "negative assertion (\"does NOT touch the billing service\")."
        ),
    )
    hardware_constraints: list[str] = Field(
        ...,
        min_length=3,
        description=(
            "Hardware envelope: SoC family, RAM ceiling, power budget, "
            "latency target, etc. ≥ 3 entries."
        ),
    )
    api_idl_schema: str = Field(
        ...,
        min_length=1,
        description=(
            "Interface contract. Holds an OpenAPI 3.0 document, a Protobuf "
            "``.proto`` body, or a C/C++ header — dialect detection happens "
            "in BP.A.6 template_validator."
        ),
    )
    bdd_executable_specs: str = Field(
        ...,
        min_length=1,
        description="Gherkin BDD (Feature / Scenario / Given-When-Then) source.",
    )
    edge_cases_handled: list[str] = Field(
        ...,
        min_length=3,
        description=(
            "Explicit edge cases the spec promises to cover. ≥ 3 entries — "
            "this is the bar the Cognitive Load scanner uses to flag a spec "
            "as under-specified before tasks are minted."
        ),
    )


__all__ = ["SCHEMA_VERSION", "SpecTemplate"]
