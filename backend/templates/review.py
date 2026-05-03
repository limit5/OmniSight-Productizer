"""BP.A.4 — ReviewTemplate Pydantic schema (Auditor Guild output).

The fourth and final Blueprint-v2 template. A ``ReviewTemplate`` is the
contract emitted by the Auditor Guild, downstream of the Coder Guild's
``ImplTemplate`` (BP.A.3). It pins the outcome of a single audit cycle —
what was found, the overall severity, who reviewed, and the actionable
recommendation.

Auxiliary disclaimer (schema-enforced):
  Two fields are pinned by the type system itself — they cannot carry any
  other value and it is impossible to construct a valid ``ReviewTemplate``
  without them:

  - ``audit_type: Literal["advisory"]`` — every Auditor-Guild output is
    advisory.  The AI reviewer may surface findings and recommend action,
    but it cannot authorise a merge.  This mirrors the project's Gerrit
    Code-Review rule: AI max score is +1; a human +2 is the hard gate for
    submission (see CLAUDE.md L1 Safety Rules).
  - ``requires_human_signoff: Literal[True]`` — always ``True``.  It is
    structurally impossible to emit a ``ReviewTemplate`` that claims human
    sign-off is not required.  Any downstream consumer that reads this
    field from JSON without loading this schema still sees ``True`` and
    knows a human must sign.

  Together these two literals constitute the Auxiliary disclaimer: every
  ReviewTemplate is, by construction, an advisory document that requires a
  human gate before any action may be taken.

Required content fields:

  - ``findings``       — ≥ 1 non-empty entry.  Auditor must surface at
                         least one observation; an empty list would mean
                         the review produced no information.
  - ``severity``       — ``Literal["low","medium","high","critical"]``.
                         Overall severity of the findings aggregate.  The
                         Auditor Guild sets this; BP.A.6 may escalate a
                         "critical" severity to a hard-blocked state.
  - ``reviewer_id``    — Non-empty stripped string identifying the Auditor
                         Guild agent (or human, on a re-review pass) that
                         produced this record.
  - ``recommendation`` — Non-empty stripped string: the actionable next
                         step the human reviewer should consider.

Cross-worker safety (SOP Step 1 強制問題 — module-global state audit):
this module declares no module-level mutable state, no singletons, no
in-memory cache.  ``SCHEMA_VERSION`` and the class itself are immutable
values derived from the same source on every worker — falls under SOP
Step 1 acceptable answer #1 ("不共享，因為每 worker 從同樣來源推導出同樣
的值"). Safe under ``uvicorn --workers N`` by construction.

Cross-references:
  - BP.A.1 ``backend/templates/spec.py``     — SpecTemplate
  - BP.A.2 ``backend/templates/task.py``     — TaskTemplate
  - BP.A.3 ``backend/templates/impl.py``     — ImplTemplate (input to
    the Auditor Guild that produces this ReviewTemplate)
  - BP.A.5 ``backend/cognitive_load.py``     — Cognitive Load Scanner
  - BP.A.6 ``backend/template_validator.py`` — FastAPI middleware that
    turns Pydantic ``ValidationError`` into the cognitive-penalty prompt;
    will treat ``severity="critical"`` as a hard-block signal.
  - BP.A.7 ``backend/tests/test_templates.py`` — unified ~150-test suite
    that will fold a superset of these checks.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"

NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, strip_whitespace=True)
]


class ReviewTemplate(BaseModel):
    """Auditor-Guild review contract. Frozen, JSON-serialisable.

    Auxiliary disclaimer: ``audit_type`` is pinned to ``"advisory"`` and
    ``requires_human_signoff`` is pinned to ``True`` at the schema layer —
    it is structurally impossible to construct a ReviewTemplate that claims
    AI authority or waives the human gate.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    schema_version: Literal["1.0.0"] = Field(
        default=SCHEMA_VERSION,
        description=(
            "Pinned schema version. Bump via discriminated union, "
            "never in-place."
        ),
    )

    # ── Auxiliary disclaimer fields ──────────────────────────────────────
    audit_type: Literal["advisory"] = Field(
        default="advisory",
        description=(
            "Auditor-Guild output type. Pinned to ``\"advisory\"`` — the "
            "AI reviewer surfaces findings and recommends action but cannot "
            "authorise a merge. Mirrors the project Gerrit rule: AI max "
            "score is +1; a human +2 is the hard submission gate."
        ),
    )
    requires_human_signoff: Literal[True] = Field(
        default=True,
        description=(
            "Always ``True``. It is structurally impossible to emit a "
            "ReviewTemplate that waives the human gate. Any downstream "
            "consumer reading this field from raw JSON still sees ``true`` "
            "and knows a human must sign before the change may be merged."
        ),
    )

    # ── Content fields ───────────────────────────────────────────────────
    findings: list[NonEmptyStr] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of Auditor observations. ≥ 1 entry required — "
            "an empty findings list would mean the review produced no "
            "information, which is a contract violation. Each entry is a "
            "non-empty stripped string."
        ),
    )
    severity: Literal["low", "medium", "high", "critical"] = Field(
        ...,
        description=(
            "Overall severity of the findings aggregate. Set by the "
            "Auditor Guild. BP.A.6 middleware treats ``\"critical\"`` as a "
            "hard-block signal that suspends the dispatch pipeline until a "
            "human explicitly clears it."
        ),
    )
    reviewer_id: NonEmptyStr = Field(
        ...,
        description=(
            "Identifier of the Auditor Guild agent (or human reviewer on a "
            "re-review pass) that produced this record. Non-empty stripped "
            "string — an anonymous review is a contract violation."
        ),
    )
    recommendation: NonEmptyStr = Field(
        ...,
        description=(
            "Actionable next step for the human reviewer. Non-empty "
            "stripped string. The Auditor must always emit a concrete "
            "recommendation, never an empty placeholder."
        ),
    )


__all__ = ["SCHEMA_VERSION", "ReviewTemplate"]
