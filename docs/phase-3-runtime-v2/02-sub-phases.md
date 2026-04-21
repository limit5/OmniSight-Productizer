# Phase-3-Runtime-v2 — Sub-phase Decomposition (Step 2 deliverable)

> Each row below = **one commit** (one SP = one sub-phase = one commit).
> Bisectable: reverting any single SP leaves the codebase compile-able +
> tests passing (except SP that explicitly depend on earlier SP — noted).
>
> **Guiding rule**: per-commit acceptance test must pass before commit. If
> the test suite is red at commit time, commit is blocked — fix or revert
> the in-flight change first.
>
> **Domain slicing**: each port SP handles one domain (e.g., "agents",
> "tasks") — db.py functions + all callers + all tests in one consistent
> commit.

Column legend:
- **SP #** — sub-phase / commit ID
- **Files** — files touched (bold = primary; italics = caller updates)
- **Tests** — test files added or updated
- **LOC** — rough line count (production code + test code combined)
- **Dep** — depends-on SP (must land first)
- **Accept** — acceptance criterion (must hold to commit)

---

## Epic 1: Foundation + Escape Hatch

Session 1. Establishes pool infrastructure, test PG, and safety net.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **1.0** | Escape-hatch tag + rollback docs | `docs/phase-3-runtime-v2/03-escape-hatch.md` (done), `git tag phase-3-runtime-v2-start` | N/A (docs) | 0 | — | Tag exists locally + operator confirms |
| **1.1** | PG `max_connections` 100→200 | `deploy/postgres-ha/postgres-primary/postgresql.conf`, `deploy/postgres-ha/docker-compose.yml` (if env-driven) | `test_pg_ha_config.py` (snapshot test) | 20 | 1.0 | PG restarts + accepts `SHOW max_connections` = 200 |
| **1.2** | Test PG container + fixtures | **`docker-compose.test.yml`**, **`backend/tests/conftest.py`**, `backend/tests/README.md` | `test_pg_test_container.py` (self-test: can connect, can run alembic, can rollback savepoint) | 150 | 1.1 | `pytest backend/tests/test_pg_test_container.py` green, creates + tears down container cleanly |
| **1.3** | `backend/db_pool.py` module | **`backend/db_pool.py`** (new) | `test_db_pool.py` — init/close/acquire/release/exhaust/timeout unit tests (8 tests) | 250 | 1.2 | `pytest backend/tests/test_db_pool.py --cov=backend.db_pool --cov-fail-under=95` green |
| **1.4** | Lifespan wiring + `get_conn` dependency | **`backend/main.py`** (lifespan adds `init_pool`/`close_pool`), **`backend/db_pool.py`** (add `get_conn`) | `test_db_pool_lifespan.py` — app startup creates pool, app shutdown closes it, get_conn yields live conn | 100 | 1.3 | App boots + `curl /readyz` returns `pool: healthy` |
| **1.5** | `/readyz` deep-check DB via pool | **`backend/routers/health.py`** (probe uses pool, not compat) | `test_readyz_with_pool.py` — readyz green when pool healthy, 503 when pool down | 80 | 1.4 | `pytest backend/tests/test_readyz_with_pool.py` green + manual `curl` confirms JSON shape |
| **1.6** | Escape-hatch drill (rollback test) | `docs/phase-3-runtime-v2/03-escape-hatch.md` (append drill log), one trivial throwaway commit to exercise the path | N/A | 10 | 1.5 | Drill completes in <15 min; decision log entry added |

**Epic 1 commits: 7. Epic 1 exit criteria**: pool foundation live in prod,
rollback drill passed, escape hatch proven functional.

---

## Epic 2: Schema Prep (Alembic 0017 FTS5 → tsvector)

Session 2. Adds the PG-native full-text column alongside SQLite FTS5 so both
can coexist until Epic 11 deletes compat.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **2.1** | Alembic 0017 migration | **`backend/alembic/versions/0017_episodic_memory_tsvector.py`** | `test_alembic_0017_tsvector.py` — upgrade idempotent, column exists, GIN index exists, STORED generation works | 150 | 1.6 | Migration runs on fresh PG + on 0016-state PG without error; `\d+ episodic_memory` shows new col + idx |
| **2.2** | SQLite compat-mode fallback | **`backend/db_pg_compat.py`** (if SQLite backend, fall back to LIKE search — no schema change needed since SQLite keeps FTS5 intact) | `test_episodic_memory_dev_fallback.py` — dev SQLite path still returns results via LIKE | 40 | 2.1 | Dev test passes on SQLite; prod path unchanged |

**Epic 2 commits: 2.** Schema ready for Epic 3.12's FTS5 code rewrite.

---

## Epic 3: db.py Domain-Slice Ports — ✅ COMPLETE (2026-04-20)

