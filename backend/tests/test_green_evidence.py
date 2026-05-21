"""OP-1569 [RT-04b] -- green-evidence store + query-by-full-SHA tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.agents.green_evidence import (
    PostgresGreenEvidenceStore,
    get_evidence,
    green_status,
    record_evidence,
)


T0 = datetime(2026, 5, 22, 1, 2, 3, tzinfo=timezone.utc)

# A real-shaped 40-hex full SHA and its 12-char abbreviation.
SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"
SHA_B_PREFIX = SHA_B[:12]
SHA_C = "c" * 40


class _FakeRow(dict[str, Any]):
    pass


class _FakeConn:
    """Minimal asyncpg-shaped fake keyed on ``green_evidence.full_sha``."""

    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        self.fetchrow_calls.append((sql, args))
        if "SELECT status" in sql:
            return self.rows.get(args[0])
        if "SELECT full_sha" in sql:
            return self.rows.get(args[0])
        if "INSERT INTO green_evidence" in sql:
            full_sha, status, pipeline_id, evidence_url, recorded_at = args
            self.rows[full_sha] = _FakeRow(
                full_sha=full_sha,
                status=status,
                pipeline_id=pipeline_id,
                evidence_url=evidence_url,
                recorded_at=recorded_at,
            )
            return self.rows[full_sha]
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


@asynccontextmanager
async def _factory(conn: _FakeConn) -> AsyncIterator[_FakeConn]:
    yield conn


# -- AC: green_status(sha) returns pass/fail -------------------------------


async def test_green_status_true_for_recorded_pass() -> None:
    conn = _FakeConn()
    await record_evidence(SHA_A, "pass", T0, conn_factory=lambda: _factory(conn))
    assert await green_status(SHA_A, conn_factory=lambda: _factory(conn)) is True


async def test_green_status_false_for_recorded_fail() -> None:
    conn = _FakeConn()
    await record_evidence(SHA_B, "fail", T0, conn_factory=lambda: _factory(conn))
    assert await green_status(SHA_B, conn_factory=lambda: _factory(conn)) is False


# -- AC: absent SHA = fail-closed ------------------------------------------


async def test_green_status_absent_sha_is_fail_closed() -> None:
    conn = _FakeConn()
    assert await green_status(SHA_A, conn_factory=lambda: _factory(conn)) is False
    # The lookup was actually attempted (no short-circuit) and matched nothing.
    assert conn.fetchrow_calls
    assert conn.fetchrow_calls[-1][1] == (SHA_A,)


async def test_candidate_gate_rejects_unknown_full_sha_when_other_sha_is_green() -> None:
    conn = _FakeConn()
    await record_evidence(SHA_A, "pass", T0, conn_factory=lambda: _factory(conn))

    assert await green_status(SHA_C, conn_factory=lambda: _factory(conn)) is False
    assert conn.fetchrow_calls[-1][1] == (SHA_C,)
    assert await green_status(SHA_A, conn_factory=lambda: _factory(conn)) is True


async def test_green_status_malformed_sha_is_fail_closed_without_db() -> None:
    conn = _FakeConn()
    # 41 chars, non-hex chars, empty, and a 12-char abbreviation are all
    # malformed. (An upper-case 40-hex string is NOT malformed -- it
    # normalizes to a valid lowercase SHA -- so it is excluded here.)
    for bad in ("", "xyz", SHA_B_PREFIX, "g" * 40, SHA_A + "a"):
        assert await green_status(bad, conn_factory=lambda: _factory(conn)) is False
    # Malformed SHAs never reach the DB -- nothing could ever match.
    assert conn.fetchrow_calls == []


async def test_full_sha_prefix_does_not_match_stored_full_sha() -> None:
    conn = _FakeConn()
    await record_evidence(SHA_B, "pass", T0, conn_factory=lambda: _factory(conn))
    # Querying by the abbreviation must NOT resolve the green full SHA.
    assert (
        await green_status(SHA_B_PREFIX, conn_factory=lambda: _factory(conn))
        is False
    )
    assert await green_status(SHA_B, conn_factory=lambda: _factory(conn)) is True


# -- record / upsert / query-row contract ----------------------------------


async def test_record_evidence_upserts_and_can_flip_status() -> None:
    conn = _FakeConn()
    await record_evidence(SHA_A, "fail", T0, conn_factory=lambda: _factory(conn))
    assert await green_status(SHA_A, conn_factory=lambda: _factory(conn)) is False

    later = datetime(2026, 5, 23, tzinfo=timezone.utc)
    await record_evidence(
        SHA_A, "pass", later, pipeline_id="pipe-9", conn_factory=lambda: _factory(conn)
    )
    assert len(conn.rows) == 1
    assert await green_status(SHA_A, conn_factory=lambda: _factory(conn)) is True

    insert_sql = conn.fetchrow_calls[0][0]
    assert "ON CONFLICT (full_sha) DO UPDATE" in insert_sql
    assert "updated_at = NOW()" in insert_sql


async def test_get_evidence_returns_row_and_normalizes_case() -> None:
    conn = _FakeConn()
    await record_evidence(
        SHA_B,
        "pass",
        T0,
        pipeline_id="pipe-1",
        evidence_url="https://ci/run/1",
        conn_factory=lambda: _factory(conn),
    )
    # Query with an upper-case spelling of the same SHA -> normalized match.
    row = await get_evidence(SHA_B.upper(), conn_factory=lambda: _factory(conn))
    assert row is not None
    assert row.full_sha == SHA_B
    assert row.status == "pass"
    assert row.pipeline_id == "pipe-1"
    assert row.evidence_url == "https://ci/run/1"


async def test_get_evidence_absent_returns_none() -> None:
    conn = _FakeConn()
    assert await get_evidence(SHA_A, conn_factory=lambda: _factory(conn)) is None


async def test_record_evidence_rejects_short_sha() -> None:
    with pytest.raises(ValueError, match="full 40-char"):
        await record_evidence(SHA_B_PREFIX, "pass", T0)


async def test_record_evidence_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="status"):
        await record_evidence(SHA_A, "green", T0)  # type: ignore[arg-type]


async def test_store_wrapper_delegates_to_module_helpers() -> None:
    conn = _FakeConn()
    store = PostgresGreenEvidenceStore(lambda: _factory(conn))
    await store.record_evidence(SHA_A, "pass", T0)
    assert await store.green_status(SHA_A) is True
    row = await store.get_evidence(SHA_A)
    assert row is not None and row.status == "pass"


# -- enum drift guard against the migration --------------------------------


def test_status_enum_matches_migration() -> None:
    from backend.agents import green_evidence as mod

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0246_green_evidence.py"
    ).read_text()
    assert mod._VALID_STATUSES == {"pass", "fail"}
    assert "'pass','fail'" in migration
