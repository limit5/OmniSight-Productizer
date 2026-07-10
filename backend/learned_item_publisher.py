"""OP-2567 U4-B + OP-2570 U4-C1 — publication-ledger ACTOR BOUNDARY.

Boundary contract: this is the ONLY sanctioned writer of the
publication ledger (migration 0258); producers (U4-I) never import it.
U4-C1 composes the atomic advisory-locked publish / revoke procedures
ON the thin writer, plus the read-only invariant probe. Ships DORMANT:
deny-by-default kill-switch + no caller until U4-I/J wire producers and
scheduling (the loader READ side is sibling U4-C2, not here).

Transaction contract (freeze G3): the CALLER owns the transaction —
every procedure REQUIRES a ``conn`` already inside a caller-managed
transaction (asyncpg idiom: ``async with conn.transaction(): ...``).
The procedures NEVER commit or roll back; any failure RAISES, aborting
the caller's txn so no partial state can persist. The per-scope
advisory lock is taken INSIDE, so it is xact-scoped as designed.

``conn`` is a parameter everywhere (pure seam) — this module never
imports backend.db_pool nor initialises a pool. One-way metrics
dependency: this module imports backend.metrics, never the reverse.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from backend import metrics
from backend.learned_item_publication import (
    PUBLICATION_STATES,
    check_publication_invariant,
    compute_live_set_hash,
    derive_membership_entry,
    is_legal_transition,
    publication_scope_key,
    reduce_publication_state,
)


# ━━ Kill-switch (deny-by-default, freeze phase gate) ━━━━━━━━━━━━━━━━━

# The truthy parse is implemented LOCALLY on purpose: the ops interlock
# helper sits behind a fastapi-heavy import chain and uses a DIFFERENT
# truthy set — this boundary must import light and stay deny-by-default.
# J2 flips the env in production; until then both write procedures
# refuse loudly.
_KILL_SWITCH_ENV = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes"})


def _promotion_enabled() -> bool:
    return os.environ.get(_KILL_SWITCH_ENV, "").strip().lower() in _TRUTHY


class PublicationDenied(RuntimeError):
    """Typed, loud refusal — kill-switch off / not-in-scope."""


class PublishValidationError(ValueError):
    """Fail-closed publish/revoke validation failure. ``reason`` is a
    stable machine-readable code (mirrors the D writer's error idiom)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublishResult:
    published: bool
    idempotent: bool
    live_set_head: int
    live_set_hash: str


@dataclass(frozen=True)
class RevokeResult:
    revoked: bool
    live_set_head: int
    live_set_hash: str


# ━━ U4-B thin writer (unchanged behavior when event_seq is None) ━━━━━


async def acquire_publish_lock(conn: Any, scope_key: str) -> None:
    """Take the per-scope xact-scoped advisory lock (freeze G3 TOCTOU
    defense). MUST be called inside an open transaction — the lock is
    released at commit/rollback, never explicitly.

    No sqlite equivalent: sqlite tests exercise the writer without the
    lock (single-writer test DB).
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))", scope_key
    )


async def insert_publication_event(
    conn: Any,
    *,
    event_id: str,
    version_id: str,
    state: str,
    approval_id: str | None = None,
    published_at: Any = None,
    revoked_at: Any = None,
    revoked_by: str | None = None,
    revoke_reason: str | None = None,
    event_seq: int | None = None,
) -> None:
    """Thin guarded INSERT of one publication event (``event_id`` maps
    to the table's ``id`` PK column). When ``event_seq`` is None the
    column is not in the column list — PG identity assigns it (sqlite
    leaves it NULL); when provided (the ``published`` event carries the
    new head, OP-2570 reconciliation) the column joins the INSERT."""
    if state not in PUBLICATION_STATES:
        raise ValueError(
            f"insert_publication_event: state {state!r} is not one of "
            f"{PUBLICATION_STATES}"
        )
    if event_seq is None:
        await conn.execute(
            "INSERT INTO memory_publications "
            "(id, version_id, approval_id, state, published_at, revoked_at, "
            " revoked_by, revoke_reason) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            event_id,
            version_id,
            approval_id,
            state,
            published_at,
            revoked_at,
            revoked_by,
            revoke_reason,
        )
    else:
        await conn.execute(
            "INSERT INTO memory_publications "
            "(id, version_id, approval_id, state, event_seq, published_at, "
            " revoked_at, revoked_by, revoke_reason) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            event_id,
            version_id,
            approval_id,
            state,
            event_seq,
            published_at,
            revoked_at,
            revoked_by,
            revoke_reason,
        )


# ━━ U4-C1 internal reads ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_json(value: Any) -> Any:
    # sqlite returns jsonb columns as TEXT; asyncpg returns str for
    # jsonb unless a codec is set — always decode a str, pass through
    # an already-decoded list/dict.
    return json.loads(value) if isinstance(value, str) else value


async def _read_version(conn: Any, version_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT audience, tenant_id, rendered_payload, "
        "rendered_payload_sha256, delivery_mode "
        "FROM learned_item_versions WHERE id = $1",
        version_id,
    )
    if row is None:
        raise PublishValidationError("version_not_found")
    # Positional normalize: asyncpg Record / sqlalchemy Row portability.
    return {
        "audience": row[0],
        "tenant_id": row[1],
        "rendered_payload": row[2],
        "rendered_payload_sha256": row[3],
        "delivery_mode": row[4],
    }


async def _read_latest_snapshot(
    conn: Any, scope_key: str
) -> tuple[int, list[dict[str, Any]]]:
    """Latest snapshot head + membership for the scope; (0, []) when no
    snapshot exists yet."""
    row = await conn.fetchrow(
        "SELECT live_set_head, membership FROM learned_item_snapshots "
        "WHERE scope_key = $1 ORDER BY live_set_head DESC LIMIT 1",
        scope_key,
    )
    if row is None:
        return 0, []
    return int(row[0]), _load_json(row[1])


async def _derive_ledger_membership(
    conn: Any, *, scope_key: str
) -> list[dict[str, Any]]:
    """Re-derive the eligible ledger membership for a scope (freeze
    V3.4 invariant leg). PRESENCE-BASED, not a frozen-order reduce:
    terminal events carry NULL ``event_seq`` and NULLS FIRST sorts them
    BEFORE the seq-carrying ``published`` event, so a naive reduction
    mis-reports superseded/revoked versions as live (empirically
    proven). Eligible iff: has a ``published`` event AND no ``revoked``
    event AND no ``superseded`` event — equivalent to true-chronology
    reduction under the frozen table, because terminals are absorbing
    and only reachable from ``published``. Module-level seam shared by
    the reconcile probe (and monkeypatchable by tests)."""
    audience, _, tenant_token = scope_key.partition(":")
    rows = await conn.fetch(
        "SELECT v.id, v.rendered_payload_sha256, v.delivery_mode, "
        "p.event_seq "
        "FROM memory_publications p "
        "JOIN learned_item_versions v ON v.id = p.version_id "
        "WHERE p.state = 'published' "
        "AND v.audience = $1 "
        "AND COALESCE(v.tenant_id, '-') = $2 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM memory_publications t "
        "WHERE t.version_id = p.version_id "
        "AND t.state IN ('revoked', 'superseded'))",
        audience,
        tenant_token,
    )
    return [
        derive_membership_entry(
            version_id=row[0],
            rendered_payload_sha256=row[1],
            delivery_mode=row[2],
            publication_event_seq=None if row[3] is None else int(row[3]),
        )
        for row in rows
    ]


async def _build_rendered_bundle(
    conn: Any, membership: list[dict[str, Any]]
) -> dict[str, Any]:
    """rendered_bundle = {version_id: stored rendered_payload} for
    EVERY member, read from the versions table — the loader must never
    re-render (freeze G2 render-drift)."""
    bundle: dict[str, Any] = {}
    for member in membership:
        row = await conn.fetchrow(
            "SELECT rendered_payload FROM learned_item_versions "
            "WHERE id = $1",
            member["version_id"],
        )
        if row is None:
            raise PublishValidationError("version_not_found")
        bundle[member["version_id"]] = row[0]
    return bundle


async def _insert_snapshot(
    conn: Any,
    *,
    scope_key: str,
    live_set_head: int,
    membership: list[dict[str, Any]],
    rendered_bundle: dict[str, Any],
    built_by: str,
) -> None:
    await conn.execute(
        "INSERT INTO learned_item_snapshots "
        "(scope_key, live_set_head, membership, rendered_bundle, built_by) "
        "VALUES ($1, $2, $3, $4, $5)",
        scope_key,
        live_set_head,
        json.dumps(membership, sort_keys=True, separators=(",", ":")),
        json.dumps(rendered_bundle, sort_keys=True, separators=(",", ":")),
        built_by,
    )


def _check_invariant_or_raise(
    scope_key: str,
    ledger_membership: list[dict[str, Any]],
    snapshot_membership: list[dict[str, Any]],
) -> None:
    reasons = check_publication_invariant(
        ledger_membership, snapshot_membership
    )
    if not reasons:
        return
    for reason in reasons:
        # Label hygiene: reason FAMILY prefix only — full reasons embed
        # version ids (unbounded cardinality).
        metrics.memory_reconcile_divergence_total.labels(
            scope=scope_key, reason=reason.split(":")[0]
        ).inc()
    raise PublishValidationError("invariant_divergence")


# ━━ U4-C1 advisory-locked procedures ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def publish_learned_item_version(
    conn: Any,
    *,
    version_id: str,
    approval_id: str,
    actor: str,
    supersedes_version_id: str | None = None,
) -> PublishResult:
    """Atomic advisory-locked publish (freeze G3/G4/V3.4).

    Caller owns the transaction (``async with conn.transaction():``) —
    this procedure never commits; any raise rolls the caller's txn back
    so ledger events and the snapshot row land together or not at all.
    Predecessor retirement is EXPLICIT via ``supersedes_version_id``
    (no name-matching auto-discovery — linkage is the caller's call).
    """
    if not _promotion_enabled():
        raise PublicationDenied("kill_switch_off")
    version = await _read_version(conn, version_id)
    scope_key = publication_scope_key(
        version["audience"], version["tenant_id"]
    )
    await acquire_publish_lock(conn, scope_key)
    approval = await conn.fetchrow(
        "SELECT version_id, live_set_hash FROM memory_approvals "
        "WHERE id = $1",
        approval_id,
    )
    if approval is None:
        raise PublishValidationError("approval_not_found")
    if approval[0] != version_id:
        raise PublishValidationError("approval_version_mismatch")
    approval_live_set_hash = approval[1]
    current_head, current_membership = await _read_latest_snapshot(
        conn, scope_key
    )
    # Idempotency (G3 retry key): a published event with the SAME
    # approval already exists → no-op success, no new events.
    existing = await conn.fetchrow(
        "SELECT 1 FROM memory_publications "
        "WHERE version_id = $1 AND approval_id = $2 "
        "AND state = 'published'",
        version_id,
        approval_id,
    )
    if existing is not None:
        return PublishResult(
            published=True,
            idempotent=True,
            live_set_head=current_head,
            live_set_hash=compute_live_set_hash(current_membership),
        )
    # Prior state → required insert chain (fail-closed: no auto-healing
    # of half-finished chains).
    rows = await conn.fetch(
        "SELECT state FROM memory_publications "
        "WHERE version_id = $1 ORDER BY event_seq NULLS FIRST, id",
        version_id,
    )
    events = [{"state": row[0]} for row in rows]
    prior = reduce_publication_state(events)
    if prior is None:
        chain: tuple[str, ...] = ("approved", "publishing", "published")
    elif prior in ("approved", "publish_failed"):
        chain = ("publishing", "published")
    else:
        raise PublishValidationError(f"illegal_transition:{prior}")
    # F4 revalidation under the lock: the approval bound the EXACT
    # current live set — a moved set needs re-eval/re-approval.
    if compute_live_set_hash(current_membership) != approval_live_set_hash:
        raise PublishValidationError("live_set_changed")
    new_head = current_head + 1
    step_prior = prior
    for state in chain:
        if not is_legal_transition(step_prior, state):
            raise PublishValidationError(f"illegal_transition:{step_prior}")
        await insert_publication_event(
            conn,
            event_id=str(uuid.uuid4()),
            version_id=version_id,
            state=state,
            approval_id=approval_id,
            event_seq=new_head if state == "published" else None,
        )
        step_prior = state
    if supersedes_version_id is not None:
        if supersedes_version_id not in {
            m["version_id"] for m in current_membership
        }:
            raise PublishValidationError("not_published")
        await insert_publication_event(
            conn,
            event_id=str(uuid.uuid4()),
            version_id=supersedes_version_id,
            state="superseded",  # ungated state — no approval_id
            revoke_reason=f"superseded_by:{version_id}",
        )
    new_membership = [
        m
        for m in current_membership
        if m["version_id"] != supersedes_version_id
    ] + [
        derive_membership_entry(
            version_id=version_id,
            rendered_payload_sha256=version["rendered_payload_sha256"],
            delivery_mode=version["delivery_mode"],
            publication_event_seq=new_head,
        )
    ]
    new_hash = compute_live_set_hash(new_membership)
    rendered_bundle = await _build_rendered_bundle(conn, new_membership)
    await _insert_snapshot(
        conn,
        scope_key=scope_key,
        live_set_head=new_head,
        membership=new_membership,
        rendered_bundle=rendered_bundle,
        built_by=actor,
    )
    # In-txn publication invariant (V3.4): re-derive the ledger side
    # from the DB state now visible in this txn; divergence rolls the
    # whole publish back.
    ledger_membership = await _derive_ledger_membership(
        conn, scope_key=scope_key
    )
    _check_invariant_or_raise(scope_key, ledger_membership, new_membership)
    metrics.memory_promotion_total.labels(decision="published").inc()
    return PublishResult(
        published=True,
        idempotent=False,
        live_set_head=new_head,
        live_set_hash=new_hash,
    )


async def revoke_learned_item_version(
    conn: Any,
    *,
    version_id: str,
    revoked_by: str,
    reason: str,
) -> RevokeResult:
    """Atomic advisory-locked revocation — the publish machine in
    reverse: the revoked version's bytes become unretrievable from the
    new snapshot head (freeze F7.5). Caller owns the transaction (see
    ``publish_learned_item_version``). ``revoked_at`` stays NULL by
    design: this module never reads a clock — the event ROW plus
    actor + reason IS the record."""
    if not _promotion_enabled():
        raise PublicationDenied("kill_switch_off")
    version = await _read_version(conn, version_id)
    scope_key = publication_scope_key(
        version["audience"], version["tenant_id"]
    )
    await acquire_publish_lock(conn, scope_key)
    current_head, current_membership = await _read_latest_snapshot(
        conn, scope_key
    )
    if version_id not in {m["version_id"] for m in current_membership}:
        raise PublishValidationError("not_published")
    await insert_publication_event(
        conn,
        event_id=str(uuid.uuid4()),
        version_id=version_id,
        state="revoked",  # ungated state — no approval_id
        revoked_by=revoked_by,
        revoke_reason=reason,
    )
    new_head = current_head + 1
    new_membership = [
        m for m in current_membership if m["version_id"] != version_id
    ]
    new_hash = compute_live_set_hash(new_membership)
    rendered_bundle = await _build_rendered_bundle(conn, new_membership)
    await _insert_snapshot(
        conn,
        scope_key=scope_key,
        live_set_head=new_head,
        membership=new_membership,
        rendered_bundle=rendered_bundle,
        built_by=revoked_by,
    )
    ledger_membership = await _derive_ledger_membership(
        conn, scope_key=scope_key
    )
    _check_invariant_or_raise(scope_key, ledger_membership, new_membership)
    metrics.memory_promotion_total.labels(decision="revoked").inc()
    return RevokeResult(
        revoked=True, live_set_head=new_head, live_set_hash=new_hash
    )


async def reconcile_publication_invariant(
    conn: Any, *, scope_key: str
) -> tuple[str, ...]:
    """The EXTERNAL reconciler's probe_once (G0.2 second leg): read the
    latest snapshot, re-derive the ledger membership, compare. READ-ONLY
    and deliberately UNGATED by the kill-switch. NO timer / thread /
    scheduling here — ship the probe, not the activation (the S3
    lesson); the operator / U4-J wires it."""
    _head, snapshot_membership = await _read_latest_snapshot(
        conn, scope_key
    )
    ledger_membership = await _derive_ledger_membership(
        conn, scope_key=scope_key
    )
    reasons = check_publication_invariant(
        ledger_membership, snapshot_membership
    )
    for reason in reasons:
        metrics.memory_reconcile_divergence_total.labels(
            scope=scope_key, reason=reason.split(":")[0]
        ).inc()
    return reasons
