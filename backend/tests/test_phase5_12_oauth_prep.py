"""Phase 5-12 — OAuth prep drift guards.

Row 5-12's scope is explicit: **PAT is MVP, OAuth 2.0 is a follow-up —
don't implement OAuth yet, but reserve the schema shape so the
eventual OAuth row doesn't need another data-model migration.**

Two columns make that reservation concrete:

* ``auth_type TEXT DEFAULT 'pat'`` — landed on alembic 0027 (row 5-1).
* ``code_verifier JSONB DEFAULT '{}'`` — landed on alembic 0028
  (this row).

This file is the lock that catches regressions in either direction:

1. **Schema drift** — someone drops the columns, renames them, or
   changes their default/type.
2. **Scope creep** — someone wires ``code_verifier`` into the CRUD
   surface / the API response shape / the credential resolver before
   an OAuth implementation actually lands. Row 5-12 explicitly does
   NOT implement OAuth; the presence of these columns in the CRUD
   allowlist / ``_GIT_ACCOUNTS_COLS`` / public-dict keys would mean
   somebody shipped OAuth flow infrastructure under this row's
   umbrella (which row 5-12 was supposed to defer).

Module-global state audit (SOP Step 1, qualified answer #1)
───────────────────────────────────────────────────────────
Schema-only contract tests. Each SQLite-live test spawns a fresh
tmp DB via the module's fixture (same pattern as
``test_git_accounts_schema.py``); no shared module-global mutated.
PG-live tests introspect ``information_schema`` with no write
side-effects.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Layer-2 and Layer-3 tests never dual-write. The ``code_verifier``
DEFAULT-populates path does a single INSERT → single SELECT on the
same connection (SQLite: direct ``aiosqlite.Connection``; PG: one
``asyncpg.Pool.acquire()``). No serialisation→parallel transition
is exercised; no downstream test reads these writes on a different
connection.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0028 = (
    _REPO_ROOT
    / "backend"
    / "alembic"
    / "versions"
    / "0028_git_accounts_code_verifier.py"
)
_MIGRATION_0027 = (
    _REPO_ROOT / "backend" / "alembic" / "versions" / "0027_git_accounts.py"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 1: Migration file sanity (pure unit, no DB).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0028_file_exists():
    assert _MIGRATION_0028.exists(), (
        f"alembic migration missing at {_MIGRATION_0028}"
    )


def test_migration_0028_chains_after_0027():
    spec = importlib.util.spec_from_file_location("m0028", _MIGRATION_0028)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0028"
    assert m.down_revision == "0027", (
        "Phase 5-12 must chain directly after 0027_git_accounts; if a "
        "newer migration was inserted between, update down_revision to "
        "point at the actual prior head so the column lands cleanly."
    )


def test_migration_0028_carries_load_bearing_fragments():
    """The 0028 source must mention the load-bearing fragments so a
    careless edit that drops them fails this test rather than failing
    silently at schema-introspection time in production."""
    src = _MIGRATION_0028.read_text(encoding="utf-8")
    must_have = [
        # PG branch — JSONB-typed column with empty-object default.
        "ADD COLUMN IF NOT EXISTS code_verifier",
        "JSONB NOT NULL DEFAULT '{}'::jsonb",
        # SQLite branch — TEXT-of-JSON dev parity.
        "ADD COLUMN code_verifier TEXT NOT NULL DEFAULT '{}'",
        # Both upgrade branches must appear.
        'dialect == "postgresql"',
        # Downgrade must drop the column on both dialects.
        "DROP COLUMN IF EXISTS code_verifier",
        "DROP COLUMN code_verifier",
    ]
    for fragment in must_have:
        assert fragment in src, (
            f"Phase 5-12 migration is missing load-bearing fragment: "
            f"{fragment!r}. If intentional, update this test + "
            f"docs/phase-5-multi-account/01-design.md §10."
        )


def test_migration_0027_still_declares_auth_type():
    """``auth_type TEXT DEFAULT 'pat'`` was a 5-1 decision that 5-12
    consumes without duplicating. If a future edit rips it out of
    0027, the 5-12 ``code_verifier`` column has no platform-level
    OAuth switch to pair with — catch it at CI time."""
    src = _MIGRATION_0027.read_text(encoding="utf-8")
    assert "auth_type                TEXT NOT NULL DEFAULT 'pat'" in src, (
        "0027 lost the ``auth_type`` column declaration; row 5-12's "
        "OAuth prep depends on the discriminator still being there."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 2: Scope discipline (pure unit, no DB).
#
#  Row 5-12 promised: don't surface code_verifier via CRUD / resolver
#  / UI until OAuth actually lands. These tests lock that promise.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_code_verifier_not_in_crud_select_list():
    """``backend/git_accounts.py::_GIT_ACCOUNTS_COLS`` must NOT mention
    ``code_verifier``. Adding it to the SELECT / RETURNING list would
    surface the OAuth container blob to every CRUD response, which
    row 5-12 explicitly defers. A PR that wires OAuth handling should
    update this test alongside the column addition — the test
    failure is the reminder that row 5-12's scope is being extended.
    """
    from backend import git_accounts as _ga
    assert "code_verifier" not in _ga._GIT_ACCOUNTS_COLS, (
        "code_verifier leaked into git_accounts._GIT_ACCOUNTS_COLS — "
        "that exposes OAuth state through the public CRUD response. "
        "Row 5-12 reserves the column for future OAuth work only."
    )


def test_code_verifier_not_in_resolver_select_list():
    """Same guarantee for the credential resolver side
    (``backend/git_credentials.py::_GIT_ACCOUNTS_COLS``). If OAuth is
    later plumbed through the resolver, this test should be updated
    as part of the OAuth landing commit — not sneak in as collateral
    under some other row."""
    from backend import git_credentials as _gc
    assert "code_verifier" not in _gc._GIT_ACCOUNTS_COLS, (
        "code_verifier leaked into git_credentials._GIT_ACCOUNTS_COLS — "
        "the resolver would start carrying OAuth transport state on "
        "every read. Row 5-12 keeps the column unread until OAuth "
        "lands."
    )


def test_code_verifier_not_in_update_allowlist():
    """Row 5-12 defers OAuth — so the generic PATCH handler must not
    let operators mutate ``code_verifier`` via the UI. That first
    writer should be the OAuth callback handler itself.
    """
    import inspect
    from backend import git_accounts as _ga
    src = inspect.getsource(_ga.update_account)
    assert "code_verifier" not in src, (
        "update_account() now references code_verifier — row 5-12's "
        "scope says the first writer is the OAuth handler, not the "
        "generic PATCH. Revisit whether OAuth is actually landing in "
        "this PR and update both the allowlist and this test."
    )


def test_code_verifier_not_in_create_pydantic_model():
    """``GitAccountCreate`` / ``GitAccountUpdate`` Pydantic models
    must NOT list ``code_verifier``. If they do, the REST surface is
    accepting OAuth state inputs that no handler knows how to
    consume yet."""
    from backend.routers import git_accounts as _router
    create_fields = set(_router.GitAccountCreate.model_fields.keys())
    update_fields = set(_router.GitAccountUpdate.model_fields.keys())
    assert "code_verifier" not in create_fields, (
        "GitAccountCreate declares code_verifier — row 5-12 keeps OAuth "
        "out of the public create API until a real OAuth handler "
        "lands."
    )
    assert "code_verifier" not in update_fields, (
        "GitAccountUpdate declares code_verifier — same rationale as "
        "above."
    )


def test_auth_type_validator_still_includes_oauth():
    """Row 5-4 added ``'oauth'`` to ``_VALID_AUTH_TYPES`` so callers
    can at least set the discriminator (even if no OAuth handler
    exists yet). Losing it would force a future OAuth row to run
    two migrations — the one it actually needs plus an auth_type
    validator update — so lock it now."""
    from backend import git_accounts as _ga
    assert "oauth" in _ga._VALID_AUTH_TYPES, (
        "'oauth' missing from _VALID_AUTH_TYPES; the future OAuth "
        "row expects to be able to set auth_type='oauth' without "
        "another validator change."
    )
    assert "pat" in _ga._VALID_AUTH_TYPES, (
        "'pat' missing from _VALID_AUTH_TYPES; MVP PAT flow would "
        "start rejecting its own writes."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 3: Live SQLite contract.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _sqlite_with_git_accounts(tmp_path):
    db_path = tmp_path / "probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_code_verifier_column_present(_sqlite_with_git_accounts):
    """Column must land via ``db.py::_SCHEMA`` on fresh init (mirror
    of alembic 0028 for dev SQLite)."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    async with conn.execute("PRAGMA table_info(git_accounts)") as cur:
        rows = await cur.fetchall()
    col_names = {r[1] for r in rows}
    assert "code_verifier" in col_names, (
        "code_verifier column missing on fresh SQLite init — "
        "backend/db.py::_SCHEMA fell out of sync with alembic 0028."
    )


