"""Y1 row 4 (#277) — tenant_invites schema tests.

Mirrors the structure of ``test_user_tenant_memberships_schema.py``,
``test_projects_schema.py``, and ``test_project_members_schema.py``:
PG fixtures exercise the alembic-applied schema; pure-SQLite cases
exercise the ``_SCHEMA`` bootstrap path that fresh dev DBs go through;
and a revision-chain unit test pins the migration file.

The tests cover the exact contract from the TODO row:
``(id, tenant_id, email, role, invited_by, token_hash, expires_at,
status)`` + the four-value status enum
``pending / accepted / revoked / expired`` + the "token plaintext is
returned once at creation, only the hash is persisted" property
(verified at the schema layer by asserting the column is named
``token_hash`` and is UNIQUE).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


EXPECTED_ROLES = {"owner", "admin", "member", "viewer"}
EXPECTED_STATUSES = {"pending", "accepted", "revoked", "expired"}
EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "email",
    "role",
    "invited_by",
    "token_hash",
    "expires_at",
    "status",
    "created_at",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: alembic-applied schema sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'tenant_invites'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_pg_table_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tenant_invites'"
        )
    cols = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - cols
    extra = cols - EXPECTED_COLUMNS
    assert not missing, f"missing columns: {missing}"
    assert not extra, f"unexpected columns: {extra}"


@pytest.mark.asyncio
async def test_pg_primary_key_is_id(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'tenant_invites'::regclass
              AND i.indisprimary
            """
        )
    pk_cols = {r["col"] for r in rows}
    assert pk_cols == {"id"}


@pytest.mark.asyncio
async def test_pg_indexes_present(pg_test_pool):
    """The three explicit indexes from the migration must exist:
    tenant+status (admin list), email+status (sign-up cross-ref),
    and the partial expiry-sweep target."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'tenant_invites'"
        )
    names = {r["indexname"] for r in rows}
    assert "idx_tenant_invites_tenant_status" in names
    assert "idx_tenant_invites_email_status" in names
    assert "idx_tenant_invites_expiry_sweep" in names


async def _seed_tenant_user(conn, suffix):
    """Helper: create one tenant + inviter user for a test scope."""
    tid = f"t-inv-{suffix}-{os.urandom(3).hex()}"
    uid = f"u-inv-{suffix}-{os.urandom(3).hex()}"
    await conn.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        tid, f"INV-{suffix}",
    )
    await conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, "
        "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (id) DO NOTHING",
        uid, f"{uid}@t.com", f"INV-{suffix}", "admin", "h", tid,
    )
    return tid, uid


@pytest.mark.asyncio
async def test_pg_default_status_is_pending(pg_test_pool):
    """Inserting only the required columns sets sensible defaults:
    status='pending', role='member', created_at populated."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "def")
        iid = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            iid, tid, "alice@example.com", uid,
            "h" * 64, "2026-12-31T00:00:00",
        )
        row = await conn.fetchrow(
            "SELECT status, role, created_at FROM tenant_invites "
            "WHERE id = $1",
            iid,
        )
    assert row["status"] == "pending"
    assert row["role"] == "member"
    assert row["created_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_status", ["wizard", "PENDING", "", "active", "draft"],
)
async def test_pg_status_check_rejects_invalid(pg_test_pool, bad_status):
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "bs")
        iid = f"inv-{os.urandom(4).hex()}"
        with pytest.raises(Exception):  # asyncpg.CheckViolationError
            await conn.execute(
                "INSERT INTO tenant_invites "
                "(id, tenant_id, email, invited_by, token_hash, "
                "expires_at, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                iid, tid, "x@y.com", uid,
                "h" * 64, "2026-12-31T00:00:00", bad_status,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(EXPECTED_STATUSES))
