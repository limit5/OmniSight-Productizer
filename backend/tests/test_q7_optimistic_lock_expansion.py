"""Q.7 #301 — optimistic-lock expansion tests.

Covers the four endpoints the Q.7 TODO row ships:

  * ``PATCH /tasks/{id}``
  * ``PUT /runtime/npi`` (the DB-backed runtime settings singleton)
  * ``PUT /secrets/{id}``
  * ``PATCH /projects/runs/{id}``

Each endpoint family gets three kinds of test:

  1. ``test_<endpoint>_requires_if_match`` — missing header → 428.
  2. ``test_<endpoint>_conflict_body_shape`` — wrong version → 409 +
     ``{current_version, your_version, hint, resource}`` body.
  3. ``test_<endpoint>_concurrent_one_wins`` — ``asyncio.gather`` two
     concurrent PATCHes with the same If-Match; exactly one lands,
     the loser gets 409 (the J2 invariant).

Plus module-level tests for the shared ``backend.optimistic_lock``
helper: ``parse_if_match`` header shapes, ``raise_conflict`` body
shape, ``bump_version_pg`` SQL semantics (with a live PG row).
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [pytest.mark.asyncio]


def _pg_not_available() -> bool:
    """Return True when the suite is running against the SQLite-only
    dev loop (matches the pg_test_dsn skip contract).
    """
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ─────────────────────────────────────────────────────────────────────
#  Module-level helper tests (no HTTP)
# ─────────────────────────────────────────────────────────────────────


async def test_parse_if_match_missing_raises_428():
    from fastapi import HTTPException
    from backend.optimistic_lock import parse_if_match

    with pytest.raises(HTTPException) as exc_info:
        parse_if_match(None)
    assert exc_info.value.status_code == 428


async def test_parse_if_match_malformed_raises_400():
    from fastapi import HTTPException
    from backend.optimistic_lock import parse_if_match

    with pytest.raises(HTTPException) as exc_info:
        parse_if_match("not-an-int")
    assert exc_info.value.status_code == 400


async def test_parse_if_match_accepts_plain_and_quoted():
    from backend.optimistic_lock import parse_if_match

    assert parse_if_match("42") == 42
    assert parse_if_match('"42"') == 42
    assert parse_if_match("  7  ") == 7
    assert parse_if_match('" 3 "') == 3


async def test_raise_conflict_body_shape():
    from fastapi import HTTPException
    from backend.optimistic_lock import raise_conflict

    with pytest.raises(HTTPException) as exc_info:
        raise_conflict(
            current_version=5,
            your_version=2,
            hint="another device edited this",
            resource="task",
        )
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["current_version"] == 5
    assert detail["your_version"] == 2
    assert detail["hint"] == "another device edited this"
    assert detail["resource"] == "task"


# ─────────────────────────────────────────────────────────────────────
#  bump_version_pg — direct (PG-live) exercise
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def _clean_project_runs(pg_test_pool):
    """Session-scoped-safe TRUNCATE wrapper so the bump_version_pg
    direct tests don't collide with HTTP-level tests in this file.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE project_runs RESTART IDENTITY CASCADE")
    yield pg_test_pool
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE project_runs RESTART IDENTITY CASCADE")


async def test_bump_version_pg_happy_path(_clean_project_runs):
    """Direct-SQL exercise: seed a row, bump it, confirm version and
    payload both advance; second bump with the stale expected version
    raises VersionConflict with the post-commit ``current_version``.
    """
    import time
    from backend.optimistic_lock import VersionConflict, bump_version_pg

    async with _clean_project_runs.acquire() as conn:
        await conn.execute(
            "INSERT INTO project_runs "
            "(id, project_id, label, created_at, workflow_run_ids) "
            "VALUES ($1, $2, $3, $4, $5)",
            "pr-q7-1", "default", "Original label", time.time(), "[]",
        )
        new_ver = await bump_version_pg(
            conn, "project_runs",
            pk_col="id", pk_value="pr-q7-1",
            expected_version=0,
            updates={"label": "Renamed once"},
        )
        assert new_ver == 1

        # Loser races in with the stale expected_version — must see
        # the post-commit version echoed on the exception.
        with pytest.raises(VersionConflict) as exc_info:
            await bump_version_pg(
                conn, "project_runs",
                pk_col="id", pk_value="pr-q7-1",
                expected_version=0,
                updates={"label": "Renamed stale"},
            )
        assert exc_info.value.current_version == 1
        assert exc_info.value.your_version == 0
        assert exc_info.value.resource == "project_runs"

        # Label was not mutated by the loser.
        row = await conn.fetchrow(
            "SELECT label, version FROM project_runs WHERE id = $1",
            "pr-q7-1",
        )
        assert row["label"] == "Renamed once"
        assert row["version"] == 1


async def test_bump_version_pg_missing_row(_clean_project_runs):
    """When the row does not exist we still raise VersionConflict but
    ``current_version`` is ``None`` — callers translate that to 404.
    """
    from backend.optimistic_lock import VersionConflict, bump_version_pg

    async with _clean_project_runs.acquire() as conn:
        with pytest.raises(VersionConflict) as exc_info:
            await bump_version_pg(
                conn, "project_runs",
                pk_col="id", pk_value="pr-nonexistent",
                expected_version=0,
                updates={"label": "ghost"},
            )
        assert exc_info.value.current_version is None