@pytest.mark.asyncio
async def test_sqlite_code_verifier_default_populates_on_insert(
    _sqlite_with_git_accounts,
):
    """Minimal INSERT (id + tenant + platform + timestamps) should
    populate ``code_verifier`` from its ``DEFAULT '{}'`` clause —
    that's what keeps the PAT-only create path working without
    code changes."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    await conn.execute(
        "INSERT INTO git_accounts (id, tenant_id, platform, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("ga-cv-default", "t-default", "github", 1.0, 1.0),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT code_verifier FROM git_accounts WHERE id = ?",
        ("ga-cv-default",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "{}", (
        f"code_verifier default did not fire — got {row[0]!r}, "
        "expected '{}'"
    )


@pytest.mark.asyncio
async def test_sqlite_auth_type_default_is_pat(_sqlite_with_git_accounts):
    """Belt+braces guard on row 5-1 — the PAT default must still
    fire on minimal INSERT. If a later refactor ships
    ``auth_type`` without a DEFAULT, the whole PAT-only MVP
    create path starts raising NOT NULL violations."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    await conn.execute(
        "INSERT INTO git_accounts (id, tenant_id, platform, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("ga-at-default", "t-default", "gitlab", 1.0, 1.0),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT auth_type FROM git_accounts WHERE id = ?",
        ("ga-at-default",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pat", (
        f"auth_type default did not fire — got {row[0]!r}, expected 'pat'"
    )