async def test_pg_status_check_accepts_known(pg_test_pool, status):
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, f"ok-{status}")
        iid = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at, status) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            iid, tid, f"{status}@y.com", uid,
            "h" * 64 + status, "2026-12-31T00:00:00", status,
        )
        row = await conn.fetchrow(
            "SELECT status FROM tenant_invites WHERE id = $1", iid,
        )
    assert row["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_role", ["wizard", "OWNER", "", "contributor", "guest"],
)
async def test_pg_role_check_rejects_invalid(pg_test_pool, bad_role):
    """``contributor`` is the project-level role and is deliberately
    NOT a valid tenant invite role — invites grant tenant-scope
    membership and the materialised user_tenant_memberships row uses
    the four-value tenant enum."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "br")
        iid = f"inv-{os.urandom(4).hex()}"
        with pytest.raises(Exception):  # asyncpg.CheckViolationError
            await conn.execute(
                "INSERT INTO tenant_invites "
                "(id, tenant_id, email, role, invited_by, "
                "token_hash, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                iid, tid, "x@y.com", bad_role, uid,
                "h" * 64, "2026-12-31T00:00:00",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
async def test_pg_role_check_accepts_known(pg_test_pool, role):
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, f"ok-{role}")
        iid = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, role, invited_by, "
            "token_hash, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            iid, tid, f"{role}@y.com", role, uid,
            "h" * 64 + role, "2026-12-31T00:00:00",
        )
        row = await conn.fetchrow(
            "SELECT role FROM tenant_invites WHERE id = $1", iid,
        )
    assert row["role"] == role


@pytest.mark.asyncio
async def test_pg_token_hash_is_unique(pg_test_pool):
    """Two invites cannot share the same token_hash — the unique
    constraint is what makes the acceptance route's
    ``WHERE token_hash = $1`` lookup unambiguous, and it also
    prevents collision-based attacks where a guess matches multiple
    rows."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "uniq")
        token = "h" * 32 + os.urandom(8).hex()
        i1 = f"inv-{os.urandom(4).hex()}"
        i2 = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            i1, tid, "a@y.com", uid, token, "2026-12-31T00:00:00",
        )
        with pytest.raises(Exception):  # UniqueViolationError
            await conn.execute(
                "INSERT INTO tenant_invites "
                "(id, tenant_id, email, invited_by, token_hash, "
                "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
                i2, tid, "b@y.com", uid, token, "2026-12-31T00:00:00",
            )


@pytest.mark.asyncio
async def test_pg_token_hash_min_length_check(pg_test_pool):
    """A laughably short ``token_hash`` (e.g. accidentally storing
    plaintext or a non-hash) must be rejected — the CHECK length
    >= 16 catches the most egregious mistakes; the actual hash will
    be 64 hex chars (sha256) but the schema only enforces the floor."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "hashlen")
        iid = f"inv-{os.urandom(4).hex()}"
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "INSERT INTO tenant_invites "
                "(id, tenant_id, email, invited_by, token_hash, "
                "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
                iid, tid, "x@y.com", uid, "short", "2026-12-31",
            )


@pytest.mark.asyncio
async def test_pg_email_length_check(pg_test_pool):
    """Empty email is rejected (length CHECK >= 1).  Bounding the
    upper limit at 320 (RFC 5321) stops a rogue 1-MB-string admin
    payload from poisoning the email index."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "emaillen")
        iid = f"inv-{os.urandom(4).hex()}"
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "INSERT INTO tenant_invites "
                "(id, tenant_id, email, invited_by, token_hash, "
                "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
                iid, tid, "", uid, "h" * 64, "2026-12-31",
            )


