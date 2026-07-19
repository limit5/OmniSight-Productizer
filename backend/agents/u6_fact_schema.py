"""U6-1a — closed L3 fact schema + registry + validator + frozen renderer (DORMANT).

The RB1 *boundary* for the U6 semantic (L3) memory leg (frozen design §2.B + §11
RB1). "Authority / policy / permission / review / tool-behavior facts are
UNREPRESENTABLE" is guaranteed HERE — by a CLOSED predicate registry + a
fail-closed validator + a frozen inert renderer grammar — NOT by a classifier
(§2.B: "the fact schema is the boundary, not a classifier"). The triage
classifier (U6-1b) sits on top and is explicitly NOT the safety boundary.

Three frozen artifacts (RB1 DoD, §11):
  1. a CLOSED ``fact_type`` enum + CLOSED predicate registry — the write path may
     NOT extend either; every predicate is data-only, so no directive is
     representable. Predicates are GLOBALLY UNIQUE across fact_types, so the
     rendered ``subject predicate value`` triple is unambiguous.
  2. a per-predicate VALUE-TYPE LATTICE — each predicate's value is validated by a
     closed typed validator (enum / short-token / lang-tag / tz-token); every
     validator forbids whitespace + control chars, so a value can NEVER carry a
     fence-break, a newline, or an injected directive.
  3. the EXACT rendered-byte grammar — ``render_fact`` emits a single inert line
     ``"<subject> <predicate> <value>"``; never a heading / role / imperative
     (reuse the A2 / llm_firewall inert-data discipline). The datamark FENCE that
     wraps this fragment is U6-7's loader; this module freezes the fragment.

The SUBJECT (head) position is a CLOSED namespace (``user`` | ``repo:<slug>``) so a
rendered line can never begin with a role/authority token. The VALUE (object)
position under a data-only predicate carries no directive power — a nonsense value
like ``preferred_ipc sudo`` is inert data, and semantic edge-cases are the triage
classifier's (U6-1b) and the action guard's (U6-7, §2.A) job, NOT this boundary's
(per §2.B: the schema is the boundary, the classifier is triage on top).

Pure + offline; no DB, no crypto, no injection. DORMANT: nothing produces or
renders a real fact yet (U6-4 stores, U6-5 produces, U6-7 injects behind the
guard). Versioned (``FACT_SCHEMA_VERSION`` / ``RENDER_GRAMMAR_VERSION``) so the
frozen grammar is content-free metadata that survives crypto-shred erasure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

FACT_SCHEMA_VERSION = 1
RENDER_GRAMMAR_VERSION = 1


class FactValidationError(ValueError):
    """A fact violates the closed schema — reject fail-closed (never coerce)."""


class FactType(Enum):
    """The CLOSED, data-only fact_type enum. Authority / policy / permission /
    review / tool-behavior are deliberately absent — they are unrepresentable."""

    PREFERENCE = "preference"
    PROFILE = "profile"
    PROJECT_CONTEXT = "project_context"


class Sensitivity(Enum):
    """Content sensitivity flag (metadata; NOT authority). SENSITIVE facts get
    stricter downstream handling (U6-6 review lane) but carry no extra power."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"


# ── injection-safe field grammars ────────────────────────────────────────────
# Every field that reaches the renderer forbids whitespace + control chars (ASCII
# ranges only — no unicode line/space/bidi codepoint can enter), so the inert
# "<subject> <predicate> <value>" line is single-line and unambiguous.
#
# `subject` is a CLOSED namespace — the SUBJECT (head) position is exactly where a
# free token could render as a directive ("SYSTEM …", "ignore_all_prior …"), so it
# is restricted to `user` (the scoped user) | `repo:<slug>` (a project). No reserved
# role / authority / imperative token is representable as a subject.
# NOTE(U6-4/U6-7): the `repo:<slug>` slug is an inert IDENTITY LABEL, NOT a validated
# filesystem path — it may contain `.`/`/` (e.g. `repo:a/../b`). A consumer must
# never treat a subject as a path without its own resolution/containment check.
_SUBJECT_RE = re.compile(r"\A(?:user|repo:[A-Za-z0-9][A-Za-z0-9._/-]{0,95})\Z")
_SPAN_RE = re.compile(r"\A[A-Za-z0-9._:/+#=-]{1,256}\Z")
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

