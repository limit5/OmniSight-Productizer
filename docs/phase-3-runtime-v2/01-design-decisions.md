# Phase-3-Runtime-v2 — Design Decisions

> Operator decisions (2026-04-20):
> - **Option A1** — full Depends(get_conn) propagation through every layer
> - **FTS5 port** — must do (SQLite fts5 virtual table → PG tsvector + GIN)
> - Accepts 5-10 session downtime in exchange for stability + quality

---

## 1. API shape — Option A1 final form

### 1.1 Core dependency

```python
# backend/db_pool.py (new module)

import asyncpg
from typing import AsyncGenerator
from fastapi import Request

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size=5, max_size=20, **kwargs) -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=512,
        command_timeout=30.0,
        max_inactive_connection_lifetime=300.0,
        **kwargs,
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the global pool — raises if not initialised."""
    if _pool is None:
        raise RuntimeError("db_pool not initialised; call init_pool() first")
    return _pool


async def get_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    """FastAPI dependency: yields a connection for the lifetime of the request.

    The connection is automatically released back to the pool on request
    completion (success or exception). No explicit commit required for
    single-statement writes — asyncpg uses implicit tx per statement when
    outside an explicit transaction block.
    """
    async with get_pool().acquire() as conn:
        yield conn
```

### 1.2 Route handler signature (Tier 1 — routers)

```python
from fastapi import APIRouter, Depends
from backend.db_pool import get_conn
from backend import db
import asyncpg

router = APIRouter(...)

@router.get("/agents")
async def list_agents_route(
    conn: asyncpg.Connection = Depends(get_conn),
    tenant_id: str = Depends(require_tenant),
):
    return await db.list_agents(conn, tenant_id=tenant_id)
```

### 1.3 db.py function signature (Tier 2 — repository layer)

```python
# backend/db.py — NEW signature style

import asyncpg

async def list_agents(
    conn: asyncpg.Connection,
    *,
    tenant_id: str | None = None,
) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, name, type, status FROM agents WHERE tenant_id = $1",
        tenant_id,
    )
    return [dict(r) for r in rows]


async def upsert_agent(conn: asyncpg.Connection, agent: Agent) -> None:
    await conn.execute(
        """INSERT INTO agents (id, name, type, status, tenant_id)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (id) DO UPDATE SET
             name = EXCLUDED.name,
             type = EXCLUDED.type,
             status = EXCLUDED.status""",
        agent.id, agent.name, agent.type, agent.status, agent.tenant_id,
    )
```

### 1.4 Multi-step transaction signature

```python
async def replace_decision_rules(
    conn: asyncpg.Connection,
    tenant_id: str,
    rules: list[DecisionRule],
) -> None:
    """All-or-nothing replacement of decision rules for a tenant."""
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM decision_rules WHERE tenant_id = $1", tenant_id,
        )
        for rule in rules:
            await conn.execute(
                """INSERT INTO decision_rules (id, tenant_id, ...)
                   VALUES ($1, $2, ...)""",
                rule.id, tenant_id, ...,
            )
```

### 1.5 Non-request context (background tasks, SSE subscribers, startup)

```python
async def background_cleanup_task():
    async with get_pool().acquire() as conn:
        await db.cleanup_old_events(conn, older_than_hours=24)
```

### 1.6 Service-layer propagation

Every service/business-logic function that eventually calls DB layer gets
a `conn` parameter. This is the A1 "propagation through every layer" rule.

```python
# backend/services/agents.py (example service layer file)

async def register_agent_with_metrics(
    conn: asyncpg.Connection,
    agent: Agent,
    tenant_id: str,
) -> RegistrationResult:
    await db.upsert_agent(conn, agent)
    await audit.log(conn, action="agent.register", ...)
    await metrics.track(conn, counter="agents_registered", ...)
    return RegistrationResult(...)
```

---

## 2. Pool configuration

### 2.1 Per-worker pool sizing