@pytest.mark.asyncio
async def test_sqlite_code_verifier_roundtrip_json_shape(
    _sqlite_with_git_accounts,
):
    """Even though no app code writes code_verifier yet, the column
    must accept a JSON-serialised dict on write and return it on
    read — so the future OAuth handler's ``json.dumps`` / ``json.loads``
    round-trip works without a schema change."""
    import json
    db = _sqlite_with_git_accounts
    conn = db._conn()
    oauth_payload = {
        "verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r",
        "state": "xyz123",
        "method": "S256",
        "scopes": ["repo"],
        "expires_at": 1745512345.67,
    }
    await conn.execute(
        "INSERT INTO git_accounts (id, tenant_id, platform, "
        "code_verifier, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "ga-cv-roundtrip",
            "t-default",
            "github",
            json.dumps(oauth_payload),
            1.0,
            1.0,
        ),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT code_verifier FROM git_accounts WHERE id = ?",
        ("ga-cv-roundtrip",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert json.loads(row[0]) == oauth_payload


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 4: Live PG contract (gated on OMNI_TEST_PG_URL).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_code_verifier_column_present(pg_test_pool):
    """Column must land on PG via alembic upgrade head after 0028."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'git_accounts' "
            "AND column_name = 'code_verifier'"
        )
    assert row is not None, (
        "code_verifier column missing on PG — alembic upgrade head "
        "did not run 0028_git_accounts_code_verifier."
    )
    # JSONB — not plain TEXT — so future handlers can index into
    # the blob with ``->>`` operators without string parsing.
    assert row["data_type"] == "jsonb", (
        f"code_verifier column type is {row['data_type']!r}; "
        "expected 'jsonb' per docs/phase-5-multi-account/01-design.md §10.2"
    )


@pytest.mark.asyncio
async def test_pg_code_verifier_default_is_empty_object(pg_test_pool):
    """Default expression must be ``'{}'::jsonb`` so minimal INSERTs
    from row 5-4's CRUD continue to produce rows that future OAuth
    handlers can safely ``SELECT`` without a NULL check."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'git_accounts' "
            "AND column_name = 'code_verifier'"
        )
    assert row is not None, "code_verifier column not visible on PG"
    default_expr = (row["column_default"] or "").lower()
    assert "'{}'" in default_expr and "jsonb" in default_expr, (
        f"code_verifier column_default is {row['column_default']!r}; "
        "expected something like '\\'{}\\''::jsonb"
    )


