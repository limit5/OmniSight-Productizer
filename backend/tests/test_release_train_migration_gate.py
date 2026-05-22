"""OP-1588 [RT-11] -- promote-time migration rollback-compat gate hook.

Exercises the RT-11 acceptance criteria against the same asyncpg-shaped
fake connection RT-10a uses:

* a rollback-unsafe migration verdict BLOCKS promote
  (:class:`MigrationCompatBlocked`), with no audit / tag side effect and
  the train left ``failed``;
* a :class:`BreakGlass` override lets the unsafe promote through, but
  ONLY after a break-glass audit row is written -- and if that audit row
  cannot be persisted the promote aborts before any tag write;
* a rollback-safe verdict (or no gate) promotes normally;
* the production gate builder maps a script verdict to the seam shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from backend.agents.release_train import (
    AuditWriteError,
    BreakGlass,
    MigrationCompatBlocked,
    MigrationCompatResult,
    PromoteResult,
    create_train,
    get_train,
    migration_compat_gate,
    promote,
)

SHA_A = "a" * 40
DIGEST_BE = "sha256:" + "1" * 64
DIGEST_FE = "sha256:" + "2" * 64


class _FakeRow(dict[str, Any]):
    pass


class _FakeConn:
    """Minimal asyncpg-shaped fake keyed on ``candidate_sha`` (RT-10a shape)."""

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


async def _seed(conn: _FakeConn) -> None:
    await create_train(
        SHA_A, DIGEST_BE, DIGEST_FE, actor="creator", conn_factory=_factory(conn)
    )


def _gate(result: MigrationCompatResult):
    async def _g(train) -> MigrationCompatResult:
        return result

    return _g


# ── AC: a rollback-unsafe migration BLOCKS promote ────────────────────


async def test_unsafe_migration_blocks_promote_no_side_effects() -> None:
    conn = _FakeConn()
    await _seed(conn)

    audit_calls: list[tuple] = []
    tag_calls: list[str] = []

    async def audit_log(*a: Any, **k: Any) -> int:
        audit_calls.append((a, k))
        return 1

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    unsafe = MigrationCompatResult(
        rollback_safe=False,
        reason="0099_drop.py: rollback-unsafe: backwards-compat: breaking",
        unsafe_migrations=("backend/alembic/versions/0099_drop.py",),
    )

    with pytest.raises(MigrationCompatBlocked, match="rollback-unsafe"):
        await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_log,
            migration_gate=_gate(unsafe),
            conn_factory=_factory(conn),
        )

    # No audit row, no tag write; the train is durably failed.
    assert audit_calls == []
    assert tag_calls == []
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"
    assert row.promotion_audit_id is None


async def test_gate_raising_blocks_promote() -> None:
    conn = _FakeConn()
    await _seed(conn)

    async def gate_raises(train) -> MigrationCompatResult:
        raise RuntimeError("versions tree unreadable")

    with pytest.raises(MigrationCompatBlocked):
        await promote(
            SHA_A, "rm", migration_gate=gate_raises, conn_factory=_factory(conn)
        )
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"


# ── AC: break-glass requires an audit row ─────────────────────────────


async def test_break_glass_promotes_unsafe_and_writes_break_glass_audit() -> None:
    conn = _FakeConn()
    await _seed(conn)

    events: list[str] = []

    async def audit_log(action: str, *a: Any, **k: Any) -> int:
        events.append(action)
        return 555 if action.endswith("break_glass") else 4242

    async def tag_writer(train) -> bool:
        events.append("tag")
        return True

    unsafe = MigrationCompatResult(
        rollback_safe=False,
        reason="0099: rollback-unsafe",
        unsafe_migrations=("0099_drop.py",),
    )

    result = await promote(
        SHA_A,
        "rm",
        tag_writer=tag_writer,
        audit_log=audit_log,
        migration_gate=_gate(unsafe),
        break_glass=BreakGlass(actor="release-manager", reason="P1 incident hotfix"),
        conn_factory=_factory(conn),
    )

    assert isinstance(result, PromoteResult)
    # The break-glass audit row is written BEFORE the promote audit + tag.
    assert events[0] == "release_train.promote.break_glass"
    assert "release_train.promote" in events
    assert "tag" in events
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "promoted"


async def test_break_glass_audit_failure_aborts_before_tag_write() -> None:
    conn = _FakeConn()
    await _seed(conn)

    tag_calls: list[str] = []

    async def audit_log(action: str, *a: Any, **k: Any) -> int | None:
        if action.endswith("break_glass"):
            return None  # break-glass receipt cannot be written -> fatal
        return 1

    async def tag_writer(train) -> bool:
        tag_calls.append(train.candidate_sha)
        return True

    unsafe = MigrationCompatResult(
        rollback_safe=False, reason="unsafe", unsafe_migrations=("0099.py",)
    )

    with pytest.raises(AuditWriteError, match="break-glass"):
        await promote(
            SHA_A,
            "rm",
            tag_writer=tag_writer,
            audit_log=audit_log,
            migration_gate=_gate(unsafe),
            break_glass=BreakGlass(actor="rm", reason="incident"),
            conn_factory=_factory(conn),
        )

    assert tag_calls == []
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"


def test_break_glass_rejects_empty_reason() -> None:
    with pytest.raises(ValueError, match="non-empty reason"):
        BreakGlass(actor="rm", reason="   ")
    with pytest.raises(ValueError, match="non-empty actor"):
        BreakGlass(actor="", reason="incident")


# ── safe verdict / no gate: promote proceeds normally ─────────────────


async def test_safe_verdict_promotes_normally() -> None:
    conn = _FakeConn()
    await _seed(conn)

    async def audit_log(*a: Any, **k: Any) -> int:
        return 7

    async def tag_writer(train) -> bool:
        return True

    safe = MigrationCompatResult(rollback_safe=True, reason="all safe")
    result = await promote(
        SHA_A,
        "rm",
        tag_writer=tag_writer,
        audit_log=audit_log,
        migration_gate=_gate(safe),
        conn_factory=_factory(conn),
    )
    assert result.final_digest_equality is True
    row = await get_train(SHA_A, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "promoted"


async def test_no_gate_promotes_normally_backward_compatible() -> None:
    # RT-10a callers that pass no migration_gate are unaffected.
    conn = _FakeConn()
    await _seed(conn)

    async def audit_log(*a: Any, **k: Any) -> int:
        return 9

    async def tag_writer(train) -> bool:
        return True

    result = await promote(
        SHA_A,
        "rm",
        audit_log=audit_log,
        tag_writer=tag_writer,
        conn_factory=_factory(conn),
    )
    assert isinstance(result, PromoteResult)


# ── production gate builder maps the script verdict to the seam ───────


async def test_migration_compat_gate_builder_maps_unsafe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import check_migration_compat as cmc

    def _fake(prev, cand, *, versions_dir):
        assert prev == "0001" and cand == "0002"
        return cmc.ReleaseCompatResult(
            rollback_safe=False,
            reason="0002.py: rollback-unsafe: backwards-compat: breaking",
            unsafe_migrations=("0002.py",),
            checked_migrations=("0002.py",),
        )

    monkeypatch.setattr(cmc, "check_release_to_release_compat", _fake)

    gate = migration_compat_gate("0001", "0002", versions_dir=tmp_path)
    verdict = await gate(object())
    assert isinstance(verdict, MigrationCompatResult)
    assert verdict.rollback_safe is False
    assert verdict.unsafe_migrations == ("0002.py",)
