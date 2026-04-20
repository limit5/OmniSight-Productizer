# Phase-3-Runtime-v2 — Step 1 Audit Findings + Scope Reality Check

> Date: 2026-04-20
> Status: **awaiting operator re-confirmation of scope + timeline before Step 2**

## TL;DR

Original scope quote was **"80 call sites in db.py, ~3 sessions"**. Actual audit
reveals **~400+ call sites across 132 files**, plus a **FTS5 → PostgreSQL
full-text search** port that is a sub-project of its own. Realistic timeline
for full Option-A execution is **5-10 sessions**, not 2-3.

Operator chose Option B (skip stopgap, do it properly, 95% coverage). That
philosophy still applies, but the **timeline + prod-downtime expectation
needs revising** before we start Step 2.

---

## Step 1-a: backend/db.py inventory (1676 lines, 53 async fns)

### Function-category breakdown

| Category | Count | Examples |
|---|---|---|
| Read-only SELECT | 22 | list_agents, get_agent, list_tasks, list_token_usage, list_notifications, list_artifacts, list_events, list_debug_findings, list_simulations, list_episodic_memories, get_npi_state, get_simulation, get_artifact, get_handoff, etc. |
| Aggregate SELECT | 4 | agent_count, task_count, count_unread_notifications, episodic_memory_count |
| INSERT + commit | 6 | insert_task_comment, insert_notification, insert_artifact, insert_simulation, insert_debug_finding, insert_event |
| UPSERT (INSERT ... ON CONFLICT) | 5 | upsert_agent, upsert_task, upsert_token_usage, upsert_handoff, save_npi_state |
| UPDATE + commit | 4 | mark_notification_read, update_notification_dispatch, update_simulation, update_debug_finding |
| DELETE + commit | 5 | delete_agent, delete_task, clear_token_usage, delete_artifact, cleanup_old_events, delete_episodic_memory |
| Multi-step tx (2+ stmts in 1 logical unit) | 6 | replace_decision_rules, insert_episodic_memory, search_episodic_memory, rebuild_episodic_fts, delete_episodic_memory |
| Startup / DDL / special | 3 | init, _migrate, close, execute_raw |

### SQLite-ism catalog (SOURCE vs TARGET column must be defined before port)

| Pattern | Sites | PG replacement |
|---|---|---|
| `datetime('now')` in SQL | 4 | `NOW()` or `CURRENT_TIMESTAMP` |
| `cur.lastrowid` | 0 in db.py (already ported to RETURNING elsewhere) | N/A |
| `cur.rowcount` | 6 | asyncpg: parse status string `"UPDATE 3"` |
| `INSERT OR IGNORE` | 1 (insert_debug_finding) | `ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE` | 0 | N/A |
| `PRAGMA ...` | 4 (init, close) | no-op on PG side |
| FTS5 `CREATE VIRTUAL TABLE ... USING fts5` | 1 (init) | **PG tsvector + GIN index** |
| FTS5 `... MATCH ?` syntax | 1 (search_episodic_memory) | **PG `tsquery @@ tsvector`** |
| FTS5 special `'delete' INSERT` | 1 (delete_episodic_memory) | **PG trigger or manual UPDATE on tsvector** |
| `rowid ASC` | 1 (find_admin_requiring_password_change in auth.py) | `ORDER BY id ASC` |
| Dynamic SQL construction (whitelisted columns) | 8 | OK — already parameterized where applicable |

### tenant_id isolation pattern

- **Uses `tenant_where()` / `tenant_insert_value()` helpers**: 8 functions (load/replace_decision_rules, list/get/delete_artifact, insert_artifact, list/update_debug_finding, list_events, insert_event, insert_debug_finding)
- **Platform-wide (NOT tenant-scoped by design)**: 15+ functions (agents, tasks, handoffs, notifications, token_usage, simulations, episodic_memory, token_usage) — this is intentional per I-series design: these tables are SHARED across tenants (e.g., `notifications` is per-user, not per-tenant; `agents` is global configuration)
- **All writes from `require_current_tenant()` boundary** (router layer `Depends`): not db-layer concern

### Transaction re-entry hotspots (highest migration risk)

These 6 functions call `_conn()` multiple times within one logical unit; under
pool-borrowed model they **must** be wrapped in `async with pool.acquire() as conn:`
blocks or passed an explicit conn parameter:

1. `replace_decision_rules()` — BEGIN IMMEDIATE + loop DELETE/INSERT + COMMIT
2. `insert_episodic_memory()` — main table INSERT + FTS5 INSERT (must succeed-or-rollback)
3. `search_episodic_memory()` — FTS SELECT → LIKE fallback → loop UPDATE
4. `rebuild_episodic_fts()` — DELETE + batch INSERT from main table
5. `delete_episodic_memory()` — FTS5 DELETE + main table DELETE

---

## Step 1-b: audit.py / auth.py / tenant_secrets.py

