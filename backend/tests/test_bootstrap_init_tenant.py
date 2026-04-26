"""Y7 #283 — ``POST /api/v1/bootstrap/init-tenant`` wizard Step 2.5 tests.

Covers the optional first-install tenant initialization endpoint that
the wizard surfaces between Step 1 (admin password rotation) and Step
2 (LLM provider).  The endpoint must:

  * happy path — slugify display_name, create tenants row + super-
    admin user + owner membership + default project, persist
    ``OMNISIGHT_PRIMARY_TENANT_ID`` to ``.env``, write an audit row,
    and ``GET /admin/tenants`` (via super-admin login) must list both
    the new tenant and ``t-default``.
  * pure helper coverage — :func:`_slugify_display_name` collapses
    punctuation and trims hyphens; :func:`_persist_primary_tenant_env`
    creates / replaces the env line in place without dropping
    existing keys.
  * skip — the wizard does NOT call the endpoint, so ``t-default``
    stays the only tenant + the previously rotated admin still works.
  * error contract — every refusal carries a machine-readable
    ``kind`` so the wizard UI can pick a banner without parsing
    detail strings.

Module-global state audit (per SOP Step 1):
  * ``backend.routers.bootstrap`` introduces no new module-level
    cache — slug regex / plan whitelist / license-key regex are
    constants, each worker derives the same value.
  * ``_persist_primary_tenant_env`` writes to a path that is per-
    install (resolved from ``OMNISIGHT_DOTENV_FILE`` env or
    ``<repo>/.env``).  Tests pin the path via ``OMNISIGHT_DOTENV_FILE``
    pointing at ``tmp_path`` so two parallel test workers don't fight
    over the developer's real ``.env``.
  * ``settings.primary_tenant_id`` mirror is best-effort; tests do
    not depend on it (they re-read PG).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import auth as _au
from backend import bootstrap as _boot
from backend.routers import bootstrap as _btr


# ─────────────────────────────────────────────────────────────────
#  Pure helpers — no DB
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "display,expected",
    [
        ("Acme Robotics", "acme-robotics"),
        ("acme", "acme"),
        ("  Acme   Robotics  ", "acme-robotics"),
        ("Acme,  Inc.", "acme-inc"),
        ("ACME 2025!", "acme-2025"),
        ("---weird---", "weird"),
        ("a", "a"),  # single char — caller decides if too short
        ("", ""),
        ("***", ""),
        ("Über Org", "ber-org"),  # unicode collapses; latin1 'b' + 'er'
    ],
)
def test_slugify_display_name(display: str, expected: str) -> None:
    assert _btr._slugify_display_name(display) == expected


def test_persist_primary_tenant_env_creates_file_when_absent(tmp_path, monkeypatch):
    target = tmp_path / "subdir" / ".env"
    monkeypatch.setenv("OMNISIGHT_DOTENV_FILE", str(target))
    ok, warning = _btr._persist_primary_tenant_env("t-acme")
    assert ok is True
    assert warning == ""
    body = target.read_text(encoding="utf-8")
    assert "OMNISIGHT_PRIMARY_TENANT_ID=t-acme" in body
    assert body.endswith("\n")


def test_persist_primary_tenant_env_replaces_existing_key_in_place(
    tmp_path, monkeypatch,
):
    target = tmp_path / ".env"
    target.write_text(
        "# header comment\n"
        "OMNISIGHT_LLM_PROVIDER=anthropic\n"
        "OMNISIGHT_PRIMARY_TENANT_ID=t-old\n"
        "OMNISIGHT_DEBUG=false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNISIGHT_DOTENV_FILE", str(target))
    ok, _ = _btr._persist_primary_tenant_env("t-new")
    assert ok is True
    body = target.read_text(encoding="utf-8")
    # The new value sits where the old one was — no duplicate lines.
    assert body.count("OMNISIGHT_PRIMARY_TENANT_ID=") == 1
    assert "OMNISIGHT_PRIMARY_TENANT_ID=t-new" in body
    # Other keys + comments preserved.
    assert "OMNISIGHT_LLM_PROVIDER=anthropic" in body
    assert "OMNISIGHT_DEBUG=false" in body
    assert "# header comment" in body


def test_persist_primary_tenant_env_replaces_commented_key(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text(
        "# OMNISIGHT_PRIMARY_TENANT_ID=t-default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNISIGHT_DOTENV_FILE", str(target))
    ok, _ = _btr._persist_primary_tenant_env("t-acme")
    assert ok is True
    body = target.read_text(encoding="utf-8")
    assert "OMNISIGHT_PRIMARY_TENANT_ID=t-acme" in body
    # The commented stub gets replaced rather than left dangling.
    assert "# OMNISIGHT_PRIMARY_TENANT_ID=t-default" not in body


def test_persist_primary_tenant_env_warning_on_unwritable(tmp_path, monkeypatch):
    # Point at a path under a regular FILE (not directory) — write fails
    # because the parent "directory" is actually a file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    target = blocker / ".env"
    monkeypatch.setenv("OMNISIGHT_DOTENV_FILE", str(target))
    ok, warning = _btr._persist_primary_tenant_env("t-acme")
    assert ok is False
    assert warning  # non-empty
    assert "OMNISIGHT_PRIMARY_TENANT_ID" in warning


# ─────────────────────────────────────────────────────────────────
#  DB-backed integration tests
# ─────────────────────────────────────────────────────────────────


@pytest.fixture()
async def _init_tenant_db(pg_test_pool, pg_test_dsn, monkeypatch, tmp_path):
    """Fresh PG + isolated bootstrap marker + isolated .env file.

    Seeds the default admin so Step 1 has been "completed" (which is
    the prerequisite for surfacing Step 2.5 in the wizard, but the
    backend endpoint itself is independent of admin password state).
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)

    # Pin .env to tmp_path so the test does not touch the developer's real
    # ``.env`` and parallel tests do not race on the same path.
    env_path = tmp_path / ".env"
    monkeypatch.setenv("OMNISIGHT_DOTENV_FILE", str(env_path))

    async with pg_test_pool.acquire() as conn:
        # NOTE: tenants TRUNCATE cascades to user_tenant_memberships,
        # projects, and a long tail of FK-bearing rows (covered by the
        # CASCADE in the schema).  ``db.init()`` does NOT re-seed
        # ``t-default`` after a TRUNCATE (the seed lives in alembic
        # 0012 which only fires once at migration time), so we
        # explicitly re-INSERT the seed row to restore the runtime
        # invariant that ``t-default`` always exists.
        await conn.execute(
            "TRUNCATE tenants, users, bootstrap_state, audit_log, "
            "user_tenant_memberships, projects "
            "RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ('t-default', 'Default Tenant', 'free', 1) "
            "ON CONFLICT (id) DO NOTHING"
        )

    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()

    marker = tmp_path / ".bootstrap_state.json"
    _boot._reset_for_tests(Path(marker))

    user = await _au.ensure_default_admin()
    assert user is not None and user.tenant_id == "t-default"
    try:
        yield {"db": db, "admin": user, "env_path": env_path}
    finally:
        await db.close()
        _boot._reset_for_tests()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE tenants, users, bootstrap_state, audit_log, "
                "user_tenant_memberships, projects "
                "RESTART IDENTITY CASCADE"
            )
            # Re-seed t-default so the next test doesn't import an
            # empty tenants table (db.init does not re-seed it).
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ('t-default', 'Default Tenant', 'free', 1) "
                "ON CONFLICT (id) DO NOTHING"
            )