| Parameter | Value | Justification |
|---|---|---|
| `min_size` | 5 | Always-warm connections for responsive dashboard poll |
| `max_size` | 20 | Per-worker cap; handles burst of 20 concurrent requests |
| `statement_cache_size` | 512 | Known-query workload, larger cache > default 100 |
| `command_timeout` | 30.0 s | Kill runaway queries before user timeout |
| `max_inactive_connection_lifetime` | 300.0 s | Recycle idle conn every 5 min |

### 2.2 Deployment-wide connection budget

```
2 backend replicas × 2 workers/replica × max_size 20 = 80 PG connections (peak)
2 backend replicas × 2 workers/replica × min_size  5 = 20 PG connections (idle)

Plus: alembic migration tool, prewarm, prom_exporter, etc. ≈ 10 connections
Plus: pg-standby streaming replication  ≈ 1 connection

Total peak: ~91 connections
Total idle: ~31 connections
```

**PG `max_connections` config**:
- Current default: `100`
- **Required action**: raise to `200` in `deploy/postgres-ha/postgres-primary/postgresql.conf`
- Headroom: 2× peak = safety margin for future scale

This is a **one-time config change** in the PG cluster config file
(requires PG restart — can be done during the same downtime window).

### 2.3 Connection-level settings

Set at pool init via `init` callback:

```python
await asyncpg.create_pool(
    dsn,
    init=_set_conn_defaults,
    ...
)

async def _set_conn_defaults(conn: asyncpg.Connection) -> None:
    await conn.execute("SET timezone = 'UTC'")
    await conn.execute("SET statement_timeout = '30s'")
    await conn.execute("SET lock_timeout = '10s'")
    await conn.execute("SET idle_in_transaction_session_timeout = '60s'")
```

`idle_in_transaction_session_timeout` is critical — if a request opens a tx
and never commits/rollbacks (e.g., middleware exception), PG kills the session
after 60s rather than holding locks forever.

---

## 3. Transaction semantics

### 3.1 Read-only (default)

```python
rows = await conn.fetch("SELECT ... WHERE ... = $1", value)
# No tx needed — asyncpg handles each statement as implicit autocommit
```

### 3.2 Single-statement write

```python
await conn.execute("INSERT INTO ... VALUES ($1, $2)", a, b)
# asyncpg auto-commits — no explicit tx required
```

### 3.3 Multi-statement (atomic)

```python
async with conn.transaction():
    await conn.execute("DELETE FROM ... WHERE ...")
    for item in items:
        await conn.execute("INSERT INTO ... VALUES (...)")
# Auto-rollback on exception; auto-commit on normal exit
```

### 3.4 Nested tx (savepoints)

asyncpg detects inner `async with conn.transaction():` and uses savepoints
automatically. Useful for "try this block, rollback just this part on error,
continue outer tx":

```python
async with conn.transaction():           # outer
    await conn.execute("UPDATE ...")
    try:
        async with conn.transaction():   # savepoint
            await conn.execute("RISKY ...")
    except SomeExpectedError:
        pass  # savepoint rolled back; outer tx continues
    await conn.execute("UPDATE ...")
```

Used in: `insert_episodic_memory` (FTS failure should rollback just the
FTS sub-step, not the main insert).

### 3.5 Platform-wide tables (users/sessions/password_history)

These are NOT tenant-scoped. `conn` still passes through, but no `tenant_id`
parameter needed.

```python
async def get_user(conn: asyncpg.Connection, user_id: str) -> User | None:
    row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return User(**row) if row else None
```

---

## 4. Error handling strategy

### 4.1 Transient vs permanent errors

| asyncpg exception | Category | Action |
|---|---|---|
| `InterfaceError: connection lost` | Transient | Pool re-acquires automatically; retry 1x |
| `ConnectionDoesNotExistError` | Transient | Pool fetches fresh; retry 1x |
| `PostgresConnectionError` | Transient | 503 to caller; pool tries to reconnect |
| `UniqueViolationError` | Permanent (logic) | Let bubble up; router returns 409 Conflict |
| `ForeignKeyViolationError` | Permanent (logic) | Let bubble up; router returns 422 |
| `CheckViolationError` | Permanent (logic) | Let bubble up; router returns 422 |
| `QueryCanceledError` | Timeout (transient) | 503 with Retry-After |
| `TooManyConnectionsError` | Pool exhausted | 503 with Retry-After = 5s |