@pytest.mark.asyncio
async def test_pg_tenant_cascade_delete(pg_test_pool):
    """Deleting the tenant removes its invites (FK ON DELETE
    CASCADE).  Invites for a deleted tenant carry no semantic
    value; the row count must stay bounded under tenant churn."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "tcas")
        iid = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            iid, tid, "x@y.com", uid, "h" * 64, "2026-12-31",
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)
        row = await conn.fetchrow(
            "SELECT 1 FROM tenant_invites WHERE id = $1", iid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_inviter_set_null_on_delete(pg_test_pool):
    """Deleting the inviter must NOT delete the invite — it's the
    invitee's invite, not the inviter's.  ``invited_by`` should be
    set to NULL so the audit trail records "an admin who is no longer
    an admin issued this invite"."""
    async with pg_test_pool.acquire() as conn:
        tid, uid = await _seed_tenant_user(conn, "ucas")
        iid = f"inv-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            iid, tid, "x@y.com", uid, "h" * 64, "2026-12-31",
        )
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        row = await conn.fetchrow(
            "SELECT invited_by FROM tenant_invites WHERE id = $1",
            iid,
        )
    assert row is not None, "invite must survive inviter deletion"
    assert row["invited_by"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQLite-side: _SCHEMA bootstrap mirrors the alembic table 1:1
#  (so fresh dev DBs are not silently missing the table)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _fresh_sqlite_db(tmp_path):
    """Boot a clean SQLite via ``backend.db.init`` so we exercise the
    same _SCHEMA + _migrate path production dev-mode goes through."""
    db_path = tmp_path / "tenant_invites_probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        yield db._conn()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_table_exists(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='tenant_invites'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_columns_match_contract(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(tenant_invites)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    assert EXPECTED_COLUMNS == cols, (
        f"missing: {EXPECTED_COLUMNS - cols}; extra: {cols - EXPECTED_COLUMNS}"
    )


@pytest.mark.asyncio
async def test_sqlite_primary_key_is_id(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(tenant_invites)") as cur:
        rows = await cur.fetchall()
    pk_cols = {r[1] for r in rows if r[5] > 0}
    assert pk_cols == {"id"}


async def _sl_seed(conn, suffix):
    """SQLite-side seed helper. Uses INSERT OR IGNORE for idempotency."""
    tid = f"t-sl-inv-{suffix}"
    uid = f"u-sl-inv-{suffix}"
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        f"VALUES ('{tid}', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        f"password_hash, tenant_id) VALUES "
        f"('{uid}', '{uid}@t.com', 'SL', 'admin', 'h', '{tid}')"
    )
    return tid, uid


@pytest.mark.asyncio
async def test_sqlite_default_status_is_pending(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "def")
    iid = "inv-sldef"
    await conn.execute(
        "INSERT INTO tenant_invites "
        "(id, tenant_id, email, invited_by, token_hash, expires_at) "
        f"VALUES ('{iid}', '{tid}', 'a@y.com', '{uid}', "
        f"'{'h' * 64}', '2026-12-31T00:00:00')"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT status, role, created_at FROM tenant_invites "
        f"WHERE id = '{iid}'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "pending"
    assert row[1] == "member"
    assert row[2] is not None


@pytest.mark.asyncio
async def test_sqlite_status_check_rejects_invalid(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "bad")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at, status) VALUES "
            f"('inv-slbad', '{tid}', 'x@y.com', '{uid}', "
            f"'{'h' * 64}', '2026-12-31', 'wizard')"
        )


@pytest.mark.asyncio
async def test_sqlite_role_check_rejects_invalid(_fresh_sqlite_db):
    """``contributor`` is the project-level enum and must NOT be
    storable here — keeps tenant-vs-project role confusion out of
    even the dev SQLite path."""
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "rolebad")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, role, invited_by, token_hash, "
            "expires_at) VALUES "
            f"('inv-slrole', '{tid}', 'x@y.com', 'contributor', "
            f"'{uid}', '{'h' * 64}', '2026-12-31')"
        )


@pytest.mark.asyncio
async def test_sqlite_token_hash_unique(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "uniq")
    token = "h" * 64
    await conn.execute(
        "INSERT INTO tenant_invites "
        "(id, tenant_id, email, invited_by, token_hash, expires_at) "
        f"VALUES ('inv-sl1', '{tid}', 'a@y.com', '{uid}', "
        f"'{token}', '2026-12-31')"
    )
    await conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) "
            f"VALUES ('inv-sl2', '{tid}', 'b@y.com', '{uid}', "
            f"'{token}', '2026-12-31')"
        )


@pytest.mark.asyncio
async def test_sqlite_token_hash_min_length(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "hlen")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) "
            f"VALUES ('inv-slh', '{tid}', 'x@y.com', '{uid}', "
            f"'short', '2026-12-31')"
        )


