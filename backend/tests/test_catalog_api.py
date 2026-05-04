"""BS.2.4 — PG-live integration tests for ``backend/routers/catalog.py``.

Scope
─────
~25 cases covering the four BS.2.4 axes:

  1. **CRUD happy-path** — every entries / sources verb returns the
     documented status + payload shape against a real Postgres pool.
  2. **Permission denial** — operator role hits the admin-only writes
     and gets 403 (BS.2.3 spec already locked the dep wiring; this row
     verifies the runtime rejection end-to-end).
  3. **PEP integration** — N/A for the catalog surface; PEP fires on
     the installer router only. test_installer_api.py owns that axis.
     The catalog-side counterpart is the auth-secret-ref guard (no
     plaintext leak through the source-CRUD POST/PATCH bodies) which
     we verify here as the closest equivalent.
  4. **Tenant isolation** — operator/override rows inserted by tenant
     A must NOT appear in tenant B's list/get responses, and shipped
     rows DO appear in both.

Sibling tests intentionally not duplicated here:

* ``test_catalog_router_smoke.py`` owns Pydantic + dep wiring + the
  module-constant alignment to alembic 0051 / 0052.
* ``test_alembic_0051_catalog_tables.py`` owns the DB-side CHECK
  constraints (raw SQL).
* ``test_alembic_0052_catalog_seed.py`` owns the seed-row content.
* ``test_rbac_bs23_matrix.py`` owns the cross-router RBAC matrix +
  per-route dep alignment.

Test environment
────────────────
* Requires ``OMNI_TEST_PG_URL`` set; otherwise every PG-backed test
  in this file is skipped via ``_requires_pg`` (mirrors the pattern
  used by ``test_admin_tenants_create.py`` / ``test_tenant_projects_create.py``).
* Uses the shared ``client`` + ``pg_test_pool`` fixtures from
  ``conftest.py``. ``client`` runs the FastAPI app in open auth mode
  (synthetic anonymous super-admin); RBAC-negative tests override
  ``auth.current_user`` per-request via ``app.dependency_overrides``
  to inject a non-admin role.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure test code. The only mutable state is ``app.dependency_overrides``,
scoped per-test via ``try/finally pop`` (mirrors the pattern in
test_rbac_bs23_matrix.py). The router itself does not introduce any
new module-globals; test setup does not need cross-worker coordination
because PG is the source of truth and the asyncpg pool is already
shared via the conftest fixtures.

Read-after-write timing audit
─────────────────────────────
Every test does a synchronous write → read in a single asyncio task.
PG MVCC plus the asyncpg pool's connection-per-acquire pattern means
the post-commit GET sees the new state. There is no shared in-memory
cache that could lag (the catalog router never caches across requests).
"""

from __future__ import annotations

import os
import secrets

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG availability gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test client fixture — self-contained, avoids the conftest ``client``
#  + ``pg_test_pool`` teardown-ordering bug
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Why a custom client instead of the shared ``client`` fixture from
# conftest.py: that fixture initialises the asyncpg pool itself and on
# teardown does a final ``TRUNCATE bootstrap_state`` via
# ``_db_pool.get_pool()``. When the test combines ``client`` +
# ``pg_test_pool`` (the canonical pattern for "I need HTTP + raw PG
# inspection"), pytest tears down the fixtures in reverse-setup order:
# ``pg_test_pool`` closes the pool first, then ``client``'s finally
# block hits ``get_pool() → RuntimeError`` and pytest reports the test
# as ERROR even though the test body passed. Exit code 1 in CI.
#
# ``bs24_client`` below is a thin AsyncClient wrapper that depends on
# ``pg_test_pool`` (which already owns + closes the pool) and never
# tries to clean up infrastructure on its own — pool lifecycle stays
# entirely with ``pg_test_pool``. Bootstrap is pinned to "finalised" so
# the gate middleware doesn't 503 every request, mirroring the conftest
# ``client`` fixture's pattern but without the teardown footgun.


