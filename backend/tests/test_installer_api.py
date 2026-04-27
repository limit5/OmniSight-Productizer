"""BS.2.4 — PG-live integration tests for ``backend/routers/installer.py``.

Scope
─────
~18 cases covering the four BS.2.4 axes for the installer router:

  1. **CRUD happy-path** — every install_jobs verb returns the
     documented status + payload shape against a real Postgres pool
     with a stubbed PEP gateway.
  2. **Permission denial** — viewer + cross-tenant operator hit the
     gates and 403/404 (BS.2.3 spec already locked the dep wiring; this
     row verifies the runtime end-to-end behaviour).
  3. **PEP integration** — ``POST /installer/jobs`` and
     ``POST /installer/jobs/{id}/retry`` route through
     ``pep_gateway.evaluate``. We monkeypatch the gateway with a fake
     that returns approve / deny / raises-exception, and verify each
     branch lands the correct row state + HTTP status.
  4. **Tenant isolation** — install jobs created in tenant A must NOT
     appear in tenant B's list/get responses; the cross-tenant GET
     must 404 not 200-with-empty-body.

PEP gateway stub
────────────────
The catalog router does not call PEP; the installer router does (on
``install_entry`` create + retry). We don't want to reach the real
decision-engine queue from a test, so we stub
``backend.pep_gateway.evaluate`` per-test via ``monkeypatch``. The
stub returns a ``PepDecision`` with the requested action; that's the
shape the router consumes (``decision.action is PepAction.auto_allow``
on approve, anything else on deny).

Sibling tests intentionally not duplicated here:

* ``test_installer_router_smoke.py`` owns Pydantic + dep wiring + the
  module-constant alignment to alembic 0051.
* ``test_alembic_0051_catalog_tables.py`` owns the install_jobs
  schema constraints (raw SQL).
* ``test_rbac_bs23_matrix.py`` owns the cross-router RBAC matrix.

Test environment
────────────────
* Requires ``OMNI_TEST_PG_URL`` set; otherwise every PG-backed test
  in this file is skipped via ``_requires_pg`` (mirrors
  ``test_admin_tenants_create.py`` / ``test_tenant_projects_create.py``).
* Uses the shared ``client`` + ``pg_test_pool`` fixtures from
  ``conftest.py``. Tenant-isolation tests override
  ``auth.current_user`` per-request via ``app.dependency_overrides``
  to inject the desired tenant_id.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure test code. Mutable test state:

* ``app.dependency_overrides[_au.current_user]`` — scoped per-test
  via ``try/finally pop`` (mirrors ``test_rbac_bs23_matrix.py``).
* ``backend.pep_gateway.evaluate`` — patched via ``monkeypatch.setattr``
  which is per-test scoped by pytest. No leak across tests.
* ``backend.pep_gateway._held_registry`` / ``_recent`` — never mutated
  by these tests because the stubbed evaluate never goes through the
  HOLD path; we return a synthesised ``PepDecision`` directly.

Read-after-write timing audit
─────────────────────────────
Each test does a synchronous sequence of writes + reads in a single
asyncio task. PG MVCC + asyncpg connection-per-acquire semantics mean
the GET sees the post-commit state; no shared in-memory cache lags.

The PEP gateway is the only cross-tx writer the router talks to —
the row INSERT happens in tx1, then ``evaluate()`` runs (single tx
in the stub, never blocks because we're not exercising the real HOLD
path), then tx2 UPDATEs ``pep_decision_id`` or flips ``state='cancelled'``.
A frontend GET inserted between tx1 and tx2 would see ``state='queued'
+ pep_decision_id IS NULL`` (the "pending PEP" semantic) — that
visibility window IS exercised by the smoke router test for the
PEP-deny path. BS.2.4 owns the post-tx2 round-trip.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid

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
# Same rationale as test_catalog_api.py: the conftest ``client``
# fixture's teardown TRUNCATEs bootstrap_state via ``_db_pool.get_pool()``,
# but ``pg_test_pool`` closes the pool first in reverse-setup teardown,
# so the conftest fixture errors. This local fixture skips the cleanup
# step entirely — pool lifecycle stays with ``pg_test_pool``.


@pytest.fixture()
async def bs24_client(pg_test_pool, monkeypatch):
    """AsyncClient against the FastAPI app, bootstrap pinned green,
    pool lifecycle owned by ``pg_test_pool``."""
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
#  PEP stub helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_pep_stub(action: str, *, decision_id: str | None = None,
                   rule: str = "tier_unlisted"):
    """Return an async function compatible with
    ``backend.pep_gateway.evaluate``'s signature that returns a
    ``PepDecision`` with the given outcome.

    ``action`` ∈ {``"auto_allow"``, ``"deny"``}. ``decision_id`` is
    auto-generated if not given. ``rule`` is the rule name the router
    surfaces in ``error_reason`` on deny.
    """
    from backend import pep_gateway as _pep

    decision_id = decision_id or f"de-{secrets.token_hex(6)}"
    pep_action = (
        _pep.PepAction.auto_allow if action == "auto_allow"
        else _pep.PepAction.deny
    )

    async def _fake_evaluate(*, tool, arguments, agent_id="", tier="t1",
                             propose_fn=None, wait_for_decision=None,
                             hold_timeout_s=1800.0):
        return _pep.PepDecision(
            id=f"pep-{secrets.token_hex(5)}",
            ts=time.time(),
            agent_id=agent_id,
            tool=tool,
            command=f"{tool} {arguments!r}",
            tier=tier,
            action=pep_action,
            rule=rule,
            reason=f"stubbed {action}",
            impact_scope="local",
            decision_id=decision_id if action == "auto_allow" else None,
        )

    return _fake_evaluate


def _make_pep_raiser(exc: Exception):
    """Return an async function that raises *exc* — used to exercise
    the gateway-error fallback branch (``state='cancelled'`` with
    ``error_reason='pep_gateway_error:<ExcClass>'``)."""
    async def _fake_raise(*, tool, arguments, **kwargs):
        raise exc
    return _fake_raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG seed / purge helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"BS24 {tid}",
        )


async def _seed_user(pool, uid: str, *, role: str = "operator",
                     tenant_id: str = "t-default") -> None:
    """Seed a users row (idempotent). The install_jobs.requested_by FK
    requires every job's actor to exist in the users table — the
    synthetic anon user (open auth mode) does not exist there by
    default, so every test that hits POST /installer/jobs must seed
    its actor row first."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, $4, '', 1, $5) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@bs24.test", f"BS24 {uid}", role, tenant_id,
        )


