"""Y2 (#278) row 6 — cross-cutting CRUD safety contract.

Each existing per-row test file (POST / LIST / DETAIL / PATCH / DELETE)
asserts its own slice of the safety contract in isolation. Row 6 is
the cross-cutting consolidation that survives per-endpoint drift: if
someone copy-pastes a sibling endpoint and accidentally swaps
``require_admin`` for ``require_super_admin``, or strips the id-format
validator from one handler but leaves it in the others, or carves out
``t-default`` protection only on DELETE while leaving POST able to
re-create it — the row-6 family fails first.

Concretely, this file consolidates four families that previously lived
as scattered per-row sentinels:

  1. **RBAC family** — every admin tenant route in ``app.routes`` is
     enumerated dynamically; each one is exercised under a
     tenant-admin (``role='admin'``) override and must 403. Drift
     guard: a future admin endpoint added without
     ``Depends(require_super_admin)`` fails this whole family.

  2. **Tenant-id format validator family** — the same set of malformed
     id samples is fed to every path-param endpoint (GET detail /
     PATCH / DELETE) and POST; each must return 422 *before* any DB
     hit. Drift guard: catches any endpoint that forgets the
     ``_is_valid_tenant_id`` belt-and-braces check.

  3. **Plan-downgrade quota matrix** — full Cartesian product of
     (current_plan, requested_plan) ∈ ``VALID_PLANS × VALID_PLANS``
     under a forced "tenant uses 1.5 × current_hard" disk
     measurement. Asserts:
       - downgrade attempts (rank(req) < rank(cur)) over-quota → 409
         + row untouched + no ``tenant_updated`` audit row written
       - upgrade attempts (rank(req) > rank(cur)) → 200 even with
         "over current hard" disk usage (the new hard is bigger)
       - no-op (rank(req) == rank(cur)) → 200, disk walk skipped

  4. **t-default cross-endpoint contract** — codifies how the seeded
     ``t-default`` row interacts with each CRUD operation:
       - POST t-default → 409 (already exists, can't be re-created)
       - PATCH t-default → 200 (rename allowed; the protection is
         scoped to deletion only)
       - DELETE t-default → 403 (PROTECTED_TENANT_IDS hard block,
         even with valid confirm)
       - PROTECTED_TENANT_IDS frozen + literal contents

The test file deliberately avoids mocking the route table — it walks
``app.routes`` so a future-added endpoint under ``/api/v1/admin/tenants``
gets included automatically. This is the same drift-guard pattern used
by ``test_admin_tenants_delete.py::_scan_db_py_for_tenant_id_tables``
and ``test_require_super_admin.py::test_admin_tenants_handlers_use_super_admin``.

Module-global state (SOP Step 1)
────────────────────────────────
None introduced. The tests read module constants
(``PROTECTED_TENANT_IDS`` / ``TENANT_ID_PATTERN`` / ``VALID_PLANS``)
which are immutable; the HTTP-path tests use the per-test ``client``
fixture (function-scoped) and a per-test tenant id so concurrent
``pytest -n auto`` runs cannot collide. The ``_measure_disk_safely``
monkeypatch is scoped to a single test and reverted by pytest's
``monkeypatch`` fixture teardown.

Read-after-write timing (SOP Step 1)
────────────────────────────────────
PATCH plan-downgrade tests assert "row was not mutated" *after* the
409 response. The handler holds a single asyncpg connection across
the FETCH-then-UPDATE; by the time the 409 returns, the UPDATE has
not been issued (the disk-quota guard runs *before* the UPDATE). No
new write path or pool-vs-compat timing assumption introduced.

Pre-commit fingerprint (SOP Step 3)
───────────────────────────────────
Pure test file; no SQL constants added. ``grep -nE
"_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"
backend/tests/test_admin_tenants_row6_security.py`` → 0 hits.
"""

from __future__ import annotations

