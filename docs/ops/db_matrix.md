# DB Engine Compatibility Matrix (N8)

> **Status:** live since 2026-04-16. Hard gate on SQLite 3.40 + 3.45; advisory on Postgres 15 + 16. Retires SQLite after **G4** ports the storage layer to Postgres.

## Why this matrix exists

OmniSight's storage is SQLite today and Postgres tomorrow. The **G4** milestone cuts the runtime over to Postgres; multi-tenancy (**I**) hard-depends on it (RLS, `statement_timeout`, role-scoped grants). Migration code written today accretes SQLite-only idioms — `AUTOINCREMENT`, `datetime('now')`, `INSERT OR IGNORE`, `CREATE VIRTUAL TABLE … USING fts5` — and every one of them is a landmine that surfaces on the G4 cut-over night, far too late.

N8 is the CI layer that **refuses to let that landmine pass merge today** and **exercises the Postgres target continuously** so G4 is a graph-coloring problem, not a safari.

## Layer architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                 .github/workflows/db-engine-matrix.yml             │
│                                                                    │
│   sqlite-matrix        (hard gate, always run)                     │
│     ├ sqlite 3.40.1 — LD_PRELOAD'd libsqlite3.so + dual-track      │
│     └ sqlite 3.45.3 — LD_PRELOAD'd libsqlite3.so + dual-track      │
│                                                                    │
│   postgres-matrix      (advisory until G4)                         │
│     ├ postgres:15 service container + dual-track                   │
│     └ postgres:16 service container + dual-track                   │
│                                                                    │
│   engine-syntax-scan   (advisory linter)                           │
│     └ scripts/check_migration_syntax.py → ::warning annotations    │
│                                                                    │
│   matrix-summary       (roll-up table on run page)                 │
└────────────────────────────────────────────────────────────────────┘
```

Triggers: PRs touching `backend/alembic/**`, `backend/db.py`, `backend/db_context.py`, or the N8 scripts themselves; plus push to `master` and manual `workflow_dispatch`.

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

Service container approach: `services.postgres.image: postgres:<ver>` starts a container on `localhost:5432`. The validator connects through `postgresql+psycopg://omnisight:omnisight@127.0.0.1:5432/omnitest`. `psycopg[binary]==3.2.3` is installed only in this cell — it is **not** added to `requirements.txt` until G4 lands, because pulling the 11MB psycopg binary wheel into every other CI job (backend-tests, openapi-contract, renovate-config) would cost minutes for no benefit.

## Known pre-G4 findings

The engine-syntax-scan today reports ~30 findings across the 15 committed revisions. They are not fixed before G4 because:

* The runtime (`backend/db.py`) speaks SQLite natively; rewriting the migrations to be dialect-agnostic without changing the runtime introduces churn with zero payoff.
* Most findings are `datetime('now')` defaults. The G4 port replaces them with `CURRENT_TIMESTAMP` in a single sweep.
* `INSERT OR IGNORE` sites in `0015_tenant_egress_policies.py` need `ON CONFLICT (tenant_id) DO NOTHING / UPDATE` — mechanical.
* `CREATE VIRTUAL TABLE … USING fts5` in the runtime schema is SQLite-exclusive; G4 will gate it by dialect (`if bind.dialect.name == 'sqlite'`) or replace with `tsvector` + GIN.

The advisory scan exists today so **new** migrations (post-N8) are authored with Postgres in mind. If the pre-G4 finding count grows on a PR, reviewers ask "why are we adding a new SQLite-only idiom when we're about to migrate?"

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

## G4 handoff plan

When the G4 branch is ready to merge:

1. **Port the migrations** — sweep every engine-syntax-scan finding, use `bind.dialect.name` to fork where unavoidable (FTS5).
2. **Run the Postgres matrix green** — confirm both `postgres:15` and `postgres:16` cells pass.
3. **Flip postgres-matrix to hard gate** — remove `continue-on-error: true` on the `postgres-matrix` job.
4. **Retire sqlite-matrix** — delete the `sqlite-matrix` job block from `db-engine-matrix.yml`.
5. **Add postgres:17 as advisory** — mirror N7's "forward-look" pattern; one cell advisory, two cells hard.
6. **Flip engine-syntax-scan to `--strict`** — the linter now rejects PRs that re-introduce SQLite-only SQL.
7. **Delete the pre-G4 workaround** — `OMNISIGHT_SKIP_FS_MIGRATIONS` env var in `0014_tenant_filesystem_namespace.py` can go; the filesystem move is a one-shot upgrade-time action and the matrix doesn't need to re-run it.

That's the whole N8 handoff. The SOP lives here; the code scaffolding is already in place.

## Cross-references

* **G4 TODO row** — `TODO.md` §G (Postgres cutover).
* **I-series** — `TODO.md` §I (multi-tenancy) hard-depends on G4 for RLS.
* **N7 matrix** — `.github/workflows/multi-version-matrix.yml` — same "advisory until hardened" design, same `continue-on-error` pattern.
* **Dependency runbook** — `docs/ops/dependency_upgrade_runbook.md` — references the DB matrix under Phase 1.4 ("are all engine-related cells green?") once G4 ships.