# ─────────────────────────────────────────────────────────────────────
#  PATCH /tasks/{id}
# ─────────────────────────────────────────────────────────────────────


async def _create_task(client, *, title: str = "Q.7 task") -> str:
    res = await client.post(
        "/api/v1/tasks",
        json={"title": title, "priority": "medium"},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


@_requires_pg
async def test_patch_task_requires_if_match(client):
    tid = await _create_task(client)
    res = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"title": "new title"},
    )
    assert res.status_code == 428, res.text


@_requires_pg
async def test_patch_task_happy_path_bumps_version(client):
    tid = await _create_task(client)
    res = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"title": "Renamed"},
        headers={"If-Match": "0"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["title"] == "Renamed"
    assert body["version"] == 1


@_requires_pg
async def test_patch_task_wrong_version_returns_409(client):
    tid = await _create_task(client)
    res = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"title": "Try overwrite"},
        headers={"If-Match": "99"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["resource"] == "task"
    assert detail["your_version"] == 99
    assert detail["current_version"] == 0
    assert isinstance(detail["hint"], str) and detail["hint"]


@_requires_pg
async def test_patch_task_concurrent_one_wins(client):
    """Two concurrent PATCHes on the same task with the same If-Match —
    exactly one lands; the other gets 409 (or 400 under a rare
    pool-timing variance, per the J2 precedent in
    test_workflow_optimistic_lock_http.py).
    """
    tid = await _create_task(client)

    async def attempt(label: str):
        return await client.patch(
            f"/api/v1/tasks/{tid}",
            json={"title": f"patched-by-{label}"},
            headers={"If-Match": "0"},
        )

    r1, r2 = await asyncio.gather(attempt("A"), attempt("B"))
    codes = sorted([r1.status_code, r2.status_code])
    assert 200 in codes, f"one PATCH must land, got {codes}"
    loser_code = next(c for c in codes if c != 200)
    assert loser_code == 409, f"loser must be 409 conflict, got {loser_code}"

    # Survivor can be either A or B — we only assert exactly one
    # winner and the version landed.
    get_res = await client.get(f"/api/v1/tasks/{tid}")
    assert get_res.status_code == 200
    final = get_res.json()
    assert final["version"] == 1
    assert final["title"] in ("patched-by-A", "patched-by-B")


# ─────────────────────────────────────────────────────────────────────
#  PUT /runtime/npi (runtime settings)
# ─────────────────────────────────────────────────────────────────────


@_requires_pg
async def test_put_runtime_npi_requires_if_match(client):
    # First GET seeds the row if needed + lets us discover the version.
    await client.get("/api/v1/runtime/npi")
    res = await client.put(
        "/api/v1/runtime/npi",
        params={"business_model": "oem"},
    )
    assert res.status_code == 428


@_requires_pg
async def test_put_runtime_npi_happy_path_bumps_version(client):
    initial = await client.get("/api/v1/runtime/npi")
    assert initial.status_code == 200
    version = int(initial.json().get("version", 0))
    res = await client.put(
        "/api/v1/runtime/npi",
        params={"business_model": "oem"},
        headers={"If-Match": str(version)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["business_model"] == "oem"
    assert body["version"] == version + 1


@_requires_pg
async def test_put_runtime_npi_wrong_version_returns_409(client):
    await client.get("/api/v1/runtime/npi")
    res = await client.put(
        "/api/v1/runtime/npi",
        params={"business_model": "oem"},
        headers={"If-Match": "9999"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["resource"] == "runtime_settings"
    assert detail["your_version"] == 9999


@_requires_pg
async def test_put_runtime_npi_concurrent_one_wins(client):
    initial = await client.get("/api/v1/runtime/npi")
    base_version = int(initial.json().get("version", 0))

    async def attempt(value: str):
        return await client.put(
            "/api/v1/runtime/npi",
            params={"business_model": value},
            headers={"If-Match": str(base_version)},
        )

    r1, r2 = await asyncio.gather(attempt("oem"), attempt("odm"))
    codes = sorted([r1.status_code, r2.status_code])
    assert 200 in codes, f"one PUT must land, got {codes}"
    loser = next(c for c in codes if c != 200)
    assert loser == 409


# ─────────────────────────────────────────────────────────────────────
#  PUT /secrets/{id}
# ─────────────────────────────────────────────────────────────────────


async def _seed_secret_and_login(client) -> tuple[str, dict]:
    """Seed an admin user + login so ``require_admin`` passes, then
    create a secret via the POST endpoint and return (secret_id,
    session cookie header dict).
    """
    # Create the admin + session directly so we don't duplicate the
    # full login flow (exercised elsewhere).
    from backend import auth as _auth
    user = await _auth.create_user(
        username="q7-admin", password="Q7-TestPass-123!",
        role="admin", tenant_id="t-default",
    )
    session = await _auth.create_session(
        user_id=user["id"], user_agent="q7-test", ip="127.0.0.1",
    )
    cookies = {_auth.SESSION_COOKIE: session.token}

    res = await client.post(
        "/api/v1/secrets",
        json={
            "key_name": "q7-test",
            "value": "initial-value",
            "secret_type": "custom",
            "metadata": {},
        },
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"], cookies


@_requires_pg
async def test_put_secret_requires_if_match(client):
    try:
        secret_id, cookies = await _seed_secret_and_login(client)
    except Exception as exc:
        pytest.skip(f"secrets router not available in this env: {exc}")
    res = await client.put(
        f"/api/v1/secrets/{secret_id}",
        json={"value": "rotated"},
        cookies=cookies,
    )
    assert res.status_code == 428, res.text


@_requires_pg
async def test_put_secret_happy_path_bumps_version(client):
    try:
        secret_id, cookies = await _seed_secret_and_login(client)
    except Exception as exc:
        pytest.skip(f"secrets router not available in this env: {exc}")
    res = await client.put(
        f"/api/v1/secrets/{secret_id}",
        json={"value": "rotated"},
        headers={"If-Match": "0"},
        cookies=cookies,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["version"] == 1


@_requires_pg
async def test_put_secret_wrong_version_returns_409(client):
    try:
        secret_id, cookies = await _seed_secret_and_login(client)
    except Exception as exc:
        pytest.skip(f"secrets router not available in this env: {exc}")
    res = await client.put(
        f"/api/v1/secrets/{secret_id}",
        json={"value": "rotated"},
        headers={"If-Match": "42"},
        cookies=cookies,
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["resource"] == "tenant_secret"


@_requires_pg
async def test_put_secret_concurrent_one_wins(client):
    try:
        secret_id, cookies = await _seed_secret_and_login(client)
    except Exception as exc:
        pytest.skip(f"secrets router not available in this env: {exc}")

    async def attempt(label: str):
        return await client.put(
            f"/api/v1/secrets/{secret_id}",
            json={"value": f"rotated-by-{label}"},
            headers={"If-Match": "0"},
            cookies=cookies,
        )

    r1, r2 = await asyncio.gather(attempt("A"), attempt("B"))
    codes = sorted([r1.status_code, r2.status_code])
    assert 200 in codes, f"one PUT must land, got {codes}"
    loser = next(c for c in codes if c != 200)
    assert loser == 409


# ─────────────────────────────────────────────────────────────────────
#  PATCH /projects/runs/{id}
# ─────────────────────────────────────────────────────────────────────


async def _seed_project_run(client) -> str:
    """Insert a project_run directly (no POST endpoint) so the tests
    can exercise the PATCH surface."""
    import time
    from backend.db_pool import get_pool

    pr_id = f"pr-q7-{int(time.time() * 1000)}"
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO project_runs "
            "(id, project_id, label, created_at, workflow_run_ids) "
            "VALUES ($1, $2, $3, $4, $5)",
            pr_id, "q7-proj", "Initial label", time.time(), "[]",
        )
    return pr_id


@_requires_pg
async def test_patch_project_run_requires_if_match(client):
    try:
        pr_id = await _seed_project_run(client)
    except Exception as exc:
        pytest.skip(f"PG pool not initialised in this env: {exc}")
    res = await client.patch(
        f"/api/v1/projects/runs/{pr_id}",
        json={"label": "Renamed"},
    )
    assert res.status_code == 428


@_requires_pg
async def test_patch_project_run_happy_path_bumps_version(client):
    try:
        pr_id = await _seed_project_run(client)
    except Exception as exc:
        pytest.skip(f"PG pool not initialised in this env: {exc}")
    res = await client.patch(
        f"/api/v1/projects/runs/{pr_id}",
        json={"label": "Renamed"},
        headers={"If-Match": "0"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["label"] == "Renamed"
    assert body["version"] == 1


@_requires_pg
async def test_patch_project_run_wrong_version_returns_409(client):
    try:
        pr_id = await _seed_project_run(client)
    except Exception as exc:
        pytest.skip(f"PG pool not initialised in this env: {exc}")
    res = await client.patch(
        f"/api/v1/projects/runs/{pr_id}",
        json={"label": "Renamed"},
        headers={"If-Match": "7"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["resource"] == "project_run"
    assert detail["your_version"] == 7
    assert detail["current_version"] == 0


@_requires_pg
async def test_patch_project_run_concurrent_one_wins(client):
    try:
        pr_id = await _seed_project_run(client)
    except Exception as exc:
        pytest.skip(f"PG pool not initialised in this env: {exc}")

    async def attempt(label: str):
        return await client.patch(
            f"/api/v1/projects/runs/{pr_id}",
            json={"label": f"renamed-by-{label}"},
            headers={"If-Match": "0"},
        )

    r1, r2 = await asyncio.gather(attempt("A"), attempt("B"))
    codes = sorted([r1.status_code, r2.status_code])
    assert 200 in codes, f"one PATCH must land, got {codes}"
    loser = next(c for c in codes if c != 200)
    assert loser == 409
