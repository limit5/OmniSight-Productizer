"""OP-1585 [RT-10a] -- release_train model + CAS / audit-hard-gate promote.

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

Out of scope (RT-10a): the version *reservation* flow is RT-10b; the
image retag itself is RT-12; the standalone race test is RT-10c. This
module ships the table, the model, the CAS, and the audit hard gate.
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

# A full git commit SHA is exactly 40 lowercase hex characters; querying
# / keying is by full SHA only (mirrors green_evidence). The 40-char
# ``length`` CHECK in migration 0247 is the DB-side backstop.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

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
        tag_writer: TagWriter | None = None,
        audit_log: AuditLog | None = None,
    ) -> PromoteResult:
        return await promote(
            candidate_sha,
            actor,
            tag_writer=tag_writer,
            audit_log=audit_log,
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


async def promote(
    candidate_sha: str,
    actor: str,
    *,
    tag_writer: TagWriter | None = None,
    audit_log: AuditLog | None = None,
    conn_factory: ConnFactory | None = None,
) -> PromoteResult:
    """Promote ``candidate_sha``: CAS-acquire, audit hard-gate, then tag.

    Ordering (the two RT-10a invariants):

    1. **CAS-acquire** flips ``pending`` -> ``promoting`` atomically. If
       no ``pending`` row matches (another promote already won, or the
       train is absent / already promoted), raises
       :class:`PromoteRaceLost` (or :class:`UnknownTrain` when there is
       no row at all). No audit / tag side effect on the loser.

    2. **Audit hard gate.** The winner writes an audit row. If the
       insert fails (the seam returns ``None`` or raises), the train is
       marked ``failed`` and :class:`AuditWriteError` is raised *before*
       ``tag_writer`` is ever called. This is the AC's "audit-insert
       failure aborts before any tag write".

    3. **Tag write** (``tag_writer``, RT-12) runs only after the audit
       row exists. Its boolean return is persisted as
       ``final_digest_equality``. A ``tag_writer`` failure also marks
       the train ``failed`` (the audit row already records the attempt).
       When ``tag_writer`` is ``None`` (RT-12 not yet wired) the promote
       completes with ``final_digest_equality`` left unverified
       (recorded ``False``: nothing proved equality).

    On success transitions ``promoting`` -> ``promoted`` and returns the
    :class:`PromoteResult`.
    """

    normalized = _normalize_sha(candidate_sha)
    if normalized is None:
        raise UnknownTrain(f"not a 40-hex candidate SHA: {candidate_sha!r}")
    actor = _text(actor)

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

        # (2) Audit HARD gate -- before any tag write.
        try:
            audit_id = await _audit_insert(audit_log, train, actor)
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
    audit_log: AuditLog | None, train: ReleaseTrainRow, actor: str
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

    return await audit_log(
        "release_train.promote",
        "release_train",
        train.candidate_sha,
        before={"promotion_state": "pending"},
        after={
            "promotion_state": "promoting",
            "source_digest_backend": train.source_digest_backend,
            "source_digest_frontend": train.source_digest_frontend,
            "reserved_version": train.reserved_version,
        },
        actor=actor,
    )


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


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("actor must be a string")
    return value.strip()


__all__ = [
    "AuditWriteError",
    "PROMOTION_STATES",
    "PostgresReleaseTrainStore",
    "PromoteRaceLost",
    "PromoteResult",
    "PromotionState",
    "ReleaseTrainError",
    "ReleaseTrainRow",
    "UnknownTrain",
    "create_train",
    "get_train",
    "promote",
]