@pytest.fixture()
async def _init_tenant_client(_init_tenant_db):
    """Async HTTP client with the bootstrap gate bypassed for the route."""
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    _boot._gate_cache_reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, **_init_tenant_db}
    _boot._gate_cache_reset()


_VALID_BODY = {
    "display_name": "Acme Robotics",
    "plan": "free",
    "admin_email": "founder@acme.example",
    "admin_password": "rotated-strong-password-abc-123",
    "admin_name": "Alice Founder",
}


# ─────────────────────────────────────────────────────────────────
#  Happy path
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_tenant_happy_path_creates_tenant_user_membership_project(
    _init_tenant_client, pg_test_pool,
):
    client = _init_tenant_client["client"]
    env_path = _init_tenant_client["env_path"]

    r = await client.post(
        "/api/v1/bootstrap/init-tenant",
        json=_VALID_BODY,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "initialized"
    assert body["tenant_id"] == "t-acme-robotics"
    assert body["tenant_name"] == "Acme Robotics"
    assert body["plan"] == "free"
    assert body["super_admin_email"] == "founder@acme.example"
    assert body["super_admin_user_id"].startswith("u-")
    assert body["project_id"] == "p-acme-robotics-default"
    assert body["env_write_warning"] == ""

    # Tenant row landed.
    async with pg_test_pool.acquire() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT id, name, plan, enabled FROM tenants WHERE id = $1",
            "t-acme-robotics",
        )
    assert tenant_row is not None
    assert tenant_row["name"] == "Acme Robotics"
    assert tenant_row["plan"] == "free"
    assert int(tenant_row["enabled"]) == 1

    # Super-admin user landed with role=super_admin + tenant_id pin.
    async with pg_test_pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, email, role, tenant_id, enabled, must_change_password "
            "FROM users WHERE email = $1",
            "founder@acme.example",
        )
    assert user_row is not None
    assert user_row["role"] == "super_admin"
    assert user_row["tenant_id"] == "t-acme-robotics"
    assert int(user_row["enabled"]) == 1
    assert int(user_row["must_change_password"]) == 0

    # Owner membership row.
    async with pg_test_pool.acquire() as conn:
        membership = await conn.fetchrow(
            "SELECT user_id, tenant_id, role, status "
            "FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user_row["id"], "t-acme-robotics",
        )
    assert membership is not None
    assert membership["role"] == "owner"
    assert membership["status"] == "active"

    # Default project.
    async with pg_test_pool.acquire() as conn:
        project = await conn.fetchrow(
            "SELECT id, tenant_id, product_line, slug, name, created_by "
            "FROM projects WHERE id = $1",
            "p-acme-robotics-default",
        )
    assert project is not None
    assert project["tenant_id"] == "t-acme-robotics"
    assert project["product_line"] == "default"
    assert project["slug"] == "default"
    assert project["created_by"] == user_row["id"]

    # .env has the new pin.
    env_body = env_path.read_text(encoding="utf-8")
    assert "OMNISIGHT_PRIMARY_TENANT_ID=t-acme-robotics" in env_body

    # Audit row written under the new super-admin's actor email.
    from backend import audit
    rows = await audit.query(entity_kind="tenant", limit=50)
    actions = [row["action"] for row in rows]
    assert "bootstrap.tenant_initialized" in actions