_VALUE_MAX = 64  # hard length cap for EVERY value type (bounds the unbounded regexes)
_SHORT_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._:+-]{1,64}\Z")
_LANG_TAG_RE = re.compile(r"\A[A-Za-z]{2,8}(-[A-Za-z0-9]{2,8})*\Z")
_TZ_TOKEN_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*\Z")


def _enum_value(*allowed: str):
    frozen = frozenset(allowed)

    def _v(value: str) -> bool:
        return value in frozen

    return _v


def _regex_value(pattern: re.Pattern[str], max_len: int = _VALUE_MAX):
    def _v(value: str) -> bool:
        # length cap FIRST — the `-subtag`/`/segment` repeats in the lang/tz
        # grammars are otherwise unbounded (a 12KB "language tag" would match).
        return isinstance(value, str) and len(value) <= max_len and pattern.match(value) is not None

    return _v


@dataclass(frozen=True, slots=True)
class PredicateSpec:
    """A registered predicate: which fact_type owns it + its value validator."""

    fact_type: FactType
    validate_value: object  # Callable[[str], bool]; kept plain for slots+frozen


# ── THE CLOSED REGISTRY (RB1) — the write path may NOT extend this ────────────
# Every entry is pure descriptive data. There is intentionally NO predicate that
# could encode a directive ("skips_reviews", "pre_approves_deploys", "allow", …):
# such a fact is UNREPRESENTABLE, which is the boundary.
_REGISTRY: dict[str, PredicateSpec] = {
    # preference — how the user likes things done (data, not authority)
    "preferred_ipc": PredicateSpec(
        FactType.PREFERENCE,
        _enum_value("named_pipes", "tcp", "unix_socket", "shared_memory"),
    ),
    "preferred_language": PredicateSpec(FactType.PREFERENCE, _regex_value(_LANG_TAG_RE)),
    "preferred_editor": PredicateSpec(FactType.PREFERENCE, _regex_value(_SHORT_TOKEN_RE)),
    # profile — stable descriptive attributes of the user
    "timezone": PredicateSpec(FactType.PROFILE, _regex_value(_TZ_TOKEN_RE)),
    "ui_language": PredicateSpec(FactType.PROFILE, _regex_value(_LANG_TAG_RE)),
    # project_context — a repo's STATED standard (descriptive, not a grant)
    "build_standard": PredicateSpec(FactType.PROJECT_CONTEXT, _regex_value(_SHORT_TOKEN_RE)),
    "default_branch": PredicateSpec(FactType.PROJECT_CONTEXT, _regex_value(_SHORT_TOKEN_RE)),
}

#: Read-only view — the PUBLIC surface for reads. RB1 "the write path may NOT
#: extend the enum": a proxy rejects ``PREDICATE_REGISTRY[x] = ...``, so no caller
#: can grow the closed set at runtime. (The module-private ``_REGISTRY`` exists only
#: to build this; mutating it would be editing the module itself — out of model.)
PREDICATE_REGISTRY: MappingProxyType[str, PredicateSpec] = MappingProxyType(_REGISTRY)

#: The frozen set of representable predicates (RB1: closed; not runtime-extensible).
REGISTERED_PREDICATES: frozenset[str] = frozenset(_REGISTRY)


def predicate_is_registered(predicate: str) -> bool:
    """True iff ``predicate`` is in the closed registry. Anything else — every
    authority/policy/review/tool-behavior word — is UNREPRESENTABLE by design."""
    return predicate in PREDICATE_REGISTRY