@pytest.mark.asyncio
async def test_sqlite_email_length(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "elen")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, invited_by, token_hash, "
            "expires_at) "
            f"VALUES ('inv-sle', '{tid}', '', '{uid}', "
            f"'{'h' * 64}', '2026-12-31')"
        )


@pytest.mark.asyncio
async def test_sqlite_tenant_cascade_delete(_fresh_sqlite_db):
    """SQLite enforces FK only when ``PRAGMA foreign_keys=ON`` —
    db.init sets it on init.  Cascade deletes the invite when the
    parent tenant is removed.

    The seed helper also creates a ``users`` row for the inviter, and
    ``users.tenant_id`` FKs to ``tenants(id)`` without ON DELETE
    CASCADE — so the user must be removed first before the tenant
    can be deleted.  This mirrors the real offboarding flow where
    tenant deletion is preceded by user purge."""
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "tcas")
    await conn.execute(
        "INSERT INTO tenant_invites "
        "(id, tenant_id, email, invited_by, token_hash, expires_at) "
        f"VALUES ('inv-sltcas', '{tid}', 'x@y.com', '{uid}', "
        f"'{'h' * 64}', '2026-12-31')"
    )
    await conn.commit()
    # Drop the inviter first so the tenant FK from users is cleared.
    await conn.execute(f"DELETE FROM users WHERE id = '{uid}'")
    await conn.commit()
    await conn.execute(f"DELETE FROM tenants WHERE id = '{tid}'")
    await conn.commit()
    async with conn.execute(
        "SELECT 1 FROM tenant_invites WHERE id = 'inv-sltcas'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_sqlite_inviter_set_null_on_delete(_fresh_sqlite_db):
    """Deleting the inviter sets ``invited_by`` to NULL but keeps the
    invite row alive — the invitee's invite must survive the
    inviter's deletion."""
    conn = _fresh_sqlite_db
    tid, uid = await _sl_seed(conn, "ucas")
    await conn.execute(
        "INSERT INTO tenant_invites "
        "(id, tenant_id, email, invited_by, token_hash, expires_at) "
        f"VALUES ('inv-slucas', '{tid}', 'x@y.com', '{uid}', "
        f"'{'h' * 64}', '2026-12-31')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM users WHERE id = '{uid}'")
    await conn.commit()
    async with conn.execute(
        "SELECT invited_by FROM tenant_invites WHERE id = 'inv-slucas'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "invite must survive inviter deletion"
    assert row[0] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file sanity (revision chain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0035_tenant_invites.py"
)


def test_migration_0035_file_exists():
    assert _MIGRATION.exists(), str(_MIGRATION)


def test_migration_0035_revision_chain():
    spec = importlib.util.spec_from_file_location("m0035", str(_MIGRATION))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0035"
    assert m.down_revision == "0034"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migrator coverage (drift guard hand-off)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_migrator():
    import sys as _sys
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg",
        Path(__file__).resolve().parents[2]
        / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass-decorated members can resolve
    # their forward-referenced types via ``sys.modules`` lookups.
    _sys.modules["migrate_sqlite_to_pg"] = mig
    spec.loader.exec_module(mig)
    return mig


def test_migrator_lists_tenant_invites():
    """The SQLite→PG migrator must replay the new table.  The
    ``test_migrator_schema_coverage`` drift guard would also catch
    this, but the explicit assertion here makes the contract
    visible at the point the new table is added."""
    mig = _load_migrator()
    assert "tenant_invites" in mig.TABLES_IN_ORDER
    # TEXT PK — must NOT be in the identity-reset list (would crash
    # sequence reset since ``inv-*`` is not an INTEGER IDENTITY).
    assert "tenant_invites" not in mig.TABLES_WITH_IDENTITY_ID


def test_migrator_orders_tenant_invites_after_users_and_tenants():
    """``tenant_invites.tenant_id → tenants(id)`` (CASCADE) and
    ``tenant_invites.invited_by → users(id)`` (SET NULL) mean replay
    order must put both parents first."""
    mig = _load_migrator()
    order = mig.TABLES_IN_ORDER
    assert order.index("tenant_invites") > order.index("tenants")
    assert order.index("tenant_invites") > order.index("users")
