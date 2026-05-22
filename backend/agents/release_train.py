"""OP-1585/OP-1586 [RT-10a/RT-10b] -- release_train model.

Owns the persistence + concurrency primitive behind the release-train
promote (design doc ``docs/design/2026-05-21-release-train-stories.md``
RT-10a). A *candidate* (a develop commit SHA whose backend+frontend
images have been built and validated) gets one ``release_train`` row;
:func:`promote` is the one-shot transition that retags those validated
digests to a reserved version.

Two invariants this module exists to guarantee (the RT-10a AC):

1. **CAS single-winner.** :func:`promote` flips ``promotion_state``
   ``pending`` -> ``promoting`` with a compare-and-swap UPDATE whose
   WHERE clause re-checks the state at commit time. Two concurrent
   promotes of the same candidate therefore resolve to *exactly one*
   winner; the loser raises :class:`PromoteRaceLost` having performed
   no audit write and no tag write.

2. **Audit insert is a HARD gate, ordered before any tag write.** The
   winner writes an audit row *before* the tag write. Unlike
   :mod:`backend.audit` -- whose ``log`` is deliberately best-effort
   ("don't kill the train because the receipt printer ran out of
   paper") -- the promote path treats a failed/empty audit insert as
   fatal: it marks the train ``failed`` and raises
   :class:`AuditWriteError` *without ever invoking* ``tag_writer``. The
   tag write (the actual image retag) is RT-12 and is supplied here as
   an injected ``tag_writer`` seam so this ticket can prove the ordering
   without owning the retag.

Storage follows the established PG-only store pattern
(``backend.agents.green_evidence`` / ``achievement_unlock_store``):
asyncpg ``$1`` parameter style, an injectable ``conn_factory`` for
tests, and the process-global pool from ``backend.db_pool`` in
production. The table is created by alembic migration
``0247_release_train`` (PG schema is alembic-owned; the sqlite branch
exists for the migration's own portability tests).

OP-1586 / RT-10b adds the planning-only version reservation: a
``reserved_version`` can be attached to a pending train so JIRA
fixVersion / RELEASE META / release notes have a stable version before
promotion. It deliberately does not create image tags or git tags; the
image retag itself remains RT-12.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal


PromotionState = Literal["pending", "promoting", "promoted", "failed"]
ConnFactory = Callable[[], Any]

# Audit-insert seam: a coroutine returning the new audit row id, or
# ``None`` on failure. The production default adapts
# ``backend.audit.log`` (which returns ``None`` rather than raising on a
# failed insert) into this shape; the hard gate converts a ``None``
# return into :class:`AuditWriteError`.
AuditLog = Callable[..., Awaitable[int | None]]

# Tag-writer seam (RT-12): a coroutine performing the actual image
# retag and returning whether the retagged *final* digest equalled the
# validated *source* digest. Invoked ONLY after the audit gate passes.
TagWriter = Callable[["ReleaseTrainRow"], Awaitable[bool]]
ReservationEventSink = Callable[[str, dict[str, Any]], None]

EVENT_RELEASE_VERSION_RESERVED = "release_version_reserved"

# Migration rollback-compat gate seam (RT-11): a coroutine that, given
# the train being promoted, returns a :class:`MigrationCompatResult`
# verdict on whether the previous-final → candidate migration delta can
# be cleanly rolled back. Invoked AFTER CAS-acquire and BEFORE the audit
# / tag writes. The production builder :func:`migration_compat_gate`
# wires it to ``scripts.check_migration_compat`` but it is injectable so
# tests can drive the block / break-glass paths without a versions tree.
MigrationGate = Callable[["ReleaseTrainRow"], Awaitable["MigrationCompatResult"]]

# A full git commit SHA is exactly 40 lowercase hex characters; querying
# / keying is by full SHA only (mirrors green_evidence). The 40-char
# ``length`` CHECK in migration 0247 is the DB-side backstop.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?$")

# Closed promotion-state enum -- mirrors the CHECK constraint literal in
# alembic migration 0247 (asserted equal in the tests).
PROMOTION_STATES: frozenset[str] = frozenset(
    {"pending", "promoting", "promoted", "failed"}
)


class ReleaseTrainError(Exception):
    """Base class for release-train promote failures."""


class UnknownTrain(ReleaseTrainError):
    """No ``release_train`` row exists for the candidate SHA."""


class PromoteRaceLost(ReleaseTrainError):
    """A concurrent promote already moved the train out of ``pending``.

    Raised by the CAS-acquire loser -- it performed no audit write and
    no tag write. Exactly one of N concurrent promotes wins; the rest
    raise this.
    """


class AuditWriteError(ReleaseTrainError):
    """The HARD-gate audit insert failed; the promote aborted.

    Raised *before* any tag write. The train row is left in ``failed``
    state so the failed promote is durable and the candidate can be
    re-promoted by an operator once the audit backend recovers.
    """


class DigestEqualityError(ReleaseTrainError):
    """Final tag digest equality was not proven; promote aborted."""


class MigrationCompatBlocked(ReleaseTrainError):
    """RT-11: a rollback-unsafe migration blocks the promote.

    Raised after CAS-acquire and *before* any audit / tag write when the
    previous-final → candidate migration delta cannot be cleanly rolled
    back and no break-glass override was supplied. The train row is left
    in ``failed`` state. A deliberate operator override
    (:class:`BreakGlass`) bypasses this — but only after a break-glass
    audit row is durably written.
    """


class VersionReservationConflict(ReleaseTrainError):
    """The train already reserved a different release version."""


class VersionReservationStateError(ReleaseTrainError):
    """The train is not in a state where planning reservation is allowed."""


@dataclass(frozen=True)
class ReleaseTrainRow:
    """One release-train candidate row."""

    candidate_sha: str
    source_digest_backend: str
    source_digest_frontend: str
    promotion_state: PromotionState = "pending"
    actor: str = ""
    row_version: int = 0
    reserved_version: str | None = None
    final_digest_equality: bool | None = None
    promotion_audit_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_sha", _full_sha("candidate_sha", self.candidate_sha)
        )
        object.__setattr__(
            self,
            "source_digest_backend",
            _digest("source_digest_backend", self.source_digest_backend),
        )
        object.__setattr__(
            self,
            "source_digest_frontend",
            _digest("source_digest_frontend", self.source_digest_frontend),
        )
        object.__setattr__(
            self, "promotion_state", _state(self.promotion_state)
        )
        object.__setattr__(
            self, "reserved_version", _optional("reserved_version", self.reserved_version)
        )
        if self.final_digest_equality is not None:
            object.__setattr__(
                self, "final_digest_equality", bool(self.final_digest_equality)
            )


@dataclass(frozen=True)
class PromoteResult:
    """Outcome of a winning :func:`promote`."""

    candidate_sha: str
    promotion_audit_id: int
    final_digest_equality: bool
    row_version: int


@dataclass(frozen=True)
class VersionReservationResult:
    """Outcome of a planning-only :func:`reserve_version` call."""

    candidate_sha: str
    reserved_version: str
    row_version: int
    event: dict[str, Any]


@dataclass(frozen=True)
class MigrationCompatResult:
    """RT-11 verdict from the migration rollback-compat gate.

    ``rollback_safe`` is ``False`` when promoting the candidate would
    leave prod with no clean downgrade path to the previous-final
    release. ``unsafe_migrations`` lists the offending migration paths so
    the block reason / break-glass audit row can name them.
    """

    rollback_safe: bool
    reason: str = ""
    unsafe_migrations: tuple[str, ...] = ()


@dataclass(frozen=True)
class BreakGlass:
    """Audited human override of a promote-time safety gate — the digest-equality
    force-promote (RT-22) or the rollback-unsafe migration block (RT-11). Never
    bypasses final digest equality; always requires a durable break-glass audit row."""

    actor: str
    reason: str
    candidate_sha: str = ""   # when non-empty, the force-promote path requires it to match the promoted SHA exactly
    human_command: str = ""   # optional operator command string, recorded in the audit payload when present

    def __post_init__(self) -> None:
        actor = (self.actor or "").strip()
        reason = (self.reason or "").strip()
        if not actor:
            raise ValueError("break-glass requires a non-empty actor")
        if not reason:
            raise ValueError("break-glass requires a non-empty reason")
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "reason", reason)


class PostgresReleaseTrainStore:
    """``release_train`` table backed store."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def create_train(
        self,
        candidate_sha: str,
        source_digest_backend: str,
        source_digest_frontend: str,
        *,
        actor: str = "",
        reserved_version: str | None = None,
    ) -> ReleaseTrainRow:
        return await create_train(
            candidate_sha,
            source_digest_backend,
            source_digest_frontend,
            actor=actor,
            reserved_version=reserved_version,
            conn_factory=self._factory,
        )

    async def get_train(self, candidate_sha: str) -> ReleaseTrainRow | None:
        return await get_train(candidate_sha, conn_factory=self._factory)

    async def promote(
        self,
        candidate_sha: str,
        actor: str,
        *,
        break_glass: BreakGlass | None = None,
        tag_writer: TagWriter | None = None,
        audit_log: AuditLog | None = None,
        migration_gate: MigrationGate | None = None,
    ) -> PromoteResult:
        return await promote(
            candidate_sha,
            actor,
            break_glass=break_glass,
            tag_writer=tag_writer,
            audit_log=audit_log,
            migration_gate=migration_gate,
            conn_factory=self._factory,
        )

    async def reserve_version(
        self,
        candidate_sha: str,
        reserved_version: str,
        *,
        actor: str = "",
        meta_ticket: str | None = None,
        event_sink: ReservationEventSink | None = None,
    ) -> VersionReservationResult:
        return await reserve_version(
            candidate_sha,
            reserved_version,
            actor=actor,
            meta_ticket=meta_ticket,
            event_sink=event_sink,
            conn_factory=self._factory,
        )