@pytest.fixture()
async def bs24_client(pg_test_pool, monkeypatch):
    """AsyncClient against the FastAPI app, with pool lifecycle owned
    by ``pg_test_pool``. Bootstrap is pinned to all-green so non-gate
    requests reach the auth dep + the router."""
    from backend.main import app
    from backend import bootstrap as _boot
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    _boot._gate_cache_reset()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — tenant + entry seeding / purging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    """Idempotent ``tenants`` seed — every test seeds its own tid before
    inserting catalog rows that FK-back to it."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"BS24 {tid}",
        )


async def _seed_shipped(pool, entry_id: str, *, family: str = "embedded",
                        vendor: str = "test-vendor",
                        install_method: str = "noop") -> None:
    """Insert a shipped catalog row directly (bypassing the API).

    Used to set up the ``shipped`` base for override / tombstone tests
    when the alembic 0052 seed migration hasn't or can't run in this
    env. The migration is idempotent ``INSERT … ON CONFLICT DO
    NOTHING`` so re-seeding the same id is safe.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO catalog_entries "
            "  (id, source, tenant_id, vendor, family, display_name, "
            "   version, install_method) "
            "VALUES ($1, 'shipped', NULL, $2, $3, $4, '1.0.0', $5) "
            "ON CONFLICT DO NOTHING",
            entry_id, vendor, family, f"Test {entry_id}", install_method,
        )