All 13 sub-phases landed. 48 domain CRUD functions ported to
``conn: asyncpg.Connection`` + FastAPI-Depends propagation. The
compat wrapper (``db._conn()``) is still alive — it now services
**only** the 16+ files flagged for Epic 7 (audit.py, tenant_secrets.py,
memory_decay.py, bootstrap.py, dag_storage.py, etc.) plus the one
remaining direct caller inside db.py itself (``execute_raw``).

**Completion summary** (from commits `2eeaa55d..a47de1d1`):

| SP | Commit | Files | LoC net | Notes |
|---|---|---|---|---|
| 3.1 agents | 2eeaa55d | 11 | +614 | Pool foundation + agents CRUD |
| 3.2 tasks | 90a759fd | 14 | +476 | + caught + fixed SP-3.1 invoke.py regression |
| 3.3 handoffs | b435e8a0 | 8 | +267 | polymorphic wrapper + clock_timestamp fix |
| 3.4 notifications | 194f8cc4 | 9 | +396 | pool-ownership consolidation in pg_test_pool |
| 3.5 token_usage | 9f25a702 | 6 | +177 | first tight-scope slice under new calibration |
| 3.6a artifacts runtime | d449885a | 16 | +360 | tenant-scoped port + split-commit pattern |
| 3.6b artifacts tests | f6c7a9ed | 5 | +39 | ancillary test migrations |
| 3.7 npi_state | 8e12cbcd | 6 | +125 | smallest slice |
| 3.8 simulations | c15038a0 | 9 | +273 | whitelist-driven update SQL |
| 3.9 debug_findings | f29ae97b | 12 | +327 | promoted tenant_where_pg helper |
| 3.10 event_log | 5e1e0bc3 | 8 | +259 | fixed cleanup tenant-leak + SP-3.5 flake |
| 3.11 decision_rules | c1cd15a1 | 6 | +236 | atomic-replace rollback contract locked |
| 3.12 episodic_memory | a47de1d1 | 12 | +404 | FTS5 → tsvector search port |
| 3.13 closing gate | (this commit) | 2 | +170 | test_db_startup + doc completion |

Sessions 3-5. Each SP ports one domain's db.py functions + all callers +
all tests in one commit. The function signatures change to `(conn, ...)`
FastAPI-Depends-propagated form; callers updated in the same commit so
there's no in-flight inconsistency.

**Rule**: each SP keeps the old `_conn()`-based functions removed and the
new `conn`-param functions added. After each SP, the compat wrapper still
services **other** domains not yet ported.

| SP # | Slice | db.py fns | External callers | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|---|
| **3.1** | Agents | `list_agents`, `get_agent`, `upsert_agent`, `delete_agent`, `agent_count` | `routers/agents.py`, `agents/tools.py`, tests | `test_db_agents.py` (refactor to use pg_conn fixture; 15 cases) | 300 | 2.2 | All agent-related tests green + routers still respond 200 on `/api/v1/agents` |
| **3.2** | Tasks | `list_tasks`, `get_task`, `upsert_task`, `delete_task`, `task_count`, `insert_task_comment`, `list_task_comments` | `routers/tasks.py`, `decision_engine.py`, `agents/tools.py`, tests | `test_db_tasks.py` (20 cases) | 400 | 3.1 | `/api/v1/tasks` endpoints green; task_comments preserved |
| **3.3** | Handoffs | `upsert_handoff`, `get_handoff`, `list_handoffs` | `backend/handoff.py` | `test_db_handoffs.py` (9 cases) | 200 | 3.2 | Handoff persistence round-trips + `datetime('now')` → `NOW()` verified |
| **3.4** | Notifications | `insert_notification`, `list_notifications`, `mark_notification_read`, `count_unread_notifications`, `update_notification_dispatch`, `list_failed_notifications` | `routers/profile.py`, `routers/system.py`, `backend/notifications.py`, `backend/events.py` | `test_db_notifications.py` (18 cases) | 450 | 3.3 | Notification fan-out works + unread count correct per tenant |
| **3.5** | Token usage | `list_token_usage`, `upsert_token_usage`, `clear_token_usage` | `routers/system.py`, `routers/observability.py`, `backend/agents/tools.py` | `test_db_token_usage.py` (9 cases) | 200 | 3.4 | `/runtime/tokens` returns correct shape; no divergence from shared_state |
| **3.6** | Artifacts (tenant-scoped) | `insert_artifact`, `list_artifacts`, `get_artifact`, `delete_artifact` | `routers/artifacts.py`, `backend/artifact_pipeline.py`(?), tests | `test_db_artifacts.py` (existing, update fixtures) | 300 | 3.5 | Tenant-A cannot see Tenant-B artifacts (regression test) |
| **3.7** | NPI state | `get_npi_state`, `save_npi_state` | `routers/system.py` | `test_db_npi_state.py` (6 cases) | 150 | 3.6 | save + get round-trip; `:data` placeholder fully replaced |
| **3.8** | Simulations | `insert_simulation`, `get_simulation`, `list_simulations`, `update_simulation` | `routers/simulations.py`, `backend/agents/tools.py` | `test_db_simulations.py` (12 cases) | 300 | 3.7 | Simulation CRUD + whitelist column update safe |
| **3.9** | Debug findings (tenant) | `insert_debug_finding`, `list_debug_findings`, `update_debug_finding` | `backend/events.py`, `routers/system.py` | `test_db_debug_findings.py` (10 cases) | 250 | 3.8 | `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING` verified + tenant filter |
| **3.10** | Events (tenant) | `insert_event`, `list_events`, `cleanup_old_events` | `backend/events.py`, `routers/events.py`, `routers/safety.py` | `test_db_events.py` (12 cases) | 300 | 3.9 | Event bus still delivers + cleanup removes old rows |
| **3.11** | Decision rules (tenant) | `load_decision_rules`, `replace_decision_rules` | `backend/decision_rules.py`, `backend/decision_profiles.py` | `test_db_decision_rules.py` (8 cases) | 250 | 3.10 | Multi-step tx atomic — all-rules-replaced or none |
| **3.12** | **Episodic memory + FTS5 port** (the big one) | `insert_episodic_memory`, `search_episodic_memory`, `rebuild_episodic_fts`, `get_episodic_memory`, `list_episodic_memories`, `delete_episodic_memory`, `episodic_memory_count` | `backend/memory_decay.py`, `backend/rag_prefetch.py`, `backend/agents/tools.py` | `test_db_episodic_memory.py` (existing, rewrite for tsvector) + `test_episodic_memory_search_equivalence.py` (new — SQLite FTS5 vs PG tsvector result-set equivalence per I10.3) | 600 | 3.11 | **Search result-set equivalence** (ranking may differ, row IDs identical); FTS5 code paths removed; LIKE fallback still works in dev SQLite |
| **3.13** | Startup / init / close / migrate | `init`, `_migrate`, `close`, `execute_raw` | `backend/main.py`, `backend/bootstrap.py`, `backend/routers/health.py` | `test_db_startup.py` — init idempotent, close closes pool, _migrate no-op on PG (alembic owns) | 200 | 3.12 | App boot + shutdown clean; no dangling connections in PG `pg_stat_activity` |