async def create_train(
    candidate_sha: str,
    source_digest_backend: str,
    source_digest_frontend: str,
    *,
    actor: str = "",
    reserved_version: str | None = None,
    conn_factory: ConnFactory | None = None,
) -> ReleaseTrainRow:
    """Insert one ``pending`` release-train row for ``candidate_sha``.

    Validation is strict (via :class:`ReleaseTrainRow`): a malformed SHA
    or empty digest raises before anything reaches the DB. The
    ``candidate_sha`` UNIQUE constraint means a second create for the
    same SHA surfaces the DB ``UniqueViolation`` rather than forking the
    promote lifecycle.
    """

    row = ReleaseTrainRow(
        candidate_sha=candidate_sha,
        source_digest_backend=source_digest_backend,
        source_digest_frontend=source_digest_frontend,
        actor=_text(actor),
        reserved_version=reserved_version,
    )
    async with _acquire(conn_factory) as conn:
        saved = await conn.fetchrow(
            """
            INSERT INTO release_train (
                candidate_sha, source_digest_backend, source_digest_frontend,
                reserved_version, promotion_state, actor, row_version
            )
            VALUES ($1, $2, $3, $4, 'pending', $5, 0)
            RETURNING candidate_sha, source_digest_backend,
                      source_digest_frontend, reserved_version,
                      promotion_state, actor, row_version,
                      final_digest_equality, promotion_audit_id
            """,
            row.candidate_sha,
            row.source_digest_backend,
            row.source_digest_frontend,
            row.reserved_version,
            row.actor,
        )
    return _row_to_train(saved)


