"""OP-1590 [RT-12] -- promote wires the real image-retag tag_writer.

Exercises the RT-12 acceptance criteria at the seam where the
release-train state machine (RT-10a) hands off to the actual GitLab CR
retag:

* a successful promote retags the validated backend+frontend pair to the
  train's ``reserved_version`` (``vX.Y.Z``) and records
  ``final_digest_equality`` True;
* the retag NEVER invokes ``git`` and NEVER rebuilds (RT-20);
* a rollback-unsafe migration gate (RT-11) BLOCKS the promote *before*
  the tag_writer runs -- so no retag happens until both the migration
  gate and (upstream) the staging gate have passed.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from backend.agents.release_train import (
    MigrationCompatBlocked,
    MigrationCompatResult,
    bundle_tag_writer,
    create_train,
    get_train,
    promote,
)

SHA = "a" * 40
DIGEST_BE = "sha256:" + "1" * 64
DIGEST_FE = "sha256:" + "2" * 64
VERSION = "v2.0.0"
REGISTRY = "registry.test/omnisight"


class _FakeRow(dict):
    pass


class _FakeConn:
    """asyncpg-shaped fake keyed on candidate_sha (RT-10a shape)."""

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
            row = self.rows.get(args[0])
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


class FakeRegistry:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:4] == ["docker", "buildx", "imagetools", "create"]:
            self.tags[cmd[cmd.index("-t") + 1]] = cmd[-1].split("@", 1)[1]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            ref = cmd[4]
            if ref in self.tags:
                return subprocess.CompletedProcess(cmd, 0, self.tags[ref] + "\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
        return subprocess.CompletedProcess(cmd, 0, "", "")


async def _seed(conn: _FakeConn) -> None:
    await create_train(
        SHA,
        DIGEST_BE,
        DIGEST_FE,
        actor="creator",
        reserved_version=VERSION,
        conn_factory=_factory(conn),
    )


def _safe_gate():
    async def _g(train) -> MigrationCompatResult:
        return MigrationCompatResult(rollback_safe=True, reason="ok")

    return _g


def _unsafe_gate():
    async def _g(train) -> MigrationCompatResult:
        return MigrationCompatResult(
            rollback_safe=False, reason="0099: unsafe", unsafe_migrations=("0099.py",)
        )

    return _g


async def test_promote_retags_pair_via_bundle_tag_writer(tmp_path) -> None:
    conn = _FakeConn()
    await _seed(conn)
    reg = FakeRegistry()

    async def audit_log(*a: Any, **k: Any) -> int:
        return 4242

    writer = bundle_tag_writer(
        bundle_id="rt12-bundle",
        registry=REGISTRY,
        actor="release-manager",
        approval_refs=("OP-1590",),
        audit_log_path=tmp_path / "audit.jsonl",
        require_staging_gate=False,
        verify_cosign=False,
        runner=reg,
    )

    result = await promote(
        SHA,
        "release-manager",
        tag_writer=writer,
        audit_log=audit_log,
        migration_gate=_safe_gate(),
        conn_factory=_factory(conn),
    )

    assert result.final_digest_equality is True
    assert reg.tags[f"{REGISTRY}/backend:{VERSION}"] == DIGEST_BE
    assert reg.tags[f"{REGISTRY}/frontend:{VERSION}"] == DIGEST_FE
    # RT-20: never a git tag, never a rebuild.
    for cmd in reg.calls:
        assert cmd[0] != "git"
        assert cmd[:3] != ["docker", "build", "-t"]
    row = await get_train(SHA, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "promoted"


async def test_unsafe_migration_blocks_before_any_retag(tmp_path) -> None:
    conn = _FakeConn()
    await _seed(conn)
    reg = FakeRegistry()

    async def audit_log(*a: Any, **k: Any) -> int:
        return 1

    writer = bundle_tag_writer(
        bundle_id="rt12-bundle",
        registry=REGISTRY,
        audit_log_path=tmp_path / "audit.jsonl",
        require_staging_gate=False,
        verify_cosign=False,
        runner=reg,
    )

    with pytest.raises(MigrationCompatBlocked):
        await promote(
            SHA,
            "rm",
            tag_writer=writer,
            audit_log=audit_log,
            migration_gate=_unsafe_gate(),
            conn_factory=_factory(conn),
        )

    # The tag_writer never ran: no retag happened until the gate passed.
    assert reg.calls == []
    row = await get_train(SHA, conn_factory=_factory(conn))
    assert row is not None and row.promotion_state == "failed"
