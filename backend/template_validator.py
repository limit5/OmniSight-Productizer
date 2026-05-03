"""BP.A.6 — FastAPI validation middleware (Pydantic ValidationError → cognitive-penalty prompt).

Provides:
  1. ``CognitivePenaltyPrompt`` — frozen Pydantic response model; the structured
     rejection body consumed by guild agents on a failed submission.
  2. ``format_validation_penalty(exc, template_type)`` — converts a Pydantic
     ``ValidationError`` into a ``CognitivePenaltyPrompt`` with per-field fix hints.
  3. ``format_cognitive_overload_penalty(task, report)`` — emitted when a
     ``TaskTemplate`` passes schema validation but its estimated cognitive load
     exceeds the ``max_cognitive_load_tokens`` ceiling. Instructs PM Guild to
     decompose the task before dispatching to Coder Guild.
  4. ``format_critical_review_penalty(review)`` — emitted when a
     ``ReviewTemplate`` carries ``severity="critical"``, triggering a hard-block
     on the dispatch pipeline until a human explicitly clears it.
  5. ``router`` — FastAPI ``APIRouter`` with POST endpoints for each template type:
       POST /validate/spec    — SpecTemplate gate
       POST /validate/task    — TaskTemplate gate (+ cognitive load check)
       POST /validate/impl    — ImplTemplate gate
       POST /validate/review  — ReviewTemplate gate (+ critical-severity hard-block)

HTTP contract
─────────────
  200 OK  — validation passed; body is a typed ``*ValidationResult`` JSON object.
  422 Unprocessable Entity — validation failed; body is always ``CognitivePenaltyPrompt``.

penalty_type values
───────────────────
  "validation_error"   — one or more Pydantic field constraints violated.
  "cognitive_overload" — TaskTemplate passes schema but estimated_tokens > ceiling;
                         PM Guild must re-decompose before the task may be dispatched.
  "critical_review"    — ReviewTemplate passes schema but severity="critical";
                         hard-blocks the pipeline until a human explicitly signs off.

Module-global state audit (SOP Step 1 強制問題):
  Only one module-level value: ``_FIX_HINTS`` — an immutable dict of string constants.
  No singletons, no in-memory cache, no mutable state. Every worker derives identical
  values from the same source — SOP Step 1 acceptable answer #1 ("不共享，因為每 worker
  從同樣來源推導出同樣的值"). Safe under ``uvicorn --workers N`` by construction.

Cross-references:
  BP.A.1  backend/templates/spec.py       — SpecTemplate
  BP.A.2  backend/templates/task.py       — TaskTemplate
  BP.A.3  backend/templates/impl.py       — ImplTemplate
  BP.A.4  backend/templates/review.py     — ReviewTemplate
  BP.A.5  backend/cognitive_load.py       — scan_cognitive_load, CognitiveLoadReport
  BP.A.5b backend/rlm_dispatch.py         — plan_dispatch (Coder dispatch, not gated here)
  BP.A.7  backend/tests/test_templates.py — unified ~150-test suite
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.cognitive_load import CognitiveLoadReport, scan_cognitive_load
from backend.templates.impl import ImplTemplate
from backend.templates.review import ReviewTemplate
from backend.templates.spec import SpecTemplate
from backend.templates.task import TaskTemplate

# ── Fix-hint registry ─────────────────────────────────────────────────────────
# Maps top-level field names to short, actionable guidance strings. Unknown
# fields fall through to the raw Pydantic error message. This dict is an
# immutable module-level constant — identical across all workers (SOP Step 1
# answer #1).

_FIX_HINTS: dict[str, str] = {
    "target_triple": (
        "Provide a Rust-style triple: arch-vendor-os or arch-vendor-os-env "
        "(alnum+_ segments separated by dashes). "
        "Examples: 'x86_64-pc-linux-gnu', 'aarch64-vendor-linux'."
    ),
    "max_cognitive_load_tokens": (
        "Provide a positive integer > 0, e.g. 4096. "
        "This is the hard ceiling for the Cognitive Load Scanner."
    ),
    "guild_id": (
        "Provide a non-empty guild identifier string, e.g. 'coder-guild'. "
        "The Phase-B Guild registry will enforce the 21-Guild enum; "
        "this layer only rejects empty/whitespace values."
    ),
    "size": "Must be exactly one of: 'S', 'M', 'XL'.",
    "compiled_exit_code": (
        "Must be exactly 0. A non-zero exit code is a Blueprint v2 contract "
        "violation — the Coder must fix all compilation errors before emitting "
        "an ImplTemplate."
    ),
    "time_complexity": (
        "Must match Bachmann-Landau notation: a leading symbol (O, o, Θ, θ, Ω, ω) "
        "immediately followed by a parenthesised body. "
        "Valid examples: 'O(1)', 'O(n log n)', 'O(n*log(n))', 'Θ(n)', 'Ω(log n)'. "
        "Invalid: 'fast', 'n^2', 'O()'."
    ),
    "source_code_payload": (
        "Must be a non-empty string. An empty payload is a contract violation — "
        "the Coder must emit actual source code or raise an error, never return ''."
    ),
    "severity": "Must be exactly one of: 'low', 'medium', 'high', 'critical'.",
    "audit_type": (
        "Must be 'advisory'. AI reviewers are advisory-only and cannot authorise a merge. "
        "This mirrors the Gerrit Code-Review rule: AI max score is +1; "
        "a human +2 is the hard submission gate."
    ),
    "requires_human_signoff": (
        "Must be True. Human sign-off is always required. "
        "It is structurally impossible to emit a ReviewTemplate that waives this gate."
    ),
    "system_boundaries": (
        "Must contain at least 3 entries. Each entry is a single-line "
        "negative assertion (e.g. 'does NOT touch the billing service')."
    ),
    "hardware_constraints": (
        "Must contain at least 3 entries. Each entry describes one hardware "
        "constraint (SoC family, RAM ceiling, power budget, latency target, etc.)."
    ),
    "edge_cases_handled": (
        "Must contain at least 3 entries. Each entry explicitly names one edge case "
        "the spec promises to cover."
    ),
    "api_idl_schema": (
        "Must be a non-empty string containing an OpenAPI 3.0 document, "
        "a Protobuf .proto body, or a C/C++ header."
    ),
    "bdd_executable_specs": (
        "Must be a non-empty string containing Gherkin BDD source "
        "(Feature / Scenario / Given-When-Then)."
    ),
    "findings": (
        "Must contain at least 1 entry. An empty findings list means the review "
        "produced no information — that is a contract violation. "
        "The Auditor must surface at least one observation."
    ),
    "reviewer_id": (
        "Must be a non-empty stripped string identifying the Auditor Guild agent. "
        "Anonymous reviews are a contract violation."
    ),
    "recommendation": (
        "Must be a non-empty stripped string with a concrete actionable next step. "
        "The Auditor must never emit an empty placeholder."
    ),
    "allowed_dependencies": (
        "Each entry must be a non-empty stripped string. "
        "Remove or fix any empty/whitespace-only entries."
    ),
    "schema_version": (
        "Must be '1.0.0'. Do not override the schema_version field — "
        "bump via a discriminated union, never in-place mutation."
    ),
}


# ── Response models ───────────────────────────────────────────────────────────


class ValidationErrorDetail(BaseModel):
    """One field-level validation failure with a fix hint."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(..., description="Dotted field path, e.g. 'target_triple' or 'findings[0]'.")
    message: str = Field(..., description="Raw Pydantic error message for this field.")
    fix_hint: str = Field(..., description="Actionable guidance on how to correct the field.")