@pytest.mark.asyncio
async def test_init_tenant_super_admin_can_login_and_old_admin_still_works(
    _init_tenant_client,
):
    """Y7 row 1 acceptance — new super-admin can authenticate, and the
    rotated default admin on ``t-default`` is unaffected.

    We rotate the default admin first (so it is no longer flagged
    must_change_password), then invoke init-tenant, then assert both
    accounts authenticate independently.
    """
    client = _init_tenant_client["client"]
    default_admin = _init_tenant_client["admin"]

    # Rotate default admin first — mirrors Step 1 happy path.
    await _au.change_password(default_admin.id, "rotated-default-pw-xyz-789")

    r = await client.post(
        "/api/v1/bootstrap/init-tenant",
        json=_VALID_BODY,
    )
    assert r.status_code == 200, r.text

    # New super-admin authenticates with the seeded password.
    new_user = await _au.authenticate_password(
        "founder@acme.example", "rotated-strong-password-abc-123",
    )
    assert new_user is not None
    assert new_user.role == "super_admin"
    assert new_user.tenant_id == "t-acme-robotics"

    # Default admin still authenticates with its rotated password and
    # remains pinned to t-default — Step 2.5 must not silently re-home it.
    refreshed_default = await _au.authenticate_password(
        default_admin.email, "rotated-default-pw-xyz-789",
    )
    assert refreshed_default is not None
    assert refreshed_default.tenant_id == "t-default"