async def _purge_entry(pool, entry_id: str) -> None:
    """Hard-delete every row with this entry_id across tenants. Tests
    own their entry id namespace so this is safe."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'catalog_entry' "
            "AND entity_id = $1",
            entry_id,
        )
        await conn.execute(
            "DELETE FROM catalog_entries WHERE id = $1", entry_id,
        )


async def _purge_tenant(pool, tid: str) -> None:
    """Drop everything for *tid* — catalog entries + subscriptions +
    audit + tenant. Order matters because of FK CASCADE on tenants."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind IN "
            "  ('catalog_entry', 'catalog_subscription') "
            "  AND entity_id IN ("
            "    SELECT id FROM catalog_entries WHERE tenant_id = $1 "
            "    UNION SELECT id FROM catalog_subscriptions "
            "    WHERE tenant_id = $1"
            "  )",
            tid,
        )
        await conn.execute(
            "DELETE FROM catalog_subscriptions WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM catalog_entries WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


def _override_user_factory(role: str, tenant_id: str = "t-default"):
    """Return a coroutine the dependency override will call to inject
    a fake current_user with the given role + tenant.
    """
    from backend import auth as _au

    fake = _au.User(
        id=f"u-bs24-{role}", email=f"{role}@bs24.test",
        name=f"BS24 {role}", role=role, enabled=True, tenant_id=tenant_id,
    )

    async def _fake() -> _au.User:
        return fake

    return _fake


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Marker — anchors the BS.2.4 row in test reports without PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bs24_marker_module_imports():
    """Sanity: the catalog router still imports cleanly. Catches a
    refactor that breaks the import chain (the entire test module would
    otherwise fail to collect with a confusing trace)."""
    from backend.routers import catalog
    assert hasattr(catalog, "router")
    assert hasattr(catalog, "list_entries")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /catalog/entries — list happy-path + filters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_entries_returns_envelope_shape(bs24_client, pg_test_pool):
    """The list endpoint always returns {items, count, total, limit,
    offset}. Empty result is fine — the assertion is on the envelope."""
    tid = "t-default"
    entry_id = "bs24-list-shape"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.get(
            "/api/v1/catalog/entries",
            params={"q": entry_id, "limit": 5},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        for k in ("items", "count", "total", "limit", "offset"):
            assert k in body, f"missing envelope key: {k}"
        assert body["limit"] == 5
        assert body["offset"] == 0
        assert any(it["id"] == entry_id for it in body["items"])
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_entries_filter_by_family(bs24_client, pg_test_pool):
    """``family=embedded`` returns only embedded rows. We seed a known
    embedded row + a known mobile row and assert the family filter
    excludes the mobile."""
    e1 = "bs24-filter-embedded"
    e2 = "bs24-filter-mobile"
    try:
        await _seed_shipped(pg_test_pool, e1, family="embedded")
        await _seed_shipped(pg_test_pool, e2, family="mobile")
        res = await bs24_client.get(
            "/api/v1/catalog/entries",
            params={"family": "embedded", "limit": 500},
        )
        assert res.status_code == 200, res.text
        ids = {it["id"]: it["family"] for it in res.json()["items"]}
        assert e1 in ids and ids[e1] == "embedded"
        assert e2 not in ids
    finally:
        await _purge_entry(pg_test_pool, e1)
        await _purge_entry(pg_test_pool, e2)


@_requires_pg
async def test_get_entries_filter_unknown_family_422(bs24_client):
    """An ``family`` value outside ENTRY_FAMILIES is 422 — defence in
    depth so the SQL never touches a malformed filter."""
    res = await bs24_client.get(
        "/api/v1/catalog/entries", params={"family": "ufo"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_get_entries_q_ilike_matches_display_name(bs24_client, pg_test_pool):
    """The ``q`` filter does ILIKE on display_name + vendor + id. We seed
    a row whose display_name carries a unique token and verify the
    match."""
    entry_id = "bs24-q-ilike-match"
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO catalog_entries "
            "  (id, source, tenant_id, vendor, family, display_name, "
            "   version, install_method) "
            "VALUES ($1, 'shipped', NULL, 'v', 'embedded', "
            "        'Zebra Sparkle One', '1.0', 'noop') "
            "ON CONFLICT DO NOTHING",
            entry_id,
        )
    try:
        res = await bs24_client.get(
            "/api/v1/catalog/entries", params={"q": "Sparkle"},
        )
        assert res.status_code == 200, res.text
        ids = [it["id"] for it in res.json()["items"]]
        assert entry_id in ids
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_entries_pagination_limit_offset(bs24_client, pg_test_pool):
    """``limit=1`` returns at most 1 item; ``offset`` advances past the
    first row. We seed two rows whose ids sort consecutively to make
    the assertion deterministic."""
    e1 = "bs24-page-aaa"
    e2 = "bs24-page-bbb"
    try:
        await _seed_shipped(pg_test_pool, e1)
        await _seed_shipped(pg_test_pool, e2)
        first = await bs24_client.get(
            "/api/v1/catalog/entries",
            params={"q": "bs24-page", "limit": 1, "offset": 0,
                    "sort": "id", "order": "asc"},
        )
        second = await bs24_client.get(
            "/api/v1/catalog/entries",
            params={"q": "bs24-page", "limit": 1, "offset": 1,
                    "sort": "id", "order": "asc"},
        )
        assert first.status_code == 200 and second.status_code == 200
        f_items = first.json()["items"]
        s_items = second.json()["items"]
        assert len(f_items) == 1 and len(s_items) == 1
        assert f_items[0]["id"] == e1
        assert s_items[0]["id"] == e2
    finally:
        await _purge_entry(pg_test_pool, e1)
        await _purge_entry(pg_test_pool, e2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /catalog/entries/{id} — resolved + raw + 404 paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_entry_404_for_nonexistent(bs24_client):
    """Unknown entry id → 404. The id is well-formed so the route's
    422 guard doesn't fire; the resolver just finds no rows."""
    res = await bs24_client.get("/api/v1/catalog/entries/bs24-does-not-exist")
    assert res.status_code == 404, res.text


@_requires_pg
async def test_get_entry_422_for_invalid_id_pattern(bs24_client):
    """``Foo`` (uppercase) doesn't match the kebab-case pattern → 422
    before any DB access."""
    res = await bs24_client.get("/api/v1/catalog/entries/Foo")
    assert res.status_code == 422, res.text


@_requires_pg
async def test_get_entry_resolved_returns_shipped_when_only_shipped(
    bs24_client, pg_test_pool,
):
    """When only a shipped row exists, the resolved view returns
    ``source='shipped'`` plus the row's columns."""
    entry_id = "bs24-resolve-shipped-only"
    try:
        await _seed_shipped(pg_test_pool, entry_id, vendor="acme",
                            family="software")
        res = await bs24_client.get(f"/api/v1/catalog/entries/{entry_id}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == entry_id
        assert body["source"] == "shipped"
        assert body["vendor"] == "acme"
        assert body["family"] == "software"
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_entry_raw_returns_layers(bs24_client, pg_test_pool):
    """``raw=true`` returns ``{id, layers}`` instead of the merged view —
    admin UI uses this to render the override diff."""
    entry_id = "bs24-resolve-raw"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.get(
            f"/api/v1/catalog/entries/{entry_id}", params={"raw": "true"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == entry_id
        assert isinstance(body["layers"], list)
        assert len(body["layers"]) >= 1
        sources = [layer["source"] for layer in body["layers"]]
        assert "shipped" in sources
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_entry_hidden_tombstone_returns_404(bs24_client, pg_test_pool):
    """A tenant-scoped override row with hidden=TRUE tombstones the
    shipped base for that tenant — the resolved view 404s."""
    entry_id = "bs24-resolve-tombstone"
    tid = "t-default"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO catalog_entries "
                "  (id, source, tenant_id, hidden) "
                "VALUES ($1, 'override', $2, TRUE)",
                entry_id, tid,
            )
        res = await bs24_client.get(f"/api/v1/catalog/entries/{entry_id}")
        assert res.status_code == 404, res.text
    finally:
        await _purge_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /catalog/entries — admin only
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_entry_operator_201(bs24_client, pg_test_pool):
    """Operator-source create succeeds with 201 + persisted row."""
    entry_id = "bs24-post-operator"
    body = {
        "id": entry_id,
        "source": "operator",
        "vendor": "acme",
        "family": "embedded",
        "display_name": "Acme Embedded",
        "version": "1.2.3",
        "install_method": "noop",
        "depends_on": [],
        "metadata": {"channel": "stable"},
    }
    try:
        res = await bs24_client.post("/api/v1/catalog/entries", json=body)
        assert res.status_code == 201, res.text
        out = res.json()
        assert out["id"] == entry_id
        assert out["source"] == "operator"
        assert out["vendor"] == "acme"
        assert out["tenant_id"] == "t-default"
        # Row really exists in PG.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, source, vendor FROM catalog_entries "
                "WHERE id = $1 AND source = 'operator'",
                entry_id,
            )
        assert row is not None
        assert row["vendor"] == "acme"
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_entry_override_requires_shipped_base_404(
    bs24_client, pg_test_pool,
):
    """Override against a non-existent shipped id is 404 — prevents
    phantom-override rows."""
    entry_id = "bs24-post-override-no-base"
    res = await bs24_client.post(
        "/api/v1/catalog/entries",
        json={"id": entry_id, "source": "override",
              "display_name": "Phantom"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_entry_override_succeeds_when_shipped_exists(
    bs24_client, pg_test_pool,
):
    """An override on an existing shipped row creates a tenant-scoped
    overlay row."""
    entry_id = "bs24-post-override-ok"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.post(
            "/api/v1/catalog/entries",
            json={"id": entry_id, "source": "override",
                  "display_name": "Tenant Branding"},
        )
        assert res.status_code == 201, res.text
        out = res.json()
        assert out["source"] == "override"
        assert out["display_name"] == "Tenant Branding"
        assert out["tenant_id"] == "t-default"
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_entry_operator_missing_required_422(bs24_client):
    """Operator rows must carry every required column — missing fields
    → 422."""
    res = await bs24_client.post(
        "/api/v1/catalog/entries",
        json={"id": "bs24-post-incomplete", "source": "operator"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_entry_source_shipped_rejected_422(bs24_client):
    """``source='shipped'`` is not a writable source — Pydantic Literal
    rejects it before reaching the handler."""
    res = await bs24_client.post(
        "/api/v1/catalog/entries",
        json={"id": "bs24-post-shipped", "source": "shipped"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_entry_duplicate_returns_409(bs24_client, pg_test_pool):
    """Two operator-source POSTs with the same (id, tenant) hit the
    partial UNIQUE index ``uq_catalog_entries_visible`` → 409."""
    entry_id = "bs24-post-dup"
    body = {
        "id": entry_id, "source": "operator",
        "vendor": "v", "family": "embedded", "display_name": "D",
        "version": "1.0", "install_method": "noop",
    }
    try:
        first = await bs24_client.post("/api/v1/catalog/entries", json=body)
        assert first.status_code == 201, first.text
        second = await bs24_client.post("/api/v1/catalog/entries", json=body)
        assert second.status_code == 409, second.text
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_entry_operator_role_gets_403(bs24_client):
    """RBAC: BS.2.3 says POST /entries is admin-only. An operator must
    hit 403, NOT 201, even if the body would have been valid."""
    from backend.main import app
    from backend import auth as _au

    app.dependency_overrides[_au.current_user] = (
        _override_user_factory("operator")
    )
    try:
        res = await bs24_client.post(
            "/api/v1/catalog/entries",
            json={"id": "bs24-rbac-operator", "source": "operator",
                  "vendor": "v", "family": "embedded", "display_name": "D",
                  "version": "1.0", "install_method": "noop"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATCH /catalog/entries/{id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_entry_creates_override_over_shipped(
    bs24_client, pg_test_pool,
):
    """PATCH on an entry with no tenant-scoped row yet creates an
    ``override`` overlay row."""
    entry_id = "bs24-patch-creates-override"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.patch(
            f"/api/v1/catalog/entries/{entry_id}",
            json={"display_name": "Tenant Override"},
        )
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["source"] == "override"
        assert out["display_name"] == "Tenant Override"
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_patch_entry_404_for_no_base(bs24_client):
    """PATCH on an entry id with no shipped/operator/override row → 404."""
    res = await bs24_client.patch(
        "/api/v1/catalog/entries/bs24-patch-no-base",
        json={"display_name": "X"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_patch_entry_empty_body_422(bs24_client, pg_test_pool):
    """``has_any_field()`` rejects an all-None body so the handler
    doesn't UPDATE with zero columns."""
    entry_id = "bs24-patch-empty"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.patch(
            f"/api/v1/catalog/entries/{entry_id}", json={},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DELETE /catalog/entries/{id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_entry_creates_tombstone_over_shipped(
    bs24_client, pg_test_pool,
):
    """DELETE on a shipped-only row creates an override row with
    hidden=TRUE — the resolved GET 404s afterwards."""
    entry_id = "bs24-delete-tombstone"
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        gone = await bs24_client.delete(f"/api/v1/catalog/entries/{entry_id}")
        assert gone.status_code == 200, gone.text
        # Verify the resolver hides this from the tenant.
        after = await bs24_client.get(f"/api/v1/catalog/entries/{entry_id}")
        assert after.status_code == 404, after.text
        # And a tombstone row really exists in PG.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT hidden FROM catalog_entries "
                "WHERE id = $1 AND source = 'override' "
                "  AND tenant_id = 't-default'",
                entry_id,
            )
        assert row is not None and row["hidden"] is True
    finally:
        await _purge_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_delete_entry_404_when_no_base(bs24_client):
    """DELETE on a non-existent entry id → 404."""
    res = await bs24_client.delete(
        "/api/v1/catalog/entries/bs24-delete-no-base",
    )
    assert res.status_code == 404, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /catalog/sources CRUD (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_sources_get_post_patch_delete_round_trip(
    bs24_client, pg_test_pool,
):
    """Single happy-path round-trip across every /sources verb so the
    BS.2.4 envelope keeps the test count tight without losing coverage
    of each verb."""
    feed_url = f"https://example.test/feed-{secrets.token_hex(4)}.xml"
    created_id: str | None = None
    try:
        # GET (initially does not include our new feed)
        first_list = await bs24_client.get("/api/v1/catalog/sources")
        assert first_list.status_code == 200, first_list.text
        before_ids = {it["id"] for it in first_list.json()["items"]}

        # POST
        post = await bs24_client.post(
            "/api/v1/catalog/sources",
            json={"feed_url": feed_url, "auth_method": "bearer",
                  "auth_secret_ref": "tenant_secret_a", "enabled": True},
        )
        assert post.status_code == 201, post.text
        created = post.json()
        created_id = created["id"]
        assert created_id.startswith("sub-")
        assert created["feed_url"] == feed_url
        assert created["auth_method"] == "bearer"
        # GET shows the new row
        second_list = await bs24_client.get("/api/v1/catalog/sources")
        ids = {it["id"] for it in second_list.json()["items"]}
        assert created_id in ids
        assert created_id not in before_ids

        # PATCH
        patched = await bs24_client.patch(
            f"/api/v1/catalog/sources/{created_id}",
            json={"enabled": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["enabled"] is False

        # DELETE
        deleted = await bs24_client.delete(
            f"/api/v1/catalog/sources/{created_id}",
        )
        assert deleted.status_code == 200, deleted.text
        # 404 on second delete proves the row is gone.
        again = await bs24_client.delete(
            f"/api/v1/catalog/sources/{created_id}",
        )
        assert again.status_code == 404, again.text
        created_id = None  # already cleaned up
    finally:
        if created_id is not None:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM catalog_subscriptions WHERE id = $1",
                    created_id,
                )


@_requires_pg
async def test_sources_post_duplicate_feed_url_returns_409(
    bs24_client, pg_test_pool,
):
    """Two POSTs for the same (tenant, feed_url) pair hit the alembic
    0051 UNIQUE constraint — second one is 409."""
    feed_url = f"https://example.test/dup-{secrets.token_hex(4)}.xml"
    first_id = None
    try:
        first = await bs24_client.post(
            "/api/v1/catalog/sources",
            json={"feed_url": feed_url, "auth_method": "none"},
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]
        second = await bs24_client.post(
            "/api/v1/catalog/sources",
            json={"feed_url": feed_url, "auth_method": "basic"},
        )
        assert second.status_code == 409, second.text
    finally:
        if first_id is not None:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM catalog_subscriptions WHERE id = $1",
                    first_id,
                )


@_requires_pg
async def test_sources_post_whitespace_secret_ref_rejected_422(bs24_client):
    """The secret-ref field rejects any whitespace — defence against an
    operator pasting the actual bearer token in the field instead of
    the secret-store key. PEP-integration analogue for the catalog
    surface (write-side guard against credential leak into PG)."""
    res = await bs24_client.post(
        "/api/v1/catalog/sources",
        json={"feed_url": "https://example.test/ws.xml",
              "auth_method": "bearer",
              "auth_secret_ref": "looks like a token here"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_sources_get_operator_role_gets_403(bs24_client):
    """RBAC: BS.2.3 says every /sources verb is admin-only. Operator
    on GET /sources → 403."""
    from backend.main import app
    from backend import auth as _au

    app.dependency_overrides[_au.current_user] = (
        _override_user_factory("operator")
    )
    try:
        res = await bs24_client.get("/api/v1/catalog/sources")
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BS.8.5 — POST /catalog/sources/{sub_id}/sync (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_sources_sync_now_stamps_pending_manual(
    bs24_client, pg_test_pool,
):
    """BS.8.5 — POST /catalog/sources/{id}/sync stamps the row's
    last_sync_status to ``pending_manual`` and clears last_synced_at so
    the feed-sync cron worker picks it up on the next tick."""
    feed_url = f"https://example.test/sync-{secrets.token_hex(4)}.xml"
    sub_id: str | None = None
    try:
        post = await bs24_client.post(
            "/api/v1/catalog/sources",
            json={"feed_url": feed_url, "auth_method": "none"},
        )
        assert post.status_code == 201, post.text
        sub_id = post.json()["id"]

        # Set last_synced_at + last_sync_status so we can prove the
        # sync route clears one and stamps the other.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE catalog_subscriptions "
                "SET last_synced_at = now(), last_sync_status = 'ok' "
                "WHERE id = $1",
                sub_id,
            )

        res = await bs24_client.post(
            f"/api/v1/catalog/sources/{sub_id}/sync",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == sub_id
        assert body["last_sync_status"] == "pending_manual"
        assert body["last_synced_at"] is None
    finally:
        if sub_id is not None:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM catalog_subscriptions WHERE id = $1", sub_id,
                )


@_requires_pg
async def test_sources_sync_now_404_for_unknown_sub_id(bs24_client):
    """BS.8.5 — Unknown sub_id (or wrong tenant) → 404."""
    res = await bs24_client.post(
        "/api/v1/catalog/sources/sub-deadbeefdeadbeef/sync",
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_sources_sync_now_operator_role_gets_403(bs24_client):
    """BS.8.5 — Sync is admin-only like every other /sources verb.
    Operator role hitting POST /sources/{id}/sync → 403."""
    from backend.main import app
    from backend import auth as _au

    app.dependency_overrides[_au.current_user] = (
        _override_user_factory("operator")
    )
    try:
        res = await bs24_client.post(
            "/api/v1/catalog/sources/sub-anything/sync",
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_tenant_isolation_operator_rows_not_visible_across_tenants(
    bs24_client, pg_test_pool,
):
    """An ``operator`` row inserted by tenant A is invisible to tenant
    B's GET /entries list — but a shipped row is visible to both. We
    inject a fake current_user with each tenant in turn via the
    dependency override.

    The asyncpg pool is shared across the two requests; the only thing
    distinguishing them is ``set_tenant_id`` (called by the router from
    the injected user) plus the SQL predicate
    ``tenant_id IS NULL OR tenant_id=$1``. This test is exactly the
    drift gate for that predicate.
    """
    from backend.main import app
    from backend import auth as _au

    tid_a = "t-bs24-iso-a"
    tid_b = "t-bs24-iso-b"
    op_id = "bs24-iso-operator-only"
    shipped_id = "bs24-iso-shipped-shared"
    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        # Shipped row is global — both tenants should see it.
        await _seed_shipped(pg_test_pool, shipped_id)
        # Operator row scoped to tenant A.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO catalog_entries "
                "  (id, source, tenant_id, vendor, family, display_name, "
                "   version, install_method) "
                "VALUES ($1, 'operator', $2, 'v', 'embedded', 'A only', "
                "        '1.0', 'noop')",
                op_id, tid_a,
            )

        # Tenant A — sees both rows.
        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("admin", tenant_id=tid_a)
        )
        try:
            a_list = await bs24_client.get(
                "/api/v1/catalog/entries",
                params={"q": "bs24-iso", "limit": 500},
            )
            assert a_list.status_code == 200
            a_ids = {it["id"] for it in a_list.json()["items"]}
            assert op_id in a_ids
            assert shipped_id in a_ids
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Tenant B — sees shipped but NOT the operator row.
        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("admin", tenant_id=tid_b)
        )
        try:
            b_list = await bs24_client.get(
                "/api/v1/catalog/entries",
                params={"q": "bs24-iso", "limit": 500},
            )
            assert b_list.status_code == 200
            b_ids = {it["id"] for it in b_list.json()["items"]}
            assert shipped_id in b_ids, "shipped row must be cross-tenant"
            assert op_id not in b_ids, (
                "operator row leaked across tenants — "
                f"BS.2.4 isolation contract broken; got {b_ids}"
            )
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_entry(pg_test_pool, op_id)
        await _purge_entry(pg_test_pool, shipped_id)
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint guard — pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """SOP Step 3 pattern: this file MUST NOT contain any of the four
    compat-era SQL fingerprints. Catches accidental copy-paste from
    legacy test code."""
    import pathlib
    import re

    path = pathlib.Path(__file__)
    text = path.read_text(encoding="utf-8")
    body = text.split("def test_self_fingerprint_clean")[0]
    forbidden = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = forbidden.findall(body)
    assert not hits, f"Compat fingerprint(s) found in test body: {hits}"
