"""U6-3 — L2 offline write-signal evaluation (DORMANT).

Answers the §2.C / L2 question "is the write signal USEFUL + CLEAN?" for the L2
pipeline: raw turns → (a distiller, not built here) → a CANDIDATE dict → U6-2a
``from_jsonb`` validation → a stored ``SessionOutcome``. This scores the distiller
CANDIDATE (before/around validation) against its source turns, on the §6 metrics:

  * CLEANLINESS — verbatim-overlap (does the candidate copy source text?),
    sensitive-data retention (does it carry an email / long digit-run / credential
    keyword?), and schema-reject (does U6-2a reject it — a leaky candidate with a
    free-form field is rejected, which is a SAFE outcome we still want to measure).
  * USEFULNESS — is the outcome non-trivial (not an empty NO_OUTCOME with nothing)?

Gated on numeric THRESHOLDS over a frozen clean/leaky corpus (§6: "rollout gates
on thresholds, not metric existence"), mirroring the U6-1b qualification. Pure +
offline; no LLM, no DB, no injection. DORMANT: nothing runs this in production (a
later eval harness / the U6-5 producer consumes it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from backend.agents.u6_l2_outcome import (
    _TASK_REF_RE,
    OutcomeValidationError,
    from_jsonb,
)

EVAL_VERSION = 1

# Cleanliness gates. A clean closed candidate has NO free-text field, so it cannot
# form a 4-gram shared with the source → overlap 0; any verbatim copy trips this.
MAX_VERBATIM_OVERLAP = 0.0
_SHINGLE_K = 4

# Sensitive-data signatures scanned across ALL candidate string values. NOT
# exhaustive — names/addresses/paraphrase are out of a regex signal's remit (a
# later semantic eval covers those); this catches the structured-PII surface.
_SENSITIVE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),          # email
    re.compile(r"\d{3}-\d{2}-\d{4}"),                  # SSN-style (hyphens split a digit run)
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),        # IPv4
    re.compile(r"\d{7,}"),                             # long digit run (card/phone)
    re.compile(r"(?i)(password|secret|api[_-]?key|token|bearer)\s*[:=]\s*\S"),  # credential
)


class Label(Enum):
    CLEAN = "clean"    # a well-behaved distiller candidate — must pass
    LEAKY = "leaky"    # a misbehaving candidate — must be CAUGHT (never silently clean)


@dataclass(frozen=True, slots=True)
class EvalCase:
    label: Label
    source_texts: tuple[str, ...]
    candidate: dict
    note: str


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _shingles(tokens: list[str], k: int = _SHINGLE_K) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def _candidate_string_values(candidate: dict) -> list[str]:
    """Every string value ANYWHERE in the candidate — top-level, list items, and
    NESTED dicts + UNKNOWN fields — so a leaky ``notes``/``meta.x`` field a denylist
    never anticipated is still scanned (a complete leak-surface walk)."""
    out: list[str] = []

    def _walk(value: object) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(candidate)
    return out


def verbatim_overlap(candidate_values: list[str], source_texts: list[str]) -> float:
    """Fraction of the candidate's ``k``-gram shingles that also appear in the
    source — high ⇒ the candidate copied source text verbatim. 0 when the candidate
    carries no ``k``-gram (the closed-schema case)."""
    source_sh: set[tuple[str, ...]] = set()
    for text in source_texts:
        source_sh |= _shingles(_tokens(text))
    cand_sh: set[tuple[str, ...]] = set()
    for value in candidate_values:
        cand_sh |= _shingles(_tokens(value))
    if not cand_sh:
        return 0.0
    return len(cand_sh & source_sh) / len(cand_sh)


def sensitive_hit(candidate: dict) -> bool:
    """True if any candidate string value matches a sensitive-data signature. A
    value that is a valid (format-bounded) task ref is exempt — e.g. ``OP-1234567``
    is a legitimate 7-digit ref, not a leaked digit run (avoids a false positive
    that would depress ``clean_pass_rate``)."""
    for value in _candidate_string_values(candidate):
        if _TASK_REF_RE.match(value):
            continue
        if any(pat.search(value) for pat in _SENSITIVE_RES):
            return True
    return False


def schema_valid(candidate: dict) -> bool:
    """True iff U6-2a accepts the candidate (a free-form/leaky candidate is rejected)."""
    try:
        from_jsonb(candidate)
    except OutcomeValidationError:
        return False
    return True


def is_useful(candidate: dict) -> bool:
    """A non-trivial write signal: not an empty NO_OUTCOME with no tasks/topics."""
    kind = candidate.get("outcome_kind")
    return bool(kind and kind != "no_outcome") or bool(candidate.get("tasks_created")) or bool(
        candidate.get("topics")
    )


@dataclass(frozen=True, slots=True)
class CandidateEval:
    schema_valid: bool
    verbatim_overlap: float
    sensitive: bool
    useful: bool

    @property
    def clean(self) -> bool:
        """A clean write: schema-valid, no verbatim copy, no sensitive data."""
        return (
            self.schema_valid
            and self.verbatim_overlap <= MAX_VERBATIM_OVERLAP
            and not self.sensitive
        )

    @property
    def leak_caught(self) -> bool:
        """A leaky candidate is caught if ANY cleanliness signal flags it."""
        return not self.clean


def evaluate_candidate(source_texts: list[str], candidate: dict) -> CandidateEval:
    # Fail closed on a malformed candidate — never crash the eval (a non-dict is
    # not schema-valid and not useful).
    if not isinstance(candidate, dict):
        return CandidateEval(schema_valid=False, verbatim_overlap=0.0, sensitive=False, useful=False)
    return CandidateEval(
        schema_valid=schema_valid(candidate),
        verbatim_overlap=verbatim_overlap(_candidate_string_values(candidate), source_texts),
        sensitive=sensitive_hit(candidate),
        useful=is_useful(candidate),
    )


@dataclass(frozen=True, slots=True)
class L2EvalMetrics:
    n_clean: int
    n_leaky: int
    clean_passed: int   # clean cases that are clean AND useful
    leaks_caught: int   # leaky cases flagged not-clean

    @property
    def clean_pass_rate(self) -> float:
        return self.clean_passed / self.n_clean if self.n_clean else 0.0

    @property
    def leak_catch_rate(self) -> float:
        return self.leaks_caught / self.n_leaky if self.n_leaky else 0.0

    @property
    def meets_thresholds(self) -> bool:
        # non-vacuous (both classes present) AND every clean case passes AND every
        # leaky case is caught — a corpus of only-clean (or an always-clean judge)
        # does NOT pass.
        return (
            self.n_clean > 0
            and self.n_leaky > 0
            and self.clean_pass_rate >= 1.0
            and self.leak_catch_rate >= 1.0
        )


def evaluate_corpus(corpus: tuple[EvalCase, ...]) -> L2EvalMetrics:
    n_clean = n_leaky = clean_passed = leaks_caught = 0
    for case in corpus:
        ev = evaluate_candidate(list(case.source_texts), case.candidate)
        if case.label is Label.CLEAN:
            n_clean += 1
            if ev.clean and ev.useful:
                clean_passed += 1
        else:
            n_leaky += 1
            if ev.leak_caught:
                leaks_caught += 1
    return L2EvalMetrics(n_clean, n_leaky, clean_passed, leaks_caught)


# ── frozen clean / leaky corpus (§6 qualification fixture) ───────────────────
_SRC = (
    "user: my email is alice@example.com and my card is 4111111111111111",
    "assistant: I filed OP-1234 to track the camera setup and set the timezone",
    "user: please remember the deploy standard is checkpatch strict",
)
_CLEAN_OUTCOME = {
    "schema_version": 1, "outcome_kind": "task_created", "turn_count": 6,
    "resolved": True, "tasks_created": ["OP-1234"], "topics": ["camera_setup"],
}

L2_EVAL_CORPUS: tuple[EvalCase, ...] = (
    # CLEAN — closed, accurate, no leak, useful.
    EvalCase(Label.CLEAN, _SRC, dict(_CLEAN_OUTCOME), "canonical closed outcome"),
    EvalCase(
        Label.CLEAN, _SRC,
        {"schema_version": 1, "outcome_kind": "question_answered", "turn_count": 2,
         "resolved": True, "tasks_created": [], "topics": []},
        "closed Q&A outcome",
    ),
    # LEAKY — a misbehaving distiller; each MUST be caught by a cleanliness signal.
    EvalCase(
        Label.LEAKY, _SRC,
        {**_CLEAN_OUTCOME, "notes": "my email is alice@example.com and my card is 4111111111111111"},
        "free-text field copying source verbatim (schema-reject + overlap + sensitive)",
    ),
    EvalCase(
        Label.LEAKY, _SRC,
        {**_CLEAN_OUTCOME, "summary": "please remember the deploy standard is checkpatch strict"},
        "free-text summary copying source (schema-reject + verbatim overlap)",
    ),
    EvalCase(
        Label.LEAKY, _SRC,
        {**_CLEAN_OUTCOME, "model_fingerprint": "alice@example.com"},
        "sensitive email smuggled into an unknown field (schema-reject + sensitive)",
    ),
)
