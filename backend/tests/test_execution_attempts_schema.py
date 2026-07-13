"""OP-2641 — dormant execution-attempt and lease-epoch schema tests.

Offline tests exercise the async store helpers against the SQLite ``_SCHEMA``
subset. The final tests use the standard PG fixture and skip when
``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from backend import db
from scripts import migrate_sqlite_to_pg as mig


FUTURE_PG = datetime(2099, 1, 1, tzinfo=UTC)


class _SQLiteConn:
    """Minimal asyncpg-shaped adapter for exercising helpers on SQLite."""

    def __init__(self) -> None:
        self.raw = sqlite3.connect(":memory:")
        self.raw.row_factory = sqlite3.Row
        self.raw.executescript(db._SCHEMA)

    @staticmethod
    def _translate(sql: str, params: tuple) -> tuple[str, tuple]:
        param_indexes: list[int] = []

        def replace_placeholder(match: re.Match) -> str:
            param_indexes.append(int(match.group(1)) - 1)
            return "?"

        sqlite_sql = re.sub(
            r"\$(\d+)(?:::jsonb)?", replace_placeholder, sql
        )
        sqlite_params = tuple(params[index] for index in param_indexes)
        return sqlite_sql, sqlite_params

    async def fetchrow(self, sql: str, *params):
        sqlite_sql, sqlite_params = self._translate(sql, params)
        return self.raw.execute(sqlite_sql, sqlite_params).fetchone()

    async def fetch(self, sql: str, *params):
        sqlite_sql, sqlite_params = self._translate(sql, params)
        return self.raw.execute(sqlite_sql, sqlite_params).fetchall()

    def close(self) -> None:
        self.raw.close()


@pytest.fixture
def sqlite_conn():
    conn = _SQLiteConn()
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-attempt", "t-attempt"),
    )
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-other", "t-other"),
    )
    try:
        yield conn
    finally:
        conn.close()


def _attempt(attempt_id: str, **overrides) -> dict:
    values = {
        "attempt_id": attempt_id,
        "tenant_id": "t-attempt",
        "grant_id": "grant-attempt-1",
        "outcome": "unknown",
        "evidence": "connection closed before response",
        "error": "connection reset",
        "lease_owner": "worker-1",
        "lease_epoch": 7,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_sqlite_multiple_execution_attempts_append_and_roundtrip(
    sqlite_conn,
) -> None:
    first = _attempt("attempt-1")
    second = _attempt(
        "attempt-2",
        outcome="applied",
        evidence="sink receipt 42",
        error="",
        lease_owner="worker-2",
        lease_epoch=8,
    )

    assert await db.put_execution_attempt(sqlite_conn, **first) is True
    assert await db.put_execution_attempt(sqlite_conn, **second) is True

    stored = await db.get_execution_attempts(
        sqlite_conn,
        first["grant_id"],
        tenant_id=first["tenant_id"],
    )
    assert len(stored) == 2
    by_id = {row["attempt_id"]: row for row in stored}
    assert set(by_id) == {"attempt-1", "attempt-2"}
    assert by_id["attempt-1"]["outcome"] == "unknown"
    assert by_id["attempt-1"]["lease_epoch"] == 7
    assert by_id["attempt-2"]["evidence"] == "sink receipt 42"
    assert by_id["attempt-2"]["lease_owner"] == "worker-2"
    assert all(row["created_at"] for row in stored)


@pytest.mark.asyncio
async def test_sqlite_execution_attempt_get_is_tenant_scoped(sqlite_conn) -> None:
    attempt = _attempt("attempt-cross-tenant")
    await db.put_execution_attempt(sqlite_conn, **attempt)

    assert await db.get_execution_attempts(
        sqlite_conn,
        attempt["grant_id"],
        tenant_id="t-other",
    ) == []


@pytest.mark.asyncio
async def test_sqlite_execution_attempt_replay_is_idempotent(sqlite_conn) -> None:
    attempt = _attempt("attempt-replay")
    assert await db.put_execution_attempt(sqlite_conn, **attempt) is True
    assert await db.put_execution_attempt(
        sqlite_conn,
        **{
            **attempt,
            "outcome": "definitely_not_applied",
            "evidence": "changed",
            "lease_epoch": 99,
        },
    ) is False

    stored = await db.get_execution_attempts(
        sqlite_conn,
        attempt["grant_id"],
        tenant_id=attempt["tenant_id"],
    )
    assert len(stored) == 1
    assert stored[0]["outcome"] == "unknown"
    assert stored[0]["lease_epoch"] == 7


@pytest.mark.asyncio
async def test_sqlite_execution_attempt_outcome_check_rejects_invalid(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await db.put_execution_attempt(
            sqlite_conn,
            **_attempt("attempt-invalid", outcome="maybe_applied"),
        )


@pytest.mark.asyncio
async def test_sqlite_attempt_helpers_reject_empty_scope_and_ids(
    sqlite_conn,
) -> None:
    for field in ("tenant_id", "attempt_id", "grant_id"):
        attempt = _attempt("attempt-empty")
        attempt[field] = ""
        with pytest.raises(ValueError, match="must be non-empty"):
            await db.put_execution_attempt(
                sqlite_conn,
                **attempt,
            )
    with pytest.raises(ValueError, match="tenant_id must be non-empty"):
        await db.get_execution_attempts(
            sqlite_conn,
            "grant-attempt-1",
            tenant_id="",
        )


def _insert_sqlite_grant_state(conn: sqlite3.Connection, state: str) -> None:
    conn.execute(
        """INSERT INTO action_grants
           (grant_id, tenant_id, challenge_id, action_instance_id,
            principal_type, actor_id, request_id, model_call_id,
            adapter_namespace, tool_name, schema_version, family,
            canonical_target, args_hash, provenance_kind,
            no_model_input_source, prepared_action_digest,
            grant_issuer_source, recovery_mode, state, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"grant-{state}",
            "t-bootstrap",
            f"challenge-{state}",
            f"action-{state}",
            "user",
            "user-1",
            "request-1",
            "",
            "workspace",
            "write_file",
            "v1",
            "workspace.write",
            "/workspace/output.txt",
            "a" * 64,
            "no_model_input",
            "slash_command",
            "d" * 64,
            "slash_command",
            "non_replayable",
            state,
            "2099-01-01 00:00:00",
        ),
    )