### 4.2 Pool exhaustion path

When 20 connections per worker all busy + pool queue full:
- `asyncpg.Pool.acquire()` with no `timeout` blocks forever (bad)
- **Set `timeout=10.0`** in `get_conn` dependency
- On timeout: raise `HTTPException(503, "DB pool exhausted, retry in 5s")`

### 4.3 Connection recovery

- asyncpg's pool auto-detects stale conn via `init` callback returning error
- On PG primary failover (G4 topology): pool drops all conn, re-creates from DSN which now points to new primary
- No app-level retry needed for failover — middleware returns 503 for ~2s window

---

## 5. FTS5 → PostgreSQL tsvector strategy

### 5.1 Alembic migration (new, to be written)

```sql
-- 0017_episodic_memory_tsvector.sql
ALTER TABLE episodic_memory
    ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(content, '') || ' ' ||
            coalesce(tags, '')
        )
    ) STORED;

CREATE INDEX episodic_memory_tsv_gin ON episodic_memory USING GIN(tsv);
```

`GENERATED ... STORED` means `tsv` is auto-maintained on every INSERT/UPDATE
— no trigger, no app-layer FTS insert, no rebuild function.

### 5.2 Query rewrite

**Before (SQLite FTS5):**
```python
SELECT m.* FROM episodic_memory m
JOIN episodic_memory_fts fts ON m.rowid = fts.rowid
WHERE episodic_memory_fts MATCH ?
ORDER BY rank
```

**After (PG tsvector):**
```python
SELECT *, ts_rank(tsv, plainto_tsquery('english', $1)) AS rank
FROM episodic_memory
WHERE tsv @@ plainto_tsquery('english', $1)
ORDER BY rank DESC
LIMIT $2
```

### 5.3 Functions affected

- `insert_episodic_memory()` — removes FTS5 INSERT sub-step (tsv auto-generated)
- `search_episodic_memory()` — rewrite to use `@@` + `plainto_tsquery`
- `delete_episodic_memory()` — removes FTS5 DELETE sub-step (tsv auto-deleted on row delete)
- `rebuild_episodic_fts()` — becomes `REINDEX INDEX episodic_memory_tsv_gin` or no-op (STORED col auto-maintained)

### 5.4 SQLite dev path

SQLite doesn't have tsvector. Options:
- **Dev uses SQLite still** — fall back to LIKE-based search (degraded but functional)
- **Dev migrates to PG** — requires local PG for dev, higher setup cost