import os
import re
from typing import Iterator

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: policy constants — frozen shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_protected_tenant_ids_is_frozen_and_contains_only_default():
    """``PROTECTED_TENANT_IDS`` is a ``frozenset`` so a stray
    ``.add(...)`` from elsewhere in the codebase cannot widen the
    blocklist at runtime. The literal contents are pinned to
    ``{'t-default'}`` — adding a new protected tenant should be a
    deliberate code change reviewed via this test, not silent drift.
    """
    from backend.routers.admin_tenants import PROTECTED_TENANT_IDS
    assert isinstance(PROTECTED_TENANT_IDS, frozenset), (
        "PROTECTED_TENANT_IDS must be a frozenset to prevent "
        "runtime mutation"
    )
    assert PROTECTED_TENANT_IDS == frozenset({"t-default"}), (
        f"PROTECTED_TENANT_IDS contents drifted: {PROTECTED_TENANT_IDS!r}"
    )


def test_tenant_id_pattern_is_literal_spec_value():
    """The literal regex string is the source of truth for the
    ``id`` field validator on POST + the ``_is_valid_tenant_id``
    helper used by GET / PATCH / DELETE. Drift in the literal
    means a Pydantic-layer accept can disagree with a handler-layer
    reject (or vice versa)."""
    from backend.routers.admin_tenants import TENANT_ID_PATTERN
    assert TENANT_ID_PATTERN == r"^t-[a-z0-9][a-z0-9-]{2,62}$"


