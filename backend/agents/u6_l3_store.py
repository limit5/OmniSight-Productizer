"""U6-4 — L3 per-user erasable semantic-fact store (DORMANT).

The greenfield L3 store (design §2.D / §3 L3). Each fact's VALUE + provenance +
semantic key is SEALED (U6-0b crypto-shred: ``sealed_ciphertext`` + ``dek_ref``);
the semantic key ``(fact_type, subject, predicate)`` is ALSO stored clear for
querying (and bound into the seal, so a clear-column tamper is caught at open).

Isolation = explicit ``tenant_id = $ AND user_id = $`` predicates on EVERY query
(PRIMARY + always-live) + FORCED row-level security (DEFENSE-IN-DEPTH). NOTE(ops):
PostgreSQL never applies RLS to a SUPERUSER, and the current prod app connects as a
superuser (boreas-A1) — so the APP PREDICATES are the sole LIVE isolation until the
app role is de-superuser-ed; the RLS layer is verified correct (tested under a
non-superuser role) and waits for that deploy change. Do NOT drop an app predicate
trusting the RLS backstop.

Provides the store CRUD (insert-quarantined / get / list-live / promote with
one-current-value supersession) + the **hard-erase** primitive: destroy every
``dek_ref`` (crypto-shred ⇒ the surviving ciphertext is unrecoverable even from a
backup) then delete the rows, leaving only a content-free ``l3_erasure_audit``
tombstone. The multi-step ACTIVE→ERASING→ERASED lifecycle (``u6_memory_scope``)
is U6-6's UX; this primitive is atomic.

PG-native (asyncpg ``$N`` + RLS). DORMANT: no producer/consumer is wired (U6-5
writes, U6-6 confirms/erases, U6-7 reads). Not gated by a flag because nothing
calls it; it executes only when a caller passes a live connection.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from backend.agents.u6_fact_schema import Fact, FactType, Sensitivity
from backend.agents.u6_memory_crypto import SealedMemory, open_sealed, seal
from backend.agents.u6_memory_scope import MemoryScope, scope_key

_STATES = ("quarantined", "promoted", "superseded", "rejected")


class L3StoreError(Exception):
    """An L3 store invariant was violated — fail closed."""


@dataclass(frozen=True, slots=True)
class StoredFact:
    """A row's clear metadata + the opened (decrypted) fact."""

    id: str
    state: str
    revision: int
    fact: Fact


def _new_fact_id() -> str:
    return "l3f-" + uuid.uuid4().hex


async def _enter_scope(conn, scope: MemoryScope) -> None:
    """Set the per-transaction RLS session settings. MUST run inside a txn (the
    ``true`` local flag scopes them to it), so the FORCED policy applies."""
    if not isinstance(scope, MemoryScope):
        raise L3StoreError("scope must be a MemoryScope")
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", scope.tenant_id)
    await conn.execute("SELECT set_config('app.user_id', $1, true)", scope.user_id)


def _seal_payload(scope: MemoryScope, fact: Fact) -> SealedMemory:
    # seal the value + provenance AND the semantic key, so the clear queryable key
    # is bound to the sealed content: a clear-column swap/tamper is detected at open
    # (the envelope only AAD-binds tenant, so this is the key↔value integrity tie).
    return seal(scope, json.dumps({
        "value": fact.value, "source_span": fact.source_span,
        "fact_type": fact.fact_type.value, "subject": fact.subject, "predicate": fact.predicate,
    }))


def _open_to_fact(row: dict, scope: MemoryScope) -> Fact:
    # asyncpg returns a jsonb column as a str; offline callers pass a dict.
    raw_ref = row["dek_ref"]
    dek_ref = raw_ref if isinstance(raw_ref, dict) else json.loads(raw_ref)
    payload = json.loads(open_sealed(SealedMemory(ciphertext=row["sealed_ciphertext"], dek_ref=dek_ref)))
    # the sealed semantic key MUST match the clear columns — else a clear-column
    # tamper (or a mis-associated ciphertext) is being served; fail closed.
    if (
        payload.get("fact_type") != row["fact_type"]
        or payload.get("subject") != row["subject"]
        or payload.get("predicate") != row["predicate"]
    ):
        raise L3StoreError("sealed semantic key does not match the clear columns (tamper)")
    return Fact(
        fact_type=FactType(row["fact_type"]),
        subject=row["subject"],
        predicate=row["predicate"],
        value=payload["value"],
        source_span=payload["source_span"],
        sensitivity=Sensitivity(row["sensitivity"]),
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
    )


async def insert_fact(conn, scope: MemoryScope, fact: Fact) -> str:
    """Seal + insert a fact in the QUARANTINED state (not live). Returns its id."""
    if not isinstance(fact, Fact):
        raise L3StoreError("fact must be a validated Fact")
    fact_id = _new_fact_id()
    async with conn.transaction():
        await _enter_scope(conn, scope)
        sealed = _seal_payload(scope, fact)
        await conn.execute(
            """INSERT INTO l3_facts
               (id, tenant_id, user_id, fact_type, subject, predicate,
                sealed_ciphertext, dek_ref, sensitivity, valid_from, valid_until, state, revision)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,'quarantined',0)""",
            fact_id, scope.tenant_id, scope.user_id, fact.fact_type.value,
            fact.subject, fact.predicate, sealed.ciphertext, json.dumps(sealed.dek_ref),
            fact.sensitivity.value, fact.valid_from, fact.valid_until,
        )
    return fact_id