**Decision**: dev uses LIKE fallback (matches original code's "LIKE fallback
when FTS5 unavailable" pattern). Prod/staging use PG tsvector. One `if IS_PG`
branch in `search_episodic_memory`.

---

## 6. Test strategy for 95% coverage

### 6.1 Coverage target scope

- `backend/db.py` — all 53 functions
- `backend/db_pool.py` — new module
- `backend/db_context.py` — existing, contextvar helpers
- `backend/audit.py` — 5 functions
- `backend/auth.py` — 22+ DB functions (exclude non-DB helpers)
- `backend/tenant_secrets.py` — 6 functions
- **Total**: ~87 functions, ~3000 lines

**Out of scope for coverage gate**:
- Routers (integration-tested via E2E tests, not coverage-gated)
- Non-router app files calling db (business logic; Tier 2+)

### 6.2 Test categories per DB function

Every function gets 3-5 test cases:

1. **Happy path** — expected input → expected output
2. **Empty result** — query returns 0 rows
3. **Tenant filter** — if tenant-aware, verify cross-tenant blocked
4. **Error path** — PG raises UniqueViolation / FK violation / NULL constraint
5. **Concurrency** — 10 parallel callers; verify no cross-contamination

### 6.3 Test infrastructure

- **Real PG in tests**: `OMNI_TEST_PG_URL` env var points to test PG instance
- **Per-test cleanup**: each test runs inside `async with conn.transaction(): ... raise SavepointRollback` pattern — no state leaks between tests
- **Fixtures**:
  - `pg_pool` (session-scoped) — creates test pool, runs alembic, yields pool
  - `pg_conn` (function-scoped) — borrows conn from pool, yields, auto-rollback
  - `tenant_ctx` — sets `current_tenant_id` contextvar to test tenant

### 6.4 Coverage gate implementation

Add to `backend/pytest.ini`:

```ini
[pytest]
addopts =
    --cov=backend.db
    --cov=backend.db_pool
    --cov=backend.db_context
    --cov=backend.audit
    --cov=backend.auth
    --cov=backend.tenant_secrets
    --cov-report=term-missing
    --cov-report=html
    --cov-fail-under=95
    --cov-branch
```

Runs on every `pytest` invocation; fails build if < 95%.

### 6.5 Concurrency test suite (Step 4)

Already scoped in earlier plan — 5 new modules:
- `test_db_pool_concurrency.py`
- `test_multi_tenant_isolation.py`
- `test_tx_boundary.py`
- `test_pool_exhaustion.py`
- `test_dashboard_stress_e2e.py`

---

## 7. Session-by-session roadmap (revised realistic estimate)

| # | Session | Scope | Commits |
|---|---|---|---|
| 1 | **Foundation** | 3-1 pool module + lifespan + get_conn dependency + PG max_connections=200 config | 2-3 |
| 2 | **Schema prep** | Alembic 0017 FTS5→tsvector migration + smoke tests | 1 |
| 3 | **db.py reads** | Port 22 read-only SELECT + 4 aggregate SELECT (26 fns) | 2-3 |
| 4 | **db.py writes** | Port 6 INSERT + 5 UPSERT + 4 UPDATE + 5 DELETE (20 fns) | 3-4 |
| 5 | **db.py tx + FTS5** | Port 5 multi-step tx + FTS5 rewrite (episodic_memory 5 fns) | 3-4 |
| 6 | **Adjacent files** | Port audit.py (5 fns) + auth.py (22 fns) + tenant_secrets.py (6 fns) | 3 |
| 7 | **Tier 1 routers** | Port system.py + tasks.py + auth.py + other hot routers (~40 callsites) | 4-6 |
| 8 | **Tier 2 non-router** | Port dag_storage + prompt_registry + tenant_egress + agents/tools + notifications + bootstrap + events + main (~80 callsites) | 4-6 |
| 9 | **Tier 3 remainder** | Port remaining routers + remaining app files (~130 callsites) | 4-6 |
| 10 | **Tests port** | Update 71 test files to new signatures; regression suite green | 4-6 |
| 11 | **Delete compat** | Remove db_pg_compat.py + test_db_pg_compat.py + imports | 1 |
| 12 | **Rate limit tuning** | backend/quota.py adjust | 1 |
| 13 | **Concurrency test suite** | Write 5 new test modules | 3 |
| 14 | **Coverage 95% iteration** | Fill coverage gaps until gate green | 2-4 |
| 15 | **HANDOFF + TODO** | Final documentation | 1 |
| 16 | **Deploy + verify** | Build + recreate + multi-user test + 24h observation | 1 |

**Estimated total**: **10-16 focused sessions** (realistically 2-3 weeks of
full-time work; longer if interrupted).

Total commits: **~45-50**, each sub-phase independently bisectable.

---

## 8. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hidden SQLite-ism not in audit | Medium | Medium | Per-batch regression suite catches; commit boundaries let us isolate |
| asyncpg tx semantic differs from expectation | Medium | High | Explicit `async with conn.transaction()` everywhere; no implicit tx assumptions |
| Pool exhaustion under unexpected load | Low | Medium | `timeout=10.0` + 503 + monitoring |
| PG `max_connections` not raised before deploy | Medium | High | Session 1 includes PG config change + PG restart |
| Test DB env var not set in CI | Low | Medium | Document `OMNI_TEST_PG_URL` requirement; CI config |
| 71 test files breaking en masse mid-port | High | High | Strict "commit-only-when-suite-green" rule per sub-phase |
| FTS5 functionality regression (different ranking) | Medium | Low | Add `test_episodic_memory_search_equivalence.py` comparing old vs new result sets |
| Depends propagation leaks into places it shouldn't | Low | Low | Code review per batch; lint rule if we have one |
| Prod downtime extends beyond estimate | Medium | Medium | **Stop + ship partial migration** if session count hits 20 — rollback via `git revert` if needed; operator keeps compat wrapper as safety net |

---

## 9. Operator approval checkpoints

Before starting each session:
- Review session's planned scope + risks
- Confirm approval to proceed
- After session end: review commits + test results before locking in

Specifically **Step 2 (sub-phase decomposition)** requires its own approval
after this design doc is signed off.

---

## Approval requested

Once operator approves this design doc:
1. Task #72 (Step 1) marked complete
2. Step 2 decomposition begins — granular sub-phase breakdown with estimated
   commits per sub-phase
3. Step 3-1 (pool foundation) starts execution


---

## 10. Operator approvals (2026-04-20)

The following four design questions were posed and answered; decisions are
now locked and must not be revisited mid-Phase without an explicit design
revision round.

### 10.1 PG `max_connections` 100 → 200 (infra config change)

**Approved**: agent has full authority to modify
`deploy/postgres-ha/postgres-primary/postgresql.conf` and trigger a
`pg_ctl reload` (or restart if the parameter requires it — this one does).
This is bundled into SP-1.1 in the sub-phase plan.

### 10.2 Test PG container setup

**Approved + included in scope**: operator does not have a test PG
environment yet. Setting one up is part of SP-1.2. Delivers:
- `docker-compose.test.yml` with a dedicated `pg_test` service (isolated
  volume, non-prod port, test-only password)
- `backend/tests/conftest.py` fixture `pg_pool` that creates the pool, runs
  alembic to HEAD, and yields it (session-scoped)
- `backend/tests/conftest.py` fixture `pg_conn` that borrows a connection,
  wraps it in a savepoint, and rolls back on test exit (function-scoped,
  no inter-test bleed)
- `OMNI_TEST_PG_URL` env var documented in `backend/tests/README.md`
  (or the nearest equivalent)
- CI path: if `OMNI_TEST_PG_URL` is set, tests run against real PG;
  if unset, PG-backed tests skip with a clear reason (keeps local dev
  lightweight)

### 10.3 FTS5 → tsvector ranking drift

**Approved**: SQLite FTS5 and PG tsvector use different ranking algorithms
(BM25 vs ts_rank_cd). Top-K result **ordering may shift**; result **set
equivalence** (same rows match-or-not) must be preserved. Test contract:
- Per-search-case test in `test_episodic_memory_search_equivalence.py`:
  given a fixed corpus + query, **same row IDs** appear in result set,
  even if order changes
- **No silent data loss** — if SQLite FTS5 matches a row and PG tsvector
  doesn't (or vice versa), test fails + issue flagged before merge
- `ts_rank` is used, not `ts_rank_cd`, to minimise algorithmic drift
  (`ts_rank_cd` considers cover density, more aggressive re-ordering)

### 10.4 Escape hatch (rollback safety net)

**Approved**: escape hatch MUST be in place from before Epic 1 commit #1.
Operator explicit wording: *"一勞永逸的方法，還是要買個保險，因為沒人知道
實際動手下去後會發生什麼事"*. Concrete implementation:

- **Git tag** `phase-3-runtime-v2-start` at current HEAD **before any
  Phase-3-Runtime-v2 work commits** — preserves the known-working
  (single-user) pre-migration state
- **`db_pg_compat.py` stays intact** through Epics 1-10 — both old and
  new code paths coexist until Epic 11 (delete)
- **Revert procedure documented** in `docs/phase-3-runtime-v2/03-escape-hatch.md`
  — any operator can `git reset --hard phase-3-runtime-v2-start` +
  rebuild + deploy to return to pre-migration state within ~15 minutes
- **Test-the-escape-hatch dry run** in SP-1.6: *before* any domain-slice
  port, verify the tag resets cleanly + prod deploy works from the tag
  — so the rollback isn't theoretical
- **When to pull the rip-cord**: stuck on a single sub-phase for >1
  session AND no obvious path forward → pull in second opinion /
  revert to tag + reassess, not "grind harder"