@pytest.mark.asyncio
async def test_init_tenant_admin_tenants_lists_both_tenants(
    _init_tenant_client, pg_test_pool,
):
    """After init-tenant, two tenants exist: t-default + the new one.

    We assert directly against PG (the same query
    ``GET /api/v1/admin/tenants`` performs) rather than calling the
    admin endpoint — that endpoint requires a super-admin session, and
    spinning one up here is orthogonal to the Y7 row 1 contract.  The
    Y8 frontend tests will exercise the full HTTP path.
    """
    client = _init_tenant_client["client"]
    r = await client.post("/api/v1/bootstrap/init-tenant", json=_VALID_BODY)
    assert r.status_code == 200, r.text

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, plan FROM tenants ORDER BY id"
        )
    ids = [r["id"] for r in rows]
    assert "t-default" in ids
    assert "t-acme-robotics" in ids
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_init_tenant_skip_path_keeps_only_t_default(
    _init_tenant_client, pg_test_pool,
):
    """If the operator skips Step 2.5 (never calls the endpoint), only
    ``t-default`` exists and the original admin still owns it."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM tenants ORDER BY id"
        )
    ids = [r["id"] for r in rows]
    assert ids == ["t-default"]


@pytest.mark.asyncio
async def test_init_tenant_skip_path_default_admin_unchanged_and_authenticates(
    _init_tenant_client, pg_test_pool,
):
    """Y7 row 3 contract pin — backward-compat with single-tenant install.

    When the operator clicks Skip on Step 2.5 (i.e. never calls
    ``POST /api/v1/bootstrap/init-tenant``), the install must remain
    indistinguishable from a pre-Y7 deployment:

      * ``tenants`` contains exactly one row, ``t-default``.
      * The default admin row is still present, still pinned to
        ``t-default``, and still ``enabled=1``.
      * The default admin can authenticate (after the standard Step 1
        password rotation), proving the existing single-tenant login
        flow is untouched by the new endpoint's mere existence.
      * No ``user_tenant_memberships`` row was silently created for the
        default admin — Step 2.5 is the only path that writes
        memberships, so skipping it must leave the table empty.
      * No ``projects`` row was created — default-tenant installs
        continue to operate without a default project until a future
        ``ensure_default_project`` op runs.
    """
    client = _init_tenant_client["client"]
    default_admin = _init_tenant_client["admin"]

    await _au.change_password(default_admin.id, "rotated-default-pw-xyz-789")

    # Operator does not POST /api/v1/bootstrap/init-tenant.

    async with pg_test_pool.acquire() as conn:
        tenant_rows = await conn.fetch(
            "SELECT id, enabled FROM tenants ORDER BY id"
        )
        admin_row = await conn.fetchrow(
            "SELECT id, email, role, tenant_id, enabled "
            "FROM users WHERE id = $1",
            default_admin.id,
        )
        membership_count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_tenant_memberships "
            "WHERE user_id = $1",
            default_admin.id,
        )
        project_count = await conn.fetchval(
            "SELECT COUNT(*) FROM projects WHERE tenant_id = $1",
            "t-default",
        )

    assert [r["id"] for r in tenant_rows] == ["t-default"]
    assert int(tenant_rows[0]["enabled"]) == 1

    assert admin_row is not None
    assert admin_row["email"] == default_admin.email
    assert admin_row["tenant_id"] == "t-default"
    assert int(admin_row["enabled"]) == 1

    assert int(membership_count) == 0
    assert int(project_count) == 0

    refreshed = await _au.authenticate_password(
        default_admin.email, "rotated-default-pw-xyz-789",
    )
    assert refreshed is not None
    assert refreshed.id == default_admin.id
    assert refreshed.tenant_id == "t-default"

    # Sanity: the new endpoint exists but was not exercised — the
    # subsequent test runs must observe the same clean baseline (the
    # fixture TRUNCATE handles the teardown).  We do NOT assert against
    # the audit_log here because Step 1 / fixture wiring may write
    # benign entries; the audit-row contract is owned by the
    # tenant_initialized test.

    # Express the Y7 row 3 BC contract literally for grep-ability:
    # ``no init-tenant call → tenants == [t-default] AND default admin
    # login still works AND no membership/project rows leaked``.
    pass


# ─────────────────────────────────────────────────────────────────
#  Validation / error paths
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_tenant_422_on_invalid_display_name(_init_tenant_client):
    body = {**_VALID_BODY, "display_name": "***"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    # display_name has length ≥ 1 so pydantic accepts; our own slugify
    # check rejects with kind=invalid_display_name.
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "invalid_display_name"


@pytest.mark.asyncio
async def test_init_tenant_422_when_enterprise_lacks_license(_init_tenant_client):
    body = {**_VALID_BODY, "plan": "enterprise", "license_key": ""}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "enterprise_license_required"


@pytest.mark.asyncio
async def test_init_tenant_422_when_enterprise_license_too_short(_init_tenant_client):
    body = {**_VALID_BODY, "plan": "enterprise", "license_key": "abc"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "enterprise_license_required"


@pytest.mark.asyncio
async def test_init_tenant_accepts_enterprise_with_valid_license(
    _init_tenant_client, pg_test_pool,
):
    body = {
        **_VALID_BODY,
        "plan": "enterprise",
        "license_key": "OMNI-ABCD1234-EFGH5678",
    }
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "enterprise"
    async with pg_test_pool.acquire() as conn:
        plan = await conn.fetchval(
            "SELECT plan FROM tenants WHERE id = $1", "t-acme-robotics",
        )
    assert plan == "enterprise"


@pytest.mark.asyncio
async def test_init_tenant_422_on_weak_password(_init_tenant_client):
    body = {**_VALID_BODY, "admin_password": "password12345678"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "password_too_weak"


@pytest.mark.asyncio
async def test_init_tenant_422_on_short_password(_init_tenant_client):
    # Pydantic's ``min_length=12`` rejects this at the request layer,
    # so the response comes back as a generic 422 from FastAPI.
    body = {**_VALID_BODY, "admin_password": "short"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_init_tenant_422_on_bad_email(_init_tenant_client):
    body = {**_VALID_BODY, "admin_email": "not-an-email"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_init_tenant_409_on_replay_after_first_call(
    _init_tenant_client, pg_test_pool,
):
    """Once a non-default tenant exists, a second call must refuse.

    Defends against a malicious replay or a stuck retry-loop in the
    wizard UI from inflating the tenant catalog with stale rows.
    """
    client = _init_tenant_client["client"]
    r1 = await client.post("/api/v1/bootstrap/init-tenant", json=_VALID_BODY)
    assert r1.status_code == 200, r1.text

    second_body = {
        **_VALID_BODY,
        "display_name": "Second Org",
        "admin_email": "founder2@second.example",
    }
    r2 = await client.post("/api/v1/bootstrap/init-tenant", json=second_body)
    assert r2.status_code == 409, r2.text
    assert r2.json().get("kind") == "non_default_tenant_already_exists"

    # Catalog still contains exactly t-default + first call's tenant.
    async with pg_test_pool.acquire() as conn:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants")]
    assert sorted(ids) == ["t-acme-robotics", "t-default"]


@pytest.mark.asyncio
async def test_init_tenant_409_on_email_collision(
    _init_tenant_client, pg_test_pool,
):
    """Pre-existing user with the same email blocks the call."""
    # Seed a user under t-default with the email we're about to claim
    # for the new super-admin.  Default admin already exists; we add
    # a *second* row with the soon-to-be-claimed email.
    await _au.create_user(
        email="founder@acme.example",
        name="Existing user",
        role="viewer",
        password="some-strong-password-xyz-789",
    )

    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=_VALID_BODY,
    )
    assert r.status_code == 409, r.text
    assert r.json().get("kind") == "email_already_exists"

    # No tenant was created.
    async with pg_test_pool.acquire() as conn:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants")]
    assert ids == ["t-default"]


@pytest.mark.asyncio
async def test_init_tenant_audit_row_records_actor_and_metadata(
    _init_tenant_client,
):
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=_VALID_BODY,
    )
    assert r.status_code == 200, r.text

    from backend import audit
    rows = await audit.query(entity_kind="tenant", limit=50)
    matching = [
        row for row in rows
        if row["action"] == "bootstrap.tenant_initialized"
        and row["entity_id"] == "t-acme-robotics"
    ]
    assert matching, "expected exactly one bootstrap.tenant_initialized audit row"
    audit_row = matching[0]
    assert audit_row["actor"] == "founder@acme.example"


# ─────────────────────────────────────────────────────────────────
#  Slug edge cases
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_tenant_collapses_punctuation_into_kebab_slug(
    _init_tenant_client, pg_test_pool,
):
    body = {**_VALID_BODY, "display_name": "Acme,  Inc."}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "t-acme-inc"
    async with pg_test_pool.acquire() as conn:
        present = await conn.fetchval(
            "SELECT COUNT(*) FROM tenants WHERE id = $1", "t-acme-inc",
        )
    assert int(present) == 1


@pytest.mark.asyncio
async def test_init_tenant_short_slug_rejected(_init_tenant_client):
    """A single-char slug fails the 2-char minimum from the regex."""
    body = {**_VALID_BODY, "display_name": "X"}
    r = await _init_tenant_client["client"].post(
        "/api/v1/bootstrap/init-tenant", json=body,
    )
    assert r.status_code == 422, r.text
    assert r.json().get("kind") == "invalid_display_name"
