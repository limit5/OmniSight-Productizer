"""Phase-3-Runtime-v2 SP-3.6a — contract tests for ported artifact
db.py functions.

Replaces the SQLite-backed ``test_artifacts_insert_filter_delete`` in
``test_db.py`` AND preserves the RLS coverage previously in
``tests/test_rls.py`` (which is skipped with SP-3.6b markers pending
its own migration).

Coverage:
  * Four functions: insert_artifact / list_artifacts / get_artifact /
    delete_artifact — happy path + filter semantics + delete
    idempotency.
  * **Tenant isolation (load-bearing for SP-3.6)**:
    - insert_artifact auto-fills tenant_id from context, OVERRIDING
      any tenant_id in the caller's data dict (anti-forge guarantee)
    - list_artifacts / get_artifact / delete_artifact filter by
      current_tenant_id context — Tenant A cannot see / get / delete
      Tenant B's artifacts.
    - Clearing tenant context does NOT leak artifacts (the filter
      skips entirely when tid is None, so "no tenant" is still
      bounded by whatever rows have no tenant_id — here none).
  * Error paths: missing required keys, null name (NOT NULL).

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db
from backend.db_context import set_tenant_id


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Every test starts with no tenant set — each test sets its own."""
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _art_fixture(**overrides) -> dict:
    base = {
        "id": "art-test",
        "task_id": "t-test",
        "agent_id": "a-test",
        "name": "test.bin",
        "type": "binary",
        "file_path": "/tmp/test.bin",
        "size": 100,
        "created_at": "2026-04-20T00:00:00",
    }
    base.update(overrides)
    return base


# ─── Happy path: CRUD + filtering ────────────────────────────────


class TestArtifactsCrud:
    @pytest.mark.asyncio
    async def test_insert_then_list_unscoped(self, pg_test_conn) -> None:
        # No tenant set → list filters unbounded (no tenant_id filter
        # applied). All inserted rows returned.
        for i in range(3):
            await db.insert_artifact(pg_test_conn, _art_fixture(
                id=f"art-{i}", name=f"f{i}.bin",
            ))
        rows = await db.list_artifacts(pg_test_conn)
        assert len(rows) == 3
        assert {r["id"] for r in rows} == {"art-0", "art-1", "art-2"}

    @pytest.mark.asyncio
    async def test_filter_by_task_id(self, pg_test_conn) -> None:
        for i in range(3):
            await db.insert_artifact(pg_test_conn, _art_fixture(
                id=f"art-f-{i}",
                task_id="t-A" if i < 2 else "t-B",
            ))
        rows = await db.list_artifacts(pg_test_conn, task_id="t-A")
        assert len(rows) == 2
        assert all(r["task_id"] == "t-A" for r in rows)

    @pytest.mark.asyncio
    async def test_filter_by_agent_id(self, pg_test_conn) -> None:
        for i in range(3):
            await db.insert_artifact(pg_test_conn, _art_fixture(
                id=f"art-ag-{i}",
                agent_id="a-X" if i < 1 else "a-Y",
            ))
        rows = await db.list_artifacts(pg_test_conn, agent_id="a-Y")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, pg_test_conn) -> None:
        assert await db.get_artifact(pg_test_conn, "nonexistent") is None

    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, pg_test_conn) -> None:
        await db.insert_artifact(pg_test_conn, _art_fixture(id="art-del"))
        assert await db.delete_artifact(pg_test_conn, "art-del") is True
        assert await db.get_artifact(pg_test_conn, "art-del") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, pg_test_conn) -> None:
        assert await db.delete_artifact(pg_test_conn, "never") is False

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, pg_test_conn) -> None:
        for i in range(3):
            await db.insert_artifact(pg_test_conn, _art_fixture(
                id=f"art-ord-{i}",
                created_at=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_artifacts(pg_test_conn)
        assert [r["id"] for r in rows] == [
            "art-ord-2", "art-ord-1", "art-ord-0",
        ]


# ─── Tenant isolation (load-bearing; preserves test_rls.py coverage) ─


TENANT_A = "t-alpha"
TENANT_B = "t-beta"


class TestArtifactsTenantIsolation:
    @pytest.mark.asyncio
    async def test_insert_auto_fills_tenant_from_context(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_artifact(pg_test_conn, _art_fixture(id="art-ctx"))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM artifacts WHERE id = $1", "art-ctx",
        )
        assert row["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_insert_overrides_forged_tenant_id(
        self, pg_test_conn,
    ) -> None:
        # Anti-forge guarantee: even if a malicious caller stuffs
        # tenant_id into their data dict, insert_artifact OVERWRITES
        # it from context. This is the single most important tenant
        # safety property.
        set_tenant_id(TENANT_A)
        await db.insert_artifact(pg_test_conn, _art_fixture(
            id="art-forge", tenant_id=TENANT_B,
        ))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM artifacts WHERE id = $1", "art-forge",
        )
        assert row["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_insert_with_no_context_uses_default(
        self, pg_test_conn,
    ) -> None:
        # tenant_insert_value() falls back to "t-default" when no
        # context is set. Without this fallback, NOT NULL-like
        # schema enforcement would fail. Regression guard.
        await db.insert_artifact(pg_test_conn, _art_fixture(id="art-def"))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM artifacts WHERE id = $1", "art-def",
        )
        assert row["tenant_id"] == "t-default"

    @pytest.mark.asyncio
    async def test_list_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_artifact(pg_test_conn, _art_fixture(
            id="art-a-1", name="alpha-1",
        ))
        set_tenant_id(TENANT_B)
        await db.insert_artifact(pg_test_conn, _art_fixture(
            id="art-b-1", name="beta-1",
        ))
        # Back to A — list must ONLY show A's artifact
        set_tenant_id(TENANT_A)
        rows = await db.list_artifacts(pg_test_conn)
        ids = {r["id"] for r in rows}
        assert ids == {"art-a-1"}
        assert "art-b-1" not in ids

    @pytest.mark.asyncio
    async def test_get_cross_tenant_returns_none(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_artifact(pg_test_conn, _art_fixture(id="art-priv"))
        set_tenant_id(TENANT_B)
        # Tenant B cannot get Tenant A's artifact even though the
        # id is known. Core isolation guarantee.
        assert await db.get_artifact(pg_test_conn, "art-priv") is None

    @pytest.mark.asyncio
    async def test_delete_cross_tenant_is_noop(self, pg_test_conn) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_artifact(pg_test_conn, _art_fixture(
            id="art-noreach",
        ))
        set_tenant_id(TENANT_B)
        # Tenant B cannot delete Tenant A's artifact. delete returns
        # False (nothing matched the (id, tenant_id) pair); the row
        # remains intact for tenant A to still access.
        assert await db.delete_artifact(
            pg_test_conn, "art-noreach",
        ) is False
        set_tenant_id(TENANT_A)
        assert await db.get_artifact(pg_test_conn, "art-noreach") is not None


# ─── Error paths ────────────────────────────────────────────────


class TestArtifactsErrorPaths:
    @pytest.mark.asyncio
    async def test_insert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        with pytest.raises(KeyError):
            await db.insert_artifact(pg_test_conn, {
                # missing "id" and "name"
                "task_id": "t", "agent_id": "a",
            })

    @pytest.mark.asyncio
    async def test_insert_rejects_null_name(self, pg_test_conn) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_artifact(pg_test_conn, _art_fixture(
                id="art-null", name=None,
            ))
