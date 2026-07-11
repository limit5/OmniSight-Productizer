"""OP-2576 U4-I — quarantined learned-item version WRITER.

Boundary contract: producers write ONLY through this module (freeze
``docs/design/2026-07-10-phase-u4-a0-contract-freeze.md`` §G0/G1/V3.2 —
"no dual truth"). The redirect of the two live producers (skill_distiller
and skills_extractor) into the ONE governed path lands here alongside
this module; ``auto_distilled_skills`` and ``configs/skills/_pending/*.md``
are FROZEN ARCHIVES from now on — no reader edits, no dual write.

Every submission is INSERTed as a quarantined version row (append-only,
migration 0258). Downstream is inert: no eval, no approval, no
publication — the C1 publisher / D approvals / C2 loader stay dormant
until U4-J. The per-audience partial-unique index makes a duplicate
canonical-content-hash in-scope a no-op re-observation (idempotent).

Evidence is BEST-EFFORT at submit time (E's ``derive_ground_truths`` is
called with INJECTED raw lookup data; a ``ProvenanceUnconfirmable`` is
loud-metric + skip, never fatal). Approval/eval demand real evidence
later — the missing evidence rows land as fail-closed there.

``conn`` is a parameter everywhere (pure seam) — this module never
imports backend.db_pool nor initialises a pool; ``now`` is passed by the
caller (clock-ban). One-way dependencies: this module composes the A1
hash, A2 record+renderer, and E provenance modules; live Gerrit/JIRA
fetch wiring is U4-J's.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from backend import metrics
from backend.learned_item_hash import canonical_content_hash
from backend.learned_item_provenance import (
    ProvenanceUnconfirmable,
    derive_ground_truths,
    normalize_change_ref,
    reverify_for_promotion,
)
from backend.learned_item_renderer import validate_and_render

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    """Return shape of :func:`submit_quarantined_version`.

    ``created=False`` means the row was ALREADY there — the per-audience
    partial-unique index folds an identical resubmission into the
    existing id (idempotent re-observation, not an error).
    """

    version_id: str
    created: bool


@dataclass(frozen=True)
class ReverifyResult:
    """Verdict from a make_reverify_hook callable (dormant in this
    ticket — U4-J wires it into publish)."""

    ok: bool
    reasons: tuple[str, ...]
    revert_state: str


def _is_unique_violation(exc: BaseException) -> bool:
    """Dialect-agnostic recognition of the "duplicate canonical hash in
    scope" case: sqlite raises ``sqlalchemy.exc.IntegrityError`` (unique
    constraint failed), PG raises ``asyncpg.UniqueViolationError``.
    Import at check time to avoid pulling either dependency into module
    load."""
    name = type(exc).__name__
    if name == "UniqueViolationError":
        return True
    if name == "IntegrityError":
        return True
    text = str(exc).lower()
    return "unique" in text and (
        "constraint" in text or "violat" in text or "duplicate" in text
    )


async def _select_existing_version_id(
    conn: Any, *, audience: str, tenant_id: str | None, content_hash: str
) -> str | None:
    """Look up the existing version row that owns *content_hash* within
    the scope. Uses ``COALESCE(tenant_id, '-')`` so global rows (tenant
    is NULL by CHECK) and tenant rows are compared consistently.

    Post-catch SELECT works with no rollback: submit runs outside a
    caller txn (empirically proven — the sqlite adapter is the same
    connection that just failed the INSERT, and asyncpg outside a txn
    autocommits per statement)."""
    row = await conn.fetchrow(
        "SELECT id FROM learned_item_versions "
        "WHERE audience = $1 "
        "AND COALESCE(tenant_id, '-') = $2 "
        "AND canonical_content_hash = $3",
        audience,
        tenant_id if tenant_id is not None else "-",
        content_hash,
    )
    if row is None:
        return None
    return row[0]


async def submit_quarantined_version(
    conn: Any,
    *,
    payload: dict,
    kind: str,
    audience: str,
    tenant_id: str | None,
    created_by: str,
    name: str,
    description: str = "",
    trigger_condition: str = "",
    keywords: list[str] | None = None,
    delivery_mode: str = "retrieved",
    evidence: tuple = (),
    now: str,
) -> SubmitResult:
    """Validate + render + hash + INSERT one quarantined version row.

    Steps (per U4-I contract):
      1. ``validate_and_render(payload)`` (A2 — raises on injection/shape)
      2. ``canonical_content_hash(payload)`` (A1 helper)
      3. INSERT the versions row; catch the partial-unique violation and
         return the existing row's id with ``created=False``.
      4. For each evidence item (raw injected lookup data), derive
         ground truths (E) and INSERT evidence rows; unconfirmable ones
         are LOUD-metric + skipped, never fatal at submit.

    Returns a :class:`SubmitResult` — the caller can key idempotent
    downstream work on ``created``.
    """
    import json

    # 1. validate + render (A2). Fail-closed: any injection/shape/size
    #    violation raises; NO row is written.
    _record, rendered = validate_and_render(payload)

    # 2. canonical content hash (A1 — content-only, provenance-blind).
    content_hash = canonical_content_hash(payload)

    version_id = str(uuid.uuid4())
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    keywords_json = (
        json.dumps(keywords) if keywords is not None else None
    )

    # 3. INSERT the versions row (asyncpg $N ascending). The partial
    #    unique index on (audience, hash) / (tenant_id, hash) folds an
    #    in-scope duplicate into an error we CATCH → return the existing
    #    id. Any OTHER error (bad kind, bad audience, etc.) RE-RAISES.
    try:
        await conn.execute(
            "INSERT INTO learned_item_versions ("
            "id, canonical_content_hash, kind, audience, tenant_id, "
            "payload, rendered_payload, renderer_version, "
            "rendered_payload_sha256, delivery_mode, name, description, "
            "trigger_condition, keywords, created_by"
            ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, "
            "$13, $14, $15)",
            version_id,
            content_hash,
            kind,
            audience,
            tenant_id,
            payload_json,
            rendered.rendered_payload,
            rendered.renderer_version,
            rendered.rendered_payload_sha256,
            delivery_mode,
            name,
            description,
            trigger_condition,
            keywords_json,
            created_by,
        )
        created = True
    except Exception as exc:
        if not _is_unique_violation(exc):
            raise
        existing_id = await _select_existing_version_id(
            conn,
            audience=audience,
            tenant_id=tenant_id,
            content_hash=content_hash,
        )
        if existing_id is None:
            # Unique-shaped error but no matching row = a real error, not
            # a dup — re-raise (freeze fail-closed).
            raise
        return SubmitResult(version_id=existing_id, created=False)

    # 4. best-effort evidence rows (E). Unconfirmable → metric + skip;
    #    a real derive failure (bad_change_ref etc.) is not a submit
    #    failure — evidence is best-effort HERE, DEMANDED at promotion.
    for item in evidence:
        change_ref = item.get("change_ref")
        gerrit_change = item.get("gerrit_change")
        jira_labels = item.get("jira_labels")
        try:
            truths = derive_ground_truths(
                change_ref=change_ref,
                gerrit_change=gerrit_change,
                jira_labels=jira_labels,
                now=now,
            )
        except ProvenanceUnconfirmable:
            metrics.memory_failclosed_total.labels(
                reason="provenance_unconfirmable"
            ).inc()
            continue
        for truth in truths:
            evidence_span_json = json.dumps(
                truth.evidence_span, sort_keys=True, separators=(",", ":")
            )
            await conn.execute(
                "INSERT INTO learned_item_evidence ("
                "id, version_id, ground_truth_kind, source_change_id, "
                "verified_at, revert_state, evidence_span"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7)",
                str(uuid.uuid4()),
                version_id,
                truth.kind,
                truth.source_change_id,
                truth.verified_at,
                truth.revert_state,
                evidence_span_json,
            )

    return SubmitResult(version_id=version_id, created=created)


def make_reverify_hook(
    *,
    fetch_change: Callable[[str], Any],
    fetch_labels: Callable[[str], Any],
) -> Callable[..., Any]:
    """Build the reverify-at-promotion callable (freeze G3 reverify seam).

    DORMANT in this ticket: nothing calls the returned hook — U4-J wires
    it into the publish flow. Ships + unit-tested only.

    The hook reads the version's stored evidence rows, re-derives via
    E's ``reverify_for_promotion`` with FRESH injected data, and returns
    the accumulated verdict across ALL evidence rows.

    ⚠ Stored ``source_change_id`` is CANONICAL (``gerrit:2046``) but
    E's ``normalize_change_ref`` REJECTS that form as INPUT — the hook
    strips the ``gerrit:`` prefix before re-deriving.
    """

    async def _hook(
        version_id: str, conn: Any, now: str
    ) -> ReverifyResult:
        rows = await conn.fetch(
            "SELECT ground_truth_kind, source_change_id "
            "FROM learned_item_evidence WHERE version_id = $1",
            version_id,
        )
        reasons: list[str] = []
        revert_state = "none"
        any_row = False
        for row in rows:
            any_row = True
            original_kind = row[0]
            stored_ref = row[1] or ""
            # Strip the canonical "gerrit:" prefix — E rejects that form.
            probe_ref = stored_ref
            if probe_ref.startswith("gerrit:"):
                probe_ref = probe_ref[len("gerrit:"):]
            elif probe_ref.startswith("#"):
                probe_ref = probe_ref[1:]
            gerrit_change = fetch_change(stored_ref)
            jira_labels = fetch_labels(stored_ref)
            if callable(getattr(gerrit_change, "__await__", None)):
                gerrit_change = await gerrit_change  # type: ignore[assignment]
            if callable(getattr(jira_labels, "__await__", None)):
                jira_labels = await jira_labels  # type: ignore[assignment]
            # Canonicalize back through E so we compare apples-to-apples
            # if the fetched form differs (paranoia; E raises on bad).
            try:
                normalize_change_ref(probe_ref)
            except ProvenanceUnconfirmable:
                reasons.append("bad_change_ref")
                continue
            verdict = reverify_for_promotion(
                original_kind=original_kind,
                change_ref=probe_ref,
                gerrit_change=gerrit_change,
                jira_labels=jira_labels,
                now=now,
            )
            if verdict.revert_state == "reverted":
                revert_state = "reverted"
            reasons.extend(verdict.reasons)
        if not any_row:
            reasons.append("no_evidence")
        return ReverifyResult(
            ok=not reasons, reasons=tuple(reasons), revert_state=revert_state
        )

    return _hook


__all__ = [
    "ReverifyResult",
    "SubmitResult",
    "make_reverify_hook",
    "submit_quarantined_version",
]