def test_valid_plans_matches_plan_disk_quotas_keys():
    """``VALID_PLANS`` is derived from ``PLAN_DISK_QUOTAS.keys()`` —
    if someone adds a plan to the quota table without thinking about
    the API surface, the derived tuple covers them automatically.
    But we still pin the literal so a *removal* (or order change)
    is loud."""
    from backend.routers.admin_tenants import VALID_PLANS
    from backend.tenant_quota import PLAN_DISK_QUOTAS
    assert VALID_PLANS == tuple(PLAN_DISK_QUOTAS.keys())
    # Lock the canonical four plans — Y2 spec literal.
    assert set(VALID_PLANS) == {"free", "starter", "pro", "enterprise"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: cross-CRUD RBAC drift guard — every admin tenant route
#  must depend on require_super_admin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _admin_tenant_routes() -> list[tuple[str, str, object]]:
    """Walk ``app.routes`` and return every route whose path starts
    with ``/api/v1/admin/tenants``. Each entry is ``(method, path,
    endpoint_callable)``. Used as the source-of-truth for the
    cross-CRUD RBAC family below — a future endpoint added under the
    same prefix gets exercised automatically.
    """
    from backend.main import app
    out: list[tuple[str, str, object]] = []
    for r in app.routes:
        path = getattr(r, "path", "") or ""
        if not path.startswith("/api/v1/admin/tenants"):
            continue
        endpoint = getattr(r, "endpoint", None)
        if endpoint is None:
            continue
        for method in sorted(getattr(r, "methods", set()) or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((method, path, endpoint))
    return out


def test_at_least_five_admin_tenant_routes_exist():
    """Sanity: row 1-5 ship POST / GET-list / GET-detail / PATCH /
    DELETE — five method+path combinations. If this drops below five
    something has been removed without updating row 6 expectations.
    """
    routes = _admin_tenant_routes()
    methods_seen = {m for m, _p, _e in routes}
    assert {"POST", "GET", "PATCH", "DELETE"}.issubset(methods_seen), (
        f"missing one of POST/GET/PATCH/DELETE under "
        f"/api/v1/admin/tenants: {sorted(methods_seen)!r}"
    )
    assert len(routes) >= 5, (
        f"expected ≥5 admin tenant routes, got {len(routes)}: "
        f"{[(m, p) for m, p, _ in routes]!r}"
    )


def test_every_admin_tenant_handler_depends_on_super_admin():
    """Cross-CRUD drift guard: any future endpoint added under
    ``/api/v1/admin/tenants`` must depend on ``require_super_admin``.
    Catches the classic copy-paste regression where a sibling
    endpoint inherits ``Depends(require_admin)`` and silently
    downgrades platform-tier protection to tenant-tier.
    """
    import inspect
    from backend import auth

    routes = _admin_tenant_routes()
    bad: list[tuple[str, str, list]] = []
    for method, path, endpoint in routes:
        sig = inspect.signature(endpoint)
        deps = []
        for _name, param in sig.parameters.items():
            target = getattr(param.default, "dependency", None)
            if target is not None:
                deps.append(target)
        if auth.require_super_admin not in deps:
            bad.append((method, path, deps))
    assert not bad, (
        "the following admin tenant routes do NOT depend on "
        f"auth.require_super_admin (RBAC drift): {bad!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: id-validator coverage — every malformed sample must be
#  rejected by the helper used by GET / PATCH / DELETE handlers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MALFORMED_TENANT_IDS = (
    "T-UPPERCASE",          # uppercase in prefix
    "t-Acme",               # uppercase mid-id
    "no-prefix-xyz",        # missing 't-' prefix
    "t-",                   # too short
    "t-a",                  # only 1 char after 't-' (need 1 lead + ≥2 trail)
    "t-ab",                 # 0 trailing chars (need ≥2)
    "t--double",            # leading char in trail section is '-'
    "t-acme_corp",          # underscore not in charset
    "t-acme.corp",          # dot not in charset
    "t-acme/corp",          # slash not in charset (also URL-meaningful)
    "t-z" + "z" * 63,       # trailing section overflows max-62
)


@pytest.mark.parametrize("bad_id", _MALFORMED_TENANT_IDS)
def test_is_valid_tenant_id_rejects_every_malformed_sample(bad_id):
    """The same malformed-id list will be fed to GET /{id}, PATCH /{id},
    and DELETE /{id} in the HTTP family below. Asserting them at the
    helper level first means a regex regression manifests as a single
    pure-unit failure rather than 30+ HTTP failures."""
    from backend.routers.admin_tenants import _is_valid_tenant_id
    assert not _is_valid_tenant_id(bad_id), (
        f"_is_valid_tenant_id should reject {bad_id!r}"
    )


def test_is_valid_tenant_id_accepts_t_default():
    """The seeded ``t-default`` tenant id must remain valid under
    the regex — the protection on ``t-default`` is policy-level
    (PROTECTED_TENANT_IDS), not validator-level."""
    from backend.routers.admin_tenants import _is_valid_tenant_id
    assert _is_valid_tenant_id("t-default")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: plan-downgrade matrix shape — every (cur, req) pair the
#  handler may see is exhaustively enumerated below
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _plan_rank() -> dict[str, int]:
    """Position-in-tuple = rank (free=0, …, enterprise=3). Mirrors
    the order ``PLAN_DISK_QUOTAS`` declares so a future re-order is
    surfaced by the matrix tests below."""
    from backend.tenant_quota import PLAN_DISK_QUOTAS
    return {p: i for i, p in enumerate(PLAN_DISK_QUOTAS.keys())}


def _all_plan_pairs() -> Iterator[tuple[str, str]]:
    """Cartesian product (cur, req) over every plan combination —
    16 pairs for 4 plans. Includes (cur, cur) no-ops; the handler
    short-circuits the disk-walk on those."""
    from backend.tenant_quota import PLAN_DISK_QUOTAS
    plans = list(PLAN_DISK_QUOTAS.keys())
    for cur in plans:
        for req in plans:
            yield (cur, req)


def test_plan_pairs_matrix_is_complete():
    """Sanity: 4 plans → 16 (cur, req) pairs. If this drifts the
    parametrised tests below have lost coverage."""
    pairs = list(_all_plan_pairs())
    assert len(pairs) == 16, f"expected 16 plan pairs, got {len(pairs)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant admin must 403 on every CRUD endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# (method, url-template, json-body-or-None) tuples. Each tuple is
# exercised under a tenant-admin override and must 403. The id
# placeholder is filled with a real seeded tenant per test so the
# 403 fires at the dependency layer, not at id-not-found.
_RBAC_ENDPOINT_INVOCATIONS: tuple[tuple[str, str, dict | None], ...] = (
    ("POST",   "/api/v1/admin/tenants",
        {"id": "t-rbac-newcomer", "name": "Hostile Create"}),
    ("GET",    "/api/v1/admin/tenants",                None),
    ("GET",    "/api/v1/admin/tenants/{tid}",          None),
    ("PATCH",  "/api/v1/admin/tenants/{tid}",
        {"name": "Hostile Rename"}),
    ("DELETE", "/api/v1/admin/tenants/{tid}?confirm={tid}",
        None),
)


@_requires_pg
@pytest.mark.parametrize(
    "method,url_tpl,body", _RBAC_ENDPOINT_INVOCATIONS,
    ids=[
        "POST_create", "GET_list", "GET_detail",
        "PATCH_update", "DELETE_cascade",
    ],
)
async def test_tenant_admin_gets_403_on_every_crud_endpoint(
    client, pg_test_pool, method, url_tpl, body,
):
    """Cross-CRUD RBAC: a tenant-admin (role='admin', NOT
    super_admin) must be refused at the ``require_super_admin``
    gate, regardless of which CRUD verb / path they call. We
    override BOTH ``current_user`` (to install the tenant-admin
    identity) and ``require_super_admin`` (to bypass session/CSRF
    setup that the open-mode test client doesn't install) — the
    override emulates the prod deny-path: HTTP 403.

    The test seeds a real tenant when the URL contains ``{tid}``
    so the 403 is forced at the dependency layer rather than
    coincidentally matching at the 404-not-found layer.
    """
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    needs_seed = "{tid}" in url_tpl
    tid = "t-rbac-y2-row6" if needs_seed else None
    if needs_seed:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'RBAC Seed', 'free', 1) "
                "ON CONFLICT (id) DO NOTHING",
                tid,
            )

    tenant_admin = _au.User(
        id="u-tadmin-row6", email="tadmin-row6@acme.local",
        name="Tenant Admin Row6", role="admin", enabled=True,
        tenant_id=tid or "t-default",
    )

    async def _fake_current_user():
        return tenant_admin

    def _deny():
        raise HTTPException(
            status_code=403,
            detail="Requires role=super_admin or higher (you are admin)",
        )

    app.dependency_overrides[_au.current_user] = _fake_current_user
    app.dependency_overrides[_au.require_super_admin] = _deny
    try:
        url = url_tpl.format(tid=tid) if needs_seed else url_tpl
        if method == "POST":
            res = await client.post(url, json=body)
        elif method == "GET":
            res = await client.get(url)
        elif method == "PATCH":
            res = await client.patch(url, json=body or {})
        elif method == "DELETE":
            res = await client.delete(url)
        else:  # pragma: no cover — _RBAC_ENDPOINT_INVOCATIONS pinned
            pytest.fail(f"unexpected method in fixture: {method!r}")
        assert res.status_code == 403, (
            f"{method} {url} should be 403 for tenant-admin; "
            f"got {res.status_code}: {res.text}"
        )
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)
        if needs_seed:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM audit_log WHERE tenant_id = $1", tid,
                )
                await conn.execute(
                    "DELETE FROM tenants WHERE id = $1", tid,
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — id-format validator across every endpoint that takes
#  a tenant id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Subset of the malformed id list — the broader negative cases are
# covered by the pure-unit ``_is_valid_tenant_id`` test above. Here
# we only need a few representative samples per HTTP path to verify
# that the handler-layer guard fires for each endpoint.
_HTTP_MALFORMED_SAMPLES = ("T-UPPERCASE", "no-prefix-xyz", "t-acme_corp")


@_requires_pg
@pytest.mark.parametrize("bad_id", _HTTP_MALFORMED_SAMPLES)
async def test_get_tenant_detail_rejects_malformed_id_with_422(
    client, bad_id,
):
    res = await client.get(f"/api/v1/admin/tenants/{bad_id}")
    assert res.status_code == 422, (
        f"GET /admin/tenants/{bad_id!r} should 422; got {res.status_code}: "
        f"{res.text}"
    )


@_requires_pg
@pytest.mark.parametrize("bad_id", _HTTP_MALFORMED_SAMPLES)
async def test_patch_tenant_rejects_malformed_id_with_422(client, bad_id):
    res = await client.patch(
        f"/api/v1/admin/tenants/{bad_id}",
        json={"name": "would-be-rename"},
    )
    assert res.status_code == 422, (
        f"PATCH /admin/tenants/{bad_id!r} should 422; got {res.status_code}: "
        f"{res.text}"
    )


@_requires_pg
@pytest.mark.parametrize("bad_id", _HTTP_MALFORMED_SAMPLES)
async def test_delete_tenant_rejects_malformed_id_with_422(client, bad_id):
    """Delete requires both id-format validity AND a matching
    ``?confirm=``; we send the matching confirm so the failure is
    unambiguously the id-format guard, not the handshake."""
    res = await client.delete(
        f"/api/v1/admin/tenants/{bad_id}?confirm={bad_id}",
    )
    assert res.status_code == 422, (
        f"DELETE /admin/tenants/{bad_id!r} should 422; got "
        f"{res.status_code}: {res.text}"
    )


@_requires_pg
@pytest.mark.parametrize("bad_id", _HTTP_MALFORMED_SAMPLES)
async def test_post_tenant_rejects_malformed_id_with_422(client, bad_id):
    """POST validates the id at the Pydantic layer (regex pattern on
    the body field). Same malformed samples must be rejected here
    too — drift between the body validator and the path validator
    would mean a malformed id can be created via POST but not
    addressed via GET / PATCH / DELETE afterwards."""
    res = await client.post(
        "/api/v1/admin/tenants",
        json={"id": bad_id, "name": "Malformed Acme"},
    )
    assert res.status_code == 422, (
        f"POST /admin/tenants with id={bad_id!r} should 422; got "
        f"{res.status_code}: {res.text}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — t-default cross-endpoint contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_tenant_cannot_recreate_t_default(client):
    """``t-default`` is seeded by migration 0012. POST attempting to
    re-create it must collide on the unique-id constraint and return
    409 — the validator accepts the well-formed id, but the row
    already exists."""
    res = await client.post(
        "/api/v1/admin/tenants",
        json={
            "id": "t-default", "name": "Hostile Takeover",
            "plan": "enterprise", "enabled": True,
        },
    )
    assert res.status_code == 409, res.text
    assert "already exists" in res.json()["detail"].lower()


@_requires_pg
async def test_delete_tenant_cannot_remove_t_default(client):
    """Even with a valid confirm handshake, ``t-default`` is rejected
    at the PROTECTED_TENANT_IDS guard with 403 (not 404) so the
    operator sees the policy reason."""
    res = await client.delete(
        "/api/v1/admin/tenants/t-default?confirm=t-default",
    )
    assert res.status_code == 403, res.text
    body = res.json()
    assert body["tenant_id"] == "t-default"
    assert "protected" in body["detail"].lower()


@_requires_pg
async def test_patch_tenant_can_rename_t_default(client, pg_test_pool):
    """The protection on ``t-default`` is scoped to *deletion* — the
    Y2 spec explicitly allows renaming (operator may want to
    customise the platform tenant's display name). This test pins
    that boundary so a future "tighten t-default protection" sweep
    doesn't accidentally widen the rule beyond DELETE.

    Restores the original name in the cleanup so subsequent tests
    that introspect ``t-default`` don't see a polluted display name.
    """
    async with pg_test_pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT name FROM tenants WHERE id = 't-default'"
        )
    assert original is not None, (
        "t-default must be seeded; run alembic upgrade head"
    )
    original_name = original["name"]

    try:
        res = await client.patch(
            "/api/v1/admin/tenants/t-default",
            json={"name": "Default Tenant (renamed)"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["name"] == "Default Tenant (renamed)"
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name FROM tenants WHERE id = 't-default'"
            )
        assert row["name"] == "Default Tenant (renamed)"
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenants SET name = $1 WHERE id = 't-default'",
                original_name,
            )
            # Trim the audit rows the rename emitted so per-tenant
            # event counts in unrelated tests are not inflated.
            await conn.execute(
                "DELETE FROM audit_log "
                "WHERE entity_kind = 'tenant' "
                "  AND entity_id = 't-default' "
                "  AND action = 'tenant_updated'"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — plan-downgrade quota matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _is_downgrade(cur: str, req: str) -> bool:
    return _plan_rank()[req] < _plan_rank()[cur]


def _is_upgrade(cur: str, req: str) -> bool:
    return _plan_rank()[req] > _plan_rank()[cur]


@_requires_pg
@pytest.mark.parametrize(
    "cur,req", [(c, r) for c, r in _all_plan_pairs() if c != r and
                _plan_rank()[r] < _plan_rank()[c]],
)
async def test_plan_downgrade_over_quota_returns_409_and_does_not_mutate(
    client, pg_test_pool, monkeypatch, cur, req,
):
    """For every (cur → req) downgrade pair where the live disk
    measurement exceeds ``PLAN_DISK_QUOTAS[req].hard_bytes``, the
    PATCH must:

      * return 409 with the standard payload shape
      * leave the ``tenants`` row UNTOUCHED (plan, name, enabled
        all unchanged)
      * NOT emit a ``tenant_updated`` audit row (the doomed-
        downgrade guard runs *before* the UPDATE so no audit
        bridge is written)

    We force the disk-usage signal by monkey-patching
    ``_measure_disk_safely`` to claim ``1.5 × current_hard`` —
    that is always > new_hard for any downgrade pair (because
    new_hard < current_hard by definition of "downgrade").
    """
    from backend.routers import admin_tenants as mod
    from backend.tenant_quota import PLAN_DISK_QUOTAS

    cur_hard = PLAN_DISK_QUOTAS[cur].hard_bytes
    new_hard = PLAN_DISK_QUOTAS[req].hard_bytes
    fake_used = cur_hard + (cur_hard // 2)  # 1.5 × current_hard
    # Sanity for matrix construction: the forced usage must exceed
    # the requested plan's hard_bytes for the 409 branch to fire.
    assert fake_used > new_hard, (
        f"matrix construction error: fake_used {fake_used} not > "
        f"new_hard {new_hard} for {cur}->{req}"
    )

    # Tenant id is unique per pair so concurrent xdist runs don't
    # collide and so cleanup is scoped to this test.
    tid = f"t-row6-down-{cur}-to-{req}"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, $2, $3, 1) "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Row6 {cur}->{req}", cur,
            )

        def _fake_measure(t):
            assert t == tid, (
                f"disk measurement fired against wrong tid: {t!r}"
            )
            return fake_used
        monkeypatch.setattr(mod, "_measure_disk_safely", _fake_measure)

        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": req},
        )
        assert res.status_code == 409, (
            f"{cur}->{req} should be 409; got {res.status_code}: {res.text}"
        )
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["current_plan"] == cur
        assert body["requested_plan"] == req
        assert body["disk_used_bytes"] == fake_used
        assert body["new_hard_bytes"] == new_hard

        # Critical no-mutation contract: row must still be at the
        # original plan / name / enabled.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, plan, enabled FROM tenants WHERE id = $1",
                tid,
            )
        assert row is not None
        assert row["plan"] == cur, (
            f"row plan mutated despite 409; was {cur}, became {row['plan']!r}"
        )
        assert row["name"] == f"Row6 {cur}->{req}"
        assert row["enabled"] == 1

        # Audit row must NOT have been written for this attempt.
        async with pg_test_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_updated' AND entity_id = $1",
                tid,
            )
        assert int(n) == 0, (
            f"refused downgrade emitted {n} audit row(s) — should be 0"
        )
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
@pytest.mark.parametrize(
    "cur,req", [(c, r) for c, r in _all_plan_pairs() if
                _plan_rank()[r] > _plan_rank()[c]],
)
async def test_plan_upgrade_succeeds_even_when_over_current_hard(
    client, pg_test_pool, monkeypatch, cur, req,
):
    """Upgrades (rank(req) > rank(cur)) must succeed regardless of
    current disk usage — by definition the new plan's hard_bytes is
    larger, so "over current_hard" cannot be "over new_hard".

    Forced disk usage = ``1.5 × current_hard``; new_hard > current_hard
    so the guard does not fire. This pins the contract that the disk
    walk happens but its outcome can never block an upgrade.
    """
    from backend.routers import admin_tenants as mod
    from backend.tenant_quota import PLAN_DISK_QUOTAS

    cur_hard = PLAN_DISK_QUOTAS[cur].hard_bytes
    fake_used = cur_hard + (cur_hard // 2)

    tid = f"t-row6-up-{cur}-to-{req}"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, $2, $3, 1) "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Row6 up {cur}->{req}", cur,
            )

        monkeypatch.setattr(
            mod, "_measure_disk_safely", lambda t: fake_used,
        )

        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": req},
        )
        assert res.status_code == 200, (
            f"upgrade {cur}->{req} should be 200; got "
            f"{res.status_code}: {res.text}"
        )
        assert res.json()["plan"] == req

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT plan FROM tenants WHERE id = $1", tid,
            )
        assert row["plan"] == req
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
@pytest.mark.parametrize(
    "plan", ["free", "starter", "pro", "enterprise"],
)
async def test_plan_no_op_skips_disk_check(
    client, pg_test_pool, monkeypatch, plan,
):
    """If ``body.plan == cur_row.plan`` the handler skips the disk
    walk entirely (handler shortcut: ``body.plan != cur_row['plan']``
    gates the I/O). Pin this by setting ``_measure_disk_safely`` to
    a sentinel that fails the test if called — a same-plan PATCH
    must NEVER hit the filesystem.
    """
    from backend.routers import admin_tenants as mod

    tid = f"t-row6-noop-{plan}"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, $2, $3, 1) "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Row6 noop {plan}", plan,
            )

        called: list[str] = []
        def _trip(t):
            called.append(t)
            return 0
        monkeypatch.setattr(mod, "_measure_disk_safely", _trip)

        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": plan},
        )
        assert res.status_code == 200, res.text
        assert res.json()["plan"] == plan
        assert called == [], (
            f"same-plan PATCH must skip the disk walk; got calls "
            f"{called!r}"
        )
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
async def test_plan_downgrade_under_quota_succeeds(
    client, pg_test_pool, monkeypatch,
):
    """Counter-test to the over-quota matrix: same downgrade path
    but with a forced usage that fits under the new hard_bytes
    succeeds with 200. Pin the boundary case ``disk_used == new_hard``
    so the inequality is correctly ``>`` (not ``>=``)."""
    from backend.routers import admin_tenants as mod
    from backend.tenant_quota import PLAN_DISK_QUOTAS

    new_hard = PLAN_DISK_QUOTAS["free"].hard_bytes

    tid = "t-row6-down-boundary"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Boundary Acme', 'pro', 1) "
                "ON CONFLICT (id) DO NOTHING",
                tid,
            )

        # Equal-to-hard must succeed (the handler condition is
        # ``disk_used > new_quota.hard_bytes``, strict inequality).
        monkeypatch.setattr(
            mod, "_measure_disk_safely", lambda t: new_hard,
        )

        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "free"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["plan"] == "free"
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_this_test_file_is_fingerprint_clean():
    """SOP Step-3 fingerprint grep on this file — the same four
    compat-residue patterns checked on every prod SQL constant.
    Even though this is a test file (no SQL), pinning the grep
    here means a future copy-paste from a SQLite-era helper
    cannot land here unnoticed.
    """
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    src = open(__file__, "r", encoding="utf-8").read()
    matches = fingerprint.findall(src)
    assert not matches, (
        f"row-6 test file contains compat-residue fingerprint: {matches!r}"
    )
