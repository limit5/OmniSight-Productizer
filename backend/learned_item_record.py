"""OP-2566 U4-A2 — constrained learned-item record + INSERT-time validator.

Pure module, no I/O, no DB, no config. This is layer 3 of the U4
anti-injection defense (freeze doc
``docs/design/2026-07-10-phase-u4-a0-contract-freeze.md`` §G5/F5):
promoted learned items become PROMPT TEXT, so U4 promotes ONLY this
constrained typed record — validated here at INSERT time, then rendered
through the trusted template in ``backend.learned_item_renderer``
(layer 1) and positioned as lower-authority fenced data (layer 2).

HONEST layering: a typed record does NOT make leaf values safe —
``procedure_steps=["ignore previous rules…"]`` is still an instruction.
This validator rejects injection-shaped content loudly (never a bare
bool); the eval negative-controls (U4-H) are the real teeth and human
approval of the exact rendered card (U4-D) is the backstop.

False-positive tolerance is ACCEPTED by design: a benign lesson that
happens to contain the literal phrase "allowed tools" is rejected —
this validator is one layer of five, not a precision classifier.

Ships DORMANT: no non-test module may import this until U4-C/I.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from backend.security.prompt_hardening import looks_like_injection

# ─── Size caps (validator-enforced; tests import these) ──────────────

MAX_LEAF_CHARS = 1200
MAX_LIST_ITEMS = 16
MAX_RECORD_JSON_BYTES = 8192

# Untrusted-content fence grammar. Defined HERE (not in the renderer)
# because the renderer imports this module for the record type — the
# reverse import would create a cycle. The renderer neutralizes leaf
# lines matching this; the validator additionally rejects them at
# INSERT (defense-in-depth).
FENCE_LINE_RE = re.compile(r"^\s*-{3,}\s*(BEGIN|END)\s+UNTRUSTED\b.*$", re.I | re.M)

_EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9_./#:@=?& -]{0,119}$")

_FAKE_SECURITY_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s*"
    r"(core rules|safety rules|security|operating rules|system|l1 memory|agent behavior)"
)
_TRAILER_SPOOF_RE = re.compile(
    r"(?im)^\s*(co-authored-by|signed-off-by|change-id|reviewed-by)\s*:"
)
_TOOL_POLICY_RE = re.compile(
    r"(?i)\b(allowed[-_ ]tools?|tool[- ]policy|permission[- ]mode|bypasspermissions)\b"
)
_IMPORT_DIRECTIVE_RES = (
    re.compile(r"(?i)@import\b"),
    re.compile(r"(?im)^\s*@[\w./~-]+\s*$"),
)

_EXCERPT_MAX = 80


class LearnedItemRecord(BaseModel):
    """The frozen F5 typed record — the ONLY shape U4 ever promotes.

    Field PRESENCE and TYPES are pydantic's job (unknown keys and wrong
    types are rejected at parse). Content rules (emptiness, size caps,
    injection patterns, evidence-ref grammar) belong to
    :func:`validate_learned_item` so every rejection carries an
    attributable ``pattern_id``.
    """

    model_config = ConfigDict(extra="forbid")

    scope: str
    preconditions: list[str] = []
    procedure_steps: list[str] = []
    verification: str = ""
    known_failures: list[str] = []
    prohibited_actions: list[str] = []
    evidence_references: list[str] = []


@dataclass(frozen=True)
class LearnedItemViolation:
    field_path: str
    pattern_id: str
    excerpt: str

    def __post_init__(self) -> None:
        if len(self.excerpt) > _EXCERPT_MAX:
            object.__setattr__(self, "excerpt", self.excerpt[:_EXCERPT_MAX])


class LearnedItemValidationError(ValueError):
    """Loud, attributable rejection — never a bare bool (freeze G0.4)."""

    def __init__(self, violations: list[LearnedItemViolation]) -> None:
        self.violations = violations
        summary = "; ".join(
            f"{v.field_path}: {v.pattern_id}" for v in violations
        )
        super().__init__(f"learned-item record rejected: {summary}")


_SCALAR_FIELDS = ("scope", "verification")
_LIST_FIELDS = (
    "preconditions",
    "procedure_steps",
    "known_failures",
    "prohibited_actions",
    "evidence_references",
)
_ACTIONABLE_LIST_FIELDS = (
    "procedure_steps",
    "known_failures",
    "prohibited_actions",
)


def _iter_leaves(record: LearnedItemRecord):
    """Yield ``(field_path, leaf)`` for every string leaf of *record*."""
    for name in _SCALAR_FIELDS:
        yield name, getattr(record, name)
    for name in _LIST_FIELDS:
        for i, leaf in enumerate(getattr(record, name)):
            yield f"{name}[{i}]", leaf


def _leaf_pattern_violations(
    path: str, leaf: str
) -> list[LearnedItemViolation]:
    found: list[LearnedItemViolation] = []

    def hit(pattern_id: str) -> None:
        found.append(LearnedItemViolation(path, pattern_id, leaf))

    if looks_like_injection(leaf):
        hit("injection_hint")
    if _FAKE_SECURITY_HEADING_RE.search(leaf):
        hit("fake_security_heading")
    if _TRAILER_SPOOF_RE.search(leaf):
        hit("trailer_spoof")
    if _TOOL_POLICY_RE.search(leaf):
        hit("tool_policy")
    if any(p.search(leaf) for p in _IMPORT_DIRECTIVE_RES):
        hit("import_directive")
    if FENCE_LINE_RE.search(leaf):
        hit("fence_grammar")
    return found


def _canonical_json_bytes(record: LearnedItemRecord) -> int:
    # Mirrors learned_item_hash.py's canonical form exactly.
    return len(
        json.dumps(
            record.model_dump(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )


def validate_learned_item(
    record: LearnedItemRecord,
    *,
    answer_key_terms: frozenset[str] = frozenset(),
    answer_key_threshold: int = 3,
) -> None:
    """Reject injection-shaped / malformed content at INSERT time.

    Raises :class:`LearnedItemValidationError` carrying ALL violations
    (not first-only). ``answer_key_terms`` defaults to an empty set =
    the answer-key check is inert; U4-H wires the real suite terms.
    """
    violations: list[LearnedItemViolation] = []

    # ── leaf-level checks ────────────────────────────────────────────
    for path, leaf in _iter_leaves(record):
        violations.extend(_leaf_pattern_violations(path, leaf))
        if len(leaf) > MAX_LEAF_CHARS:
            violations.append(
                LearnedItemViolation(path, "oversize_leaf", leaf)
            )
    for path, leaf in (
        (f"evidence_references[{i}]", ref)
        for i, ref in enumerate(record.evidence_references)
    ):
        if not _EVIDENCE_REF_RE.match(leaf):
            violations.append(
                LearnedItemViolation(path, "bad_evidence_ref", leaf)
            )

    # ── list / record size caps ──────────────────────────────────────
    for name in _LIST_FIELDS:
        items = getattr(record, name)
        if len(items) > MAX_LIST_ITEMS:
            violations.append(
                LearnedItemViolation(
                    name, "oversize_list", f"{len(items)} items"
                )
            )
    if _canonical_json_bytes(record) > MAX_RECORD_JSON_BYTES:
        violations.append(
            LearnedItemViolation(
                "<record>",
                "oversize_record",
                f"{_canonical_json_bytes(record)} bytes",
            )
        )

    # ── record-level: answer-key leak ────────────────────────────────
    if answer_key_terms:
        blob = "\n".join(leaf for _, leaf in _iter_leaves(record)).lower()
        hits = sorted(
            term for term in answer_key_terms if term.lower() in blob
        )
        if len(hits) >= answer_key_threshold:
            violations.append(
                LearnedItemViolation(
                    "<record>", "answer_key_leak", ", ".join(hits)
                )
            )

    # ── record-level: empty content ──────────────────────────────────
    has_actionable = any(
        leaf.strip()
        for name in _ACTIONABLE_LIST_FIELDS
        for leaf in getattr(record, name)
    )
    if not record.scope.strip() or not has_actionable:
        violations.append(
            LearnedItemViolation(
                "<record>",
                "empty_content",
                "no scope and/or no actionable content",
            )
        )

    if violations:
        raise LearnedItemValidationError(violations)