async def list_facts(conn, scope: MemoryScope, *, state: str = "promoted") -> list[StoredFact]:
    """List a user's facts in ``state`` (RLS-scoped; opens each sealed value)."""
    if state not in _STATES:
        raise L3StoreError(f"invalid state: {state!r}")
    async with conn.transaction():
        await _enter_scope(conn, scope)
        # EXPLICIT app predicates (primary isolation) + RLS (defense-in-depth): a
        # query is scoped by BOTH, so isolation holds even if RLS is bypassed.
        rows = await conn.fetch(
            "SELECT id, fact_type, subject, predicate, sealed_ciphertext, dek_ref, "
            "sensitivity, valid_from, valid_until, state, revision "
            "FROM l3_facts WHERE state = $1 AND tenant_id = $2 AND user_id = $3 ORDER BY created_at",
            state, scope.tenant_id, scope.user_id,
        )
    return [
        StoredFact(id=r["id"], state=r["state"], revision=r["revision"], fact=_open_to_fact(dict(r), scope))
        for r in rows
    ]


async def promote_fact(conn, scope: MemoryScope, fact_id: str) -> int:
    """Promote a quarantined fact to live, superseding any existing promoted fact
    with the same semantic key (one-current-value). Returns the new revision."""
    async with conn.transaction():
        await _enter_scope(conn, scope)
        target = await conn.fetchrow(
            "SELECT fact_type, subject, predicate, state FROM l3_facts "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
            fact_id, scope.tenant_id, scope.user_id,
        )
        if target is None:
            raise L3StoreError("no such fact in scope")
        if target["state"] != "quarantined":
            raise L3StoreError(f"cannot promote from state {target['state']!r}")
        # serialize concurrent promotes for the SAME (scope, semantic key) so the
        # loser WAITS then correctly supersedes (revision+1) instead of aborting on
        # the one-current-value partial unique index (mirrors the U6-2b writer lock).
        lock_key = (
            scope_key(scope) + ":" + f"{len(target['fact_type'])}:{target['fact_type']}"
            + f":{len(target['subject'])}:{target['subject']}"
            + f":{len(target['predicate'])}:{target['predicate']}"
        )
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
        prev = await conn.fetchrow(
            "SELECT id, revision FROM l3_facts "
            "WHERE state = 'promoted' AND tenant_id = $1 AND user_id = $2 "
            "AND fact_type = $3 AND subject = $4 AND predicate = $5",
            scope.tenant_id, scope.user_id,
            target["fact_type"], target["subject"], target["predicate"],
        )
        new_revision = (prev["revision"] + 1) if prev is not None else 0
        if prev is not None:
            await conn.execute(
                "UPDATE l3_facts SET state = 'superseded' "
                "WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
                prev["id"], scope.tenant_id, scope.user_id,
            )
        promoted = await conn.fetchrow(
            "UPDATE l3_facts SET state = 'promoted', revision = $2 "
            "WHERE id = $1 AND tenant_id = $3 AND user_id = $4 AND state = 'quarantined' "
            "RETURNING id",
            fact_id, new_revision, scope.tenant_id, scope.user_id,
        )
        if promoted is None:
            # the row vanished (e.g. a concurrent erase) between the read and here.
            raise L3StoreError("fact no longer promotable (concurrently erased/changed)")
    return new_revision


async def erase_user(conn, scope: MemoryScope) -> int:
    """HARD-ERASE (crypto-shred): destroy every ``dek_ref`` (the only key ⇒ the
    surviving ciphertext is unrecoverable) then delete the rows, recording only a
    content-free tombstone. RLS scopes it to this user. Returns the erased count."""
    async with conn.transaction():
        await _enter_scope(conn, scope)
        # crypto-shred FIRST (overwrite the key in the live tuple), THEN delete +
        # count via RETURNING (accurate even if a row lands concurrently). NOTE(ops):
        # the shred COMPLETES only once the dek_ref is gone from every copy
        # (backups/WAL/PITR/replicas) within the retention RPO — a purge SLA the
        # ops layer owns; txn commit alone does not scrub a WAL/dead-tuple pre-image.
        await conn.execute(
            "UPDATE l3_facts SET dek_ref = '{}'::jsonb WHERE tenant_id = $1 AND user_id = $2",
            scope.tenant_id, scope.user_id,
        )
        deleted = await conn.fetch(
            "DELETE FROM l3_facts WHERE tenant_id = $1 AND user_id = $2 RETURNING id",
            scope.tenant_id, scope.user_id,
        )
        count = len(deleted)
        # the tombstone is content-free: a PSEUDONYM (one-way hash of the scope key),
        # never the raw user_id — no per-user identifier survives the erase.
        user_ref = hashlib.sha256(scope_key(scope).encode()).hexdigest()
        await conn.execute(
            "INSERT INTO l3_erasure_audit (id, tenant_id, user_ref, fact_count) VALUES ($1,$2,$3,$4)",
            "l3e-" + uuid.uuid4().hex, scope.tenant_id, user_ref, count,
        )
    return count