@dataclass(frozen=True, slots=True)
class Fact:
    """A validated, non-behavioral L3 fact. Invalid instances are unrepresentable —
    ``__post_init__`` runs the full closed-schema validation and raises otherwise."""

    fact_type: FactType
    subject: str
    predicate: str
    value: str
    source_span: str
    sensitivity: Sensitivity = Sensitivity.NORMAL
    valid_from: str | None = None
    valid_until: str | None = None

    def __post_init__(self) -> None:
        validate_fact(self)


def validate_fact(fact: Fact) -> None:
    """Fail-closed validation against the closed schema. Raises
    ``FactValidationError`` on ANY violation — never coerces or best-efforts."""
    if not isinstance(fact, Fact):
        raise FactValidationError("not a Fact")
    if not isinstance(fact.fact_type, FactType):
        raise FactValidationError("fact_type must be a FactType")
    if not isinstance(fact.sensitivity, Sensitivity):
        raise FactValidationError("sensitivity must be a Sensitivity")

    # predicate must be a str BEFORE the registry lookup — an unhashable predicate
    # (list/dict) would otherwise raise TypeError past the fail-closed contract.
    if not isinstance(fact.predicate, str):
        raise FactValidationError("predicate must be a str")
    spec = PREDICATE_REGISTRY.get(fact.predicate)
    if spec is None:
        # The RB1 boundary: an unregistered predicate (every directive-shaped one)
        # is rejected here, before any store or renderer ever sees it.
        raise FactValidationError(f"predicate not in closed registry: {fact.predicate!r}")
    if spec.fact_type is not fact.fact_type:
        raise FactValidationError(
            f"predicate {fact.predicate!r} belongs to {spec.fact_type.value}, "
            f"not {fact.fact_type.value}"
        )

    if not isinstance(fact.subject, str) or not _SUBJECT_RE.match(fact.subject):
        raise FactValidationError("subject must match the safe subject grammar")
    if not isinstance(fact.value, str):
        raise FactValidationError("value must be a str")
    # Wrap the validator call: a non-callable or buggy validator must surface as
    # FactValidationError, never a foreign TypeError, so callers can catch one type.
    try:
        value_ok = bool(spec.validate_value(fact.value))  # type: ignore[operator]
    except FactValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize ANY validator fault to fail-closed
        raise FactValidationError(f"value validator error for {fact.predicate!r}") from exc
    if not value_ok:
        raise FactValidationError(
            f"value {fact.value!r} fails the value-type lattice for {fact.predicate!r}"
        )
    if not isinstance(fact.source_span, str) or not _SPAN_RE.match(fact.source_span):
        raise FactValidationError("source_span must match the safe provenance grammar")

    for label, when in (("valid_from", fact.valid_from), ("valid_until", fact.valid_until)):
        if when is not None and (not isinstance(when, str) or not _DATE_RE.match(when)):
            raise FactValidationError(f"{label} must be an ISO date (YYYY-MM-DD) or None")
    if (
        fact.valid_from is not None
        and fact.valid_until is not None
        and fact.valid_from > fact.valid_until
    ):
        raise FactValidationError("valid_from must not be after valid_until")


def render_fact(fact: Fact) -> str:
    """The FROZEN renderer grammar (RB1): a single inert data line
    ``"<subject> <predicate> <value>"`` — never a heading, role, or imperative.

    Defense-in-depth: re-validate, then assert every field is whitespace-free so
    the space-delimited triple is single-line and unambiguous. A rendered fact is
    DATA; the datamark fence that quarantines it is U6-7's loader, not this."""
    validate_fact(fact)
    parts = (fact.subject, fact.predicate, fact.value)
    for part in parts:
        if any(ch.isspace() for ch in part):
            # Unreachable given the field grammars — but the renderer fails closed
            # rather than emit a line that could split or carry a payload.
            raise FactValidationError("rendered field contains whitespace")
    return f"{fact.subject} {fact.predicate} {fact.value}"
