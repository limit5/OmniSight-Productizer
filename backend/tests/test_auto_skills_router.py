"""BP.M.3 -- auto-distilled skills router contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeConn:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def transaction(self):
        return _FakeTx()

    async def fetch(self, sql: str, *args: Any):
        tenant_id = args[0]
        rows = [row for row in self.rows.values() if row["tenant_id"] == tenant_id]
        if "AND status = $2" in sql:
            rows = [row for row in rows if row["status"] == args[1]]
        return rows

    async def fetchrow(self, sql: str, *args: Any):
        if sql.startswith("INSERT INTO auto_distilled_skills"):
            row = {
                "id": args[0],
                "tenant_id": args[1],
                "skill_name": args[2],
                "source_task_id": args[3],
                "markdown_content": args[4],
                "version": 1,
                "status": "draft",
                "created_at": "2026-05-05T00:00:00Z",
            }
            self.rows[row["id"]] = row
            return row
        if sql.startswith("SELECT"):
            row = self.rows.get(args[0])
            if row and row["tenant_id"] == args[1]:
                return row
            return None
        if sql.startswith("UPDATE auto_distilled_skills SET status = 'reviewed'"):
            row = self.rows[args[0]]
            row["status"] = "reviewed"
            row["version"] += 1
            if len(args) > 2:
                row["markdown_content"] = args[2]
            return row
        if sql.startswith("UPDATE auto_distilled_skills SET status = 'promoted'"):
            row = self.rows[args[0]]
            row["status"] = "promoted"
            row["version"] += 1
            return row
        raise AssertionError(f"unexpected SQL: {sql}")


def _admin_user():
    from backend import auth as _au

    return _au.User(
        id="u-admin",
        email="admin@example.test",
        name="Admin",
        role="admin",
        tenant_id="t-default",
    )


async def test_direct_review_promote_uses_db_row_and_writes_pack(
    monkeypatch, tmp_path,
):
    conn = _FakeConn()
    from backend import audit as _audit
    import backend.db_pool as _db_pool
    from backend.db_context import current_tenant_id, set_tenant_id
    from backend.routers import auto_skills as _router

    captured: list[dict[str, Any]] = []

    async def fake_log(**kwargs: Any) -> None:
        captured.append({**kwargs, "tenant_context": current_tenant_id()})

    monkeypatch.setattr(_audit, "log", fake_log, raising=True)
    monkeypatch.setattr(_db_pool, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(_router, "_SKILLS_LIVE", tmp_path / "skills")

    created = await _router.create_auto_skill(
        _router.AutoSkillCreate(
            skill_name="auto-direct",
            markdown_content="---\nname: auto-direct\n---\n# Draft\n",
        ),
        user=_admin_user(),
    )
    assert created["status"] == "draft"

    reviewed = await _router.review_auto_skill(
        created["id"],
        _router.AutoSkillUpdate(
            markdown_content="---\nname: auto-direct\n---\n# Reviewed\n",
            expected_version=1,
        ),
        user=_admin_user(),
    )
    assert reviewed["status"] == "reviewed"

    set_tenant_id("t-prior")
    try:
        promoted = await _router.promote_auto_skill(
            created["id"], user=_admin_user(),
        )
    finally:
        assert current_tenant_id() == "t-prior"
        set_tenant_id(None)
    assert promoted["skill"]["status"] == "promoted"
    skill_file = tmp_path / "skills" / "auto-direct" / "SKILL.md"
    assert Path(promoted["path"]) == skill_file
    assert skill_file.read_text(encoding="utf-8").endswith("# Reviewed\n")

    assert len(captured) == 1
    row = captured[0]
    assert row["tenant_context"] == "t-default"
    assert row["action"] == "skill_promoted"
    assert row["entity_kind"] == "skill"
    assert row["entity_id"] == "auto-direct"
    assert row["actor"] == "admin@example.test"
    assert row["before"]["auto_distilled_skill_id"] == created["id"]
    assert row["after"]["auto_distilled_skill_id"] == created["id"]
    assert row["after"]["source_task_id"] is None
    assert row["after"]["path"] == str(skill_file)
    assert len(row["after"]["markdown_sha256"]) == 64


@pytest.fixture
async def _auto_skills_client(pg_test_pool, pg_test_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE auto_distilled_skills RESTART IDENTITY CASCADE"
        )

    from backend import db as _db
    from backend.main import app
    from backend.routers import auto_skills as _router

    monkeypatch.setattr(
        _router, "_SKILLS_LIVE", tmp_path / "skills", raising=False,
    )
    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, tmp_path / "skills"
    finally:
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE auto_distilled_skills RESTART IDENTITY CASCADE"
            )


async def test_create_list_get_patch_delete_auto_skill(_auto_skills_client):
    client, _ = _auto_skills_client
    created = await client.post(
        "/api/v1/auto-skills",
        json={
            "skill_name": "auto-router",
            "markdown_content": "# Auto router\n",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"].startswith("ads-")
    assert body["status"] == "draft"
    assert body["version"] == 1

    listed = await client.get("/api/v1/auto-skills?status=draft")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    patched = await client.patch(
        f"/api/v1/auto-skills/{body['id']}",
        json={
            "markdown_content": "# Auto router\n\nReviewed draft.\n",
            "expected_version": 1,
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2

    fetched = await client.get(f"/api/v1/auto-skills/{body['id']}")
    assert fetched.status_code == 200
    assert "Reviewed draft" in fetched.json()["markdown_content"]

    deleted = await client.delete(f"/api/v1/auto-skills/{body['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["previous_status"] == "draft"


async def test_review_then_promote_writes_skill_pack(_auto_skills_client):
    client, live_root = _auto_skills_client
    created = await client.post(
        "/api/v1/auto-skills",
        json={
            "skill_name": "auto-promoted",
            "markdown_content": "---\nname: auto-promoted\n---\n# Body\n",
        },
    )
    skill_id = created.json()["id"]

    promote_too_early = await client.post(
        f"/api/v1/auto-skills/{skill_id}/promote"
    )
    assert promote_too_early.status_code == 409

    reviewed = await client.post(
        f"/api/v1/auto-skills/{skill_id}/review",
        json={
            "markdown_content": "---\nname: auto-promoted\n---\n# Reviewed\n",
            "expected_version": 1,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["version"] == 2

    promoted = await client.post(f"/api/v1/auto-skills/{skill_id}/promote")
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert body["skill"]["status"] == "promoted"
    skill_file = Path(body["path"])
    assert skill_file == live_root / "auto-promoted" / "SKILL.md"
    assert skill_file.read_text(encoding="utf-8").endswith("# Reviewed\n")

    delete_promoted = await client.delete(f"/api/v1/auto-skills/{skill_id}")
    assert delete_promoted.status_code == 409


async def test_tenant_scoping_filters_rows(pg_test_pool, _auto_skills_client):
    client, _ = _auto_skills_client
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            "t-other",
            "Other",
            "starter",
        )
        await conn.execute(
            "INSERT INTO auto_distilled_skills ("
            "id, tenant_id, skill_name, markdown_content"
            ") VALUES ($1, $2, $3, $4)",
            "ads-other",
            "t-other",
            "auto-other",
            "# Other",
        )

    res = await client.get("/api/v1/auto-skills")
    assert res.status_code == 200
    assert res.json()["items"] == []


async def test_review_rejects_version_conflict(_auto_skills_client):
    client, _ = _auto_skills_client
    created = await client.post(
        "/api/v1/auto-skills",
        json={"skill_name": "auto-conflict", "markdown_content": "# C\n"},
    )
    skill_id = created.json()["id"]
    res = await client.post(
        f"/api/v1/auto-skills/{skill_id}/review",
        json={"expected_version": 99},
    )
    assert res.status_code == 409
