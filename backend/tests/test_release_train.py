"""OP-1585 [RT-10a] -- release_train model + CAS + audit hard-gate tests.

Exercises the two RT-10a acceptance criteria against an asyncpg-shaped
fake connection:

* **CAS single-winner** -- two concurrent :func:`promote` calls on the
  same candidate resolve to exactly one :class:`PromoteResult` and one
  :class:`PromoteRaceLost`.
* **Audit hard gate before tag write** -- a failing audit insert raises
  :class:`AuditWriteError`, the ``tag_writer`` is never invoked, and the
  train lands in ``failed``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.agents.release_train import (
    PROMOTION_STATES,
    AuditWriteError,
    PostgresReleaseTrainStore,
    PromoteRaceLost,
    PromoteResult,
    ReleaseTrainError,
    UnknownTrain,
    create_train,
    get_train,
    promote,
)


SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"
SHA_ABSENT = "f" * 40
DIGEST_BE = "sha256:" + "1" * 64
DIGEST_FE = "sha256:" + "2" * 64


class _FakeRow(dict[str, Any]):
    pass


class _FakeConn:
    """Minimal asyncpg-shaped fake keyed on ``release_train.candidate_sha``.

    Each ``fetchrow`` / ``execute`` runs to completion with no internal
    ``await``, so a CAS UPDATE is atomic with respect to other coroutines
    cooperatively scheduled on the same loop -- exactly the row-lock
    semantics the real Postgres CAS relies on.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}

    def _new_row(self, args: tuple[Any, ...]) -> _FakeRow:
        candidate_sha, be, fe, reserved, actor = args
        return _FakeRow(
            candidate_sha=candidate_sha,
            source_digest_backend=be,
            source_digest_frontend=fe,
            reserved_version=reserved,
            promotion_state="pending",
            actor=actor,
            row_version=0,
            final_digest_equality=None,
            promotion_audit_id=None,
        )

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        if "INSERT INTO release_train" in sql:
            row = self._new_row(args)
            self.rows[row["candidate_sha"]] = row
            return _FakeRow(row)
        if "SELECT 1 FROM release_train" in sql:
            return _FakeRow(one=1) if args[0] in self.rows else None
        if sql.strip().startswith("SELECT"):
            row = self.rows.get(args[0])
            return _FakeRow(row) if row is not None else None
        if "SET promotion_state = 'promoting'" in sql:
            sha, actor = args
            row = self.rows.get(sha)
            if row is None or row["promotion_state"] != "pending":
                return None
            row["promotion_state"] = "promoting"
            row["actor"] = actor
            row["row_version"] += 1
            return _FakeRow(row)
        if "SET promotion_state = 'promoted'" in sql:
            sha, equality, audit_id = args
            row = self.rows.get(sha)
            if row is None or row["promotion_state"] != "promoting":
                return None
            row["promotion_state"] = "promoted"
            row["final_digest_equality"] = equality
            row["promotion_audit_id"] = audit_id
            row["row_version"] += 1
            return _FakeRow(row)
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> None:
        if "SET promotion_state = 'failed'" in sql:
            sha = args[0]
            row = self.rows.get(sha)
            if row is not None and row["promotion_state"] == "promoting":
                row["promotion_state"] = "failed"
                row["row_version"] += 1
            return None
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _factory(conn: _FakeConn):
    @asynccontextmanager
    async def _cm() -> AsyncIterator[_FakeConn]:
        yield conn

    return _cm


async def _seed(conn: _FakeConn, sha: str = SHA_A) -> None:
    await create_train(
        sha, DIGEST_BE, DIGEST_FE, actor="creator", conn_factory=_factory(conn)
    )


# -- create / get round-trip ------------------------------------------------


async def test_create_train_starts_pending_at_row_version_zero() -> None:
    conn = _FakeConn()
    row = await create_train(
        SHA_A,
        DIGEST_BE,
        DIGEST_FE,
        actor="creator",
        reserved_version="v1.2.3",
        conn_factory=_factory(conn),
    )
    assert row.promotion_state == "pending"
    assert row.row_version == 0
    assert row.reserved_version == "v1.2.3"
    assert row.final_digest_equality is None
    assert row.promotion_audit_id is None

    fetched = await get_train(SHA_A, conn_factory=_factory(conn))
    assert fetched is not None
    assert fetched.source_digest_backend == DIGEST_BE
    assert fetched.source_digest_frontend == DIGEST_FE


async def test_get_train_absent_returns_none() -> None:
    conn = _FakeConn()
    assert await get_train(SHA_ABSENT, conn_factory=_factory(conn)) is None


async def test_get_train_malformed_sha_returns_none_without_db() -> None:
    conn = _FakeConn()
    assert await get_train("not-a-sha", conn_factory=_factory(conn)) is None


# -- AC #1: CAS single-winner -----------------------------------------------


async def test_promote_succeeds_and_records_audit_and_equality() -> None:
    conn = _FakeConn()
    await _seed(conn)

    tag_calls: list[str] = []

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    async def audit_ok(*a: Any, **k: Any) -> int:
        return 4242

    result = await promote(
        SHA_A,
        "release-manager",
        tag_writer=tag_writer,
        audit_log=audit_ok,
        conn_factory=_factory(conn),
    )
    assert isinstance(result, PromoteResult)
    assert result.promotion_audit_id == 4242
    assert result.final_digest_equality is True
    assert tag_calls == [SHA_A]

    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None
    assert row.promotion_state == "promoted"
    assert row.promotion_audit_id == 4242
    assert row.final_digest_equality is True
    # pending(0) -> promoting(1) -> promoted(2)
    assert row.row_version == 2


