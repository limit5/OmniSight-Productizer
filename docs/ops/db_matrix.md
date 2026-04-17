# DB Engine Compatibility Matrix (N8 + G4 #5)

> **Status (2026-04-18):** hard gate on SQLite 3.40 + 3.45, **hard gate** on Postgres 15 + 16, advisory on Postgres 17 (forward-look) and the engine-syntax-scan. The Postgres cells flipped to hard after **G4 #1** landed the runtime SQLite→Postgres compat shim (`backend/alembic_pg_compat.py`) and **G4 #4** landed the data-migration script — the shim makes every existing SQLite-idiom migration run green on Postgres, and the migration script is guarded end-to-end by a live-PG integration job. SQLite retires only after the operator runbook (**G4 #6** / `docs/ops/db_failover.md`) declares the cutover complete.

## Why this matrix exists

OmniSight's storage is SQLite today and Postgres tomorrow. The **G4** milestone cuts the runtime over to Postgres; multi-tenancy (**I**) hard-depends on it (RLS, `statement_timeout`, role-scoped grants). Migration code written today accretes SQLite-only idioms — `AUTOINCREMENT`, `datetime('now')`, `INSERT OR IGNORE`, `CREATE VIRTUAL TABLE … USING fts5` — and every one of them would be a landmine on the G4 cut-over night, far too late.

Before G4 #1 the Postgres cells were **advisory** because the migrations genuinely did not run on PG. Now that the runtime shim translates those idioms on the fly (`AUTOINCREMENT` → `IDENTITY`, `datetime('now')` → `NOW()`, `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, …), every committed revision upgrades/downgrades green on PG 15 + 16 and the cells are **hard gates**. N8 is now the CI layer that **pins the shim contract on every PR** and **exercises the real Postgres target continuously** so the G4 cutover is a graph-coloring problem, not a safari.