class CognitivePenaltyPrompt(BaseModel):
    """Structured rejection body returned on a failed template submission.

    Consumed by guild agents to self-correct and resubmit.  ``prompt`` is the
    full human/agent-readable penalty text; ``errors`` / ``cognitive_load`` /
    ``findings`` carry structured machine-parseable details depending on
    ``penalty_type``.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["rejected"] = Field(
        default="rejected",
        description="Always 'rejected' for a penalty response.",
    )
    penalty_type: Literal["validation_error", "cognitive_overload", "critical_review"] = Field(
        ...,
        description=(
            "'validation_error' — Pydantic constraint violated. "
            "'cognitive_overload' — estimated tokens exceed ceiling. "
            "'critical_review' — severity='critical' hard-blocks the pipeline."
        ),
    )
    template_type: str = Field(
        ...,
        description="Which template triggered this penalty: 'spec', 'task', 'impl', or 'review'.",
    )
    prompt: str = Field(
        ...,
        description="Full human/agent-readable penalty text with actionable guidance.",
    )
    errors: list[ValidationErrorDetail] | None = Field(
        default=None,
        description="Per-field error details (populated for penalty_type='validation_error').",
    )
    cognitive_load: dict[str, Any] | None = Field(
        default=None,
        description="CognitiveLoadReport metrics (populated for penalty_type='cognitive_overload').",
    )
    findings: list[str] | None = Field(
        default=None,
        description="Critical findings list (populated for penalty_type='critical_review').",
    )


# ── Success response models ───────────────────────────────────────────────────


class SpecValidationResult(BaseModel):
    """Returned by POST /validate/spec on success."""

    status: Literal["accepted"] = "accepted"
    template_type: Literal["spec"] = "spec"


class TaskValidationResult(BaseModel):
    """Returned by POST /validate/task on success. Includes the cognitive load report."""

    status: Literal["accepted"] = "accepted"
    template_type: Literal["task"] = "task"
    cognitive_load: CognitiveLoadReport = Field(
        ...,
        description=(
            "Cognitive load metrics computed for this task. "
            "exceeds_ceiling is always False here (True would have triggered a 422)."
        ),
    )


class ImplValidationResult(BaseModel):
    """Returned by POST /validate/impl on success."""

    status: Literal["accepted"] = "accepted"
    template_type: Literal["impl"] = "impl"


class ReviewValidationResult(BaseModel):
    """Returned by POST /validate/review on success (severity is not 'critical')."""

    status: Literal["accepted"] = "accepted"
    template_type: Literal["review"] = "review"


# ── Internal helpers ──────────────────────────────────────────────────────────


def _loc_to_str(loc: tuple[str | int, ...]) -> str:
    """Convert a Pydantic v2 error-location tuple to a readable path string.

    Examples:
      ('target_triple',)       → 'target_triple'
      ('system_boundaries', 0) → 'system_boundaries[0]'
      ()                       → '(root)'
    """
    if not loc:
        return "(root)"
    result = ""
    for part in loc:
        if isinstance(part, int):
            result += f"[{part}]"
        elif result:
            result += f".{part}"
        else:
            result = str(part)
    return result


def _hint_for_loc(loc: tuple[str | int, ...]) -> str:
    """Return the fix hint for the top-level field name in *loc*.

    Falls back to a generic message when the field is not in ``_FIX_HINTS``.
    """
    for part in loc:
        if isinstance(part, str) and part in _FIX_HINTS:
            return _FIX_HINTS[part]
    return "Correct this field to satisfy the schema constraint."


# ── Penalty prompt builders ───────────────────────────────────────────────────


def format_validation_penalty(
    exc: ValidationError,
    template_type: str,
) -> CognitivePenaltyPrompt:
    """Convert a Pydantic ``ValidationError`` into a ``CognitivePenaltyPrompt``.

    Each failed field is listed in the ``errors`` list with its location, the
    raw Pydantic message, and a fix hint from ``_FIX_HINTS`` (or a generic
    fallback). The ``prompt`` string aggregates all errors into a single
    human/agent-readable block.

    Args:
        exc:           The ``ValidationError`` raised by a Template constructor.
        template_type: The template that triggered the error ('spec', 'task',
                       'impl', or 'review').

    Returns:
        A frozen ``CognitivePenaltyPrompt`` with ``penalty_type='validation_error'``.
    """
    error_details: list[ValidationErrorDetail] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        field_path = _loc_to_str(tuple(loc))
        message = err.get("msg", "Validation error.")
        hint = _hint_for_loc(tuple(loc))
        error_details.append(
            ValidationErrorDetail(field=field_path, message=message, fix_hint=hint)
        )

    lines: list[str] = [
        f"COGNITIVE PENALTY [validation_error] — {template_type} submission rejected.",
        "",
        f"The following field(s) failed {template_type.upper()} schema validation:",
        "",
    ]
    for detail in error_details:
        lines.append(f"  • {detail.field}: {detail.message}")
        lines.append(f"    → Fix: {detail.fix_hint}")
        lines.append("")
    lines.append("Resubmit after correcting the above field(s). Schema version: 1.0.0")

    return CognitivePenaltyPrompt(
        penalty_type="validation_error",
        template_type=template_type,
        prompt="\n".join(lines),
        errors=error_details,
    )


def format_cognitive_overload_penalty(
    task: TaskTemplate,
    report: CognitiveLoadReport,
) -> CognitivePenaltyPrompt:
    """Emit a cognitive-overload penalty when a TaskTemplate exceeds its ceiling.

    Called after schema validation passes but ``report.exceeds_ceiling`` is True.
    Instructs the PM Guild to decompose the task into smaller units before the
    Coder Guild may receive a dispatch.

    Args:
        task:   The validated ``TaskTemplate`` that exceeded its ceiling.
        report: The ``CognitiveLoadReport`` from ``scan_cognitive_load(task)``.

    Returns:
        A frozen ``CognitivePenaltyPrompt`` with ``penalty_type='cognitive_overload'``.
    """
    overage = report.estimated_tokens - report.ceiling
    pct = round(overage / report.ceiling * 100, 1) if report.ceiling else 0.0

    lines: list[str] = [
        "COGNITIVE PENALTY [cognitive_overload] — TaskTemplate submission rejected.",
        "",
        f"Estimated cognitive load ({report.estimated_tokens} tokens) exceeds "
        f"the task ceiling ({report.ceiling} tokens).",
        "",
        "Structural metrics:",
        f"  fan_in:           {report.fan_in}  (upstream allowed_dependencies)",
        f"  fan_out:          {report.fan_out}  (estimated downstream dispatches)",
        f"  mock_limit:       {report.mock_limit}  (max mocked deps in test suite)",
        f"  size:             {report.size}",
        f"  estimated_tokens: {report.estimated_tokens}",
        f"  ceiling:          {report.ceiling}",
        f"  overage:          +{overage} tokens ({pct}% over ceiling)",
        "",
        "Action required: Return task to PM Guild for re-decomposition.",
        "Suggested approaches:",
        f"  1. Reduce allowed_dependencies (current: {report.fan_in}) — "
        f"each dependency adds {150} tokens to the estimated load.",
        f"  2. Use a smaller size class (current: '{report.size}') — "
        f"'S' / 'M' / 'XL' baselines are 500 / 2000 / 8000 tokens.",
        "  3. Split into two or more smaller TaskTemplates with separate ceilings.",
        "",
        "Do NOT dispatch to Coder Guild until load is within ceiling.",
    ]

    return CognitivePenaltyPrompt(
        penalty_type="cognitive_overload",
        template_type="task",
        prompt="\n".join(lines),
        cognitive_load=report.model_dump(),
    )


def format_critical_review_penalty(review: ReviewTemplate) -> CognitivePenaltyPrompt:
    """Emit a critical-review hard-block penalty for a ReviewTemplate with severity='critical'.

    Called after schema validation passes but ``review.severity == 'critical'``.
    Suspends the dispatch pipeline until a human reviewer explicitly clears the block.

    Args:
        review: The validated ``ReviewTemplate`` that carries severity='critical'.

    Returns:
        A frozen ``CognitivePenaltyPrompt`` with ``penalty_type='critical_review'``.
    """
    findings_block = "\n".join(
        f"  {i + 1}. {finding}" for i, finding in enumerate(review.findings)
    )

    lines: list[str] = [
        "COGNITIVE PENALTY [critical_review] — ReviewTemplate triggers hard-block.",
        "",
        "A ReviewTemplate with severity='critical' suspends the dispatch pipeline.",
        "No Guild dispatch may proceed until a human reviewer explicitly clears this block.",
        "",
        f"Reviewer:       {review.reviewer_id}",
        f"audit_type:     {review.audit_type}",
        f"requires_human_signoff: {review.requires_human_signoff}",
        "",
        f"Critical findings ({len(review.findings)}):",
        findings_block,
        "",
        f"Recommendation: {review.recommendation}",
        "",
        "Action required: Human sign-off mandatory before pipeline may resume.",
        "  • AI reviewer authority is advisory (+1 max score). Human +2 is the hard gate.",
        "  • Do not re-dispatch to any Guild until a human has reviewed and signed off.",
    ]

    return CognitivePenaltyPrompt(
        penalty_type="critical_review",
        template_type="review",
        prompt="\n".join(lines),
        findings=list(review.findings),
    )


# ── FastAPI router ────────────────────────────────────────────────────────────

JsonBody = Annotated[dict[str, Any], Body()]

router = APIRouter(prefix="/validate", tags=["template-validation"])


@router.post("/spec", response_model=SpecValidationResult)
async def validate_spec(body: JsonBody) -> SpecValidationResult | JSONResponse:
    """Validate a SpecTemplate payload.

    Returns 200 ``SpecValidationResult`` on success.
    Returns 422 ``CognitivePenaltyPrompt`` if schema validation fails.
    """
    try:
        SpecTemplate(**body)
    except ValidationError as exc:
        penalty = format_validation_penalty(exc, "spec")
        return JSONResponse(status_code=422, content=penalty.model_dump())
    return SpecValidationResult()


@router.post("/task", response_model=TaskValidationResult)
async def validate_task(body: JsonBody) -> TaskValidationResult | JSONResponse:
    """Validate a TaskTemplate payload and run the Cognitive Load Scanner.

    Returns 200 ``TaskValidationResult`` (with embedded ``CognitiveLoadReport``)
    when the task passes schema validation AND estimated load ≤ ceiling.

    Returns 422 ``CognitivePenaltyPrompt`` when:
      - Schema validation fails (penalty_type='validation_error'), OR
      - Estimated cognitive load exceeds ceiling (penalty_type='cognitive_overload').
    """
    try:
        task = TaskTemplate(**body)
    except ValidationError as exc:
        penalty = format_validation_penalty(exc, "task")
        return JSONResponse(status_code=422, content=penalty.model_dump())

    report = scan_cognitive_load(task)
    if report.exceeds_ceiling:
        penalty = format_cognitive_overload_penalty(task, report)
        return JSONResponse(status_code=422, content=penalty.model_dump())

    return TaskValidationResult(cognitive_load=report)


@router.post("/impl", response_model=ImplValidationResult)
async def validate_impl(body: JsonBody) -> ImplValidationResult | JSONResponse:
    """Validate an ImplTemplate payload.

    Returns 200 ``ImplValidationResult`` on success.
    Returns 422 ``CognitivePenaltyPrompt`` if schema validation fails.
    """
    try:
        ImplTemplate(**body)
    except ValidationError as exc:
        penalty = format_validation_penalty(exc, "impl")
        return JSONResponse(status_code=422, content=penalty.model_dump())
    return ImplValidationResult()


@router.post("/review", response_model=ReviewValidationResult)
async def validate_review(body: JsonBody) -> ReviewValidationResult | JSONResponse:
    """Validate a ReviewTemplate payload and check for critical severity.

    Returns 200 ``ReviewValidationResult`` when the review passes schema
    validation AND severity is not 'critical'.

    Returns 422 ``CognitivePenaltyPrompt`` when:
      - Schema validation fails (penalty_type='validation_error'), OR
      - severity='critical' (penalty_type='critical_review') — hard-blocks
        the pipeline until a human explicitly signs off.
    """
    try:
        review = ReviewTemplate(**body)
    except ValidationError as exc:
        penalty = format_validation_penalty(exc, "review")
        return JSONResponse(status_code=422, content=penalty.model_dump())

    if review.severity == "critical":
        penalty = format_critical_review_penalty(review)
        return JSONResponse(status_code=422, content=penalty.model_dump())

    return ReviewValidationResult()


__all__ = [
    # Response models
    "CognitivePenaltyPrompt",
    "ValidationErrorDetail",
    "SpecValidationResult",
    "TaskValidationResult",
    "ImplValidationResult",
    "ReviewValidationResult",
    # Penalty builders
    "format_validation_penalty",
    "format_cognitive_overload_penalty",
    "format_critical_review_penalty",
    # FastAPI router
    "router",
    # Internal helpers (exported for test coverage)
    "_FIX_HINTS",
    "_loc_to_str",
    "_hint_for_loc",
]