**Epic 3 commits: 14** (13 domain slices + 3.6 split into 3.6a/3.6b).
**Epic 3 exit criteria (verified)**: all 48 domain CRUD functions
ported; lifecycle + helper functions unchanged; compat wrapper still
in place (used by un-ported auth/audit/tenant_secrets/memory_decay
etc.); domain-level router endpoints still work via the new pool
path; new `test_db_startup.py` locks the init/close/execute_raw
contracts so Epic 7's compat deletion has a regression guard.

**Deferred follow-ups tracked as separate tasks:**
- Task #90: `auth_baseline` cross-test pollution (surfaced SP-3.8).
- Task #93: port `memory_decay.py` off compat wrapper (Epic 7 prep).

---

## Epic 4: Adjacent Files Port (audit.py, auth.py, tenant_secrets.py)

Session 6. Platform-wide (not tenant-scoped) data access layer. Complex tx
flows in auth.py get own SP.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **4.1** | audit.py port | **`backend/audit.py`** (5 DB fns + `_chain_lock` integration) + callers | `test_audit.py` (existing, update) | 400 | 3.13 | Hash chain preserves cross-commit; `verify_chain()` + `verify_all_chains()` green |
| **4.2** | auth.py user CRUD | `get_user`, `get_user_by_email`, `create_user`, `find_admin_requiring_password_change`, `ensure_default_admin` | `test_auth_users.py` (new, extract from test_auth.py) | 350 | 4.1 | User table queries; `rowid` → `id ASC` swap verified |
| **4.3** | auth.py session CRUD | `create_session`, `get_session`, `update_session_metadata`, `delete_session`, `cleanup_expired_sessions`, `rotate_session`, `rotate_user_sessions`, `list_sessions`, `revoke_session`, `revoke_other_sessions` | `test_auth_sessions.py` | 500 | 4.2 | Session lifecycle correct; throttled last_seen_at update preserved |
| **4.4** | auth.py password history + auth flow (complex tx) | `change_password`, `authenticate_password`, `_record_password_history`, `_record_login_failure`, `_reset_login_failures`, `is_account_locked`, `check_password_history` | `test_auth_passwords.py`, `test_auth_login_tx.py` (new — atomic tx around login failure counter) | 500 | 4.3 | Login failure count + lockout atomic; password history trimmed to N |
| **4.5** | auth.py misc | `flag_all_admins_must_change_password` + any residual | Existing tests | 100 | 4.4 | Admin flag migration safe |
| **4.6** | tenant_secrets.py port + upsert tx fix | **`backend/tenant_secrets.py`** (6 fns, `upsert_secret` atomicity bug fixed) | `test_tenant_secrets.py` (existing, update fixtures) + `test_tenant_secrets_upsert_atomic.py` (new — concurrent upsert doesn't race) | 300 | 4.5 | Upsert is single-tx; cross-tenant filter verified |

**Epic 4 commits: 6. Epic 4 exit criteria**: auth / audit / secrets all
on pool. Compat wrapper no longer used by any of the ported files.

---

## Epic 5: Residual Callers (Tier 2 + Tier 3)

Session 7-8. Files not caught by domain slicing that still call `_conn()` or
old-style db functions. These are mostly thin wrappers and background code.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **5.1** | `backend/dag_storage.py` | ~14 callsites | `test_dag_storage.py` | 400 | 4.6 | DAG persistence round-trips |
| **5.2** | `backend/prompt_registry.py` | ~13 callsites | `test_prompt_registry.py` | 400 | 5.1 | Prompt CRUD + version list |
| **5.3** | `backend/tenant_egress.py` | ~13 callsites | `test_tenant_egress.py` | 400 | 5.2 | Egress policy + allowlist per tenant |
| **5.4** | `backend/agents/tools.py` — ✅ **no-op (done early)** | ~9 callsites, **already pool-native** — all DB touch-points went through `db.*` pool-aware helpers consumed during SP-3.2 / SP-3.4 / SP-3.6 / SP-3.8 / SP-3.12 | `test_tools.py` (existing, 60 tests pass) | 0 | 5.3 | no port work; plan-doc marker only (2026-04-21). Module-globals audit: `_active_workspace` + `_active_agent_id` are `contextvars.ContextVar`, answer (1) per-context stable; `WORKSPACE_ROOT` / `BASH_TIMEOUT` / regex constants are import-time deterministic. No cross-worker coordination needed. |
| **5.5** | Mid-tier app code batch 1 | `backend/events.py`, `backend/notifications.py`, `backend/bootstrap.py`, `backend/main.py` lifespan bits, `backend/lifecycle.py` | Corresponding tests | 600 | 5.4 | Startup + notification dispatcher OK |
| **5.6** | Mid-tier app code batch 2 | `backend/decision_engine.py`, `backend/workflow.py`, `backend/intent_memory.py`, `backend/release.py`, `backend/slash_commands.py`, `backend/report_generator.py`, `backend/project_runs.py`, `backend/workspace.py`, `backend/handoff.py` | Corresponding tests | 800 | 5.5 | Decision engine path still works E2E |
| **5.7** | Mid-tier app code batch 3 | `backend/api_keys.py`, `backend/mfa.py`, `backend/memory_decay.py`, `backend/project_report.py`, `backend/iq_nightly.py`, `backend/tenant_quota.py`, `backend/github_app.py`, `backend/chatops_handlers.py`, `backend/finetune_export.py` | Corresponding tests | 600 | 5.6 | Admin + nightly jobs safe |
| **5.8** | Router batch 1 (auth + secrets + preferences + webhooks) | `routers/auth.py`, `routers/secrets.py`, `routers/preferences.py`, `routers/webhooks.py` | Corresponding tests | 500 | 5.7 | Auth/bootstrap flows green |
| **5.9** | Router batch 2 (rest) | `routers/bootstrap.py`, `routers/profile.py`, `routers/observability.py`, `routers/integration.py`, `routers/simulations.py`, `routers/workspaces.py`, `routers/health.py`, etc. | Corresponding tests | 600 | 5.8 | Full router suite green |
| **5.10** | Scripts | `scripts/backfill_project_runs.py`, `scripts/migrate_sqlite_to_pg.py` | `test_scripts_smoke.py` | 150 | 5.9 | Backfill script dry-run works |

**Epic 5 commits: 10.** All code is now pool-based.

---

## Epic 6: Test Suite Consistency Pass

Session 9. Some tests were updated en route (in Epic 3/4/5). Others (lots of
integration tests) may need fixture-conversion. This epic sweeps the remaining
71 test files for any remaining references to `_conn()` / old `db.X()` signatures.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **6.1** | Test fixture audit + batch fix #1 | ~20 test files | N/A | 500 | 5.10 | pytest suite green, no `_conn()` in test code |
| **6.2** | Test fixture batch #2 | ~20 test files | N/A | 500 | 6.1 | same |
| **6.3** | Test fixture batch #3 | ~20 test files | N/A | 500 | 6.2 | same |
| **6.4** | Test fixture batch #4 (residual) | ~11 test files | N/A | 300 | 6.3 | Zero grep hits for `_conn(` in tests |

**Epic 6 commits: 4.**

---

## Epic 7: Cleanup — Delete Compat Wrapper

Session 10. The one-way door. Should be tiny.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **7.1** | Delete db_pg_compat.py + its test | Delete `backend/db_pg_compat.py`, `backend/tests/test_db_pg_compat.py` | N/A | -900 | 6.4 | zero grep hits for `db_pg_compat` across repo (`lib/`, `scripts/`, `backend/`, `tests/`); full pytest suite green |
| **7.2** | Remove `translate_sql_and_params` imports | `backend/db.py` (any leftover), other imports | N/A | -50 | 7.1 | zero grep hits for `translate_sql` |

**Epic 7 commits: 2.** From now on there's no safety net back to compat
wrapper behaviour. Rollback = `phase-3-runtime-v2-start` tag.

---

## Epic 8: Rate Limit Tuning

Session 10. Config-only.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **8.1** | Free plan per-IP 60/60s → 300/60s | **`backend/quota.py`** | `test_rate_limit_free_plan.py` — updated numbers | 30 | 7.2 | Dashboard load does not 429 under single-user |

**Epic 8 commits: 1.**

---

## Epic 9: Concurrency + Multi-user Test Suite

Session 11.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **9.1** | Pool concurrency | `test_db_pool_concurrency.py` — 50 asyncio tasks hit pool, zero `another operation in progress` | N/A | 250 | 8.1 | Test green on CI PG + local PG |
| **9.2** | Multi-tenant isolation | `test_multi_tenant_isolation.py` — A/B parallel reads + writes, zero cross-tenant leak | N/A | 300 | 9.1 | Isolation verified under 20 parallel workers |
| **9.3** | Tx boundary | `test_tx_boundary.py` — explicit rollback, exception rollback, savepoint nested, concurrent tx | N/A | 250 | 9.2 | All 4 scenarios green |
| **9.4** | Pool exhaustion | `test_pool_exhaustion.py` — fill pool + queue + timeout, verify 503 shape | N/A | 200 | 9.3 | `timeout=10s` + proper 503 verified |
| **9.5** | E2E dashboard stress | `test_dashboard_stress_e2e.py` — 5 simulated tabs × 11 endpoints × every 5s × 60s duration | N/A | 400 | 9.4 | zero 500 / zero asyncpg error / 429 gated correctly by I9 per-tenant limiter |

**Epic 9 commits: 5.**

---

## Epic 10: Coverage Gate

Session 12.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **10.1** | Initial coverage measurement | `backend/pytest.ini` add `--cov=...` + `--cov-fail-under=95` | N/A | 20 | 9.5 | coverage report produced; gap list generated |
| **10.2** | Fill gaps iteration 1 | Various `test_*.py` | N/A | 500 | 10.1 | measurable coverage increase |
| **10.3** | Fill gaps iteration 2 | Various `test_*.py` | N/A | 500 | 10.2 | same |
| **10.4** | Fill gaps iteration N (until green) | Various `test_*.py` | N/A | ? | 10.3 | `pytest --cov-fail-under=95` **green** |

**Epic 10 commits: 4-6** (depends on how many iterations needed).

---

## Epic 11: HANDOFF + Deploy + Verify

Session 13.

| SP # | Scope | Files | Tests | LOC | Dep | Accept |
|---|---|---|---|---|---|---|
| **11.1** | HANDOFF entry | `HANDOFF.md` prepend v2 entry | N/A | 300 | 10.4 | Operator-reviewable summary |
| **11.2** | TODO update | `TODO.md` — G4 `[D]-runtime → [x]`; mark Phase-3-Runtime v1 deprecated | N/A | 50 | 11.1 | TODO reflects reality |
| **11.3** | Build + deploy | `docker compose -f docker-compose.prod.yml build backend-a frontend` + recreate | N/A | 0 | 11.2 | all containers `Up (healthy)`; `/readyz` green + pool stats healthy |
| **11.4** | Operator multi-user verify | N/A (manual) | N/A | 0 | 11.3 | Operator confirms: fresh private window login + 2-3 parallel tabs + dashboard + SSE all stable; 24h observation window opens |

**Epic 11 commits: 3** (+ 1 post-24h observation commit if needed).

---

## Grand total

| Epic | Commits | Session (aggressive) | Session (quality-first, operator directive) |
|---|---|---|---|
| 1 — Foundation | 7 | 1 | 2 |
| 2 — Schema prep | 2 | 2 | 2 |
| 3 — db.py domain slices | 13 | 3-5 | 5-7 |
| 4 — Adjacent files | 6 | 6 | 2-3 |
| 5 — Residual callers | 10 | 7-8 | 3-4 |
| 6 — Test consistency | 4 | 9 | 2 |
| 7 — Delete compat | 2 | 10 (part) | 1 |
| 8 — Rate limit | 1 | 10 (part) | 0.5 |
| 9 — Concurrency tests | 5 | 11 | 2 |
| 10 — Coverage gate | 4-6 | 12 | 2-3 |
| 11 — HANDOFF + deploy | 3-4 | 13 | 1 |
| **TOTAL** | **57-60** | **~13 sessions** | **≥20 sessions** |

### Operator quality directive (2026-04-20)

Operator explicit: *"目標是品質最大化，系統最穩定。盡量不要為了減少工時和
成本而讓後面埋下更多的問題"*.

Applied to this plan:
- **Aggressive 13-session estimate is REJECTED** as the target. Agent
  plans for **≥20 sessions** with a conscious bias toward more tests,
  more edge-case coverage, more defensive code, more intermediate
  smoke tests — rather than fewer.
- When two approaches appear equivalent in final state, pick the one
  with better observable failure modes / more logging / more tests —
  even if it costs 20% more effort.
- If a single SP's acceptance criteria are marginal, **split it into
  two SPs** rather than forcing a marginal commit through. The 57-60
  commit count is a floor, not a ceiling.
- Edge cases that look unlikely still get a test case. Better to
  delete a test that turns out to be irrelevant than to skip writing
  it and discover the case matters in production.
- Each Epic's exit criteria include a **deliberate pause for reflection**
  — checkpoints are not just operator gates but agent self-review
  moments. If anything feels off, flag it before proceeding.

This is not a license to over-engineer. It IS a license to
prioritise correctness + observability over velocity. Trade-off
goes: **quality > stability > coverage % > commit count > speed**.

---

## Approval checkpoints embedded in the plan

Operator explicit confirmation required at:

1. **After SP-1.6** — rollback drill passed; approve to start domain-slice ports
2. **After Epic 4 (SP-4.6)** — all core DB layer ported; approve to continue to residual callers
3. **After Epic 7 (SP-7.2)** — **one-way door**; approve deletion of compat wrapper
4. **After Epic 10 (coverage green)** — approve to deploy
5. **After SP-11.3 (deploy)** — approve to close Phase (or initiate rollback if anything looks off during 24h window)

Each checkpoint = agent pauses, summarises status, operator responds go/no-go.

---

## What I am NOT promising

- **Zero test failures mid-session**: tests may red intermittently within a
  session while actively editing; they must green at each commit boundary.
- **No new bugs discovered**: porting may surface dormant SQLite-era bugs
  (e.g., a NULL that SQLite silently handled differently from PG). These
  get their own hot-fix SP if critical, or a TODO entry if deferrable.
- **Exact session count**: 13 is target, but test-writing Epic 10 could
  take longer if coverage gaps are clustered in hard-to-test code paths
  (e.g., error-recovery branches).
- **Final HEAD commit hash predictable**: commits will be identified by
  message at review time, not by pre-assigned hash.

---

## Ready to execute

After operator approves this sub-phase decomposition:

1. Task #73 (Step 2) marked complete
2. Step 3-1 execution begins — **SP-1.0 escape-hatch tag first**, no other code changes until tag + docs committed
3. Each subsequent SP creates its own commit with 3 co-authors trailer

---

## Actual execution log (2026-04-21 snapshot)

This section is the **reality** view — the estimates above are the
at-Step-2 projections, this is what actually landed. Kept verbatim
so we can retro: what was mis-estimated, what surfaced unexpectedly,
what deferred.

### Epic 3 — domain slices (✅ COMPLETE)

| SP | Commit | Actual notes |
|---|---|---|
| 3.1 | `2eeaa55d` | agents — polymorphic `conn=None` pattern established here, reused everywhere after |
| 3.2 | `90a759fd` | tasks + task_comments + regression hunt for 21 invoke.py callsites |
| 3.3 | `b435e8a0` | handoffs — clock_timestamp() vs now() discovery |
| 3.4 | `194f8cc4` | notifications — pool ownership consolidated into `pg_test_pool` fixture |
| 3.5 | `9f25a702` | token_usage — Epic 2 alembic drift caught, test flake resolved in 3.10 |
| 3.6a/b | `d449885a` / `f6c7a9ed` | artifacts split (port + test-migration) — first real a/b split |
| 3.7 | `8e12cbcd` | npi_state |
| 3.8 | `c15038a0` | simulations — auth_baseline_mode pollution surfaced (task #90) |
| 3.9 | `f29ae97b` | debug_findings + `tenant_where_pg` helper promoted |
| 3.10 | `5e1e0bc3` | event_log — cleanup tenant-leak fix + SP-3.5 flake eliminated |
| 3.11 | `c1cd15a1` | decision_rules |
| 3.12 | `a47de1d1` | episodic_memory + FTS5→tsvector — tokenisation drift caught |
| 3.13 | `e33f4866` | closing gate — lifecycle contract tests |

### Epic 4 — adjacent files (✅ COMPLETE)

| SP | Commit | Actual notes |
|---|---|---|
| 4.1 | `448e70f1` | audit — `pg_advisory_xact_lock` recipe established (bonus bug #1) |
| 4.2 | `7d1b681a` | users CRUD |
| 4.3a | `2578e108` | simple session CRUD |
| 4.3b | `5a54e863` | rotate + FOR UPDATE + advisory lock (bonus bug #2) |
| 4.4 | `18a26cbe` | **password flow + atomic-increment fix (bonus bug #3 — lockout bypass)** |
| 4.5 | `1fe4d063` | flag_all_admins + atomic UPDATE RETURNING |
| 4.6 | `4e0c1ba8` | tenant_secrets + ON CONFLICT (bonus bug #4) |

### Epic 5 — residual callers (🟢 IN PROGRESS — 7 of 10 slices)

| SP | Commit | Planned LOC | Actual LOC | Notes |
|---|---|---|---|---|
| 5.1 | `9b7a6550` | 400 | 238 | dag_storage; workflow_runs link fixed |
| 5.2 | `4c8ac004` | 400 | 165 | prompt_registry — promote_canary return-type latent bug fixed too |
| 5.3 | `0a10fb6b` | 400 | 342 | tenant_egress — `_dns_cache` first "answer (3) drift" classification |
| 5.4 | `a85b38da` | 350 | 0 | **no-op** — already pool-native from Epic 3 ports |
| 5.5 | `413ab172` | 600 | 156 | events/notifications/lifecycle/main already pool; only bootstrap had 6 compat |
| 5.6a | `33489790` | (split of 800) | 205 | workflow.py — optimistic lock via RETURNING (bonus bug #5 + timing note #6) |
| 5.6b | `f8c833b8` | (split of 800) | 131 | decision_engine 1 fn + project_runs 6 fns (bonus bug #7) |
| 5.7a | `40c36faf` | (split of 600) | 131 | api_keys — unsticks test_bearer_session_fingerprint |
| 5.7b | `9ca9131e` | (split of 600) | 202 | mfa — atomic backup code (bonus bug #8) + generate tx (bonus bug #9); 2 module-globals flagged for #116 |
| 5.7c | `5aaf0b4d` | (split of 600) | 88 | 5 small files; unsticks test_github_installation — **last Epic-5 skip cleared** |
| 5.8 | (pending) | 500 | — | routers batch 1 |
| 5.9 | (pending) | 600 | — | routers batch 2 |
| 5.10 | (pending) | 150 | — | scripts |

**Plan-vs-actual observations**:
  * **Epic 5 is running ~40% under planned LOC**. Root cause: 5 of 9 SP-5.5
    files + multiple SP-5.7 modules were already pool-native from Epic 3's
    earlier domain ports. The plan estimated per-file without deducting
    for cross-cutting dependencies Epic 3 consumed.
  * **Blast radius split happened organically**: SP-5.6 split into 5.6a/b,
    SP-5.7 into 5.7a/b/c — driven by the new SOP Step 2 rule (> 2 test
    importers → default split).
  * **Bonus bugs found** (see `04-bonus-bugs-found.md` for detail): 9
    concurrency/correctness bugs uncovered during Epic 3+4+5 port, 2 of
    them load-bearing for security (lockout bypass, backup-code double-
    consumption).

### Process improvements mid-execution

  * `3b1bfa51` (2026-04-21) — SOP Step 1 module-global audit, Step 2
    test-fixture blast-radius split, Step 3 compat-fingerprint grep +
    runtime-smoke checklist, `.env.test` isolation infrastructure,
    `client` fixture TRUNCATEs `bootstrap_state` between tests.
  * Task #111 closed same commit.

### Follow-up task cluster (Epic 6 prep)

See `05-epic6-prep.md` for the failing-tests-as-spec for #90, #102,
#104, #116 (the "multi-worker state that's not in one of the three
SOP-acceptable answers" cluster).

### Escape-hatch

Tag `phase-3-runtime-v2-start` (commit `983985fc` per SP-1.6) is the
pre-migration rollback point. Still valid.

---

## Post-Epic-5 reordering (2026-04-21, operator-approved)

The original Epic 6-10 plan was written at Step-2 with estimates
based on a mechanical "port then test-sweep" model. After Epic 5
closed, a state-of-world audit turned up:

  * **18 real test-file ``_conn().execute(...)`` callers** (not the
    originally estimated 71 — domain tests migrated inline during
    Epic 3/4/5 ports).
  * **``decision_profiles.py``** — 2 unplanned compat calls; not
    in the original Epic 5 plan.
  * **``memory_decay.py``** (task #93) — originally labeled "Epic 7
    prep"; it's now the only substantial backend module left on
    compat.
  * **Epic 6 prep cluster** (#90, #102, #104, #116) — 4 real
    multi-worker bugs with xfail-failing regression tests already
    in place (see ``05-epic6-prep.md``). Higher operational impact
    than a mechanical test-file sweep. Two of them (#104 secret_
    store race, #116 MFA challenges) are prod-path blockers under
    ``uvicorn --workers N``.

The rearranged execution order (forward-looking from Epic 5 close):

### Step A — quick cleanup (2 commits)

  1. Port ``backend/decision_profiles.py`` (unplanned discovery, 2
     compat calls).
  2. Port ``backend/memory_decay.py`` + ``backend/tests/
     test_memory_decay.py`` (task #93, promoted from Epic 7 prep).

### Step B — Epic 6 prep cluster (4 commits, dependency order)

Each closes one ``xfail(strict=True)`` regression test in
``backend/tests/test_epic6_prep_multi_worker_state.py`` — the fix
commit MUST delete the xfail marker.

  3. **#102** — refactor ``test_j5_per_session_mode.py`` off
     ``auth._conn`` monkey-patch. Pure test work. Unblocks Epic 7's
     compat wrapper deletion (``auth.py`` still carries a stub
     ``_conn()`` helper that can't be removed until this lands).
  4. **#90** — ``auth_baseline_mode`` → ``ContextVar``. Smallest
     surface. Establishes the request-scoped-value pattern that
     #116 will reuse.
  5. **#104** — ``secret_store`` first-boot key-file race. Fix:
     flock around read-or-generate-or-write, OR require
     ``OMNISIGHT_SECRET_KEY`` env in prod (fail-closed on empty +
     missing file). Decision at port time.
  6. **#116** — MFA challenges (``_webauthn_challenges`` +
     ``_pending_mfa``) → PG ephemeral challenge table. Biggest
     surface: new PG table (Alembic migration), touches both
     WebAuthn begin/complete AND MFA login begin/complete call
     paths. Reuses #90's ContextVar pattern for user_id binding.

### Step C — Epic 7 (compat deletion, ~2 commits)

  7. Sweep the remaining ~18 test files off ``_conn()`` (most will
     auto-resolve as Step B/A deletes the code their fixtures
     depend on — expect ~5-10 real migrations).
  8. ``git rm backend/db_pg_compat.py``, remove ``translate_sql_
     and_params`` imports, remove ``auth.py``'s ``_conn()`` stub.

### Step D — Epic 8+ (independent workstreams)

  9. **#81** — rate-limit quota tuning. **CLOSED** 2026-04-21
     (commit 1952e020) — `free.per_ip` 60→300 with proportional
     scaling of starter/pro/enterprise to preserve the plan
     hierarchy invariant.
  10. **#83** — coverage-gate 95% sweep. **IN PROGRESS** 2026-04-21.
      Operator chose option A (pragmatic). Baseline measurement +
      strategy captured in "Coverage gate (#83) baseline snapshot"
      below.
  11. **#84** — HANDOFF.md update (has been cadence-lagging since
      Epic 4).
  12. **#85** / **#70** — prod-parity live verify / multi-user
      deploy check. Requires operator-side ops work.

### Coverage gate (#83) baseline snapshot — 2026-04-21

Ran a 216-test targeted subset (14 test files covering db / pool /
context / audit / auth / tenant_secrets integration tests) with
``--cov --cov-branch`` against the six safety-critical modules
named in ``01-design-decisions.md §8.1``. **This is a low-biased
number** — the full suite (~14,672 tests) would raise every row
meaningfully, but the full run takes 60-180 min per the project
memory and doesn't fit in a single session. CI's sharded 4-way
aggregate is the authoritative measurement.

| Module | Baseline | Target | Notes |
|---|---|---|---|
| ``backend.db_pool`` | **100%** | 95% | Already above gate |
| ``backend.db_context`` | **100%** | 95% | Already above gate |
| ``backend.tenant_secrets`` | 84% → **98%** | 95% | Filled 2026-04-21 (commit 3b672bde). Targeted 7 branches: decrypt/JSON fallback + polymorphic-conn arms + not-found exit. |
| ``backend.audit`` | 56% | 95% | Next to tackle. Likely gaps in the hash-chain verify + the ``audit.query`` filter matrix. |
| ``backend.auth`` | 65% | 95% | Largest gap. Covers session rotation, password reset, MFA challenge flow — most unhit branches are the error paths. |
| ``backend.db`` | 53% | 95% | Post-Step-C.2 this is mostly legacy SQLite dev-mode helpers (``init()`` is a no-op on the PG path). Scope caveat: raising this to 95% requires tests against SQLite code paths that prod never executes. |

**Option A scope (operator-approved):** land the 95% gate as an
opt-in CLI invocation in ``backend/pytest.ini`` (not an ``addopts``
default — that would break individual-file pytest runs), fill the
short gaps in ``tenant_secrets`` (done) plus the worst
auth/audit gaps the subset exposed, and defer the full ``backend.db``
fill to CI's sharded measurement or a follow-up session. Option B
(rigorous full-suite iteration) and option C (carve ``backend.db``
out of the 95% gate with a 75% guard rail) were considered and
shelved.

### Shift from original plan

The original plan's "Epic 6 = test-file sweep (4 SPs)" deflates to
a ~5-10 file Step C residual. The reclaimed budget goes into Step B
(real bugs with specs) and Step A (known-missing work). Net effect:
more concurrency-correctness delivered per commit, mechanical
work-items collapsed into smaller residuals.

### Open follow-up tasks still on the board

Unchanged from Epic-5-close snapshot:
  * **#82** — multi-worker subprocess test harness (skeleton
    landed; library expansion is ongoing through Step B).
  * **#97** — closed (8 test files migrated in batches A + B during
    Epic 5).

### xfail-strict discipline

Step B's 4 fixes each MUST remove the corresponding
``@pytest.mark.xfail(strict=True, ...)`` marker in
``backend/tests/test_epic6_prep_multi_worker_state.py``. If the
fix lands but the marker stays, pytest's ``strict=True`` will
convert ``XPASS`` to test-suite FAILURE — this is intentional,
forcing the fix author to confirm "yes the bug is actually fixed
and my marker removal reflects that".