async def test_two_concurrent_promotes_exactly_one_wins() -> None:
    conn = _FakeConn()
    await _seed(conn)

    audit_ids = iter(range(1000, 1100))

    async def audit_ok(*a: Any, **k: Any) -> int:
        # Yield control so both promote coroutines interleave around the
        # audit step; the CAS-acquire already decided the single winner.
        await asyncio.sleep(0)
        return next(audit_ids)

    async def tag_writer(train) -> bool:
        await asyncio.sleep(0)
        return True

    async def _attempt():
        return await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_ok,
            conn_factory=_factory(conn),
        )

    results = await asyncio.gather(
        _attempt(), _attempt(), return_exceptions=True
    )

    wins = [r for r in results if isinstance(r, PromoteResult)]
    losses = [r for r in results if isinstance(r, PromoteRaceLost)]
    assert len(wins) == 1, results
    assert len(losses) == 1, results

    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "promoted"


async def test_promote_unknown_train_raises_unknown_train() -> None:
    conn = _FakeConn()
    with pytest.raises(UnknownTrain):
        await promote(
            SHA_ABSENT, "rm", audit_log=_unused_audit, conn_factory=_factory(conn)
        )


async def test_promote_malformed_sha_raises_unknown_train() -> None:
    conn = _FakeConn()
    with pytest.raises(UnknownTrain):
        await promote("nope", "rm", conn_factory=_factory(conn))


async def test_second_sequential_promote_loses_after_first_promoted() -> None:
    conn = _FakeConn()
    await _seed(conn)

    async def audit_ok(*a: Any, **k: Any) -> int:
        return 7

    await promote(SHA_A, "rm", audit_log=audit_ok, conn_factory=_factory(conn))
    # The train is now 'promoted'; a re-promote finds no 'pending' row.
    with pytest.raises(PromoteRaceLost):
        await promote(SHA_A, "rm", audit_log=audit_ok, conn_factory=_factory(conn))


# -- AC #2: audit insert is a HARD gate, ordered before any tag write -------


async def test_audit_insert_returning_none_aborts_before_tag_write() -> None:
    conn = _FakeConn()
    await _seed(conn)

    tag_calls: list[str] = []

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    async def audit_fails(*a: Any, **k: Any) -> None:
        return None  # the receipt printer is out of paper -- but here it's fatal

    with pytest.raises(AuditWriteError):
        await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_fails,
            conn_factory=_factory(conn),
        )

    # The tag write must NOT have happened.
    assert tag_calls == []
    # The train is durably 'failed', not 'promoted'.
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None
    assert row.promotion_state == "failed"
    assert row.promotion_audit_id is None
    assert row.final_digest_equality is None


async def test_audit_insert_raising_aborts_before_tag_write() -> None:
    conn = _FakeConn()
    await _seed(conn)

    tag_calls: list[str] = []

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    async def audit_raises(*a: Any, **k: Any) -> int:
        raise RuntimeError("DSNUnreachable")

    with pytest.raises(AuditWriteError):
        await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_raises,
            conn_factory=_factory(conn),
        )
    assert tag_calls == []
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"


async def test_tag_writer_failure_after_audit_marks_failed() -> None:
    conn = _FakeConn()
    await _seed(conn)

    async def audit_ok(*a: Any, **k: Any) -> int:
        return 99

    async def tag_writer(train) -> bool:
        raise RuntimeError("registry retag refused")

    with pytest.raises(ReleaseTrainError):
        await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_ok,
            conn_factory=_factory(conn),
        )
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"


async def test_promote_without_tag_writer_records_equality_false() -> None:
    conn = _FakeConn()
    await _seed(conn)

    async def audit_ok(*a: Any, **k: Any) -> int:
        return 1

    result = await promote(
        SHA_A, "rm", audit_log=audit_ok, conn_factory=_factory(conn)
    )
    # No tag_writer (RT-12 not wired): nothing proved digest equality.
    assert result.final_digest_equality is False


# -- store wrapper + validation ---------------------------------------------


async def test_store_wrapper_delegates() -> None:
    conn = _FakeConn()
    store = PostgresReleaseTrainStore(_factory(conn))
    await store.create_train(SHA_B, DIGEST_BE, DIGEST_FE, actor="c")

    async def audit_ok(*a: Any, **k: Any) -> int:
        return 5

    result = await store.promote(SHA_B, "rm", audit_log=audit_ok)
    assert result.candidate_sha == SHA_B
    row = await store.get_train(SHA_B)
    assert row is not None and row.promotion_state == "promoted"


async def test_create_train_rejects_short_sha() -> None:
    conn = _FakeConn()
    with pytest.raises(ValueError, match="full 40-char"):
        await create_train("abc", DIGEST_BE, DIGEST_FE, conn_factory=_factory(conn))


async def test_create_train_rejects_empty_digest() -> None:
    conn = _FakeConn()
    with pytest.raises(ValueError, match="non-empty image digest"):
        await create_train(SHA_A, "", DIGEST_FE, conn_factory=_factory(conn))


# -- enum drift guard against the migration ---------------------------------


def test_promotion_state_enum_matches_migration() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0247_release_train.py"
    ).read_text()
    assert PROMOTION_STATES == {"pending", "promoting", "promoted", "failed"}
    assert "'pending','promoting','promoted','failed'" in migration


async def _unused_audit(*a: Any, **k: Any) -> int:
    raise AssertionError("audit should not be reached for an unknown train")