def test_sqlite_bootstrap_has_attempts_epoch_and_six_grant_states() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    resume_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(resume_jobs)")
    }

    assert "execution_attempts" in tables
    assert "lease_epoch" in resume_columns
    _insert_sqlite_grant_state(conn, "failed")
    _insert_sqlite_grant_state(conn, "manual")
    assert conn.execute(
        "SELECT state FROM action_grants ORDER BY state"
    ).fetchall() == [("failed",), ("manual",)]
    conn.close()


def test_migrator_catalog_contains_execution_attempts_after_grants() -> None:
    grant_index = mig.TABLES_IN_ORDER.index("action_grants")
    attempt_index = mig.TABLES_IN_ORDER.index("execution_attempts")
    assert attempt_index == grant_index + 1
    assert "execution_attempts" not in mig.TABLES_WITH_IDENTITY_ID


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


async def _seed_pg_grant(
    conn,
    suffix: str,
    tenant_id: str,
    *,
    state: str = "pending",
) -> tuple[str, str]:
    action_instance_id = f"act-attempt-{suffix}"
    challenge_id = f"challenge-attempt-{suffix}"
    grant_id = f"grant-attempt-{suffix}"
    await db.put_prepared_action(
        conn,
        tenant_id=tenant_id,
        action_instance_id=action_instance_id,
        principal_type="user",
        actor_id="user-1",
        request_id=f"request-{suffix}",
        model_call_id="",
        adapter_namespace="workspace",
        tool_name="write_file",
        schema_version="v1",
        family="workspace.write",
        canonical_target="/workspace/output.txt",
        args_hash="a" * 64,
        provenance_kind="no_model_input",
        model_snapshot_id=None,
        no_model_input_source="slash_command",
        prepared_action_digest="d" * 64,
        effect="mutating",
        recovery_mode="non_replayable",
        executable_args_json='{"content":"done","path":"output.txt"}',
        human_rendering_json='{"summary":"Write output.txt"}',
    )
    await db.put_challenge(
        conn,
        challenge_id=challenge_id,
        tenant_id=tenant_id,
        action_instance_id=action_instance_id,
        expires_at=FUTURE_PG,
    )
    await conn.execute(
        """INSERT INTO action_grants
           (grant_id, tenant_id, challenge_id, action_instance_id,
            principal_type, actor_id, request_id, model_call_id,
            adapter_namespace, tool_name, schema_version, family,
            canonical_target, args_hash, provenance_kind, model_snapshot_id,
            no_model_input_source, prepared_action_digest, grant_issuer_source,
            idempotency_key, recovery_mode, state, expires_at)
           SELECT $1, p.tenant_id, $2, p.action_instance_id,
                  p.principal_type, p.actor_id, p.request_id, p.model_call_id,
                  p.adapter_namespace, p.tool_name, p.schema_version, p.family,
                  p.canonical_target, p.args_hash, p.provenance_kind,
                  p.model_snapshot_id, p.no_model_input_source,
                  p.prepared_action_digest, 'slash_command', NULL,
                  'non_replayable', $5, $6
           FROM prepared_actions p
           WHERE p.tenant_id = $3 AND p.action_instance_id = $4""",
        grant_id,
        challenge_id,
        tenant_id,
        action_instance_id,
        state,
        FUTURE_PG,
    )
    return grant_id, action_instance_id