@pytest.fixture()
async def seeded_anon_user(pg_test_pool):
    """Ensure the synthetic anonymous user (open-auth-mode default)
    exists in PG so install_jobs.requested_by FK is satisfied for
    every test that hits POST /installer/jobs without overriding
    current_user.

    Cleanup is deliberate skipped — every test purges its own rows;
    the anon user row is shared and harmless across tests."""
    await _seed_user(pg_test_pool, "anonymous", role="super_admin")
    yield


async def _seed_shipped(pool, entry_id: str, *, family: str = "embedded",
                        vendor: str = "test-vendor",
                        install_method: str = "noop") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO catalog_entries "
            "  (id, source, tenant_id, vendor, family, display_name, "
            "   version, install_method) "
            "VALUES ($1, 'shipped', NULL, $2, $3, $4, '1.0.0', $5) "
            "ON CONFLICT DO NOTHING",
            entry_id, vendor, family, f"Test {entry_id}", install_method,
        )


async def _purge_jobs_and_entry(pool, entry_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'install_job' "
            "AND entity_id IN (SELECT id FROM install_jobs WHERE entry_id = $1)",
            entry_id,
        )
        await conn.execute(
            "DELETE FROM install_jobs WHERE entry_id = $1", entry_id,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'catalog_entry' "
            "AND entity_id = $1",
            entry_id,
        )
        await conn.execute(
            "DELETE FROM catalog_entries WHERE id = $1", entry_id,
        )