@pytest.mark.asyncio
async def test_pg_code_verifier_default_populates_on_insert(pg_test_pool):
    """Parallel to the SQLite default-fires test: the DEFAULT must
    produce ``{}`` on an INSERT that doesn't mention the column."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM git_accounts WHERE id = $1",
            "ga-pg-cv-default",
        )
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "created_at, updated_at) VALUES ($1, $2, $3, $4, $5)",
            "ga-pg-cv-default", "t-default", "github", 1.0, 1.0,
        )
        try:
            row = await conn.fetchrow(
                "SELECT code_verifier FROM git_accounts WHERE id = $1",
                "ga-pg-cv-default",
            )
            assert row is not None
            # asyncpg returns JSONB as a dict.
            assert row["code_verifier"] == {} or row["code_verifier"] == "{}", (
                f"code_verifier default did not fire — got "
                f"{row['code_verifier']!r}, expected empty JSON object"
            )
        finally:
            await conn.execute(
                "DELETE FROM git_accounts WHERE id = $1",
                "ga-pg-cv-default",
            )


@pytest.mark.asyncio
async def test_pg_code_verifier_roundtrip_json_shape(pg_test_pool):
    """asyncpg serialises Python dict → JSONB on write and JSONB →
    dict on read (via the default codec). Lock the round-trip so a
    future Python-side change of encoder doesn't silently corrupt
    OAuth state."""
    import json
    payload = {
        "verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r",
        "state": "xyz123",
        "method": "S256",
        "scopes": ["repo", "workflow"],
        "expires_at": 1745512345.67,
    }
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM git_accounts WHERE id = $1",
            "ga-pg-cv-roundtrip",
        )
        # Write via JSONB cast — same path a future OAuth handler
        # would use (asyncpg accepts ``$N::jsonb`` param explicitly).
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "code_verifier, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6)",
            "ga-pg-cv-roundtrip", "t-default", "github",
            json.dumps(payload), 1.0, 1.0,
        )
        try:
            row = await conn.fetchrow(
                "SELECT code_verifier FROM git_accounts WHERE id = $1",
                "ga-pg-cv-roundtrip",
            )
            assert row is not None
            cv = row["code_verifier"]
            # asyncpg may return JSONB as dict (if codec registered) or
            # string (if not); normalise to dict for assertion.
            if isinstance(cv, str):
                cv = json.loads(cv)
            assert cv == payload
        finally:
            await conn.execute(
                "DELETE FROM git_accounts WHERE id = $1",
                "ga-pg-cv-roundtrip",
            )


@pytest.mark.asyncio
async def test_pg_code_verifier_not_null_constraint(pg_test_pool):
    """Column is NOT NULL — writing an explicit NULL should raise.
    This matters because downstream OAuth code that forgets to set
    the field will fall through to DEFAULT, but code that
    explicitly assigns NULL (buggy) must fail loud rather than
    silently store nothing."""
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO git_accounts (id, tenant_id, platform, "
                "code_verifier, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                "ga-pg-cv-null", "t-default", "github", None, 1.0, 1.0,
            )
