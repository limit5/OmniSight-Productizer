"""U6-5a — L3 semantic-fact producer (quarantine pipeline, DORMANT).

The write path (frozen design §3 L3): a distilled candidate fact →
U6-1b triage → U6-1a schema validation → the U6-5a memory-safety eval
(action-influence negative control) → lands QUARANTINED (never live) via the U6-4
store. A candidate that fails ANY gate is dropped with a reason; only a
triage-clean, schema-valid, ACTION-INERT fact reaches quarantine — and even then it
is not live until the U6-6 user-confirm path promotes it.

The distiller (the LLM that turns raw turns into a candidate triple) is out of
scope — the producer takes a pre-formed candidate, exactly as the L2 writer takes a
pre-built outcome. Pure gates are offline; the quarantine insert is the U6-4 store.
DORMANT: nothing produces a real candidate yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agents.u6_fact_schema import Fact, FactType, FactValidationError, Sensitivity
from backend.agents.u6_fact_triage import triage_proposal
from backend.agents.u6_l3_eval_adapter import record_eval
from backend.agents.u6_l3_store import insert_fact
from backend.agents.u6_memory_safety_eval import MemorySafetyDecision, evaluate_memory_safety
from backend.agents.u6_memory_scope import MemoryScope


@dataclass(frozen=True, slots=True)
class ProduceOutcome:
    accepted: bool
    reason: str                        # "accepted" | "triage_reject:.." | "schema_reject:.." | "safety_reject:.."
    fact: Fact | None = None
    safety: MemorySafetyDecision | None = None


def produce(
    scope: MemoryScope,
    *,
    fact_type: FactType,
    subject: str,
    predicate: str,
    value: str,
    source_span: str,
    declared_sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> ProduceOutcome:
    """Run a candidate through the full write-gate pipeline. Returns whether it is
    quarantine-eligible + the validated ``Fact`` (never touches the DB)."""
    if not isinstance(scope, MemoryScope):
        raise TypeError("scope must be a MemoryScope")

    # 1. triage (U6-1b) — drop junk / authority-shaped before building anything.
    triaged = triage_proposal(
        fact_type, subject, predicate, value,
        source_span=source_span, declared_sensitivity=declared_sensitivity,
    )
    if not triaged.accept:
        return ProduceOutcome(False, f"triage_reject:{triaged.reason}")

    # 2. schema validation (U6-1a) — the closed-registry boundary.
    try:
        fact = Fact(
            fact_type, subject, predicate, value,
            source_span=source_span, sensitivity=triaged.sensitivity,
        )
    except FactValidationError as exc:
        return ProduceOutcome(False, f"schema_reject:{exc}")

    # 3. memory-safety eval (U6-5a) — the action-influence negative control.
    safety = evaluate_memory_safety(fact, scope)
    if not safety.promoted:
        return ProduceOutcome(False, f"safety_reject:{','.join(safety.reasons)}", fact, safety)

    return ProduceOutcome(True, "accepted", fact, safety)


async def produce_and_quarantine(
    conn,
    scope: MemoryScope,
    *,
    fact_type: FactType,
    subject: str,
    predicate: str,
    value: str,
    source_span: str,
    declared_sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> tuple[ProduceOutcome, str | None]:
    """Produce + (if accepted) insert QUARANTINED via the U6-4 store. Returns the
    outcome + the new fact id (or None if a gate rejected it). Not live until U6-6."""
    outcome = produce(
        scope, fact_type=fact_type, subject=subject, predicate=predicate,
        value=value, source_span=source_span, declared_sensitivity=declared_sensitivity,
    )
    if not outcome.accepted or outcome.fact is None:
        return outcome, None
    fact_id = await insert_fact(conn, scope, outcome.fact)
    return outcome, fact_id


async def produce_quarantine_and_record(
    conn,
    scope: MemoryScope,
    *,
    fact_type: FactType,
    subject: str,
    predicate: str,
    value: str,
    source_span: str,
    declared_sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> tuple[ProduceOutcome, str | None, str | None]:
    """Produce + quarantine + PERSIST the safety eval to the U6-5b ledger.

    Returns ``(outcome, fact_id, eval_run_id)``. The eval_run_id is the missing
    link the U6-6 confirm path binds an approval to: ``confirm_and_publish``
    requires a persisted ``promote`` eval run, and the U6-5b eval-gate rejects
    an approval that is not. A rejected candidate returns ``(outcome, None,
    None)`` — nothing is inserted or recorded. The already-computed
    ``outcome.safety`` decision is what gets recorded (no re-eval)."""
    outcome, fact_id = await produce_and_quarantine(
        conn, scope, fact_type=fact_type, subject=subject, predicate=predicate,
        value=value, source_span=source_span, declared_sensitivity=declared_sensitivity,
    )
    if fact_id is None or outcome.safety is None:
        return outcome, None, None
    eval_run_id = await record_eval(conn, scope, fact_id=fact_id, decision=outcome.safety)
    return outcome, fact_id, eval_run_id