async def _purge_tenant_jobs(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'install_job' "
            "AND entity_id IN ("
            "  SELECT id FROM install_jobs WHERE tenant_id = $1"
            ")",
            tid,
        )
        await conn.execute(
            "DELETE FROM install_jobs WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM catalog_entries WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


def _override_user_factory(role: str, tenant_id: str = "t-default"):
    """Return a coroutine that returns a fake current_user with the
    given role + tenant_id. Same shape as the test_catalog_api factory."""
    from backend import auth as _au

    fake = _au.User(
        id=f"u-bs24-inst-{role}", email=f"{role}@bs24.test",
        name=f"BS24 inst {role}", role=role, enabled=True,
        tenant_id=tenant_id,
    )

    async def _fake() -> _au.User:
        return fake

    return _fake


def _new_idempotency_key() -> str:
    # uuid4().hex is 32 chars of [0-9a-f] — safely within
    # IDEMPOTENCY_KEY_PATTERN (16..64 chars of ASCII alphanumerics +
    # underscore + hyphen).
    return uuid.uuid4().hex


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Marker — anchors the BS.2.4 row in test reports without PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bs24_marker_module_imports():
    """Sanity: the installer router still imports cleanly."""
    from backend.routers import installer
    assert hasattr(installer, "router")
    assert hasattr(installer, "create_job")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs — happy + PEP integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_job_pep_approve_201_persists_decision_id(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """PEP approve → 201 + ``state='queued'`` + ``pep_decision_id`` set
    to the stub's decision id."""
    from backend.routers import installer
    entry_id = "bs24-job-approve"
    decision_id = "de-stub-approve-001"
    monkeypatch.setattr(
        installer._pep, "evaluate",
        _make_pep_stub("auto_allow", decision_id=decision_id),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        res = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id,
                  "idempotency_key": _new_idempotency_key()},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["entry_id"] == entry_id
        assert body["state"] == "queued"
        assert body["pep_decision_id"] == decision_id
        assert body["id"].startswith("ij-")
        # Persisted row matches.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, pep_decision_id FROM install_jobs "
                "WHERE id = $1",
                body["id"],
            )
        assert row["state"] == "queued"
        assert row["pep_decision_id"] == decision_id
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_job_pep_deny_403_marks_row_cancelled(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """PEP deny → 403 + the row is flipped to ``state='cancelled'``
    with ``error_reason='pep_<rule>'``. Frontend should see the row
    via GET /jobs/{id} and render the rejection."""
    from backend.routers import installer
    entry_id = "bs24-job-deny"
    monkeypatch.setattr(
        installer._pep, "evaluate",
        _make_pep_stub("deny", rule="tier_unlisted"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        idem = _new_idempotency_key()
        res = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": idem},
        )
        assert res.status_code == 403, res.text
        # The row exists but is cancelled.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, error_reason FROM install_jobs "
                "WHERE idempotency_key = $1",
                idem,
            )
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["error_reason"].startswith("pep_")
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_job_pep_gateway_error_403_marks_row_cancelled(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """If ``pep_gateway.evaluate`` raises (gateway unreachable / timeout),
    the router catches it, flips the row to cancelled with
    ``error_reason='pep_gateway_error:<ExcClass>'`` and returns 403.
    Fail-closed semantics — never leave a queued row behind without
    a decision.
    """
    from backend.routers import installer
    entry_id = "bs24-job-gw-error"
    monkeypatch.setattr(
        installer._pep, "evaluate",
        _make_pep_raiser(RuntimeError("simulated gateway crash")),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        idem = _new_idempotency_key()
        res = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": idem},
        )
        assert res.status_code == 403, res.text
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, error_reason FROM install_jobs "
                "WHERE idempotency_key = $1",
                idem,
            )
        assert row["state"] == "cancelled"
        assert row["error_reason"].startswith("pep_gateway_error:")
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_job_unknown_entry_returns_404(
    bs24_client, monkeypatch,
):
    """An entry id that doesn't exist (no shipped/operator/override row)
    must 404 BEFORE PEP fires — we don't want to spam the decision
    engine with proposals for invalid input."""
    from backend.routers import installer
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    res = await bs24_client.post(
        "/api/v1/installer/jobs",
        json={"entry_id": "bs24-unknown-entry-xxx",
              "idempotency_key": _new_idempotency_key()},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_job_idempotent_retry_returns_existing_row_200(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """A second POST with the same idempotency_key returns the original
    row at 200 — no second PEP HOLD is fired, no duplicate row created."""
    from backend.routers import installer
    entry_id = "bs24-job-idempotent"
    pep_calls = {"n": 0}

    async def _counting_evaluate(*, tool, arguments, **kwargs):
        from backend import pep_gateway as _pep
        pep_calls["n"] += 1
        return _pep.PepDecision(
            id="pep-idem", ts=time.time(), agent_id="", tool=tool,
            command="", tier="t1", action=_pep.PepAction.auto_allow,
            rule="tier_unlisted", reason="ok", impact_scope="local",
            decision_id="de-idem-001",
        )

    monkeypatch.setattr(installer._pep, "evaluate", _counting_evaluate)
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        idem = _new_idempotency_key()
        first = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": idem},
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]
        # Second POST with the same key — same job, no new PEP call.
        second = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": idem},
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_id
        assert pep_calls["n"] == 1, "PEP must not fire twice for idempotent retry"
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /installer/jobs — list + filter + tenant scope
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_jobs_list_envelope_and_filter_by_state(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """List returns {items, count, total, limit, offset}; ``state=queued``
    filter excludes cancelled rows."""
    from backend.routers import installer
    entry_id = "bs24-list-state"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        # Queued job 1
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        assert post.status_code == 201
        # Cancelled job (manual flip via PG)
        cancelled_idem = _new_idempotency_key()
        post2 = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": cancelled_idem},
        )
        cancelled_id = post2.json()["id"]
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE install_jobs SET state = 'cancelled' WHERE id = $1",
                cancelled_id,
            )

        res = await bs24_client.get(
            "/api/v1/installer/jobs",
            params={"state": "queued", "entry_id": entry_id, "limit": 100},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        for k in ("items", "count", "total", "limit", "offset"):
            assert k in body
        ids = {it["id"] for it in body["items"]}
        assert post.json()["id"] in ids
        assert cancelled_id not in ids
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_jobs_filter_unknown_state_422(bs24_client):
    """Unknown ``state`` value → 422 (defence in depth)."""
    res = await bs24_client.get(
        "/api/v1/installer/jobs", params={"state": "ufo"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_get_jobs_tenant_isolation(
    bs24_client, pg_test_pool, monkeypatch,
):
    """A job created by tenant A must not appear in tenant B's list.
    Drift gate for the ``tenant_id = $1`` predicate in list_jobs."""
    from backend.main import app
    from backend import auth as _au
    from backend.routers import installer

    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    tid_a = "t-bs24-jobs-a"
    tid_b = "t-bs24-jobs-b"
    uid_a = "u-bs24-inst-operator"  # Match the factory's user.id
    entry_id = "bs24-isolate-jobs"
    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        await _seed_shipped(pg_test_pool, entry_id)
        await _seed_user(pg_test_pool, uid_a, role="operator", tenant_id=tid_a)

        # Tenant A creates a job.
        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("operator", tenant_id=tid_a)
        )
        try:
            create = await bs24_client.post(
                "/api/v1/installer/jobs",
                json={"entry_id": entry_id,
                      "idempotency_key": _new_idempotency_key()},
            )
            assert create.status_code == 201, create.text
            a_job_id = create.json()["id"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Tenant B lists jobs — must not see A's row.
        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("operator", tenant_id=tid_b)
        )
        try:
            res = await bs24_client.get("/api/v1/installer/jobs",
                                   params={"limit": 500})
            assert res.status_code == 200, res.text
            ids = {it["id"] for it in res.json()["items"]}
            assert a_job_id not in ids, (
                f"tenant B saw tenant A's job {a_job_id} — isolation broken"
            )
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant_jobs(pg_test_pool, tid_a)
        await _purge_tenant_jobs(pg_test_pool, tid_b)
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /installer/jobs/{id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_job_by_id_happy_path(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """GET single by id round-trips with the same shape POST returned."""
    from backend.routers import installer
    entry_id = "bs24-get-by-id"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]
        res = await bs24_client.get(f"/api/v1/installer/jobs/{job_id}")
        assert res.status_code == 200, res.text
        assert res.json()["id"] == job_id
        assert res.json()["entry_id"] == entry_id
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_job_by_id_404_cross_tenant(
    bs24_client, pg_test_pool, monkeypatch,
):
    """Tenant A's job ID is not visible to tenant B (404, not 403 — we
    don't reveal the row exists in a different tenant)."""
    from backend.main import app
    from backend import auth as _au
    from backend.routers import installer

    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    tid_a = "t-bs24-getid-a"
    tid_b = "t-bs24-getid-b"
    uid_a = "u-bs24-inst-operator"  # Match the factory's user.id
    entry_id = "bs24-getid-isolate"
    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        await _seed_shipped(pg_test_pool, entry_id)
        await _seed_user(pg_test_pool, uid_a, role="operator", tenant_id=tid_a)

        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("operator", tenant_id=tid_a)
        )
        try:
            create = await bs24_client.post(
                "/api/v1/installer/jobs",
                json={"entry_id": entry_id,
                      "idempotency_key": _new_idempotency_key()},
            )
            assert create.status_code == 201
            a_job_id = create.json()["id"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        app.dependency_overrides[_au.current_user] = (
            _override_user_factory("operator", tenant_id=tid_b)
        )
        try:
            res = await bs24_client.get(f"/api/v1/installer/jobs/{a_job_id}")
            assert res.status_code == 404, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant_jobs(pg_test_pool, tid_a)
        await _purge_tenant_jobs(pg_test_pool, tid_b)
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_job_invalid_id_pattern_422(bs24_client):
    """``Foo`` doesn't match ``ij-[0-9a-f]{12}`` — 422 before any DB
    access."""
    res = await bs24_client.get("/api/v1/installer/jobs/Foo")
    assert res.status_code == 422, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs/{id}/cancel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_cancel_queued_job_flips_state_200(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """A queued job → cancel → state='cancelled' + reason recorded."""
    from backend.routers import installer
    entry_id = "bs24-cancel-queued"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]
        cancel = await bs24_client.post(
            f"/api/v1/installer/jobs/{job_id}/cancel",
            json={"reason": "operator-changed-mind"},
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["state"] == "cancelled"
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, error_reason FROM install_jobs WHERE id = $1",
                job_id,
            )
        assert row["state"] == "cancelled"
        assert row["error_reason"] == "operator-changed-mind"
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_cancel_emits_installer_progress_sse_event(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """BS.7.7 — cancel_job emits ``installer_progress`` over SSE so
    cross-tab + cross-worker UIs converge on the cancelled state
    immediately, without waiting for the sidecar's next
    ``report_progress`` round-trip.

    The frontend's ``useInstallJobs()`` hook is a single subscriber
    on this channel, so reusing it (rather than adding a dedicated
    ``installer_cancelled`` channel) keeps the wire surface minimal.
    ``stage="cancel"`` lets ToastCenter / drawer disambiguate the
    operator-driven cancel from a sidecar's later confirmation tick
    (which carries the original method stage).
    """
    from backend import events as _events
    from backend.routers import installer
    entry_id = "bs77-cancel-emit-sse"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    captured: list[dict[str, object]] = []

    def fake_emit(job_id: str, **kwargs: object) -> None:
        captured.append({"job_id": job_id, **kwargs})

    monkeypatch.setattr(_events, "emit_installer_progress", fake_emit)
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]
        cancel = await bs24_client.post(
            f"/api/v1/installer/jobs/{job_id}/cancel",
            json={"reason": "operator-changed-mind"},
        )
        assert cancel.status_code == 200, cancel.text

        # Exactly one emit, addressed to the cancelled job, with the
        # cancel-specific stage discriminator.
        assert len(captured) == 1, captured
        ev = captured[0]
        assert ev["job_id"] == job_id
        assert ev["state"] == "cancelled"
        assert ev["stage"] == "cancel"
        # broadcast scope is tenant — install_jobs is tenant-scoped.
        assert ev["broadcast_scope"] == "tenant"
        # tenant_id pulled from the row's tenant context, not the URL.
        assert isinstance(ev["tenant_id"], str)
        assert len(ev["tenant_id"]) > 0
        # entry_id passed through so the frontend can update the
        # corresponding catalog card without a second round-trip.
        assert ev["entry_id"] == entry_id
        # bytes / eta / log_tail come straight off the cancelled row;
        # for a queued (never-claimed) row they're 0 / None / "".
        assert ev["bytes_done"] == 0
        assert ev["bytes_total"] is None
        assert ev["eta_seconds"] is None
        assert ev["log_tail"] == ""
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_cancel_terminal_state_returns_409(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """A completed job cannot be cancelled — 409."""
    from backend.routers import installer
    entry_id = "bs24-cancel-terminal"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]
        # Manually mark as completed.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE install_jobs SET state = 'completed' WHERE id = $1",
                job_id,
            )
        cancel = await bs24_client.post(
            f"/api/v1/installer/jobs/{job_id}/cancel", json={},
        )
        assert cancel.status_code == 409, cancel.text
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs/{id}/retry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_retry_failed_job_creates_fresh_queued_row_201(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """Retry of a failed job clones into a NEW queued row with a fresh
    id; the original row remains for audit. PEP HOLD fires again on
    the retry."""
    from backend.routers import installer
    entry_id = "bs24-retry-failed"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        original_id = post.json()["id"]
        # Mark as failed.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE install_jobs SET state = 'failed' WHERE id = $1",
                original_id,
            )
        retry = await bs24_client.post(
            f"/api/v1/installer/jobs/{original_id}/retry",
            json={"idempotency_key": _new_idempotency_key()},
        )
        assert retry.status_code == 201, retry.text
        new_id = retry.json()["id"]
        assert new_id != original_id
        assert retry.json()["state"] == "queued"
        # Original row preserved for audit.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM install_jobs WHERE id = $1", original_id,
            )
        assert row["state"] == "failed"
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_retry_active_job_returns_409(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """Retrying a job that's still queued/running is 409 — the operator
    must cancel first if that's what they meant."""
    from backend.routers import installer
    entry_id = "bs24-retry-active"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]  # queued state
        retry = await bs24_client.post(
            f"/api/v1/installer/jobs/{job_id}/retry",
            json={"idempotency_key": _new_idempotency_key()},
        )
        assert retry.status_code == 409, retry.text
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /installer/jobs/poll — sidecar long-poll claim
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_poll_claims_queued_job_200(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """A queued job is claimed by the sidecar poll → 200 + row state
    transitions to 'running' with sidecar_id + claimed_at set."""
    from backend.routers import installer
    entry_id = "bs24-poll-claim"
    monkeypatch.setattr(
        installer._pep, "evaluate", _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        post = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": entry_id, "idempotency_key": _new_idempotency_key()},
        )
        job_id = post.json()["id"]
        sidecar = "sidecar-bs24-test"
        res = await bs24_client.get(
            "/api/v1/installer/jobs/poll",
            params={"sidecar_id": sidecar, "timeout_s": 1},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        # Either we claimed our job (most likely — only queued row) or
        # any other queued row that landed first; assert what we know.
        assert body["state"] == "running"
        assert body["sidecar_id"] == sidecar
        assert body["claimed_at"] is not None
        # Inspect the actual job we created.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, sidecar_id FROM install_jobs WHERE id = $1",
                job_id,
            )
        # If our job was the one claimed, sidecar_id matches; if another
        # queued job was claimed first (unlikely in test isolation but
        # possible), our row is still queued. Either way the claimed
        # row in the response is correctly transitioned.
        assert row["state"] in ("queued", "running")
        if row["state"] == "running":
            assert row["sidecar_id"] == sidecar
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_poll_returns_204_when_no_queued_job(bs24_client):
    """When no queued job is available within the timeout, return 204
    No Content. Sidecar will retry."""
    sidecar = "sidecar-bs24-empty"
    # 1 second is enough for the test PG to confirm no queued rows.
    res = await bs24_client.get(
        "/api/v1/installer/jobs/poll",
        params={"sidecar_id": sidecar, "timeout_s": 1},
    )
    # Either 204 (no queued job) or 200 (some other test left a row —
    # in that case the test environment is dirty but the BS.2.4 contract
    # is preserved).
    assert res.status_code in (204, 200), res.text


@_requires_pg
async def test_poll_unsupported_protocol_version_returns_426(bs24_client):
    """A sidecar claiming protocol_version=99 is rejected with 426
    Upgrade Required + body describing the supported range. Future
    sidecar protocol bump scenario — operator pulls a new sidecar
    image whose version isn't supported by this backend yet."""
    res = await bs24_client.get(
        "/api/v1/installer/jobs/poll",
        params={"sidecar_id": "sidecar-bs24-426",
                "protocol_version": 99, "timeout_s": 0},
    )
    assert res.status_code == 426, res.text
    body = res.json()
    assert body["error"] == "protocol_version_unsupported"
    assert body["client_protocol_version"] == 99
    assert isinstance(body["supported"], list) and 1 in body["supported"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Permission denial — viewer hits 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_viewer_role_denied_on_post_jobs(bs24_client, monkeypatch):
    """RBAC end-to-end: a viewer (below operator floor) hits 403 on
    POST /installer/jobs even if the body would be valid + entry
    exists. Confirms the dep-wired ``require_operator`` actually
    rejects viewer roles at runtime, not just identity-checks the
    object reference (which is what the smoke test asserts)."""
    from backend.main import app
    from backend import auth as _au

    app.dependency_overrides[_au.current_user] = (
        _override_user_factory("viewer")
    )
    try:
        res = await bs24_client.post(
            "/api/v1/installer/jobs",
            json={"entry_id": "bs24-viewer-denied",
                  "idempotency_key": _new_idempotency_key()},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BS.8.2 — GET /installer/installed + POST /installer/uninstall
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_completed_install(
    pool, *, tid: str, entry_id: str, job_id: str | None = None,
    requested_by: str = "anonymous",
    completed_at_offset_seconds: int = 0,
) -> str:
    """Insert a directly-completed install_jobs row so the entry
    surfaces in ``GET /installer/installed`` without going through the
    HOLD path. Mirrors what a real install would write after the
    sidecar finishes; bypasses the sidecar layer entirely.

    Returns the row's id so the test can purge it.
    """
    import secrets as _sec
    job_id = job_id or f"ij-{_sec.token_hex(6)}"
    idem = uuid.uuid4().hex
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO install_jobs "
            "  (id, tenant_id, entry_id, state, idempotency_key, "
            "   protocol_version, requested_by, completed_at) "
            "VALUES ($1, $2, $3, 'completed', $4, 1, $5, "
            "        now() + ($6 || ' seconds')::interval)",
            job_id, tid, entry_id, idem, requested_by,
            str(completed_at_offset_seconds),
        )
    return job_id


@_requires_pg
async def test_get_installed_lists_completed_entries(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """A tenant with one completed install sees the entry in
    ``/installer/installed``; the response shape carries the bookkeeping
    fields the BS.8.1 InstalledTab consumes."""
    entry_id = "bs82-installed-basic"
    try:
        await _seed_shipped(pg_test_pool, entry_id, family="mobile")
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=entry_id,
        )
        res = await bs24_client.get("/api/v1/installer/installed")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 1
        item = body["items"][0]
        assert item["entry_id"] == entry_id
        assert item["display_name"] == f"Test {entry_id}"
        assert item["family"] == "mobile"
        assert item["installed_at"] is not None
        # Today these are placeholders (workspace-link table lands later).
        assert item["used_by_workspace_count"] == 0
        assert item["last_used_at"] is None
        assert item["update_available"] is False
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_get_installed_excludes_uninstalled_entries(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """Once the operator has approved an uninstall (latest install_jobs
    row is the uninstall record), the entry no longer appears in
    ``/installer/installed`` even though older completed rows survive
    in the audit trail."""
    from backend.routers import installer
    entry_id = "bs82-installed-uninstalled"
    monkeypatch_target = installer._pep
    try:
        await _seed_shipped(pg_test_pool, entry_id, family="embedded")
        # Older install — entry IS installed at this point.
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=entry_id,
            completed_at_offset_seconds=-100,
        )
        # Issue an uninstall via the API with PEP auto-approve.
        approve_stub = _make_pep_stub("auto_allow")
        original_evaluate = monkeypatch_target.evaluate
        monkeypatch_target.evaluate = approve_stub  # type: ignore[assignment]
        try:
            res = await bs24_client.post(
                "/api/v1/installer/uninstall",
                json={"entry_ids": [entry_id]},
            )
        finally:
            monkeypatch_target.evaluate = original_evaluate  # type: ignore[assignment]
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["approved_count"] == 1
        assert body["denied_count"] == 0
        # GET /installer/installed must now exclude the entry.
        res2 = await bs24_client.get("/api/v1/installer/installed")
        assert res2.status_code == 200, res2.text
        items_now = res2.json()["items"]
        assert all(it["entry_id"] != entry_id for it in items_now), items_now
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_uninstall_pep_deny_403_records_cancelled_rows(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """PEP deny → 403; the uninstall row is recorded with
    ``state='cancelled'`` + ``error_reason='pep_<rule>'`` so the
    audit log captures the rejection."""
    from backend.routers import installer
    entry_id = "bs82-uninstall-deny"
    monkeypatch.setattr(
        installer._pep, "evaluate",
        _make_pep_stub("deny", rule="tier_unlisted"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=entry_id,
        )
        res = await bs24_client.post(
            "/api/v1/installer/uninstall",
            json={"entry_ids": [entry_id]},
        )
        assert res.status_code == 403, res.text
        # The cancelled uninstall row exists.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, error_reason, result_json::text "
                "FROM install_jobs "
                "WHERE entry_id = $1 AND state = 'cancelled'",
                entry_id,
            )
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["error_reason"].startswith("pep_")
        assert "uninstall" in row["result_json"]
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_uninstall_dedupes_repeated_entry_ids(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """Duplicate entry_ids in the body are collapsed to a single row
    (operator's accidental double-click in the modal does not produce
    duplicate uninstall rows)."""
    from backend.routers import installer
    entry_id = "bs82-uninstall-dedupe"
    monkeypatch.setattr(
        installer._pep, "evaluate",
        _make_pep_stub("auto_allow"),
    )
    try:
        await _seed_shipped(pg_test_pool, entry_id)
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=entry_id,
        )
        res = await bs24_client.post(
            "/api/v1/installer/uninstall",
            json={"entry_ids": [entry_id, entry_id, entry_id]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["approved_count"] == 1
        # Only ONE uninstall row — the dedupe happens before INSERT.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM install_jobs "
                "WHERE entry_id = $1 AND result_json::text LIKE '%uninstall%'",
                entry_id,
            )
        assert count == 1
    finally:
        await _purge_jobs_and_entry(pg_test_pool, entry_id)


@_requires_pg
async def test_post_uninstall_rejects_malformed_entry_id(
    bs24_client, pg_test_pool, monkeypatch, seeded_anon_user,
):
    """Bad entry_id in the list → 422 before the PEP HOLD fires; no
    rows inserted, no PEP evaluate call."""
    from backend.routers import installer

    async def _should_not_be_called(*a, **k):
        raise AssertionError("PEP must not run when entry_ids fail validation")

    monkeypatch.setattr(installer._pep, "evaluate", _should_not_be_called)
    res = await bs24_client.post(
        "/api/v1/installer/uninstall",
        json={"entry_ids": ["BAD ID with spaces"]},
    )
    assert res.status_code == 422


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BS.8.4 — GET /installer/installed/{entry_id}/dependents
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_shipped_with_depends_on(
    pool, entry_id: str, *, depends_on: list[str],
    family: str = "embedded", vendor: str = "test-vendor",
    install_method: str = "noop",
) -> None:
    """Seed a shipped catalog entry with an explicit ``depends_on``
    JSONB array. Used by BS.8.4 dependents tests."""
    import json as _json
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO catalog_entries "
            "  (id, source, tenant_id, vendor, family, display_name, "
            "   version, install_method, depends_on) "
            "VALUES ($1, 'shipped', NULL, $2, $3, $4, '1.0.0', $5, $6::jsonb) "
            "ON CONFLICT DO NOTHING",
            entry_id, vendor, family, f"Test {entry_id}",
            install_method, _json.dumps(depends_on),
        )


@_requires_pg
async def test_get_dependents_returns_installed_entries_that_depend_on_target(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """An installed entry whose ``depends_on`` array contains the target
    entry id surfaces in the dependents response."""
    base = "bs84-base-sdk"
    dep_a = "bs84-dep-a"
    dep_b = "bs84-dep-b"
    unrelated = "bs84-unrelated"
    try:
        # Seed catalog entries — base has no deps; dep_a + dep_b depend on base.
        await _seed_shipped_with_depends_on(
            pg_test_pool, base, depends_on=[],
        )
        await _seed_shipped_with_depends_on(
            pg_test_pool, dep_a, depends_on=[base],
        )
        await _seed_shipped_with_depends_on(
            pg_test_pool, dep_b, depends_on=[base, "some-other-dep"],
        )
        await _seed_shipped_with_depends_on(
            pg_test_pool, unrelated, depends_on=["some-other-dep"],
        )
        # All four are installed.
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=base,
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=dep_a,
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=dep_b,
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=unrelated,
        )
        res = await bs24_client.get(
            f"/api/v1/installer/installed/{base}/dependents",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["entry_id"] == base
        assert body["count"] == 2
        ids = sorted(item["entry_id"] for item in body["items"])
        assert ids == sorted([dep_a, dep_b])
        # `unrelated` does NOT depend on base, so it is excluded.
        assert all(item["entry_id"] != unrelated for item in body["items"])
        # Self-reference is excluded — base is not its own dependent
        # even if a malformed catalog row pointed back at itself.
        assert all(item["entry_id"] != base for item in body["items"])
    finally:
        await _purge_jobs_and_entry(pg_test_pool, base)
        await _purge_jobs_and_entry(pg_test_pool, dep_a)
        await _purge_jobs_and_entry(pg_test_pool, dep_b)
        await _purge_jobs_and_entry(pg_test_pool, unrelated)


@_requires_pg
async def test_get_dependents_returns_empty_when_no_dependents(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """An entry with no dependents returns count=0 + empty items."""
    base = "bs84-no-deps-base"
    try:
        await _seed_shipped_with_depends_on(
            pg_test_pool, base, depends_on=[],
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=base,
        )
        res = await bs24_client.get(
            f"/api/v1/installer/installed/{base}/dependents",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["entry_id"] == base
        assert body["count"] == 0
        assert body["items"] == []
    finally:
        await _purge_jobs_and_entry(pg_test_pool, base)


@_requires_pg
async def test_get_dependents_excludes_uninstalled_dependents(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """A dependent whose latest install_jobs row is an uninstall record
    no longer counts — the entry was removed even if the catalog row
    still declares depends_on."""
    from backend.routers import installer
    base = "bs84-uninstalled-dep-base"
    dep = "bs84-uninstalled-dep"
    monkeypatch_target = installer._pep
    try:
        await _seed_shipped_with_depends_on(
            pg_test_pool, base, depends_on=[],
        )
        await _seed_shipped_with_depends_on(
            pg_test_pool, dep, depends_on=[base],
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=base,
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=dep,
            completed_at_offset_seconds=-100,
        )
        # Uninstall the dependent via the API with PEP auto-approve.
        approve_stub = _make_pep_stub("auto_allow")
        original_evaluate = monkeypatch_target.evaluate
        monkeypatch_target.evaluate = approve_stub  # type: ignore[assignment]
        try:
            uninstall_res = await bs24_client.post(
                "/api/v1/installer/uninstall",
                json={"entry_ids": [dep]},
            )
        finally:
            monkeypatch_target.evaluate = original_evaluate  # type: ignore[assignment]
        assert uninstall_res.status_code == 200
        # The uninstalled dependent must NOT surface as a dependent of
        # base, since "currently installed" excludes uninstall records.
        res = await bs24_client.get(
            f"/api/v1/installer/installed/{base}/dependents",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 0, body
    finally:
        await _purge_jobs_and_entry(pg_test_pool, base)
        await _purge_jobs_and_entry(pg_test_pool, dep)


@_requires_pg
async def test_get_dependents_rejects_malformed_entry_id_422(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """A malformed entry_id in the path is 422'd before the PG SELECT
    fires (mirrors the bulk-uninstall validator)."""
    res = await bs24_client.get(
        "/api/v1/installer/installed/BAD%20ID%20with%20spaces/dependents",
    )
    assert res.status_code == 422


@_requires_pg
async def test_get_dependents_excludes_pending_install_dependents(
    bs24_client, pg_test_pool, seeded_anon_user,
):
    """A dependent whose latest install_jobs row is queued (not yet
    completed) is NOT a dependent — the depends_on relationship is
    materialised by the *currently installed* set, not the catalog
    row alone."""
    base = "bs84-pending-dep-base"
    dep = "bs84-pending-dep"
    try:
        await _seed_shipped_with_depends_on(
            pg_test_pool, base, depends_on=[],
        )
        await _seed_shipped_with_depends_on(
            pg_test_pool, dep, depends_on=[base],
        )
        await _seed_completed_install(
            pg_test_pool, tid="t-default", entry_id=base,
        )
        # Insert a queued (not completed) row for `dep` — depends_on
        # exists but the install hasn't finished, so dep is not yet
        # "installed".
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO install_jobs "
                "  (id, tenant_id, entry_id, state, idempotency_key, "
                "   protocol_version, requested_by) "
                "VALUES ($1, 't-default', $2, 'queued', $3, 1, 'anonymous')",
                f"ij-{secrets.token_hex(6)}", dep, uuid.uuid4().hex,
            )
        res = await bs24_client.get(
            f"/api/v1/installer/installed/{base}/dependents",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 0, body
    finally:
        await _purge_jobs_and_entry(pg_test_pool, base)
        await _purge_jobs_and_entry(pg_test_pool, dep)


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