async def get_train(
    candidate_sha: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> ReleaseTrainRow | None:
    """Return the train row for ``candidate_sha``, or ``None`` if absent.

    A malformed SHA returns ``None`` rather than raising -- there is
    nothing to find.
    """

    normalized = _normalize_sha(candidate_sha)
    if normalized is None:
        return None
    async with _acquire(conn_factory) as conn:
        row = await conn.fetchrow(
            """
            SELECT candidate_sha, source_digest_backend, source_digest_frontend,
                   reserved_version, promotion_state, actor, row_version,
                   final_digest_equality, promotion_audit_id
            FROM release_train
            WHERE candidate_sha = $1
            """,
            normalized,
        )
    return _row_to_train(row) if row is not None else None


async def reserve_version(
    candidate_sha: str,
    reserved_version: str,
    *,
    actor: str = "",
    meta_ticket: str | None = None,
    event_sink: ReservationEventSink | None = None,
    conn_factory: ConnFactory | None = None,
) -> VersionReservationResult:
    """Attach a planning-only release version to a pending train.

    This is RT-10b's boundary: the reserved ``vX.Y.Z`` is for JIRA
    fixVersion / RELEASE META / release notes and changelog preparation
    only. The function never calls a tag writer and never creates a git
    or image tag. Repeating the same reservation is idempotent enough
    for conductor retries; trying to swap to a different version raises
    :class:`VersionReservationConflict`.
    """

    normalized = _normalize_sha(candidate_sha)
    if normalized is None:
        raise UnknownTrain(f"not a 40-hex candidate SHA: {candidate_sha!r}")
    version = _release_version(reserved_version)
    actor = _text(actor)

    async with _acquire(conn_factory) as conn:
        saved = await conn.fetchrow(
            """
            UPDATE release_train
            SET reserved_version = $2,
                actor = $3,
                row_version = row_version + 1,
                updated_at = NOW()
            WHERE candidate_sha = $1
              AND promotion_state = 'pending'
              AND (reserved_version IS NULL OR reserved_version = $2)
            RETURNING candidate_sha, source_digest_backend,
                      source_digest_frontend, reserved_version,
                      promotion_state, actor, row_version,
                      final_digest_equality, promotion_audit_id
            """,
            normalized,
            version,
            actor,
        )
        if saved is None:
            existing = await conn.fetchrow(
                """
                SELECT candidate_sha, source_digest_backend, source_digest_frontend,
                       reserved_version, promotion_state, actor, row_version,
                       final_digest_equality, promotion_audit_id
                FROM release_train
                WHERE candidate_sha = $1
                """,
                normalized,
            )
            if existing is None:
                raise UnknownTrain(f"no release_train row for {normalized}")
            current = _row_to_train(existing)
            if current.promotion_state != "pending":
                raise VersionReservationStateError(
                    f"cannot reserve version for {normalized} in "
                    f"{current.promotion_state!r} state"
                )
            raise VersionReservationConflict(
                f"{normalized} already reserved "
                f"{current.reserved_version!r}; refused {version!r}"
            )

    row = _row_to_train(saved)
    event = release_version_reserved_event(row, meta_ticket=meta_ticket)
    if event_sink is not None:
        event_sink(EVENT_RELEASE_VERSION_RESERVED, event)
    return VersionReservationResult(
        candidate_sha=row.candidate_sha,
        reserved_version=version,
        row_version=row.row_version,
        event=event,
    )


def release_version_reserved_event(
    row: ReleaseTrainRow, *, meta_ticket: str | None = None
) -> dict[str, Any]:
    """Return the release-conductor event for planning-only reservation."""

    if row.reserved_version is None:
        raise ValueError("reserved_version must be set before emitting event")
    event: dict[str, Any] = {
        "event": EVENT_RELEASE_VERSION_RESERVED,
        "fixVersion": row.reserved_version,
        "candidateSha": row.candidate_sha,
    }
    if meta_ticket is not None and meta_ticket.strip():
        event["metaTicket"] = meta_ticket.strip()
    return event


async def promote(
    candidate_sha: str,
    actor: str,
    *,
    break_glass: BreakGlass | None = None,
    tag_writer: TagWriter | None = None,
    audit_log: AuditLog | None = None,
    migration_gate: MigrationGate | None = None,
    conn_factory: ConnFactory | None = None,
) -> PromoteResult:
    """Promote ``candidate_sha``: CAS-acquire, migration gate, audit, tag.

    Ordering (the RT-10a invariants + the RT-11 rollback-compat gate):

    1. **CAS-acquire** flips ``pending`` -> ``promoting`` atomically. If
       no ``pending`` row matches (another promote already won, or the
       train is absent / already promoted), raises
       :class:`PromoteRaceLost` (or :class:`UnknownTrain` when there is
       no row at all). No audit / tag side effect on the loser.

    1b. **Migration rollback-compat gate (RT-11).** When ``migration_gate``
       is supplied the winner runs it before any audit / tag write. A
       rollback-unsafe verdict (the previous-final → candidate delta has
       no clean downgrade) marks the train ``failed`` and raises
       :class:`MigrationCompatBlocked` — *unless* a :class:`BreakGlass`
       override is supplied, in which case a break-glass audit row is
       written first (itself a hard gate: if that row cannot be persisted
       the promote aborts ``failed`` rather than silently overriding).

    2. **Audit hard gate.** The winner writes an audit row. If the
       insert fails (the seam returns ``None`` or raises), the train is
       marked ``failed`` and :class:`AuditWriteError` is raised *before*
       ``tag_writer`` is ever called. This is the AC's "audit-insert
       failure aborts before any tag write".

    3. **Tag write** (``tag_writer``, RT-12) runs only after the audit
       row exists. Its boolean return is persisted as
       ``final_digest_equality``. A missing writer, false equality, or
       ``tag_writer`` failure marks the train ``failed`` (the audit row
       already records the attempt). No break-glass payload can bypass
       this equality check.

    On success transitions ``promoting`` -> ``promoted`` and returns the
    :class:`PromoteResult`.
    """

    normalized = _normalize_sha(candidate_sha)
    if normalized is None:
        raise UnknownTrain(f"not a 40-hex candidate SHA: {candidate_sha!r}")
    actor = _text(actor)
    break_glass_payload = _break_glass_payload(break_glass, normalized)

    async with _acquire(conn_factory) as conn:
        # (1) CAS-acquire. The WHERE re-checks promotion_state at commit
        # time, so of two concurrent promotes only the first to commit
        # this UPDATE flips a 'pending' row; the second matches 0 rows.
        acquired = await conn.fetchrow(
            """
            UPDATE release_train
            SET promotion_state = 'promoting',
                actor = $2,
                row_version = row_version + 1,
                updated_at = NOW()
            WHERE candidate_sha = $1 AND promotion_state = 'pending'
            RETURNING candidate_sha, source_digest_backend,
                      source_digest_frontend, reserved_version,
                      promotion_state, actor, row_version,
                      final_digest_equality, promotion_audit_id
            """,
            normalized,
            actor,
        )
        if acquired is None:
            # Distinguish "absent" from "lost the race": the loser sees a
            # row that exists but is no longer pending.
            exists = await conn.fetchrow(
                "SELECT 1 FROM release_train WHERE candidate_sha = $1",
                normalized,
            )
            if exists is None:
                raise UnknownTrain(f"no release_train row for {normalized}")
            raise PromoteRaceLost(
                f"promote for {normalized} already won by a concurrent caller"
            )

        train = _row_to_train(acquired)

        # (1b) RT-11 migration rollback-compat gate -- before any audit /
        # tag write. A rollback-unsafe delta blocks the promote unless an
        # audited break-glass override is supplied.
        if migration_gate is not None:
            try:
                compat = await migration_gate(train)
            except Exception as exc:  # noqa: BLE001 -- gate failure = fail-closed block
                await _mark_failed(conn, normalized)
                raise MigrationCompatBlocked(
                    f"migration compat gate raised for {normalized}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not compat.rollback_safe:
                if break_glass is None:
                    await _mark_failed(conn, normalized)
                    raise MigrationCompatBlocked(
                        f"promote blocked for {normalized}: rollback-unsafe "
                        f"migration(s) {list(compat.unsafe_migrations)}: "
                        f"{compat.reason}"
                    )
                # Break-glass override -- HARD gate: the override decision
                # must be durably audited before we proceed past the block.
                try:
                    bg_audit_id = await _break_glass_audit_insert(
                        audit_log, train, break_glass, compat
                    )
                except Exception as exc:  # noqa: BLE001
                    await _mark_failed(conn, normalized)
                    raise AuditWriteError(
                        f"break-glass audit insert raised for {normalized}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if bg_audit_id is None:
                    await _mark_failed(conn, normalized)
                    raise AuditWriteError(
                        f"break-glass override for {normalized} requires an "
                        "audit row; insert returned no row id"
                    )

        # (2) Audit HARD gate -- before any tag write.
        try:
            audit_id = await _audit_insert(
                audit_log, train, actor, break_glass=break_glass_payload
            )
        except Exception as exc:  # noqa: BLE001 -- convert to hard-gate failure
            await _mark_failed(conn, normalized)
            raise AuditWriteError(
                f"promote audit insert raised for {normalized}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if audit_id is None:
            await _mark_failed(conn, normalized)
            raise AuditWriteError(
                f"promote audit insert returned no row id for {normalized}; "
                "aborting before any tag write"
            )

        # (3) Tag write (RT-12 seam) -- only now, after the audit row exists.
        try:
            final_equality = (
                bool(await tag_writer(train)) if tag_writer is not None else False
            )
        except Exception as exc:  # noqa: BLE001
            await _mark_failed(conn, normalized)
            raise ReleaseTrainError(
                f"tag write failed for {normalized} after audit row "
                f"{audit_id}: {type(exc).__name__}: {exc}"
            ) from exc
        if not final_equality:
            await _mark_failed(conn, normalized)
            raise DigestEqualityError(
                f"final digest equality was not proven for {normalized}; "
                "aborting promote"
            )

        promoted = await conn.fetchrow(
            """
            UPDATE release_train
            SET promotion_state = 'promoted',
                final_digest_equality = $2,
                promotion_audit_id = $3,
                row_version = row_version + 1,
                updated_at = NOW()
            WHERE candidate_sha = $1 AND promotion_state = 'promoting'
            RETURNING row_version
            """,
            normalized,
            final_equality,
            audit_id,
        )

    return PromoteResult(
        candidate_sha=normalized,
        promotion_audit_id=audit_id,
        final_digest_equality=final_equality,
        row_version=promoted["row_version"],
    )


async def _audit_insert(
    audit_log: AuditLog | None,
    train: ReleaseTrainRow,
    actor: str,
    *,
    break_glass: dict[str, str] | None = None,
) -> int | None:
    """Write the promote audit row; return its id or ``None`` on failure.

    The default adapts :func:`backend.audit.log`, which records a
    hash-chained row and returns its id (or ``None`` if the insert could
    not be persisted). The caller treats ``None`` as a hard-gate
    failure.
    """

    if audit_log is None:
        from backend import audit

        audit_log = audit.log

    after = {
        "promotion_state": "promoting",
        "source_digest_backend": train.source_digest_backend,
        "source_digest_frontend": train.source_digest_frontend,
        "reserved_version": train.reserved_version,
    }
    if break_glass is not None:
        after["break_glass"] = break_glass

    return await audit_log(
        "release_train.promote",
        "release_train",
        train.candidate_sha,
        before={"promotion_state": "pending"},
        after=after,
        actor=actor,
    )


def _break_glass_payload(
    break_glass: BreakGlass | None, candidate_sha: str
) -> dict[str, str] | None:
    if break_glass is None:
        return None
    if not isinstance(break_glass, BreakGlass):
        raise TypeError("break_glass must be a BreakGlass")
    actor = _required_text("break_glass.actor", break_glass.actor)
    reason = _required_text("break_glass.reason", break_glass.reason)
    if break_glass.candidate_sha:
        exact_sha = _full_sha("break_glass.candidate_sha", break_glass.candidate_sha)
        if exact_sha != candidate_sha:
            raise ValueError(
                "break_glass.candidate_sha must match candidate_sha exactly"
            )
    else:
        exact_sha = candidate_sha
    payload = {
        "actor": actor,
        "reason": reason,
        "candidate_sha": exact_sha,
    }
    if break_glass.human_command:
        payload["human_command"] = break_glass.human_command
    return payload


async def _break_glass_audit_insert(
    audit_log: AuditLog | None,
    train: ReleaseTrainRow,
    break_glass: BreakGlass,
    compat: MigrationCompatResult,
) -> int | None:
    """Write the RT-11 break-glass override audit row; return its id.

    Returns ``None`` (or raises) on a failed insert -- the caller treats
    either as a hard-gate failure and aborts the promote. Records the
    overriding actor, their reason, and the rollback-unsafe migrations so
    a post-incident review can reconstruct exactly what was force-promoted.
    """

    if audit_log is None:
        from backend import audit

        audit_log = audit.log

    return await audit_log(
        "release_train.promote.break_glass",
        "release_train",
        train.candidate_sha,
        before={"rollback_safe": False},
        after={
            "break_glass_actor": break_glass.actor,
            "break_glass_reason": break_glass.reason,
            "unsafe_migrations": list(compat.unsafe_migrations),
            "compat_reason": compat.reason,
        },
        actor=break_glass.actor,
    )


def migration_compat_gate(
    previous_final_head: str | None,
    candidate_head: str,
    *,
    versions_dir: Any | None = None,
) -> MigrationGate:
    """Build the production RT-11 gate backed by ``check_migration_compat``.

    Wraps the synchronous release-to-release scan in
    :func:`asyncio.to_thread` so the promote coroutine never blocks the
    event loop on filesystem / AST work. Kept as a factory (and the
    ``promote`` seam injectable) so tests can substitute a fake gate.
    """

    async def _gate(train: ReleaseTrainRow) -> MigrationCompatResult:
        import asyncio

        from scripts.check_migration_compat import (
            VERSIONS_DIR,
            check_release_to_release_compat,
        )

        target_dir = versions_dir if versions_dir is not None else VERSIONS_DIR

        def _run() -> MigrationCompatResult:
            res = check_release_to_release_compat(
                previous_final_head, candidate_head, versions_dir=target_dir
            )
            return MigrationCompatResult(
                rollback_safe=res.rollback_safe,
                reason=res.reason,
                unsafe_migrations=tuple(res.unsafe_migrations),
            )

        return await asyncio.to_thread(_run)

    return _gate


def bundle_tag_writer(
    *,
    bundle_id: str,
    from_env: str = "staging",
    actor: str = "",
    approval_refs: tuple[str, ...] = (),
    registry: str | None = None,
    parent_attestation: str | None = None,
    predicate_out_dir: Any | None = None,
    audit_log_path: Any | None = None,
    staging_evidence_path: Any | None = None,
    require_staging_gate: bool = True,
    verify_cosign: bool = True,
    runner: Any | None = None,
) -> TagWriter:
    """Build the production RT-12 ``tag_writer`` for :func:`promote`.

    The release-train state machine (RT-10a) owns the CAS, the RT-11
    migration gate, and the audit hard-gate; the actual image retag is
    RT-12 and is supplied here. This factory wraps
    ``scripts.promote_image_bundle`` so :func:`promote` can call it as its
    ``tag_writer`` seam: given the winning :class:`ReleaseTrainRow` it
    retags the validated backend+frontend digests (RT-21 pair) to the
    train's ``reserved_version`` (``vX.Y.Z``) in GitLab CR, verifies each
    final tag resolves to the exact validated digest, attests per image,
    and returns whether **every** final digest equalled the validated
    source digest. That boolean becomes ``final_digest_equality`` on the
    train; a ``False`` (or any retag failure) aborts the promote.

    The retag never rebuilds and never creates a ``v*`` git tag (RT-20);
    the synchronous subprocess work runs in :func:`asyncio.to_thread` so
    the promote coroutine never blocks the event loop.
    """

    async def _writer(train: ReleaseTrainRow) -> bool:
        import asyncio
        from pathlib import Path as _Path

        import subprocess

        from scripts.promote_image_bundle import (
            DEFAULT_AUDIT_LOG,
            DEFAULT_REGISTRY,
            build_promotions,
            load_staging_evidence,
            promote_images,
        )

        if train.reserved_version is None:
            raise ReleaseTrainError(
                f"promote of {train.candidate_sha} requires a reserved_version "
                "(RT-10b) before the RT-12 retag"
            )
        target_registry = registry or DEFAULT_REGISTRY

        def _run() -> bool:
            promotions = build_promotions(
                version=train.reserved_version,
                digests={
                    "backend": train.source_digest_backend,
                    "frontend": train.source_digest_frontend,
                },
                registry=target_registry,
            )
            staging_evidence = None
            if require_staging_gate and staging_evidence_path is not None:
                staging_evidence = load_staging_evidence(
                    _Path(staging_evidence_path), bundle_id=bundle_id
                )

            outcome = promote_images(
                promotions,
                version=train.reserved_version,
                bundle_id=bundle_id,
                from_env=from_env,
                actor=actor or train.actor,
                approval_refs=list(approval_refs),
                audit_log=_Path(audit_log_path) if audit_log_path else DEFAULT_AUDIT_LOG,
                parent_attestation=parent_attestation,
                predicate_out_dir=_Path(predicate_out_dir) if predicate_out_dir else None,
                staging_evidence=staging_evidence,
                require_staging_gate=require_staging_gate,
                dry_run=False,
                verify_cosign=verify_cosign,
                runner=runner if runner is not None else subprocess.run,
            )
            return outcome.final_digest_equality

        return await asyncio.to_thread(_run)

    return _writer


async def _mark_failed(conn: Any, candidate_sha: str) -> None:
    """Move an in-flight ``promoting`` row to ``failed`` (best-effort)."""

    await conn.execute(
        """
        UPDATE release_train
        SET promotion_state = 'failed',
            row_version = row_version + 1,
            updated_at = NOW()
        WHERE candidate_sha = $1 AND promotion_state = 'promoting'
        """,
        candidate_sha,
    )


@asynccontextmanager
async def _acquire(factory: ConnFactory | None) -> AsyncIterator[Any]:
    if factory is None:
        from backend.db_pool import get_pool

        async with get_pool().acquire() as conn:
            yield conn
        return
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_train(row: Any) -> ReleaseTrainRow:
    equality = row["final_digest_equality"]
    return ReleaseTrainRow(
        candidate_sha=row["candidate_sha"],
        source_digest_backend=row["source_digest_backend"],
        source_digest_frontend=row["source_digest_frontend"],
        reserved_version=row["reserved_version"],
        promotion_state=row["promotion_state"],
        actor=row["actor"],
        row_version=row["row_version"],
        final_digest_equality=None if equality is None else bool(equality),
        promotion_audit_id=row["promotion_audit_id"],
    )


def _normalize_sha(value: Any) -> str | None:
    """Lowercase + validate a full SHA; return ``None`` if malformed.

    Used by the read / promote-entry paths, which must not raise on bad
    input (a malformed SHA can never have been stored).
    """

    if not isinstance(value, str):
        return None
    clean = value.strip().lower()
    return clean if _FULL_SHA_RE.fullmatch(clean) else None


def _full_sha(field: str, value: Any) -> str:
    """Strict full-SHA validator for the write path."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip().lower()
    if not _FULL_SHA_RE.fullmatch(clean):
        raise ValueError(
            f"{field} must be a full 40-char lowercase hex commit SHA"
        )
    return clean


def _digest(field: str, value: Any) -> str:
    """Validate a non-empty image digest string for the write path."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} must be a non-empty image digest")
    return clean


def _state(value: Any) -> PromotionState:
    if not isinstance(value, str):
        raise TypeError("promotion_state must be a string")
    clean = value.strip().lower()
    if clean not in PROMOTION_STATES:
        raise ValueError(
            "promotion_state must be one of "
            f"{sorted(PROMOTION_STATES)}"
        )
    return clean  # type: ignore[return-value]


def _optional(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    clean = value.strip()
    return clean or None


def _release_version(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("reserved_version must be a string")
    clean = value.strip()
    if not _RELEASE_VERSION_RE.fullmatch(clean):
        raise ValueError("reserved_version must match vMAJOR.MINOR.PATCH")
    return clean


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("actor must be a string")
    return value.strip()


def _required_text(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} must be non-empty")
    return clean


__all__ = [
    "AuditWriteError",
    "BreakGlass",
    "DigestEqualityError",
    "EVENT_RELEASE_VERSION_RESERVED",
    "MigrationCompatBlocked",
    "MigrationCompatResult",
    "PROMOTION_STATES",
    "PostgresReleaseTrainStore",
    "PromoteRaceLost",
    "PromoteResult",
    "PromotionState",
    "ReservationEventSink",
    "ReleaseTrainError",
    "ReleaseTrainRow",
    "UnknownTrain",
    "VersionReservationConflict",
    "VersionReservationResult",
    "VersionReservationStateError",
    "bundle_tag_writer",
    "create_train",
    "get_train",
    "migration_compat_gate",
    "promote",
    "release_version_reserved_event",
    "reserve_version",
]
