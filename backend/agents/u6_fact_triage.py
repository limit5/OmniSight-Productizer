"""U6-1b — fact triage classifier + catch/FP metrics (DORMANT).

The triage layer that sits ON TOP of the U6-1a schema boundary (frozen design
§2.B: "the classifier/validator is a triage layer on top (rejects obvious junk,
flags sensitivity) — it is qualified adversarially but is explicitly NOT the
safety boundary"). The SCHEMA is the boundary; this adds QUALITY (junk rejection)
+ a monotone SENSITIVITY flag, and is QUALIFIED by a frozen attack/benign corpus
(§6: catch rate + false-positive rate, gated on numeric thresholds).

Order of decision (so the boundary, not the classifier, does the safety-critical
rejecting): (1) build the ``Fact`` — the U6-1a schema gate rejects every
authority predicate / role subject / injection value / cross-type / out-of-enum /
oversized input; (2) junk triage rejects schema-valid-but-worthless values; (3)
sensitivity flag — MONOTONE, never downgrades a declared-sensitive candidate.

Pure + offline; no DB, no injection. DORMANT: the semantic producer (U6-5) is the
first caller. Versioned so a threshold/metric change is a reviewed diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.agents.u6_fact_corpus import CorpusCase, Label
from backend.agents.u6_fact_schema import (
    Fact,
    FactType,
    FactValidationError,
    Sensitivity,
)

TRIAGE_VERSION = 1

# Numeric qualification gates (§6: rollout gates on THRESHOLDS, not metric
# existence). The frozen corpus must be caught completely with no benign loss.
MIN_CATCH_RATE = 1.0
MAX_FALSE_POSITIVE_RATE = 0.0

# Schema-valid but worthless values the triage rejects (quality, NOT safety).
# Case-folded membership; none of these is a legitimate registered value. Note:
# `na`/`nil` are DELIBERATELY excluded — `na` is ISO-639 (Nauru), a legitimate
# language tag we must not silently drop (a junk FP fails closed but still loses a
# real fact).
_JUNK_VALUES: frozenset[str] = frozenset(
    {"none", "null", "tbd", "tba", "todo", "xxx", "unknown", "undefined", "placeholder", "n/a"}
)

# Per-predicate sensitivity baseline. EMPTY in v1 (the closed registry is entirely
# non-sensitive data) — this baseline-upgrade branch is INTENTIONALLY DORMANT until
# a sensitive predicate is registered; the sensitivity flag's live path in v1 is the
# producer-DECLARED sensitivity (preserved monotonically). Sensitivity is a metadata
# flag, never authority.
_SENSITIVE_PREDICATES: frozenset[str] = frozenset()


class TriageLayer(Enum):
    """Which layer decided. SCHEMA = the U6-1a boundary (safety); JUNK = the
    triage quality layer; ACCEPT = passed all layers."""

    SCHEMA = "schema"
    JUNK = "junk"
    ACCEPT = "accept"


@dataclass(frozen=True, slots=True)
class TriageResult:
    accept: bool
    layer: TriageLayer
    reason: str
    sensitivity: Sensitivity


def _is_junk_value(value: str) -> bool:
    return value.casefold() in _JUNK_VALUES


def _classify_sensitivity(fact: Fact, declared: Sensitivity) -> Sensitivity:
    """MONOTONE: SENSITIVE if the caller declared it OR the predicate baseline says
    so — never downgrades a declared-sensitive candidate to NORMAL."""
    if declared is Sensitivity.SENSITIVE or fact.predicate in _SENSITIVE_PREDICATES:
        return Sensitivity.SENSITIVE
    return Sensitivity.NORMAL


def triage_proposal(
    fact_type: FactType,
    subject: str,
    predicate: str,
    value: str,
    *,
    source_span: str = "triage",
    declared_sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> TriageResult:
    """Triage a proposed fact. Rejections carry the LAYER that caught them so the
    §2.B invariant is auditable: authority/injection is caught at ``SCHEMA``, junk
    at ``JUNK``. The classifier NEVER makes an unsafe input safe — it only ever
    rejects more, or flags sensitivity."""
    # Layer 1 — the SCHEMA boundary (U6-1a). This is the safety gate.
    try:
        fact = Fact(
            fact_type,
            subject,
            predicate,
            value,
            source_span=source_span,
            sensitivity=declared_sensitivity,
        )
    except FactValidationError as exc:
        return TriageResult(False, TriageLayer.SCHEMA, f"schema_reject: {exc}", Sensitivity.NORMAL)

    # Layer 2 — junk triage (quality, not safety).
    if _is_junk_value(fact.value):
        return TriageResult(False, TriageLayer.JUNK, "junk_value", Sensitivity.NORMAL)
    if fact.value.casefold() == fact.predicate.casefold():
        return TriageResult(False, TriageLayer.JUNK, "echo_value", Sensitivity.NORMAL)

    # Layer 3 — sensitivity flag (monotone).
    sens = _classify_sensitivity(fact, declared_sensitivity)
    return TriageResult(True, TriageLayer.ACCEPT, "accepted", sens)


@dataclass(frozen=True, slots=True)
class TriageMetrics:
    n_attack: int
    n_benign: int
    attacks_caught: int
    benign_false_rejects: int
    sensitive_declared: int = 0    # accepted benign cases the producer declared SENSITIVE
    sensitive_preserved: int = 0   # ...of those, how many the triage kept SENSITIVE

    @property
    def catch_rate(self) -> float:
        return self.attacks_caught / self.n_attack if self.n_attack else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.benign_false_rejects / self.n_benign if self.n_benign else 0.0

    @property
    def sensitivity_qualified(self) -> bool:
        # positively exercised (≥1 declared-sensitive case) AND never downgraded.
        return self.sensitive_declared > 0 and self.sensitive_preserved == self.sensitive_declared

    @property
    def meets_thresholds(self) -> bool:
        # non-vacuous AND within the frozen gates — a corpus with no attacks
        # (or a trivially-accepting classifier) does NOT pass.
        return (
            self.n_attack > 0
            and self.n_benign > 0
            and self.catch_rate >= MIN_CATCH_RATE
            and self.false_positive_rate <= MAX_FALSE_POSITIVE_RATE
        )


def evaluate_triage(corpus: tuple[CorpusCase, ...]) -> TriageMetrics:
    """Run the triage over a labelled corpus and return catch / FP / sensitivity
    metrics. Honours each case's producer-declared sensitivity so the monotone
    flag is positively qualified, not just unit-tested."""
    n_attack = n_benign = caught = false_rejects = 0
    sens_declared = sens_preserved = 0
    for case in corpus:
        result = triage_proposal(
            case.fact_type,
            case.subject,
            case.predicate,
            case.value,
            declared_sensitivity=case.declared_sensitivity,
        )
        if case.label is Label.ATTACK:
            n_attack += 1
            if not result.accept:
                caught += 1
        else:
            n_benign += 1
            if not result.accept:
                false_rejects += 1
            elif case.declared_sensitivity is Sensitivity.SENSITIVE:
                sens_declared += 1
                if result.sensitivity is Sensitivity.SENSITIVE:
                    sens_preserved += 1
    return TriageMetrics(n_attack, n_benign, caught, false_rejects, sens_declared, sens_preserved)