@pytest.mark.asyncio
async def test_pg_action_grants_accept_failed_and_manual_states(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-attempt-states-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)

    failed_id, _ = await _seed_pg_grant(
        pg_test_conn,
        f"failed-{suffix}",
        tenant_id,
        state="failed",
    )
    manual_id, _ = await _seed_pg_grant(
        pg_test_conn,
        f"manual-{suffix}",
        tenant_id,
        state="manual",
    )
    rows = await pg_test_conn.fetch(
        "SELECT state FROM action_grants WHERE grant_id = ANY($1::text[]) "
        "ORDER BY state",
        [failed_id, manual_id],
    )
    assert [row["state"] for row in rows] == ["failed", "manual"]


@pytest.mark.asyncio
async def test_pg_execution_attempt_composite_fk_rejects_missing_grant(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-attempt-fk-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await db.put_execution_attempt(
            pg_test_conn,
            attempt_id=f"attempt-missing-{suffix}",
            tenant_id=tenant_id,
            grant_id=f"grant-missing-{suffix}",
            outcome="unknown",
        )
    assert exc_info.value.constraint_name == "fk_execution_attempts_grant"


@pytest.mark.asyncio
async def test_pg_resume_job_lease_epoch_persists(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-resume-epoch-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    grant_id, action_instance_id = await _seed_pg_grant(
        pg_test_conn,
        suffix,
        tenant_id,
    )
    resume_id = f"resume-epoch-{suffix}"
    await pg_test_conn.execute(
        """INSERT INTO resume_jobs
           (resume_id, tenant_id, grant_id, action_instance_id, lease_epoch)
           VALUES ($1, $2, $3, $4, $5)""",
        resume_id,
        tenant_id,
        grant_id,
        action_instance_id,
        41,
    )

    assert await pg_test_conn.fetchval(
        "SELECT lease_epoch FROM resume_jobs "
        "WHERE resume_id = $1 AND tenant_id = $2",
        resume_id,
        tenant_id,
    ) == 41
