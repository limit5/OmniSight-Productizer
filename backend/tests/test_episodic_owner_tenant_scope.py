"""U6-0 T8-C3 — tenant/owner scoping of the NON-model episodic readers.

These tests are deliberately PG-free (no ``client``/``pg_test_pool``
fixture) so they RUN in the offline ``.env.test`` harness and prove the
fail-closed + query-shape contract without a live Postgres. The
behavioural PG-matrix coverage lives in ``test_intent_memory.py``
(owner/tenant isolation) and ``test_project_report.py``
(``test_lessons_learned_tenant_scoped``).

Contract under test:
  * ``db.search_owner_clarifications`` filters owner+tenant+source and
    fails closed (ValueError) on an empty owner or tenant.
  * ``intent_memory.lookup_prior_choice`` returns no hit without a
    resolved owner+tenant, never touching the pool.
  * ``project_report._lessons_learned`` returns an empty list without a
    resolved tenant, and its SQL carries a ``tenant_id`` filter.
"""

from __future__ import annotations

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fakes — capture SQL without a live PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return None


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _ExplodingPool:
    """A pool that fails if anyone acquires it — proves fail-closed
    paths return BEFORE touching the connection."""

    def acquire(self):
        raise AssertionError("pool must not be acquired on the fail-closed path")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  db.search_owner_clarifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_search_owner_clarifications_query_filters():
    from backend import db
    conn = _FakeConn()
    rows = await db.search_owner_clarifications(
        conn, "spec-conflict:x:hello world",
        owner_user_id="u-op", tenant_id="t-acme", min_quality=0.5,
    )
    assert rows == []
    sql, params = conn.calls[-1]
    # The three isolation filters must all be present in the SQL.
    assert "owner_user_id = $" in sql
    assert "tenant_id = $" in sql
    assert "source = 'user_clarification'" in sql
    # …and the caller's identity must be bound as params.
    assert "u-op" in params
    assert "t-acme" in params


@pytest.mark.asyncio
async def test_search_owner_clarifications_fail_closed_empty_owner():
    from backend import db
    with pytest.raises(ValueError):
        await db.search_owner_clarifications(
            _ExplodingPool(), "q", owner_user_id="", tenant_id="t-acme",
        )


@pytest.mark.asyncio
async def test_search_owner_clarifications_fail_closed_empty_tenant():
    from backend import db
    with pytest.raises(ValueError):
        await db.search_owner_clarifications(
            _ExplodingPool(), "q", owner_user_id="u-op", tenant_id="",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  intent_memory.lookup_prior_choice / annotate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_lookup_fail_closed_without_owner(monkeypatch):
    from backend import intent_memory as im
    import backend.db_pool as dbp
    monkeypatch.setattr(dbp, "get_pool", lambda: _ExplodingPool())
    prior = await im.lookup_prior_choice(
        raw_text="x", conflict_id="c", owner_user_id="", tenant_id="t-acme",
    )
    assert prior is None


@pytest.mark.asyncio
async def test_lookup_fail_closed_without_tenant(monkeypatch):
    from backend import intent_memory as im
    import backend.db_pool as dbp
    monkeypatch.setattr(dbp, "get_pool", lambda: _ExplodingPool())
    prior = await im.lookup_prior_choice(
        raw_text="x", conflict_id="c", owner_user_id="u-op", tenant_id="",
    )
    assert prior is None


@pytest.mark.asyncio
async def test_annotate_without_identity_yields_no_prior(monkeypatch):
    from backend import intent_memory as im
    import backend.db_pool as dbp
    monkeypatch.setattr(dbp, "get_pool", lambda: _ExplodingPool())
    conflicts = [{"id": "static_with_runtime_db", "options": []}]
    out = await im.annotate_conflicts_with_priors(
        "some prompt", conflicts, owner_user_id="", tenant_id="",
    )
    assert out[0].get("prior_choice") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  project_report._lessons_learned / _resolve_report_tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_lessons_learned_fail_closed_empty_tenant(monkeypatch):
    from backend import project_report as pr
    import backend.db_pool as dbp
    monkeypatch.setattr(dbp, "get_pool", lambda: _ExplodingPool())
    out = await pr._lessons_learned(tenant_id="")
    assert out == []


@pytest.mark.asyncio
async def test_lessons_learned_query_carries_tenant_filter(monkeypatch):
    from backend import project_report as pr
    import backend.db_pool as dbp
    conn = _FakeConn()
    monkeypatch.setattr(dbp, "get_pool", lambda: _FakePool(conn))
    out = await pr._lessons_learned(tenant_id="t-acme")
    assert out == []
    sql, params = conn.calls[-1]
    assert "tenant_id = $1" in sql
    assert "t-acme" in params


@pytest.mark.asyncio
async def test_resolve_report_tenant_prefers_context(monkeypatch):
    from backend import project_report as pr
    from backend import db_context
    import backend.db_pool as dbp
    # A request-scoped tenant short-circuits the DB fallback entirely.
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: "t-ctx")
    monkeypatch.setattr(dbp, "get_pool", lambda: _ExplodingPool())
    assert await pr._resolve_report_tenant("any-project") == "t-ctx"


@pytest.mark.asyncio
async def test_resolve_report_tenant_falls_back_to_project(monkeypatch):
    from backend import project_report as pr
    from backend import db_context
    import backend.db_pool as dbp

    class _ProjConn(_FakeConn):
        async def fetchrow(self, sql, *params):
            self.calls.append((sql, params))
            return {"tenant_id": "t-proj"}

    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)
    monkeypatch.setattr(dbp, "get_pool", lambda: _FakePool(_ProjConn()))
    assert await pr._resolve_report_tenant("p-42") == "t-proj"


@pytest.mark.asyncio
async def test_resolve_report_tenant_unresolved_is_empty(monkeypatch):
    from backend import project_report as pr
    from backend import db_context
    import backend.db_pool as dbp
    # No context tenant + no matching project row ⇒ "" (fail-closed).
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)
    monkeypatch.setattr(dbp, "get_pool", lambda: _FakePool(_FakeConn()))
    assert await pr._resolve_report_tenant("missing") == ""
