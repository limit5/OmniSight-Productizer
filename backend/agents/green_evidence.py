"""OP-1569 [RT-04b] -- green-evidence store + query by full commit SHA.

Persists the per-full-SHA result of the release-train fast develop
submit-gate (RT-04a, CI, out of scope here) and answers the single
question the candidate-build trigger (RT-09) asks: *is this exact
commit certified green?*

Contract
--------
* :func:`record_evidence` upserts one row keyed by the 40-char lowercase
  hex commit SHA. Re-recording the same SHA overwrites the status and
  bumps ``updated_at`` (a gate re-run may flip ``pass`` <-> ``fail``).
* :func:`green_status` is the **fail-closed** gate predicate. It returns
  ``True`` only when a row exists for the exact full SHA *and* its status
  is ``"pass"``. Every other case -- absent SHA, ``fail`` status, or a
  malformed SHA that could never have been stored -- returns ``False``.
  This is the AC's "absent SHA = fail-closed": the gate never lets an
  uncertified or unknown commit through.
* :func:`get_evidence` returns the full row (or ``None``) for operator /
  status-tool reads that need more than the boolean.

Storage follows the established PG-only store pattern
(``backend.agents.achievement_unlock_store``): asyncpg ``$1`` parameter
style, an injectable ``conn_factory`` for tests, and the process-global
pool from ``backend.db_pool`` in production. The table is created by
alembic migration ``0246_green_evidence`` (PG schema is alembic-owned;
the sqlite branch exists for the migration's own portability tests).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


GreenStatus = Literal["pass", "fail"]
ConnFactory = Callable[[], Any]

# A full git commit SHA is exactly 40 lowercase hex characters. Querying
# is by *full* SHA only (RT-04b AC) -- an abbreviated prefix is rejected
# here and so can never be confused with a stored full SHA.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Closed status enum -- mirrors the CHECK constraint literal in
# alembic migration 0246 (asserted equal in the migration's tests).
_VALID_STATUSES: frozenset[str] = frozenset({"pass", "fail"})


@dataclass(frozen=True)
class GreenEvidenceRow:
    """One durable green-evidence record for a full commit SHA."""

    full_sha: str
    status: GreenStatus
    recorded_at: datetime
    pipeline_id: str | None = None
    evidence_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "full_sha", _full_sha("full_sha", self.full_sha))
        object.__setattr__(self, "status", _status(self.status))
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at))
        object.__setattr__(
            self, "pipeline_id", _optional("pipeline_id", self.pipeline_id)
        )
        object.__setattr__(
            self, "evidence_url", _optional("evidence_url", self.evidence_url)
        )


class PostgresGreenEvidenceStore:
    """``green_evidence`` table backed store."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def green_status(self, full_sha: str) -> bool:
        return await green_status(full_sha, conn_factory=self._factory)

    async def get_evidence(self, full_sha: str) -> GreenEvidenceRow | None:
        return await get_evidence(full_sha, conn_factory=self._factory)

    async def record_evidence(
        self,
        full_sha: str,
        status: GreenStatus,
        recorded_at: datetime,
        pipeline_id: str | None = None,
        evidence_url: str | None = None,
    ) -> GreenEvidenceRow:
        return await record_evidence(
            full_sha,
            status,
            recorded_at,
            pipeline_id=pipeline_id,
            evidence_url=evidence_url,
            conn_factory=self._factory,
        )


async def green_status(
    full_sha: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> bool:
    """Fail-closed gate predicate: ``True`` iff ``full_sha`` is certified green.

    A malformed SHA (anything that is not 40 lowercase hex chars) can
    never have been stored, so it is treated as not-green rather than
    raising -- the caller is a gate and an exception would be a less
    safe failure mode than a clean reject. Absent SHA and ``fail`` status
    likewise return ``False``.
    """

    normalized = _normalize_sha(full_sha)
    if normalized is None:
        return False
    async with _acquire(conn_factory) as conn:
        row = await conn.fetchrow(
            """
            SELECT status
            FROM green_evidence
            WHERE full_sha = $1
            """,
            normalized,
        )
    return row is not None and row["status"] == "pass"


async def get_evidence(
    full_sha: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> GreenEvidenceRow | None:
    """Return the full evidence row for ``full_sha``, or ``None`` if absent.

    Like :func:`green_status`, a malformed SHA returns ``None`` rather
    than raising -- there is nothing to find.
    """

    normalized = _normalize_sha(full_sha)
    if normalized is None:
        return None
    async with _acquire(conn_factory) as conn:
        row = await conn.fetchrow(
            """
            SELECT full_sha, status, pipeline_id, evidence_url, recorded_at
            FROM green_evidence
            WHERE full_sha = $1
            """,
            normalized,
        )
    return _row_to_evidence(row) if row is not None else None


async def record_evidence(
    full_sha: str,
    status: GreenStatus,
    recorded_at: datetime,
    pipeline_id: str | None = None,
    evidence_url: str | None = None,
    *,
    conn_factory: ConnFactory | None = None,
) -> GreenEvidenceRow:
    """Upsert one green-evidence row, keyed by full SHA.

    Unlike the read helpers, the write path is strict: a malformed SHA
    or unknown status raises (via :class:`GreenEvidenceRow` validation)
    so garbage never lands in the store.
    """

    row = GreenEvidenceRow(
        full_sha=full_sha,
        status=status,
        recorded_at=recorded_at,
        pipeline_id=pipeline_id,
        evidence_url=evidence_url,
    )
    async with _acquire(conn_factory) as conn:
        saved = await conn.fetchrow(
            """
            INSERT INTO green_evidence (
                full_sha, status, pipeline_id, evidence_url, recorded_at
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (full_sha) DO UPDATE
                SET status = excluded.status,
                    pipeline_id = excluded.pipeline_id,
                    evidence_url = excluded.evidence_url,
                    updated_at = NOW()
            RETURNING full_sha, status, pipeline_id, evidence_url, recorded_at
            """,
            row.full_sha,
            row.status,
            row.pipeline_id,
            row.evidence_url,
            row.recorded_at,
        )
    return _row_to_evidence(saved)


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


def _row_to_evidence(row: Any) -> GreenEvidenceRow:
    return GreenEvidenceRow(
        full_sha=row["full_sha"],
        status=row["status"],
        recorded_at=row["recorded_at"],
        pipeline_id=row["pipeline_id"],
        evidence_url=row["evidence_url"],
    )


def _normalize_sha(value: str) -> str | None:
    """Lowercase + validate a full SHA; return ``None`` if malformed.

    Used by the read paths, which are fail-closed and must not raise on
    bad input.
    """

    if not isinstance(value, str):
        return None
    clean = value.strip().lower()
    return clean if _FULL_SHA_RE.fullmatch(clean) else None


def _full_sha(field: str, value: str) -> str:
    """Strict full-SHA validator for the write path."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip().lower()
    if not _FULL_SHA_RE.fullmatch(clean):
        raise ValueError(
            f"{field} must be a full 40-char lowercase hex commit SHA"
        )
    return clean


def _status(value: str) -> GreenStatus:
    if not isinstance(value, str):
        raise TypeError("status must be a string")
    clean = value.strip().lower()
    if clean not in _VALID_STATUSES:
        raise ValueError("status must be 'pass' or 'fail'")
    return clean  # type: ignore[return-value]


def _optional(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    clean = value.strip()
    return clean or None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("recorded_at must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "GreenEvidenceRow",
    "GreenStatus",
    "PostgresGreenEvidenceStore",
    "get_evidence",
    "green_status",
    "record_evidence",
]