### audit.py (313 lines, 5 fns) — **low risk, clean tenant isolation**

- All 5 fns use `tenant_where()` or `tenant_insert_value()` correctly.
- `log()` uses `RETURNING id` (cross-DB portable)
- `_chain_lock` asyncio.Lock protects hash chain — tx-safe
- No aiosqlite-specific surface outside `_conn()` façade

### auth.py (983 lines, 22+ DB fns) — **medium risk, 2 complex tx**

- **Platform-wide scope (by design)**: users, sessions, password_history tables have NO tenant_id — these are shared across all tenants. Confirmed via I-series schema docs.
- **Multi-step tx flows** (3 helpers expect SAME conn across multiple calls):
  - `change_password()` → `_record_password_history(conn, ...)` → UPDATE → commit
  - `authenticate_password()` → `_reset_login_failures(conn)` OR `_record_login_failure(conn)` → commit
  - `flag_all_admins_must_change_password()` — SELECT all + loop UPDATE
- **SQLite-ism**:
  - `rowid ASC` in `find_admin_requiring_password_change()` → replace with `id ASC`
  - `datetime('now')` in 3 UPDATE paths → `NOW()`
- **Direct `_conn()` use from routers**: `backend/routers/auth.py` uses `from backend.db import _conn` for raw user-table SQL

### tenant_secrets.py (162 lines, 6 DB fns) — **low risk, 1 concurrency bug**

- All fns properly use `require_current_tenant()` + WHERE tenant_id filter
- **Existing bug** (pre-existing, not our doing): `upsert_secret()` calls `_conn()` 2-3x separately (SELECT / UPDATE-or-INSERT / commit) — each borrows fresh conn under pool model, which breaks atomicity. Must be fixed during port.
- One unused `import aiosqlite` at line 23 — remove during port

---

## Step 1-c: External caller audit — **this is where scope exploded**

### Quantified call graph

| Category | Files | Call sites |
|---|---|---|
| **Routers** (`backend/routers/*.py`) | 19 | ~70 |
| **Non-router app code** (`backend/*.py`) | 40 | ~130+ |
| **Tests** (`backend/tests/` + `tests/`) | 71 | 200+ |
| **Scripts** (`scripts/*.py`) | 2 | 2 |
| **TOTAL** | **132 files** | **~400+ call sites** |

### Router hotspots (Tier 1 — must-update-first)

- `routers/system.py` — 11 imports, ~15 call sites
- `routers/tasks.py` — ~9 call sites
- `routers/auth.py` — 4 endpoints, mixed `_conn` + `db` imports

### Non-router hotspots (Tier 2 — heavy internal DB users)

- `backend/tenant_egress.py` — ~13 call sites
- `backend/dag_storage.py` — ~14 call sites
- `backend/prompt_registry.py` — ~13 call sites
- `backend/agents/tools.py` — ~9 call sites
- `backend/notifications.py` — ~7 call sites
- `backend/bootstrap.py` — ~6 call sites

### Tests (Tier 3 — must stay green throughout)

- 71 test files import DB layer directly
- Heavy: `test_audit.py` (15), `test_rls.py` (25+), `test_tenant_secrets.py` (13), `test_artifacts.py` (9)
- **Coverage target (95%) is for DB-layer modules, not these tests** — but tests must all still pass after each sub-phase commit

---

## Step 1-d: Key design decisions (still TBD)

These need operator sign-off before Step 2:

1. **Propagation depth**: does `conn = Depends(get_conn)` propagate through every layer (router → service function → db function), or does it stop at router/service and db.py uses a contextvar-borrowed conn?
2. **FTS5 strategy**: port to PG tsvector + GIN (clean, ~1 session of work), or drop FTS functionality entirely (simpler but breaks episodic_memory search UI)?
3. **Pool params**: `min_size=5, max_size=20` per worker × 2 workers per replica × 2 replicas = 80 PG connections max. PG default `max_connections=100`. Need to confirm deployment has headroom.
4. **tx model**: savepoint-based nested tx, or flat tx only (re-design `change_password` etc. to not pass conn around)?
5. **Test strategy for 95%**: use real PG in CI (needs `OMNI_TEST_PG_URL` env) vs mock everything (faster, brittle)?

---

## Scope reality check summary

| Estimate | Original | Actual |
|---|---|---|
| db.py call sites | 80 | 53 functions × avg 4 callers = ~200 |
| External files touched | db.py only | **132 files** |
| Total call sites | 80 | **~400+** |
| FTS5 port sub-project | not mentioned | **required** (1 extra session) |
| Test files to keep green | ~20 | **71** |
| Sessions needed | 3 | **5-10** |

This is not a blocker to the plan — just a **scope realization that operator
must acknowledge** before we begin Step 2. The `95% coverage` requirement
combined with 400+ call sites means **extensive test writing**, which is
where most of the time will go (not the production code).