## Layer architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                 .github/workflows/db-engine-matrix.yml             │
│                                                                    │
│   sqlite-matrix        (hard gate, always run)                     │
│     ├ sqlite 3.40.1 — LD_PRELOAD'd libsqlite3.so + dual-track      │
│     └ sqlite 3.45.3 — LD_PRELOAD'd libsqlite3.so + dual-track      │
│                                                                    │
│   postgres-matrix      (hard gate since G4 #1 shim landed)         │
│     ├ postgres:15 service container + dual-track  (hard)           │
│     ├ postgres:16 service container + dual-track  (hard)           │
│     └ postgres:17 service container + dual-track  (advisory*)      │
│                                                                    │
│   pg-live-integration  (hard gate — G4 #5)                         │
│     ├ postgres:16 service container                                │
│     ├ pytest test_alembic_pg_live_upgrade.py   (G4 #1 contract)    │
│     └ migrate_sqlite_to_pg.py --dry-run smoke  (G4 #4 contract)    │
│                                                                    │
│   engine-syntax-scan   (advisory linter)                           │
│     └ scripts/check_migration_syntax.py → ::warning annotations    │
│                                                                    │
│   matrix-summary       (roll-up table on run page)                 │
└────────────────────────────────────────────────────────────────────┘

* postgres:17 = N7-style forward-look cell, graduates to hard once a
  full quarter is green.
```

Triggers: PRs touching the Alembic tree, `backend/alembic_pg_compat.py`, `backend/db.py`, `backend/db_context.py`, `backend/db_url.py`, `backend/db_connection.py`, or any of the N8/G4 scripts (`alembic_dual_track.py`, `check_migration_syntax.py`, `migrate_sqlite_to_pg.py`, `scan_sqlite_isms.py`); plus push to `master` and manual `workflow_dispatch`.

## Dual-track validator

`scripts/alembic_dual_track.py` is the heart of the matrix. It runs once per (engine, version) cell:

1. **Upgrade head** — `alembic upgrade head` against a fresh DB.
2. **Fingerprint A** — read table + column names into a dict.
3. **Downgrade to baseline** — step down one revision at a time to revision `0001` (the baseline migration refuses to drop every table, so `0001` is the floor).
4. **Re-upgrade head** — `alembic upgrade head` again.
5. **Fingerprint B** — same read-back as step 2.
6. **Compare A and B** — any table/column asymmetry is an up/down pair bug and fails the cell.

The script sets `OMNISIGHT_SKIP_FS_MIGRATIONS=1` so data-migrations like `0014_tenant_filesystem_namespace.py` exercise their SQL path without shuffling real artifact files around the CI workspace. Migration authors that add filesystem side-effects should honour the same env var.

### Why stdlib + Alembic only

The dual-track script and the engine-syntax linter are both deliberately free of third-party deps (SQLAlchemy enters the validator only through Alembic, which is already a hard dep). Reason: N5 / N6 / N7 spelled out the self-defense argument — the tool that catches dep-upgrade breakage cannot itself be broken by the dep upgrade it's forecasting.

## SQLite version pinning

GitHub Actions' `ubuntu-latest` has one system SQLite and Python's `setup-python` bundles whatever `_sqlite3` was compiled against. To exercise two specific versions deterministically:

1. Download the SQLite amalgamation tarball from `sqlite.org` (version encoded in the URL: `3.40.1` = `3400100`, `3.45.3` = `3450300`).
2. `./configure --prefix=/opt/sqlite-<ver> --disable-static && make && sudo make install`.
3. Cache the build output via `actions/cache@v4` — first run ~60s, warm runs ~10s.
4. `LD_PRELOAD=/opt/sqlite-<ver>/lib/libsqlite3.so python3 scripts/alembic_dual_track.py --engine sqlite`. The dynamic linker replaces the stock `libsqlite3.so.0` that Python's `_sqlite3` extension would otherwise resolve to.
5. Verify with `python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'` — the printed version must match the matrix pin; the workflow asserts this explicitly.

## Postgres wiring

Service container approach: `services.postgres.image: postgres:<ver>` starts a container on `localhost:5432`.

* **`postgres-matrix` (dual-track)** — connects through `postgresql+psycopg://omnisight:omnisight@127.0.0.1:5432/omnitest` (psycopg v3, SQLAlchemy's modern dialect). `psycopg[binary]==3.2.3` is installed only in this cell.
* **`pg-live-integration`** — runs `pytest backend/tests/test_alembic_pg_live_upgrade.py` under `OMNI_TEST_PG_URL=postgresql+psycopg2://…` (sync, Alembic) and smoke-runs `scripts/migrate_sqlite_to_pg.py --dry-run` which pulls asyncpg lazily (G4 #2 dispatcher). The cell installs `psycopg2-binary==2.9.10` + `asyncpg==0.30.0` to cover both wire paths.

Neither driver is pinned in `backend/requirements.txt` — they're CI-only. The production runtime reaches PG via `backend/db_connection.py` → asyncpg; Alembic runs through psycopg2 at cutover time because Alembic has no async driver. Adding the PG drivers to every other CI job (backend-tests, openapi-contract, renovate-config) would cost minutes for no benefit pre-cutover.

## Engine-syntax-scan scope

The engine-syntax-scan today reports ~30 findings across the committed revisions. They are **intentionally not fixed in the migration files themselves** because:

* The G4 #1 runtime shim (`backend/alembic_pg_compat.py`) translates every SQLite-only idiom to its Postgres equivalent at `before_cursor_execute` time. Rewriting the migrations to be dialect-native is a one-way churn with no runtime payoff.
* The shim's self-tests (`backend/tests/test_alembic_pg_compat.py`, 226 assertions) pin every translation rule so a rule drift would fail the unit suite before the scan ever fires.
* The live-PG cell (`pg-live-integration`) proves shim + migrations are end-to-end green; the scan is a belt-and-braces advisory layer.

The advisory scan stays so **new** migrations are authored with Postgres in mind. If the finding count grows on a PR, reviewers ask "why add a new SQLite-only idiom when the shim already handles it — can the migration use a dialect-native form?"

## Engine-specific SQL — the rules

`scripts/check_migration_syntax.py` encodes the rule set. Each rule has a human label and a fix hint; adding a rule = appending to the `RULES` list.

| Rule | SQLite idiom | Postgres replacement |
|---|---|---|
| `autoincrement` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED BY DEFAULT AS IDENTITY` |
| `datetime_now` | `datetime('now')` | `CURRENT_TIMESTAMP` or `NOW()` |
| `strftime` | `strftime('%s', 'now')` | `EXTRACT(EPOCH FROM NOW())::bigint` |
| `insert_or` | `INSERT OR IGNORE/REPLACE` | `INSERT … ON CONFLICT (…) DO NOTHING / DO UPDATE` |
| `virtual_table_fts` | `CREATE VIRTUAL TABLE … USING fts5` | `tsvector` + `GIN` index |
| `pragma` | `PRAGMA foreign_keys=ON` | (no-op; Postgres FKs are always on) |
| `begin_immediate` | `BEGIN IMMEDIATE` | default transaction + `SELECT … FOR UPDATE` |
| `text_pk_as_integer` | `INTEGER PRIMARY KEY` (SQLite rowid alias) | `BIGSERIAL PRIMARY KEY` or `GENERATED BY DEFAULT AS IDENTITY` |

## G4 handoff status

**Delivered (2026-04-18):**

1. ✅ **Runtime SQLite→Postgres shim** — `backend/alembic_pg_compat.py` (G4 #1). Every committed Alembic revision runs green on Postgres 15/16 via the shim without per-migration edits.
2. ✅ **Postgres matrix is hard gate** — `continue-on-error: true` removed from the `postgres-matrix` job; PG 15 + 16 must pass for merge.
3. ✅ **Postgres 17 advisory cell added** — `continue-on-error: ${{ matrix.postgres == '17' }}` for the forward-look cell (N7 pattern).
4. ✅ **Live-PG integration job** — `pg-live-integration` runs `test_alembic_pg_live_upgrade.py` + `migrate_sqlite_to_pg.py --dry-run` smoke against a real PG 16 service container on every matrix-relevant PR.

**Deferred to the cutover sweep** (once `docs/ops/db_failover.md` / G4 #6 declares cutover complete):

1. **Retire sqlite-matrix** — delete the `sqlite-matrix` job block from `db-engine-matrix.yml`.
2. **Promote postgres:17 to hard gate** — after one quarter green.
3. **Flip engine-syntax-scan to `--strict`** — once migrations are rewritten in dialect-native SQL (or the reviewers accept the shim as the permanent translation layer).
4. **Delete `OMNISIGHT_SKIP_FS_MIGRATIONS`** — the filesystem move in `0014_tenant_filesystem_namespace.py` is a one-shot upgrade-time action and the matrix doesn't need to re-run it.

## Cross-references

* **G4 TODO row** — `TODO.md` §G (Postgres cutover).
* **I-series** — `TODO.md` §I (multi-tenancy) hard-depends on G4 for RLS.
* **N7 matrix** — `.github/workflows/multi-version-matrix.yml` — same "advisory until hardened" design, same `continue-on-error` pattern.
* **Dependency runbook** — `docs/ops/dependency_upgrade_runbook.md` — references the DB matrix under Phase 1.4 ("are all engine-related cells green?") once G4 ships.
