"""OP-2635 — dormant resume-job and execution-result store tests.

Offline tests exercise the async store helpers against the SQLite ``_SCHEMA``
subset. The final tests use the standard PG fixture and skip when
``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import json
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

    async def fetchrow(self, sql: str, *params):
        sqlite_sql = re.sub(r"\$\d+(?:::jsonb)?", "?", sql)
        return self.raw.execute(sqlite_sql, params).fetchone()

    def close(self) -> None:
        self.raw.close()


@pytest.fixture
def sqlite_conn():
    conn = _SQLiteConn()
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-resume", "t-resume"),
    )
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-other", "t-other"),
    )
    try:
        yield conn
    finally:
        conn.close()


def _resume_job(resume_id: str = "resume-1", **overrides) -> dict:
    values = {
        "resume_id": resume_id,
        "tenant_id": "t-resume",
        "grant_id": "grant-resume-1",
        "action_instance_id": "act-resume-1",
    }
    values.update(overrides)
    return values


def _execution_result(grant_id: str = "grant-result-1", **overrides) -> dict:
    values = {
        "grant_id": grant_id,
        "tenant_id": "t-resume",
        "result_json": '{"status":"ok","value":7}',
        "ambiguous": True,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_sqlite_resume_and_execution_result_roundtrip_within_tenant(
    sqlite_conn,
) -> None:
    resume_job = _resume_job()
    execution_result = _execution_result()

    assert await db.put_resume_job(sqlite_conn, **resume_job) is True
    assert await db.put_execution_result(sqlite_conn, **execution_result) is True

    stored_job = await db.get_resume_job(
        sqlite_conn,
        resume_job["resume_id"],
        tenant_id=resume_job["tenant_id"],
    )
    stored_result = await db.get_execution_result(
        sqlite_conn,
        execution_result["grant_id"],
        tenant_id=execution_result["tenant_id"],
    )

    assert stored_job is not None
    assert stored_job["grant_id"] == resume_job["grant_id"]
    assert stored_job["action_instance_id"] == resume_job["action_instance_id"]
    assert stored_job["state"] == "queued"
    assert stored_job["lease_owner"] is None
    assert stored_job["lease_expires_at"] is None
    assert stored_job["created_at"]

    assert stored_result is not None
    assert json.loads(stored_result["result"]) == json.loads(
        execution_result["result_json"]
    )
    assert stored_result["ambiguous"] is True
    assert stored_result["completed_at"]


@pytest.mark.asyncio
async def test_sqlite_cross_tenant_get_returns_none_for_both_tables(
    sqlite_conn,
) -> None:
    resume_job = _resume_job("resume-cross")
    execution_result = _execution_result("grant-result-cross")
    await db.put_resume_job(sqlite_conn, **resume_job)
    await db.put_execution_result(sqlite_conn, **execution_result)

    assert (
        await db.get_resume_job(
            sqlite_conn,
            resume_job["resume_id"],
            tenant_id="t-other",
        )
        is None
    )
    assert (
        await db.get_execution_result(
            sqlite_conn,
            execution_result["grant_id"],
            tenant_id="t-other",
        )
        is None
    )


@pytest.mark.asyncio
async def test_sqlite_resume_job_replay_is_idempotent_and_unchanged(
    sqlite_conn,
) -> None:
    resume_job = _resume_job("resume-replay")
    assert await db.put_resume_job(sqlite_conn, **resume_job) is True
    assert (
        await db.put_resume_job(
            sqlite_conn,
            **{
                **resume_job,
                "grant_id": "grant-changed",
                "action_instance_id": "act-changed",
            },
        )
        is False
    )

    stored = await db.get_resume_job(
        sqlite_conn,
        resume_job["resume_id"],
        tenant_id=resume_job["tenant_id"],
    )
    assert stored["grant_id"] == "grant-resume-1"
    assert stored["action_instance_id"] == "act-resume-1"


@pytest.mark.asyncio
async def test_sqlite_execution_result_is_write_once(sqlite_conn) -> None:
    execution_result = _execution_result("grant-result-write-once")
    assert await db.put_execution_result(sqlite_conn, **execution_result) is True
    assert await db.put_execution_result(sqlite_conn, **execution_result) is False

    with pytest.raises(ValueError, match="execution_result mismatch"):
        await db.put_execution_result(
            sqlite_conn,
            **{**execution_result, "result_json": '{"status":"changed"}'},
        )

    stored = await db.get_execution_result(
        sqlite_conn,
        execution_result["grant_id"],
        tenant_id=execution_result["tenant_id"],
    )
    assert json.loads(stored["result"]) == {"status": "ok", "value": 7}


@pytest.mark.asyncio
async def test_store_helpers_reject_empty_tenant_and_primary_keys(
    sqlite_conn,
) -> None:
    with pytest.raises(ValueError, match="tenant_id and resume_id"):
        await db.put_resume_job(
            sqlite_conn,
            resume_id="",
            tenant_id="t-resume",
            grant_id="grant-empty",
            action_instance_id="act-empty",
        )
    with pytest.raises(ValueError, match="tenant_id must be non-empty"):
        await db.get_resume_job(sqlite_conn, "resume-empty", tenant_id="")
    with pytest.raises(ValueError, match="tenant_id and grant_id"):
        await db.put_execution_result(
            sqlite_conn,
            grant_id="",
            tenant_id="t-resume",
            result_json="{}",
            ambiguous=False,
        )
    with pytest.raises(ValueError, match="tenant_id must be non-empty"):
        await db.get_execution_result(sqlite_conn, "grant-empty", tenant_id="")


def test_sqlite_bootstrap_contains_resume_and_execution_result_tables() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"resume_jobs", "execution_results"} <= tables
    resume_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(resume_jobs)")
    }
    assert "idx_resume_jobs_tenant_state" in resume_indexes
    conn.close()


def test_sqlite_resume_jobs_state_check_rejects_invalid_state(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        sqlite_conn.raw.execute(
            """INSERT INTO resume_jobs
               (resume_id, tenant_id, grant_id, action_instance_id, state)
               VALUES (?, ?, ?, ?, ?)""",
            ("resume-invalid", "t-resume", "grant-invalid", "act-invalid", "bad"),
        )


def test_migrator_catalog_contains_resume_and_result_tables_in_fk_order() -> None:
    grant_index = mig.TABLES_IN_ORDER.index("action_grants")
    resume_index = mig.TABLES_IN_ORDER.index("resume_jobs")
    result_index = mig.TABLES_IN_ORDER.index("execution_results")
    assert grant_index < resume_index < result_index
    assert "resume_jobs" not in mig.TABLES_WITH_IDENTITY_ID
    assert "execution_results" not in mig.TABLES_WITH_IDENTITY_ID


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


async def _seed_grant(conn, suffix: str, tenant_id: str) -> tuple[str, str]:
    action_instance_id = f"act-resume-{suffix}"
    challenge_id = f"challenge-resume-{suffix}"
    grant_id = f"grant-resume-{suffix}"
    identity = {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "principal_type": "user",
        "actor_id": "user-1",
        "request_id": f"request-{suffix}",
        "model_call_id": "",
        "adapter_namespace": "workspace",
        "tool_name": "write_file",
        "schema_version": "v1",
        "family": "workspace.write",
        "canonical_target": "/workspace/output.txt",
        "args_hash": "a" * 64,
        "provenance_kind": "no_model_input",
        "model_snapshot_id": None,
        "no_model_input_source": "slash_command",
        "prepared_action_digest": "d" * 64,
    }
    await db.put_prepared_action(
        conn,
        **identity,
        effect="mutating",
        executable_args_json='{"content":"done","path":"output.txt"}',
        human_rendering_json='{"summary":"Write output.txt"}',
    )
    await db.put_challenge(
        conn,
        challenge_id=challenge_id,
        **identity,
        expires_at=FUTURE_PG,
    )
    await db.put_action_grant(
        conn,
        grant_id=grant_id,
        challenge_id=challenge_id,
        **identity,
        grant_issuer_source="slash_command",
        idempotency_key=None,
        recovery_mode="non_replayable",
        expires_at=FUTURE_PG,
    )
    return grant_id, action_instance_id


@pytest.mark.asyncio
async def test_pg_resume_job_composite_fk_rejects_missing_grant(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-resume-fk-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await db.put_resume_job(
            pg_test_conn,
            resume_id=f"resume-missing-{suffix}",
            tenant_id=tenant_id,
            grant_id=f"grant-missing-{suffix}",
            action_instance_id=f"act-missing-{suffix}",
        )
    assert exc_info.value.constraint_name == "fk_resume_jobs_grant"


@pytest.mark.asyncio
async def test_pg_execution_result_composite_fk_rejects_missing_grant(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-result-fk-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await db.put_execution_result(
            pg_test_conn,
            grant_id=f"grant-missing-{suffix}",
            tenant_id=tenant_id,
            result_json='{"status":"missing"}',
            ambiguous=False,
        )
    assert exc_info.value.constraint_name == "fk_execution_results_grant"


@pytest.mark.asyncio
async def test_pg_resume_unique_and_both_stores_are_tenant_scoped(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-resume-scope-{suffix}"
    other_tenant_id = f"t-resume-other-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id, other_tenant_id)
    grant_id, action_instance_id = await _seed_grant(
        pg_test_conn,
        suffix,
        tenant_id,
    )
    resume_id = f"resume-{suffix}"

    assert await db.put_resume_job(
        pg_test_conn,
        resume_id=resume_id,
        tenant_id=tenant_id,
        grant_id=grant_id,
        action_instance_id=action_instance_id,
    )
    assert await db.put_execution_result(
        pg_test_conn,
        grant_id=grant_id,
        tenant_id=tenant_id,
        result_json='{"status":"ok"}',
        ambiguous=False,
    )
    assert (
        await db.get_resume_job(
            pg_test_conn,
            resume_id,
            tenant_id=other_tenant_id,
        )
        is None
    )
    assert (
        await db.get_execution_result(
            pg_test_conn,
            grant_id,
            tenant_id=other_tenant_id,
        )
        is None
    )

    with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
        await db.put_resume_job(
            pg_test_conn,
            resume_id=f"resume-second-{suffix}",
            tenant_id=tenant_id,
            grant_id=grant_id,
            action_instance_id=action_instance_id,
        )
    assert exc_info.value.constraint_name == "uq_resume_jobs_tenant_grant"
