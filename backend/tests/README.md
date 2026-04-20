# `backend/tests/` — runbook

## Running the test suite

```bash
# From repo root
PYTHONPATH=. pytest backend/tests/
```

Most tests run against in-memory SQLite — no external services required.
Tests marked with the `pg_test_*` fixture family (Phase-3-Runtime-v2 SP-1.2
onward) require a PostgreSQL instance; see below.

## PostgreSQL-backed tests

### When you need them

Any test that uses one of these fixtures from `conftest.py`:

- `pg_test_dsn` — resolved libpq DSN of the test PG
- `pg_test_alembic_upgraded` — DSN after `alembic upgrade head` ran
- `pg_test_pool` — async asyncpg pool (function-scoped)
- `pg_test_conn` — connection wrapped in auto-rollback transaction

Tests that touch the native asyncpg layer (`backend/db_pool.py`,
`backend/db.py` after Epic 3 ports) will predominantly use
`pg_test_conn`.

### Set-up

```bash
# 1. Start the test PG container (port 5434, isolated from prod 5432/5433)
docker compose -f docker-compose.test.yml up -d

# 2. Wait for healthy (usually ≤5 s)
docker compose -f docker-compose.test.yml ps

# 3. Point the test suite at it
export OMNI_TEST_PG_URL="postgresql://omni_test:omni_test_pw@localhost:5434/omni_test"

# 4. Run tests — PG-backed ones will auto-run, others skip cleanly
PYTHONPATH=. pytest backend/tests/
```

The env-var also accepts the SQLAlchemy forms (`postgresql+psycopg2://`
and `postgresql+asyncpg://`) for compatibility with
`test_alembic_pg_live_upgrade.py` which sets it as
`postgresql+psycopg2://`. The `pg_test_dsn` fixture normalises for
asyncpg.

### When env is unset

If `OMNI_TEST_PG_URL` is not exported, every PG-backed test calls
`pytest.skip(...)` with a clear reason. The non-PG suite still runs
to completion. **CI treats this as "ok, PG-backed tests skipped"**,
not a failure — so local dev loops don't require Docker.

CI is expected to export the env var and run the PG-backed subset
as a separate job (see `.github/workflows/` or the project's CI
equivalent).

### Tear-down

```bash
# Stop but keep data volume for faster restart
docker compose -f docker-compose.test.yml down

# Or wipe the volume (fully fresh PG on next `up`)
docker compose -f docker-compose.test.yml down -v
```

## Fixture isolation contract

The `pg_test_conn` fixture wraps each test in an outer transaction
that rolls back on teardown. This means:

- Every test starts from the **alembic HEAD state** of the DB
- Tests are **free** to `INSERT / UPDATE / DELETE` whatever they need
- At test end, **all changes disappear** — next test sees HEAD again
- Tests can do **nested savepoints** inside their body
  (asyncpg auto-detects the outer transaction and uses savepoints
   for `async with conn.transaction()` inside the test)

Tests that need a fresh full schema (drop/recreate) rather than a
rollback checkpoint should opt out of `pg_test_conn` and create their
own pool + explicit DDL — rare case, don't default to it.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| All `pg_test_*` tests skipped | `OMNI_TEST_PG_URL` not exported | export the var per "Set-up" above |
| `asyncpg.exceptions.InvalidCatalogNameError: database "omni_test" does not exist` | Fresh container still booting | wait for `pg_isready` → healthy |
| `alembic upgrade head failed` in test output | Alembic config issue or PG schema corrupt | `docker compose -f docker-compose.test.yml down -v && up -d` |
| Port 5434 already in use | Another test PG container already running | `docker ps`, stop the duplicate |
| Tests hang on `pg_test_pool` fixture | pytest-asyncio loop mismatch | check `pytest.ini` has `asyncio_mode = auto` |

## Evolution notes

SP-1.2 deliberately uses **function-scoped** async fixtures (pool +
conn). Each test creates and tears down its own pool in ~20 ms — cheap
enough that the isolation guarantee wins out over amortising pool
creation. If profile shows the overhead dominating a long run, we can
lift the pool to `module` or `session` scope by bumping
`asyncio_default_fixture_loop_scope` in `pytest.ini` and converting
the fixture accordingly.
