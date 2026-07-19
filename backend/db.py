"""Database persistence layer.

SQLite path via aiosqlite for the legacy dev flow (no
``OMNISIGHT_DATABASE_URL`` set). Production runs against PostgreSQL
through :mod:`backend.db_pool` — every domain helper in this module
takes an explicit ``conn`` argument so pool-acquired connections and
SQLite connections share the same call surface.

Phase-3 Step C.2 (2026-04-21): the PG-path compat shim
``backend.db_pg_compat.PgCompatConnection`` has been retired. The
prod entry point is now the lifespan-managed ``db_pool.init_pool``;
``db.init()`` below only opens the SQLite dev DB. If a
``OMNISIGHT_DATABASE_URL`` is set, callers are expected to be
pool-native (all domain helpers are), and ``db.init()`` short-circuits.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import sqlalchemy as sa
from alembic.ddl.base import AddColumn as _AlembicAddColumn
from sqlalchemy.dialects import sqlite as _sqlite_dialect_mod

from backend.db_context import (
    tenant_insert_value,
    tenant_where_pg,
)

logger = logging.getLogger(__name__)

# FX.1.9: dialect-aware DDL rendering for the in-process SQLite migrator
# below. We render ALTER TABLE statements via SQLAlchemy / alembic's DDL
# compiler instead of f-string interpolation so identifier quoting and
# type/default rendering go through the dialect's escape logic. The
# migration list is hardcoded in source (no user-controlled identifiers),
# but the audit row asked us to take the f-string out of the loop on
# principle — these are the rails the rest of the codebase already uses
# for schema operations (alembic ``op.add_column``).
_SQLITE_DIALECT = _sqlite_dialect_mod.dialect()
_SQLITE_PREPARER = _SQLITE_DIALECT.identifier_preparer
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# SQLite-accepted ON DELETE / ON UPDATE actions; reject anything else
# rather than splice an attacker-controlled string into DDL even though
# every current call passes a hardcoded literal.
_FK_ACTIONS = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}


def _validate_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _render_add_column(table: str, column: sa.Column) -> str:
    """Render ``ALTER TABLE <t> ADD COLUMN <c> ...`` via the SQLite dialect.

    Uses alembic's ``AddColumn`` DDL construct so identifier quoting,
    type rendering, and default-clause escaping all go through the
    SQLAlchemy compiler instead of f-string composition. SQLite has no
    ``ALTER TABLE ADD CONSTRAINT`` form, so any ``ForeignKey`` on
    ``column`` is appended as an inline ``REFERENCES`` clause built
    through the dialect's ``IdentifierPreparer`` (alembic's AddColumn
    intentionally drops FK because most other dialects need a separate
    ADD CONSTRAINT round-trip — for SQLite the inline form is the only
    option).
    """
    _validate_ident(table)
    _validate_ident(column.name)
    if column.table is None:
        # FK / DDL bookkeeping wants a parent Table; a throwaway one is fine.
        sa.Table(table, sa.MetaData(), column)
    sql = str(_AlembicAddColumn(table, column).compile(dialect=_SQLITE_DIALECT))

    fk_parts: list[str] = []
    for fk in column.foreign_keys:
        # Use ``target_fullname`` (string form) rather than ``fk.column``
        # so we don't trigger SQLAlchemy's MetaData lookup — the parent
        # tables aren't registered in our throwaway MetaData.
        spec = fk.target_fullname
        if spec.count(".") != 1:
            raise ValueError(f"Unsupported FK target: {spec!r}")
        target_table, target_col = spec.split(".", 1)
        _validate_ident(target_table)
        _validate_ident(target_col)
        clause = (
            f"REFERENCES {_SQLITE_PREPARER.quote(target_table)} "
            f"({_SQLITE_PREPARER.quote(target_col)})"
        )
        if fk.ondelete:
            action = fk.ondelete.upper()
            if action not in _FK_ACTIONS:
                raise ValueError(f"Unsupported ON DELETE action: {fk.ondelete!r}")
            clause += f" ON DELETE {action}"
        if fk.onupdate:
            action = fk.onupdate.upper()
            if action not in _FK_ACTIONS:
                raise ValueError(f"Unsupported ON UPDATE action: {fk.onupdate!r}")
            clause += f" ON UPDATE {action}"
        fk_parts.append(clause)
    if fk_parts:
        sql = f"{sql} {' '.join(fk_parts)}"
    return sql

def _resolve_db_path() -> Path:
    from backend.config import settings
    if settings.database_path:
        return Path(settings.database_path).expanduser()
    return Path(__file__).resolve().parents[1] / "data" / "omnisight.db"


def _resolve_pg_dsn() -> str:
    """Return a libpq-style DSN if OMNISIGHT_DATABASE_URL / DATABASE_URL
    points at a PG host, else empty string.

    Accepts the usual ``postgresql+asyncpg://user:pw@host/db`` form and
    strips the ``+asyncpg`` qualifier asyncpg doesn't need. Checked in
    ``OMNISIGHT_DATABASE_URL`` → ``DATABASE_URL`` precedence matching
    :mod:`backend.db_url.resolve_from_env`.
    """
    import os
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL"):
        url = (os.environ.get(key) or "").strip()
        if not url:
            continue
        low = url.lower()
        if low.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://", "postgres+asyncpg://", "asyncpg://")):
            # asyncpg accepts postgresql://... directly; strip driver qualifier.
            resolved = url  # already postgresql://
            for prefix, canon in (
                ("postgresql+asyncpg://", "postgresql://"),
                ("postgres+asyncpg://", "postgresql://"),
                ("asyncpg://", "postgresql://"),
                ("postgres://", "postgresql://"),
            ):
                if low.startswith(prefix):
                    resolved = canon + url[len(prefix):]
                    break
            # [OP-1643] A2: env↔DB contract — fail closed if a dev process
            # resolved a prod-looking PG DSN here (or vice-versa).
            from backend.env_contract import enforce_env_db_contract
            enforce_env_db_contract(resolved, source="db._resolve_pg_dsn")
            return resolved
    return ""


_DB_PATH = _resolve_db_path()
# Holds the aiosqlite connection for the dev-mode path (no PG DSN).
# Production code does NOT read ``_db`` — it goes through
# ``db_pool.get_pool()``. See Phase-3 Step C.2.
_db: Any = None


async def init() -> None:
    """Open the SQLite dev DB and create tables if they don't exist.

    On SQLite (default, no ``OMNISIGHT_DATABASE_URL``): create tables
    via CREATE TABLE IF NOT EXISTS, run ALTER TABLE ADD COLUMN
    migrations in ``_migrate()``, set WAL pragmas.

    On Postgres (any ``OMNISIGHT_DATABASE_URL`` pointing at PG): this
    function is a no-op. The PG schema is owned by alembic
    (``alembic upgrade head`` runs at deploy time) and the pool is
    opened by the lifespan handler via ``db_pool.init_pool``. The
    Phase-3 Step C.2 cleanup retired the in-process compat wrapper
    that previously coexisted with the pool on the PG path.
    """
    global _db
    pg_dsn = _resolve_pg_dsn()
    if pg_dsn:
        # Production path: schema + pool are owned elsewhere. Nothing
        # to do here — leave ``_db`` as None so anyone who still
        # reaches for it fails fast with the "not initialized" error.
        logger.info(
            "db.init() no-op on PG path — pool lifecycle owned by "
            "db_pool.init_pool, schema owned by alembic."
        )
        return

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(str(_DB_PATH))
    _db.row_factory = aiosqlite.Row
    # SQLite hardening (pragmas must be set before schema creation)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.execute("PRAGMA foreign_keys=ON")
    # Quick integrity check
    async with _db.execute("PRAGMA quick_check") as cur:
        row = await cur.fetchone()
        if row and row[0] != "ok":
            logger.critical("Database integrity check FAILED: %s", row[0])
    await _db.commit()  # Commit pragmas before executescript (which does implicit COMMIT)
    await _db.executescript(_SCHEMA)
    # FTS5 virtual table for L3 episodic memory full-text search
    # (Must be created separately — FTS5 can fail if extension not loaded)
    try:
        await _db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts
            USING fts5(error_signature, solution, soc_vendor, tags, content='episodic_memory', content_rowid='rowid')
        """)
        await _db.commit()
    except Exception as exc:
        logger.warning("FTS5 not available (L3 search will use LIKE fallback): %s", exc)
    # Run lightweight migrations for schema evolution
    await _migrate(_db)
    await _db.commit()
    logger.info("Database ready (WAL mode): %s", _DB_PATH)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Add columns that may be missing in older databases.

    FX.1.9: column specs are SQLAlchemy ``Column`` objects so the ALTER
    TABLE rendering goes through the SQLite dialect's DDL compiler
    (see :func:`_render_add_column`) rather than f-string composition.
    Numeric defaults use ``sa.text(...)`` to render unquoted (matching
    the prior raw-SQL form ``DEFAULT 0`` / ``DEFAULT 0.0``); string
    defaults are passed as plain Python strings so the compiler quotes
    them (``DEFAULT '...'``).
    """
    _t = sa.Text
    _i = sa.Integer
    _f = sa.Float
    _txt = sa.text
    _fk_proj = lambda: sa.ForeignKey("projects.id", ondelete="SET NULL")  # noqa: E731

    migrations: list[tuple[str, sa.Column]] = [
        ("agents", sa.Column("sub_type", _t(), nullable=False, server_default="")),
        ("tasks", sa.Column("suggested_sub_type", _t())),
        ("tasks", sa.Column("parent_task_id", _t())),
        ("tasks", sa.Column("child_task_ids", _t(), nullable=False, server_default="[]")),
        ("tasks", sa.Column("external_issue_id", _t())),
        ("tasks", sa.Column("issue_url", _t())),
        ("tasks", sa.Column("acceptance_criteria", _t())),
        ("tasks", sa.Column("labels", _t(), nullable=False, server_default="[]")),
        ("tasks", sa.Column("depends_on", _t(), nullable=False, server_default="[]")),
        ("tasks", sa.Column("external_issue_platform", _t())),
        ("tasks", sa.Column("last_external_sync_at", _t())),
        # Pipeline linkage (Phase 46)
        ("tasks", sa.Column("npi_phase_id", _t())),
        # RPG.W20.1 -- multi-task campaign grouping with narrative title.
        ("tasks", sa.Column("rpg_campaign_id", _t())),
        ("tasks", sa.Column("rpg_campaign_title", _t())),
        ("notifications", sa.Column("dispatch_status", _t(), nullable=False, server_default="pending")),
        ("notifications", sa.Column("send_attempts", _i(), nullable=False, server_default=_txt("0"))),
        ("notifications", sa.Column("last_error", _t())),
        # R9 row 2935 (#315) — operational-priority tag (P1/P2/P3).
        # NULLable so legacy callers without severity awareness keep
        # working unchanged; dispatcher consumes this in row 2939.
        ("notifications", sa.Column("severity", _t())),
        # BP.H.3 — persistent red-card marker. DB-backed, not worker
        # global state; every worker reads the same notification row.
        ("notifications", sa.Column(
            "is_red_card", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        )),
        # Artifact version/checksum (Phase 39)
        ("artifacts", sa.Column("version", _t(), nullable=False, server_default="")),
        ("artifacts", sa.Column("checksum", _t(), nullable=False, server_default="")),
        # NPU simulation fields (Phase 36)
        ("simulations", sa.Column("npu_latency_ms", _f(), nullable=False, server_default=_txt("0.0"))),
        ("simulations", sa.Column("npu_throughput_fps", _f(), nullable=False, server_default=_txt("0.0"))),
        ("simulations", sa.Column("accuracy_delta", _f(), nullable=False, server_default=_txt("0.0"))),
        ("simulations", sa.Column("model_size_kb", _i(), nullable=False, server_default=_txt("0"))),
        ("simulations", sa.Column("npu_framework", _t(), nullable=False, server_default="")),
        # Phase 56-DAG-B — DAG planner ↔ workflow linkage.
        ("workflow_runs", sa.Column("dag_plan_id", _i())),
        ("workflow_runs", sa.Column("successor_run_id", _t())),
        ("workflow_steps", sa.Column("dag_task_id", _t())),
        # Phase 63-E — Memory quality decay.
        ("episodic_memory", sa.Column("decayed_score", _f(), nullable=False, server_default=_txt("0.0"))),
        ("episodic_memory", sa.Column("last_used_at", _t())),
        # S0 — session/audit enhancements.
        ("audit_log", sa.Column("session_id", _t())),
        ("sessions", sa.Column("metadata", _t(), nullable=False, server_default="{}")),
        ("sessions", sa.Column("mfa_verified", _i(), nullable=False, server_default=_txt("0"))),
        ("sessions", sa.Column("rotated_from", _t())),
        # K1 — force password change for default-credential admins.
        ("users", sa.Column("must_change_password", _i(), nullable=False, server_default=_txt("0"))),
        # K2 — account lockout after consecutive login failures.
        ("users", sa.Column("failed_login_count", _i(), nullable=False, server_default=_txt("0"))),
        ("users", sa.Column("locked_until", _f())),
        # K4 — session rotation + UA binding.
        ("sessions", sa.Column("ua_hash", _t(), nullable=False, server_default="")),
        # I1 — multi-tenancy: tenant_id on all business tables.
        ("users", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("workflow_runs", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("debug_findings", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("decision_rules", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("event_log", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("audit_log", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("artifacts", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("user_preferences", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        # I4: tenant_id on api_keys
        ("api_keys", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        # OP-228: deterministic lookup for KS-enveloped api_keys.key_hash.
        ("api_keys", sa.Column("key_lookup_index", _t())),
        ("api_keys", sa.Column("expires_at", sa.Float(), nullable=True)),
        ("provisioned_storage", sa.Column("bucket_lookup_index", _t())),
        # Q.7 #301 — optimistic-lock version column expansion (mirrors
        # alembic 0023_optimistic_lock_expansion for SQLite bootstrap).
        ("tasks", sa.Column("version", _i(), nullable=False, server_default=_txt("0"))),
        ("npi_state", sa.Column("version", _i(), nullable=False, server_default=_txt("0"))),
        ("tenant_secrets", sa.Column("version", _i(), nullable=False, server_default=_txt("0"))),
        ("project_runs", sa.Column("version", _i(), nullable=False, server_default=_txt("0"))),
        # Y1 row 7 (#277): project_id on business tables. NULLable,
        # FK to projects(id) ON DELETE SET NULL — same set as alembic
        # 0038's _TABLES_NEEDING_PROJECT_ID. Backfill is *not* done
        # here (the CREATE TABLE in _SCHEMA already includes
        # project_id on a fresh DB; existing dev DBs that pre-date
        # this column rely on the alembic 0038 backfill UPDATE — which
        # this in-process migrator does NOT run because dev SQLite
        # doesn't go through alembic). The next-release NOT NULL flip
        # will need a parallel UPDATE here when it lands.
        ("workflow_runs", sa.Column("project_id", _t(), _fk_proj())),
        ("debug_findings", sa.Column("project_id", _t(), _fk_proj())),
        ("decision_rules", sa.Column("project_id", _t(), _fk_proj())),
        ("event_log", sa.Column("project_id", _t(), _fk_proj())),
        ("artifacts", sa.Column("project_id", _t(), _fk_proj())),
        ("user_preferences", sa.Column("project_id", _t(), _fk_proj())),
        # BP.B.1 (OP-249, alembic 0241): nullable guild dimension on
        # durable execution / debugging / audit trace tables.
        ("workflow_runs", sa.Column("guild_id", _t())),
        ("debug_findings", sa.Column("guild_id", _t())),
        ("audit_log", sa.Column("guild_id", _t())),
        # AS.0.2 (alembic 0056): per-tenant auth feature gating. TEXT-of-JSON
        # on SQLite, JSONB on PG. Default '{}' = no AS opinion.
        ("tenants", sa.Column("auth_features", _t(), nullable=False, server_default="{}")),
        # AS.0.3 (alembic 0058): per-user auth-methods array (account
        # linking takeover-prevention).  TEXT-of-JSON on SQLite, JSONB
        # on PG.  Default '[]' = no method recorded; the helper
        # backend.account_linking writes the explicit value.  The
        # backfill path that turns existing password users into
        # ``["password"]`` lives in alembic 0058's UPDATE — not here,
        # because the in-process SQLite migrator only adds columns and
        # the dev DB is per-test ephemeral so seeded rows go through
        # the AS-aware INSERT path (auth.py::_create_user_impl).
        ("users", sa.Column("auth_methods", _t(), nullable=False, server_default="[]")),
        # OP-884 (alembic 0225): partial / per-tenant feature flag controls.
        # ``rollout_pct`` defaults to 100 so existing rows keep the WP.7.8
        # "global on/off" semantics; ``allowed_tenants`` defaults to an
        # empty JSON array so the SDK falls through to the percent gate
        # unless the operator explicitly seeds an allow-list. ``updated_at``
        # is added nullable because SQLite forbids non-constant defaults
        # on ``ADD COLUMN``; fresh-DB CREATE TABLE in ``_SCHEMA`` carries
        # the proper ``DEFAULT CURRENT_TIMESTAMP`` already.
        ("feature_flags", sa.Column(
            "rollout_pct", _i(), nullable=False, server_default=_txt("100"),
        )),
        ("feature_flags", sa.Column(
            "allowed_tenants", _t(), nullable=False, server_default="[]",
        )),
        ("feature_flags", sa.Column("updated_at", _t())),
        # U6-0 T8-A (OP-2610, alembic 0260): source-aware episodic
        # memory, quarantined-by-default. tenant_id MUST stay a PLAIN
        # Column with NO sa.ForeignKey — _render_add_column would emit
        # an inline REFERENCES, and SQLite rejects ADD COLUMN with both
        # a non-NULL default and an FK while foreign_keys=ON; the loop
        # below would swallow that as a WARNING and the column would
        # silently never exist (the REQUIRED entries below are the
        # backstop). Fresh DBs get the FK from the CREATE TABLE in
        # _SCHEMA. verified is Integer, not Boolean — keeps both SQLite
        # schema sources reading identically.
        ("episodic_memory", sa.Column("tenant_id", _t(), nullable=False, server_default="t-default")),
        ("episodic_memory", sa.Column("source", _t(), nullable=False, server_default="legacy")),
        ("episodic_memory", sa.Column("verification_authority", _t())),
        ("episodic_memory", sa.Column("verified", _i(), nullable=False, server_default=_txt("0"))),
        ("episodic_memory", sa.Column("owner_user_id", _t())),
        ("episodic_memory", sa.Column("visibility", _t(), nullable=False, server_default="tenant_shared")),
    ]
    # N6: critical columns the runtime hard-depends on. If post-migration
    # any of these are still missing, fail-fast at startup rather than
    # silently letting the ORM raise IntegrityError on every insert.
    REQUIRED = {
        ("tasks", "npi_phase_id"),
        ("agents", "sub_type"),
        # U6-0 T8-A: the migration loop above fails OPEN (a non-
        # "duplicate" ALTER error is logged and skipped), so these
        # fail-fast entries are load-bearing: without them a silently
        # missing provenance column would let unquarantined reads
        # through once T8-C flips the readers.
        ("episodic_memory", "tenant_id"),
        ("episodic_memory", "source"),
        ("episodic_memory", "verification_authority"),
        ("episodic_memory", "verified"),
        ("episodic_memory", "owner_user_id"),
        ("episodic_memory", "visibility"),
    }
    for table, column in migrations:
        try:
            await conn.execute(_render_add_column(table, column))
            logger.info("Migration: added %s.%s", table, column.name)
        except Exception as exc:
            if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                pass  # Column already exists — expected
            else:
                logger.warning("Migration %s.%s failed: %s", table, column.name, exc)

    # Phase 63-E fix: index for decay worker's last_used_at filter.
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_last_used "
            "ON episodic_memory(last_used_at)"
        )
    except Exception as exc:
        logger.warning("idx_episodic_last_used create failed: %s", exc)

    # S0: audit_log.session_id index (safe to run after column migration).
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_session "
            "ON audit_log(session_id)"
        )
    except Exception as exc:
        logger.warning("idx_audit_log_session create failed: %s", exc)

    # I1: seed default tenant + tenant_id indexes.
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name, plan) "
            "VALUES ('t-default', 'Default Tenant', 'free')"
        )
    except Exception as exc:
        logger.warning("Default tenant seed failed: %s", exc)

    # U6-0 T8-A (OP-2610, alembic 0260): seed the runner's own tenant
    # so its episodic writes can't violate the tenants FK once P-ID-C
    # identity threading reaches the writers.
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name, plan) "
            "VALUES ('omnisight-self', 'OmniSight Internal', 'free')"
        )
    except Exception as exc:
        logger.warning("omnisight-self tenant seed failed: %s", exc)

    # U6-0 T8-A: quarantine legacy episodic rows explicitly. Redundant
    # with the ADD COLUMN defaults above on the happy path, but belt-
    # and-braces for a partially-migrated DB. Bounded to NULL/empty
    # values so a future T8-B verified service write is never clobbered
    # by this every-boot migrator.
    try:
        await conn.execute(
            "UPDATE episodic_memory SET "
            "tenant_id = COALESCE(NULLIF(tenant_id, ''), 't-default'), "
            "source = COALESCE(NULLIF(source, ''), 'legacy'), "
            "visibility = COALESCE(NULLIF(visibility, ''), 'tenant_shared'), "
            "verified = COALESCE(verified, 0) "
            "WHERE tenant_id IS NULL OR tenant_id = '' "
            "OR source IS NULL OR source = '' "
            "OR visibility IS NULL OR visibility = '' "
            "OR verified IS NULL"
        )
    except Exception as exc:
        logger.warning("episodic legacy quarantine backfill failed: %s", exc)

    # U6-0 T8-A: tenant filter index (PG gets the richer
    # (tenant_id, verified) composite in alembic 0260).
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_tenant "
            "ON episodic_memory(tenant_id)"
        )
    except Exception as exc:
        logger.warning("idx_episodic_tenant create failed: %s", exc)

    # U6-0 T8-B1: partial unique index = one verified merge-solution row per
    # change (mirrors alembic 0261; the verified-write path relies on
    # ON CONFLICT DO NOTHING). verified is INTEGER on SQLite → `verified = 1`.
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_episodic_verified_merge "
            "ON episodic_memory (gerrit_change_id) "
            "WHERE verified = 1 AND source = 'service_gerrit_merge'"
        )
    except Exception as exc:
        logger.warning("uq_episodic_verified_merge create failed: %s", exc)

    # Y1 row 6 (#277, alembic 0037): seed default project for the
    # default tenant. Mirrors the alembic backfill's deterministic id
    # projection (``p-<tenant-suffix>-default``) for the single
    # ``t-default`` tenant the dev SQLite path always carries — keeps
    # FK targets present so Y1 row 7 backfill (``project_id`` on
    # business tables) and any downstream Y2/Y3 reader sees a
    # populated projects table on a fresh dev DB.
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO projects "
            "(id, tenant_id, product_line, name, slug) "
            "VALUES ('p-default-default', 't-default', 'default', "
            "'Default', 'default')"
        )
    except Exception as exc:
        logger.warning("Default project seed failed: %s", exc)

    # Y1 row 7 (#277, alembic 0038): backfill project_id on existing
    # business rows that pre-date the column. Bounded to the
    # ``t-default`` tenant because dev SQLite is single-tenant by
    # construction (operators creating a new tenant in dev would
    # also create a corresponding project before pointing rows at
    # it). Idempotent via the ``project_id IS NULL`` predicate.
    _project_backfill_tables = [
        "workflow_runs", "debug_findings", "decision_rules",
        "event_log", "artifacts", "user_preferences",
    ]
    for t in _project_backfill_tables:
        try:
            await conn.execute(
                f"UPDATE {t} SET project_id = 'p-default-default' "
                f"WHERE project_id IS NULL AND tenant_id = 't-default'"
            )
        except Exception as exc:
            # The column may not exist yet on a DB that just bumped
            # SQLite schema before the ALTER TABLE in this same
            # _migrate() call lands — swallow and let the next boot
            # retry. Same pattern as the index-create blocks below.
            logger.warning("project_id backfill on %s failed: %s", t, exc)

    _tenant_tables = [
        "users", "artifacts", "event_log", "debug_findings",
        "decision_rules", "workflow_runs", "audit_log", "user_preferences",
        "api_keys",
    ]
    for t in _tenant_tables:
        try:
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{t}_tenant ON {t}(tenant_id)"
            )
        except Exception as exc:
            logger.warning("idx_%s_tenant create failed: %s", t, exc)

    # Y1 row 7 (#277): project_id indexes on business tables. Mirrors
    # alembic 0038's per-table ``idx_<table>_project ON <table>(project_id)``
    # so dev SQLite bootstraps see the same query plan as production PG.
    # Subset of _tenant_tables — audit_log / users / api_keys are
    # tenant-scoped only (no project_id).
    _project_tables = [
        "workflow_runs", "debug_findings", "decision_rules",
        "event_log", "artifacts", "user_preferences",
    ]
    for t in _project_tables:
        try:
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{t}_project ON {t}(project_id)"
            )
        except Exception as exc:
            logger.warning("idx_%s_project create failed: %s", t, exc)

    # Verify every REQUIRED column ended up present (defends against a YAML
    # typo or partial schema rebuild).
    for table, column in REQUIRED:
        try:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
        except Exception as exc:
            # R2-#34: if PRAGMA itself fails we cannot verify invariants,
            # so we must fail loudly instead of logging and proceeding —
            # the app would otherwise start with an invisibly broken
            # schema and every insert would IntegrityError at runtime.
            raise RuntimeError(
                f"Schema verify failed for {table}.{column}: {exc}"
            ) from exc
        if column not in cols:
            raise RuntimeError(
                f"Required column {table}.{column} missing after migration"
            )


async def close() -> None:
    """Checkpoint WAL + close the database connection.

    The checkpoint is C2 (audit 2026-04-19): previously ``close()`` just
    dropped the connection, which under aiosqlite means the WAL file
    (.db-wal) may still hold committed transactions that haven't been
    folded back into the main DB. If the process is then SIGKILLed
    before the OS flushes the WAL (e.g. drain timeout → systemd escalates
    to SIGKILL, or the host crashes), restart recovery has to replay the
    WAL. Most of the time that works — but any filesystem-level
    corruption of the WAL during the unclean shutdown becomes silent
    data loss.

    ``wal_checkpoint(RESTART)`` forces every committed page into the
    main DB and resets the WAL to size 0 before we let go of the
    connection. Takes ~ms on typical data volumes; errors are logged
    but never raised because the ``close`` path must be infallible —
    a failed checkpoint still leaves the DB readable on next boot via
    normal WAL replay. ``PASSIVE`` is a fallback for the rare case where
    ``RESTART`` is blocked by another reader (unlikely in lifespan
    teardown since all handlers have drained).
    """
    global _db
    if _db:
        # WAL checkpoint is SQLite-specific. Phase-3 Step C.2 retired
        # the compat wrapper; ``_db`` now only holds aiosqlite
        # connections, so this loop always applies when ``_db`` is set.
        for mode in ("RESTART", "PASSIVE"):
            try:
                async with _db.execute(f"PRAGMA wal_checkpoint({mode})") as cur:
                    row = await cur.fetchone()
                if row is not None:
                    # row = (busy, log, checkpointed). busy=0 means clean.
                    logger.debug(
                        "[db] wal_checkpoint(%s) busy=%s log=%s checkpointed=%s",
                        mode, row[0], row[1], row[2],
                    )
                if row is None or row[0] == 0:
                    break  # clean checkpoint → stop; no need for PASSIVE fallback
            except Exception as exc:
                logger.warning("[db] wal_checkpoint(%s) failed: %s", mode, exc)
        await _db.close()
        _db = None


async def execute_raw(sql: str, params: tuple = ()) -> int:
    """Execute raw SQL and return rows affected. For startup cleanup.

    Phase-3 Step C.1 (2026-04-21): ported off the compat wrapper to
    the asyncpg pool. The two production callers in ``backend.main``
    still pass ``?``-style placeholders so we translate them to
    asyncpg's ``$N`` here rather than forcing a change to the call
    sites — positional translation is safe because the callers pass
    a single positional tuple.

    Returns the affected row count. asyncpg's ``execute`` returns
    the command tag (e.g. ``'UPDATE 3'``); we parse the trailing int
    out of it.
    """
    from backend.db_pool import get_pool
    if "?" in sql:
        parts = sql.split("?")
        sql = "".join(
            parts[i] + (f"${i+1}" if i < len(params) else "")
            for i in range(len(parts))
        )
    async with get_pool().acquire() as conn:
        tag = await conn.execute(sql, *params)
    try:
        return int(tag.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _conn() -> Any:
    """Return the open aiosqlite connection (SQLite dev-mode only).

    Phase-3 Step C.2 (2026-04-21): after the compat wrapper retirement
    this only returns ``aiosqlite.Connection``. On the PG path
    ``db.init()`` is a no-op and ``_db`` stays None, so calling this
    here will raise — which is the intended behaviour: pool-native
    callers should go through ``db_pool.get_pool().acquire()``, not
    ``db._conn()``. The remaining ``_conn()`` callers are limited to
    ``backend/tests/test_migrator_schema_coverage.py`` (SQLite
    drift-guard by design, kept pointed at a tempfile SQLite DB
    that's the whole point of that test).
    """
    if _db is None:
        raise RuntimeError("Database not initialized — call db.init() first")
    return _db


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCHEMA = """
-- I1: Multi-tenancy foundation
-- AS.0.2 (alembic 0056): auth_features TEXT-of-JSON column gates
-- per-tenant AS roadmap knobs (oauth_login / turnstile_required /
-- honeypot_active / auth_layer).  Default '{}' = no AS opinion;
-- application interprets missing keys as legacy/false.  Kept TEXT
-- on SQLite (no native JSONB); promoted to JSONB on PG via the
-- 0056 migration's PG branch.
CREATE TABLE IF NOT EXISTS tenants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    plan            TEXT NOT NULL DEFAULT 'free',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    auth_features   TEXT NOT NULL DEFAULT '{}'
);

-- WP.7.1 (alembic 0194): tiered feature flag registry.
-- OP-884 (alembic 0225): added rollout_pct + allowed_tenants + updated_at
-- so the registry expresses partial / per-tenant enablement instead of
-- just on/off. Runtime writers must audit changes through audit_log
-- with entity_kind='feature_flag'; this table is the durable source
-- of truth.
CREATE TABLE IF NOT EXISTS feature_flags (
    flag_name       TEXT PRIMARY KEY,
    tier            TEXT NOT NULL
                    CHECK (tier IN ('debug','dogfood','early_access','staged','ga')),
    state           TEXT NOT NULL DEFAULT 'disabled'
                    CHECK (state IN ('disabled','enabled')),
    expires_at      TEXT,
    owner           TEXT NOT NULL DEFAULT '',
    rollout_pct     INTEGER NOT NULL DEFAULT 100
                    CHECK (rollout_pct BETWEEN 0 AND 100),
    allowed_tenants TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feature_flags_tier_state
    ON feature_flags(tier, state);
CREATE INDEX IF NOT EXISTS idx_feature_flags_expires_at
    ON feature_flags(expires_at);

-- WP.1.2 (alembic 0195): addressable Block primitive.
-- Runtime writers, share/permalink flows, and redaction enforcement land
-- in WP.1.3-WP.1.8; this is the durable table + lookup shape.
CREATE TABLE IF NOT EXISTS blocks (
    block_id       TEXT PRIMARY KEY,
    parent_id      TEXT REFERENCES blocks(block_id),
    tenant_id      TEXT NOT NULL,
    user_id        TEXT,
    project_id     TEXT,
    session_id     TEXT,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}',
    metadata       TEXT NOT NULL DEFAULT '{}',
    redaction_mask TEXT NOT NULL DEFAULT '{}',
    started_at     TEXT,
    completed_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_blocks_tenant_session
    ON blocks(tenant_id, session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_blocks_parent
    ON blocks(parent_id);

-- BP.A2A.4 (alembic 0196): inbound A2A invocation audit/replay index.
-- Stores only caller identity and payload/response hashes; request and
-- response plaintext remain outside this table.
CREATE TABLE IF NOT EXISTS a2a_invocations (
    invocation_id   TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL
                          REFERENCES tenants(id) ON DELETE CASCADE,
    agent_name      TEXT NOT NULL,
    caller_identity TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    response_hash   TEXT NOT NULL,
    latency_ms      INTEGER NOT NULL CHECK (latency_ms >= 0),
    status          TEXT NOT NULL
                          CHECK (status IN ('completed','failed')),
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_a2a_invocations_tenant_agent_time
    ON a2a_invocations(tenant_id, agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_invocations_payload_hash
    ON a2a_invocations(payload_hash);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    sub_type    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'idle',
    progress    TEXT NOT NULL DEFAULT '{"current":0,"total":0}',
    thought_chain TEXT NOT NULL DEFAULT '',
    ai_model    TEXT,
    sub_tasks   TEXT NOT NULL DEFAULT '[]',
    workspace   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT,
    priority            TEXT NOT NULL DEFAULT 'medium',
    status              TEXT NOT NULL DEFAULT 'backlog',
    assigned_agent_id   TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT,
    ai_analysis         TEXT,
    suggested_agent_type TEXT,
    suggested_sub_type  TEXT,
    parent_task_id      TEXT,
    child_task_ids      TEXT NOT NULL DEFAULT '[]',
    external_issue_id   TEXT,
    issue_url           TEXT,
    acceptance_criteria TEXT,
    labels              TEXT NOT NULL DEFAULT '[]',
    rpg_campaign_id     TEXT,
    rpg_campaign_title  TEXT
);

CREATE TABLE IF NOT EXISTS task_comments (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- BP.Q.4 (alembic 0186): tenant-scoped RAG embedding chunks.
-- SQLite stores embedding + metadata as TEXT for dev parity; production
-- PG uses pgvector + JSONB from the alembic migration.
CREATE TABLE IF NOT EXISTS embedding_chunks (
    chunk_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL
                     REFERENCES tenants(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_embedding_chunks_tenant_source
    ON embedding_chunks(tenant_id, source_path);

-- BP.M.1 (alembic 0192): L1 skill auto-distillation review queue.
-- Runtime summarisation, review/promote endpoints, and audit events
-- land in BP.M.2-BP.M.5.  TEXT PK avoids sequence-reset work during
-- SQLite -> PG cutover.
CREATE TABLE IF NOT EXISTS auto_distilled_skills (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL
                          REFERENCES tenants(id) ON DELETE CASCADE,
    skill_name       TEXT NOT NULL,
    source_task_id   TEXT
                          REFERENCES tasks(id) ON DELETE SET NULL,
    markdown_content TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft','promoted','reviewed')),
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_auto_distilled_skills_tenant_status
    ON auto_distilled_skills(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_auto_distilled_skills_source_task
    ON auto_distilled_skills(source_task_id);

CREATE TABLE IF NOT EXISTS npi_state (
    id          TEXT PRIMARY KEY DEFAULT 'current',
    data        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    agent_id    TEXT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'markdown',
    file_path   TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    tenant_id   TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id added by alembic 0038. NULLable
    -- until the next-release NOT NULL flip; FK target ``projects``
    -- is created later in this _SCHEMA — SQLite resolves FK
    -- references at insert time, not CREATE time, so the forward
    -- reference is safe on the SQLite-only dev path (PG production
    -- never executes _SCHEMA; alembic 0038 owns the FK there).
    project_id  TEXT REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id              TEXT PRIMARY KEY,
    level           TEXT NOT NULL DEFAULT 'info',
    title           TEXT NOT NULL,
    message         TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    read            INTEGER NOT NULL DEFAULT 0,
    action_url      TEXT,
    action_label    TEXT,
    auto_resolved   INTEGER NOT NULL DEFAULT 0,
    dispatch_status TEXT NOT NULL DEFAULT 'pending',
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    -- R9 row 2935 (#315): P1/P2/P3 severity tag (orthogonal to level).
    severity        TEXT,
    -- BP.H.3: red-card notifications route to L3 Jira + L4 PagerDuty.
    is_red_card     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS handoffs (
    task_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS token_usage (
    model               TEXT PRIMARY KEY,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    cost                REAL NOT NULL DEFAULT 0.0,
    request_count       INTEGER NOT NULL DEFAULT 0,
    avg_latency         INTEGER NOT NULL DEFAULT 0,
    last_used           TEXT NOT NULL DEFAULT '',
    -- ZZ.A1 (#303-1): prompt-cache observability. NULLable by design —
    -- rows that predate ZZ.A1 leave these as NULL and the dashboard
    -- renders an em-dash rather than misleading zeros. New rows
    -- always populate them via SharedTokenUsage.track().
    cache_read_tokens   INTEGER,
    cache_create_tokens INTEGER,
    cache_hit_ratio     REAL,
    -- ZZ.A3 (#303-3): per-turn LLM-compute boundary stamps in
    -- ISO-8601 UTC. Last-turn snapshots (overwrite, not accumulate)
    -- so downstream can compute (a) LLM compute time = ended - started
    -- of the same row, and (b) inter-turn gap = this_turn.started -
    -- prior_turn.ended (tool + event-bus + context-gather wait).
    -- NULL on pre-ZZ.A3 rows per the same NULL-vs-genuine-zero
    -- contract cache_* fields established in ZZ.A1.
    turn_started_at     TEXT,
    turn_ended_at       TEXT
);

CREATE TABLE IF NOT EXISTS simulations (
    id              TEXT PRIMARY KEY,
    task_id         TEXT,
    agent_id        TEXT,
    track           TEXT NOT NULL,
    module          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    tests_total     INTEGER NOT NULL DEFAULT 0,
    tests_passed    INTEGER NOT NULL DEFAULT 0,
    tests_failed    INTEGER NOT NULL DEFAULT 0,
    coverage_pct    REAL NOT NULL DEFAULT 0.0,
    valgrind_errors INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    report_json     TEXT NOT NULL DEFAULT '{}',
    artifact_id     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS event_log (
    id              INTEGER PRIMARY KEY,
    event_type      TEXT NOT NULL,
    data_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id (alembic 0038). See artifacts above
    -- for the forward-reference rationale.
    project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL
);

-- L3 Episodic Memory: long-term knowledge base for cross-project learning
CREATE TABLE IF NOT EXISTS episodic_memory (
    id              TEXT PRIMARY KEY,
    error_signature TEXT NOT NULL,
    solution        TEXT NOT NULL,
    soc_vendor      TEXT NOT NULL DEFAULT '',
    sdk_version     TEXT NOT NULL DEFAULT '',
    hardware_rev    TEXT NOT NULL DEFAULT '',
    source_task_id  TEXT,
    source_agent_id TEXT,
    gerrit_change_id TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    quality_score   REAL NOT NULL DEFAULT 0.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- U6-0 T8-A (OP-2610, alembic 0260): source-aware provenance,
    -- quarantined-by-default. The inline FK is safe at CREATE time
    -- (matches every sibling table); the _migrate() ALTER path adds
    -- tenant_id WITHOUT it (SQLite forbids ADD COLUMN ... REFERENCES
    -- with a non-NULL default while foreign_keys=ON). No CHECKs here:
    -- SQLite stays a deliberate subset of the PG schema; the
    -- verified=>gerrit CHECK binds on PG (prod). verified is INTEGER
    -- (SQLite has no BOOLEAN) — keep _migrate() identical.
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    source          TEXT NOT NULL DEFAULT 'legacy',
    verification_authority TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,
    owner_user_id   TEXT,
    visibility      TEXT NOT NULL DEFAULT 'tenant_shared'
);
-- NB: the T8-B1 uq_episodic_verified_merge partial index is created in
-- _migrate() (AFTER the columns land), NOT here — on an existing pre-0260
-- DB the CREATE TABLE above is a no-op and `verified` would not yet exist.

-- U6-0 T9/T10 G0a (OP-2622): durable, write-once provenance snapshots.
-- PostgreSQL's alembic 0262 schema is authoritative; SQLite deliberately
-- stores JSON as TEXT and leaves the complete-only CHECK to the repository
-- boundary.  The explicit composite UNIQUE supports later grant FKs.
CREATE TABLE IF NOT EXISTS provenance_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id),
    model_call_id   TEXT NOT NULL,
    request_id      TEXT NOT NULL DEFAULT '',
    records         TEXT NOT NULL,
    omissions       TEXT NOT NULL DEFAULT '[]',
    completeness    TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT 'v1',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_provenance_snapshots_tenant_model_call
    ON provenance_snapshots(tenant_id, model_call_id);

-- U6-0 T9/T10 G2b (OP-2629): durable, write-once prepared actions.
-- PostgreSQL's alembic 0263 schema is authoritative; SQLite deliberately
-- stores JSON as TEXT. CHECKs mirror OperationIdentity.validate, and the
-- explicit composite UNIQUE supports later challenge and grant FKs.
CREATE TABLE IF NOT EXISTS prepared_actions (
    action_instance_id     TEXT PRIMARY KEY,
    tenant_id              TEXT NOT NULL REFERENCES tenants(id),
    principal_type         TEXT NOT NULL,
    actor_id               TEXT NOT NULL,
    request_id             TEXT NOT NULL DEFAULT '',
    model_call_id          TEXT NOT NULL DEFAULT '',
    adapter_namespace      TEXT NOT NULL,
    tool_name              TEXT NOT NULL,
    schema_version         TEXT NOT NULL,
    family                 TEXT NOT NULL,
    effect                 TEXT NOT NULL
                           CHECK (effect IN ('read_only', 'mutating')),
    canonical_target       TEXT NOT NULL DEFAULT '',
    args_hash              TEXT NOT NULL,
    provenance_kind        TEXT NOT NULL
                           CHECK (provenance_kind IN ('model', 'no_model_input')),
    model_snapshot_id      TEXT,
    no_model_input_source  TEXT,
    executable_args        TEXT NOT NULL,
    human_rendering        TEXT NOT NULL DEFAULT '{}',
    prepared_action_digest TEXT NOT NULL,
    recovery_mode          TEXT NOT NULL DEFAULT 'non_replayable'
                           CHECK (recovery_mode IN (
                               'non_replayable',
                               'sink_idempotency_key',
                               'read_after_write'
                           )),
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, action_instance_id),
    CHECK (
        (provenance_kind = 'model'
         AND model_snapshot_id IS NOT NULL
         AND model_call_id <> ''
         AND no_model_input_source IS NULL)
     OR (provenance_kind = 'no_model_input'
         AND no_model_input_source IS NOT NULL
         AND model_call_id = ''
         AND model_snapshot_id IS NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS trg_prepared_actions_no_update
BEFORE UPDATE ON prepared_actions
BEGIN
    SELECT RAISE(
        ABORT,
        'PreparedActionImmutable: prepared_actions is write-once'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_prepared_actions_no_delete
BEFORE DELETE ON prepared_actions
BEGIN
    SELECT RAISE(
        ABORT,
        'PreparedActionImmutable: prepared_actions is write-once'
    );
END;
CREATE INDEX IF NOT EXISTS idx_prepared_actions_tenant_digest
    ON prepared_actions(tenant_id, prepared_action_digest);

-- U6-0 T9/T10 G4a/G5a-3 (OP-2634/OP-2637/OP-2638/OP-2642): dormant grants.
-- PostgreSQL's alembic 0264/0267/0268/0270 schema is authoritative; the
-- SQLite subset keeps the UNIQUE keys, CHECKs, composite FKs, and state-only
-- post-issue mutation rule.
CREATE TABLE IF NOT EXISTS challenges (
    challenge_id             TEXT PRIMARY KEY,
    tenant_id                TEXT NOT NULL REFERENCES tenants(id),
    action_instance_id       TEXT NOT NULL,
    principal_type           TEXT NOT NULL,
    actor_id                 TEXT NOT NULL,
    request_id               TEXT NOT NULL DEFAULT '',
    model_call_id            TEXT NOT NULL DEFAULT '',
    adapter_namespace        TEXT NOT NULL,
    tool_name                TEXT NOT NULL,
    schema_version           TEXT NOT NULL,
    family                   TEXT NOT NULL,
    canonical_target         TEXT NOT NULL DEFAULT '',
    args_hash                TEXT NOT NULL,
    provenance_kind          TEXT NOT NULL
                             CHECK (provenance_kind IN ('model', 'no_model_input')),
    model_snapshot_id        TEXT,
    no_model_input_source    TEXT,
    prepared_action_digest   TEXT NOT NULL,
    confirmer_actor          TEXT,
    confirmer_principal_type TEXT,
    confirmed_at             TEXT,
    confirmer_auth_event_id  TEXT,
    confirm_reason           TEXT,
    state                    TEXT NOT NULL DEFAULT 'pending'
                             CHECK (state IN ('pending', 'confirmed', 'rejected', 'expired')),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at               TEXT NOT NULL,
    UNIQUE (tenant_id, action_instance_id),
    UNIQUE (tenant_id, challenge_id),
    CONSTRAINT uq_challenges_tenant_challenge_instance
        UNIQUE (tenant_id, challenge_id, action_instance_id),
    FOREIGN KEY (tenant_id, action_instance_id)
        REFERENCES prepared_actions (tenant_id, action_instance_id),
    CHECK (
        (provenance_kind = 'model'
         AND model_snapshot_id IS NOT NULL
         AND model_call_id <> ''
         AND no_model_input_source IS NULL)
     OR (provenance_kind = 'no_model_input'
         AND no_model_input_source IS NOT NULL
         AND model_call_id = ''
         AND model_snapshot_id IS NULL)
    ),
    CONSTRAINT ck_challenges_decided_has_confirmer CHECK (
        state NOT IN ('confirmed', 'rejected')
        OR (confirmer_actor IS NOT NULL
            AND confirmer_principal_type IS NOT NULL
            AND confirmed_at IS NOT NULL
            AND confirmer_auth_event_id IS NOT NULL
            AND confirm_reason IS NOT NULL
            AND length(confirm_reason) > 0)
    ),
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_challenges_tenant_state
    ON challenges(tenant_id, state);

CREATE TABLE IF NOT EXISTS action_grants (
    grant_id                TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL REFERENCES tenants(id),
    challenge_id            TEXT NOT NULL,
    action_instance_id      TEXT NOT NULL,
    principal_type          TEXT NOT NULL,
    actor_id                TEXT NOT NULL,
    request_id              TEXT NOT NULL DEFAULT '',
    model_call_id           TEXT NOT NULL DEFAULT '',
    adapter_namespace       TEXT NOT NULL,
    tool_name               TEXT NOT NULL,
    schema_version          TEXT NOT NULL,
    family                  TEXT NOT NULL,
    canonical_target        TEXT NOT NULL DEFAULT '',
    args_hash               TEXT NOT NULL,
    provenance_kind         TEXT NOT NULL
                            CHECK (provenance_kind IN ('model', 'no_model_input')),
    model_snapshot_id       TEXT,
    no_model_input_source   TEXT,
    prepared_action_digest  TEXT NOT NULL,
    grant_issuer_source     TEXT NOT NULL
                            CHECK (grant_issuer_source IN ('ui_confirm', 'slash_command', 'server_auto_grant')),
    idempotency_key         TEXT,
    recovery_mode           TEXT NOT NULL DEFAULT 'non_replayable'
                            CHECK (recovery_mode IN ('non_replayable', 'sink_idempotency_key', 'read_after_write')),
    state                   TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN (
                                'pending', 'executing', 'consumed', 'expired',
                                'failed', 'manual'
                            )),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at              TEXT NOT NULL,
    UNIQUE (tenant_id, challenge_id),
    UNIQUE (tenant_id, action_instance_id),
    UNIQUE (tenant_id, grant_id),
    CONSTRAINT uq_grants_tenant_grant_instance
        UNIQUE (tenant_id, grant_id, action_instance_id),
    FOREIGN KEY (tenant_id, action_instance_id)
        REFERENCES prepared_actions (tenant_id, action_instance_id),
    FOREIGN KEY (tenant_id, challenge_id)
        REFERENCES challenges (tenant_id, challenge_id),
    CONSTRAINT fk_grants_challenge_instance
        FOREIGN KEY (tenant_id, challenge_id, action_instance_id)
        REFERENCES challenges (tenant_id, challenge_id, action_instance_id),
    FOREIGN KEY (tenant_id, model_snapshot_id)
        REFERENCES provenance_snapshots (tenant_id, snapshot_id),
    CHECK (
        (provenance_kind = 'model'
         AND model_snapshot_id IS NOT NULL
         AND model_call_id <> ''
         AND no_model_input_source IS NULL)
     OR (provenance_kind = 'no_model_input'
         AND no_model_input_source IS NOT NULL
         AND model_call_id = ''
         AND model_snapshot_id IS NULL)
    ),
    CONSTRAINT ck_grants_sink_idempotency CHECK (
        recovery_mode <> 'sink_idempotency_key'
        OR (idempotency_key IS NOT NULL AND length(idempotency_key) > 0)
    ),
    CHECK (expires_at > created_at)
);
CREATE TRIGGER IF NOT EXISTS trg_action_grants_freeze_identity
BEFORE UPDATE ON action_grants
WHEN (
       NEW.grant_id != OLD.grant_id
    OR NEW.tenant_id != OLD.tenant_id
    OR NEW.challenge_id != OLD.challenge_id
    OR NEW.action_instance_id != OLD.action_instance_id
    OR NEW.principal_type != OLD.principal_type
    OR NEW.actor_id != OLD.actor_id
    OR NEW.request_id != OLD.request_id
    OR NEW.model_call_id != OLD.model_call_id
    OR NEW.adapter_namespace != OLD.adapter_namespace
    OR NEW.tool_name != OLD.tool_name
    OR NEW.schema_version != OLD.schema_version
    OR NEW.family != OLD.family
    OR NEW.canonical_target != OLD.canonical_target
    OR NEW.args_hash != OLD.args_hash
    OR NEW.provenance_kind != OLD.provenance_kind
    OR NEW.model_snapshot_id IS NOT OLD.model_snapshot_id
    OR NEW.no_model_input_source IS NOT OLD.no_model_input_source
    OR NEW.prepared_action_digest != OLD.prepared_action_digest
    OR NEW.grant_issuer_source != OLD.grant_issuer_source
    OR NEW.idempotency_key IS NOT OLD.idempotency_key
    OR NEW.recovery_mode != OLD.recovery_mode
    OR NEW.created_at != OLD.created_at
    OR NEW.expires_at != OLD.expires_at
)
BEGIN
    SELECT RAISE(
        ABORT,
        'ActionGrantImmutable: only action_grants.state may change post-issue'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_action_grants_no_delete
BEFORE DELETE ON action_grants
BEGIN
    SELECT RAISE(
        ABORT,
        'ActionGrantImmutable: action_grants rows are not deletable'
    );
END;
CREATE INDEX IF NOT EXISTS idx_grants_tenant_state
    ON action_grants(tenant_id, state);

-- U6-0 T9/T10 G4a/G5a-3 (OP-2635/OP-2638/OP-2642): resume/results.
-- PostgreSQL's alembic 0265/0268/0270 schema is authoritative; the SQLite
-- subset keeps the write-once results, UNIQUE keys, state CHECK, and grant
-- composite FKs.
CREATE TABLE IF NOT EXISTS resume_jobs (
    resume_id          TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL REFERENCES tenants(id),
    grant_id           TEXT NOT NULL,
    action_instance_id TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'queued'
                       CHECK (state IN (
                           'queued', 'claimed', 'done', 'manual', 'failed'
                       )),
    lease_owner        TEXT,
    lease_expires_at   TEXT,
    lease_epoch        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, grant_id),
    UNIQUE (tenant_id, resume_id),
    FOREIGN KEY (tenant_id, grant_id, action_instance_id)
        REFERENCES action_grants (tenant_id, grant_id, action_instance_id)
);
CREATE INDEX IF NOT EXISTS idx_resume_jobs_tenant_state
    ON resume_jobs(tenant_id, state);

CREATE TABLE IF NOT EXISTS execution_results (
    grant_id     TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    result       TEXT NOT NULL,
    ambiguous    INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, grant_id),
    FOREIGN KEY (tenant_id, grant_id)
        REFERENCES action_grants (tenant_id, grant_id)
);
CREATE TRIGGER IF NOT EXISTS trg_execution_results_no_update
BEFORE UPDATE ON execution_results
BEGIN
    SELECT RAISE(
        ABORT,
        'ExecutionResultImmutable: execution_results is write-once'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_execution_results_no_delete
BEFORE DELETE ON execution_results
BEGIN
    SELECT RAISE(
        ABORT,
        'ExecutionResultImmutable: execution_results is write-once'
    );
END;

-- U6-0 T9/T10 G5a-2 (OP-2641): append-only execution attempt observations.
CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id  TEXT PRIMARY KEY,
    grant_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'applied', 'definitely_not_applied', 'unknown'
                )),
    evidence    TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, attempt_id),
    FOREIGN KEY (tenant_id, grant_id)
        REFERENCES action_grants (tenant_id, grant_id)
);
CREATE INDEX IF NOT EXISTS idx_execution_attempts_tenant_grant
    ON execution_attempts(tenant_id, grant_id);

CREATE TABLE IF NOT EXISTS debug_findings (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    finding_type    TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',
    content         TEXT NOT NULL,
    context         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id (alembic 0038). See artifacts above
    -- for the forward-reference rationale.
    project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
    -- BP.B.1 (OP-249, alembic 0241): nullable until guild-aware
    -- writers are updated.
    guild_id        TEXT
);

CREATE TABLE IF NOT EXISTS decision_rules (
    id                  TEXT PRIMARY KEY,
    kind_pattern        TEXT NOT NULL,
    severity            TEXT,
    auto_in_modes       TEXT NOT NULL DEFAULT '[]',
    default_option_id   TEXT,
    priority            INTEGER NOT NULL DEFAULT 100,
    enabled             INTEGER NOT NULL DEFAULT 1,
    note                TEXT NOT NULL DEFAULT '',
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    negative            INTEGER NOT NULL DEFAULT 0,
    undo_count          INTEGER NOT NULL DEFAULT 0,
    tenant_id           TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id (alembic 0038).  TODO row says
    -- "decisions"; the actual table name has always been
    -- decision_rules (matches I1 0012's TABLES_NEEDING_TENANT_ID).
    -- See artifacts above for the forward-reference rationale.
    project_id          TEXT REFERENCES projects(id) ON DELETE SET NULL
);

-- Phase 56: durable workflow checkpointing
CREATE TABLE IF NOT EXISTS workflow_runs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    status          TEXT NOT NULL DEFAULT 'running',
    last_step_id    TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 0,
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id (alembic 0038). See artifacts above
    -- for the forward-reference rationale.
    project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
    -- BP.B.1 (OP-249, alembic 0241): nullable until guild-aware
    -- writers are updated.
    guild_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    output_json     TEXT,
    error           TEXT,
    UNIQUE (run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_run ON workflow_steps(run_id);

-- Phase 53: audit & compliance hash chain
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL DEFAULT 'system',
    action          TEXT NOT NULL,
    entity_kind     TEXT NOT NULL,
    entity_id       TEXT,
    before_json     TEXT NOT NULL DEFAULT '{}',
    after_json      TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT NOT NULL DEFAULT '',
    curr_hash       TEXT NOT NULL,
    session_id      TEXT,
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- BP.B.1 (OP-249, alembic 0241): nullable until guild-aware
    -- writers are updated.
    guild_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_kind, entity_id);

-- Phase 58: decision profiles + auto-decision postmortem log
CREATE TABLE IF NOT EXISTS decision_profiles (
    id                      TEXT PRIMARY KEY,
    threshold_risky         REAL NOT NULL,
    threshold_destructive   REAL NOT NULL,
    auto_critical           INTEGER NOT NULL DEFAULT 0,
    enabled                 INTEGER NOT NULL DEFAULT 0,
    description             TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auto_decision_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         TEXT NOT NULL,
    kind                TEXT NOT NULL,
    severity            TEXT NOT NULL,
    chosen_option       TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    rationale           TEXT NOT NULL DEFAULT '',
    profile_id          TEXT NOT NULL DEFAULT '',
    auto_executed_at    REAL NOT NULL,
    undone_at           REAL,
    undone_by           TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_decision_log_kind ON auto_decision_log(kind);
CREATE INDEX IF NOT EXISTS idx_auto_decision_log_undone ON auto_decision_log(undone_at);

-- Phase 54: users / sessions / GitHub App installations
-- AS.0.3 (alembic 0058): auth_methods TEXT-of-JSON column drives the
-- account-linking takeover-prevention rule.  Default '[]' = no method
-- recorded yet; the helper module backend.account_linking owns the
-- canonical add/remove path.  Existing prod users are backfilled to
-- '["password"]' by alembic 0058 / _migrate ALTER pair.  AS.1 will
-- start writing 'oauth_<provider>' tags after the takeover guard
-- passes.
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'viewer',
    password_hash   TEXT NOT NULL DEFAULT '',
    oidc_provider   TEXT NOT NULL DEFAULT '',
    oidc_subject    TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until    REAL,
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    auth_methods    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_oidc ON users(oidc_provider, oidc_subject);

-- WP.9.1 (alembic 0197): generic share registry.
-- Runtime permalink generation, ACL resolution, expiry cleanup, and
-- redaction enforcement land in WP.9.2-WP.9.6; this table stores the
-- durable share pointer and redaction audit payload.
CREATE TABLE IF NOT EXISTS shareable_objects (
    share_id          TEXT PRIMARY KEY,
    object_kind       TEXT NOT NULL,
    object_id         TEXT NOT NULL,
    tenant_id         TEXT NOT NULL
                            REFERENCES tenants(id) ON DELETE CASCADE,
    owner_user_id     TEXT NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
    visibility        TEXT NOT NULL DEFAULT 'private'
                            CHECK (visibility IN ('private','team','tenant','public')),
    expires_at        TEXT,
    redaction_applied TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shareable_objects_tenant_object
    ON shareable_objects(tenant_id, object_kind, object_id);
CREATE INDEX IF NOT EXISTS idx_shareable_objects_owner_created
    ON shareable_objects(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shareable_objects_expires_at
    ON shareable_objects(expires_at)
    WHERE expires_at IS NOT NULL;

-- Y1 row 1 (#277): N-to-M users <-> tenants. ``users.tenant_id`` is
-- demoted to a "primary / most-recent tenant" cache; this table is
-- the authoritative source of "which tenants can this user act in".
-- Mirrors alembic 0032_user_tenant_memberships.py — keep in sync.
CREATE TABLE IF NOT EXISTS user_tenant_memberships (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_active_at  TEXT,
    PRIMARY KEY (user_id, tenant_id),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    CHECK (status IN ('active', 'suspended'))
);
CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_user
    ON user_tenant_memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_tenant
    ON user_tenant_memberships(tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_active
    ON user_tenant_memberships(tenant_id, user_id)
    WHERE status = 'active';

-- Y1 row 2 (#277): project layer between tenants and per-workload
-- business tables. plan_override / disk_budget_bytes / llm_budget_tokens
-- are NULL ⇒ inherit from the parent tenant. parent_id self-FK uses
-- ON DELETE SET NULL so a deleted parent promotes its children to
-- top-level rather than cascading away their attached workloads.
-- Mirrors alembic 0033_projects.py — keep in sync.
CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    product_line        TEXT NOT NULL DEFAULT 'default',
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL,
    parent_id           TEXT REFERENCES projects(id) ON DELETE SET NULL,
    plan_override       TEXT,
    disk_budget_bytes   INTEGER,
    llm_budget_tokens   INTEGER,
    created_by          TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at         TEXT,
    UNIQUE (tenant_id, product_line, slug),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK (
        plan_override IS NULL
        OR plan_override IN ('free', 'starter', 'pro', 'enterprise')
    ),
    CHECK (disk_budget_bytes IS NULL OR disk_budget_bytes >= 0),
    CHECK (llm_budget_tokens IS NULL OR llm_budget_tokens >= 0),
    CHECK (length(name) >= 1 AND length(name) <= 200),
    CHECK (length(slug) >= 1 AND length(slug) <= 64),
    CHECK (length(product_line) >= 1 AND length(product_line) <= 64)
);
CREATE INDEX IF NOT EXISTS idx_projects_parent
    ON projects(parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projects_tenant_active
    ON projects(tenant_id)
    WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_projects_created_by
    ON projects(created_by)
    WHERE created_by IS NOT NULL;

-- Y1 row 3 (#277): per-project explicit role bindings. Missing row ⇒
-- tenant-level role acts as default (resolver lives in Y3 application
-- code: tenant admin defaults to 'contributor' on every project, etc).
-- Roles deliberately distinct from user_tenant_memberships
-- (owner/admin/member/viewer) so tenant-admin and project-contributor
-- are not confused at the schema layer.
-- Mirrors alembic 0034_project_members.py — keep in sync.
CREATE TABLE IF NOT EXISTS project_members (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'viewer',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, project_id),
    CHECK (role IN ('owner', 'contributor', 'viewer'))
);
CREATE INDEX IF NOT EXISTS idx_project_members_project
    ON project_members(project_id);

-- Y1 row 4 (#277): email-keyed tenant invites. ``token_hash`` stores
-- only the hash of the random plaintext token; the plaintext is
-- returned ONCE in the create-invite API response and never persisted
-- (same pattern as api_keys.key_hash / mfa_backup_codes.code_hash).
-- Role enum matches user_tenant_memberships (the tenant-level enum)
-- because acceptance materialises a membership row with this role.
-- Mirrors alembic 0035_tenant_invites.py — keep in sync.
CREATE TABLE IF NOT EXISTS tenant_invites (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    invited_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    token_hash  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (token_hash),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    CHECK (length(email) >= 1 AND length(email) <= 320),
    CHECK (length(token_hash) >= 16)
);
CREATE INDEX IF NOT EXISTS idx_tenant_invites_tenant_status
    ON tenant_invites(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_invites_email_status
    ON tenant_invites(email, status);
CREATE INDEX IF NOT EXISTS idx_tenant_invites_expiry_sweep
    ON tenant_invites(expires_at)
    WHERE status = 'pending';

-- Y1 row 5 (#277): cross-tenant project share. Owning tenant A grants
-- guest tenant B project-level access (viewer / contributor — never
-- owner; cross-tenant ownership is not a sane operation). ``expires_at``
-- NULL ⇒ permanent share. ``granted_by`` SET NULL on user delete so
-- the share survives admin rotation. UNIQUE (project_id,
-- guest_tenant_id) — at most one share per (project, guest tenant) pair.
-- Mirrors alembic 0036_project_shares.py — keep in sync.
CREATE TABLE IF NOT EXISTS project_shares (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    guest_tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'viewer',
    granted_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT,
    UNIQUE (project_id, guest_tenant_id),
    CHECK (role IN ('viewer', 'contributor'))
);
CREATE INDEX IF NOT EXISTS idx_project_shares_guest_tenant
    ON project_shares(guest_tenant_id);
CREATE INDEX IF NOT EXISTS idx_project_shares_expiry_sweep
    ON project_shares(expires_at)
    WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    token               TEXT PRIMARY KEY,
    -- FX.11.2 (alembic 0189): ``token`` now stores the KS-envelope
    -- packed JSON ``{"ciphertext", "dek_ref"}`` for plaintext at
    -- rest; cookie-keyed lookups hit ``token_lookup_index``
    -- (sha256-hex of the cookie plaintext). Mirrored into _SCHEMA
    -- here so fresh dev SQLite DBs match the post-migration prod
    -- shape — the auth.py runtime now always populates both columns.
    token_lookup_index  TEXT,
    user_id             TEXT NOT NULL,
    csrf_token          TEXT NOT NULL,
    created_at          REAL NOT NULL,
    expires_at          REAL NOT NULL,
    last_seen_at        REAL NOT NULL,
    ip                  TEXT NOT NULL DEFAULT '',
    user_agent          TEXT NOT NULL DEFAULT '',
    ua_hash             TEXT NOT NULL DEFAULT '',
    metadata            TEXT NOT NULL DEFAULT '{}',
    mfa_verified        INTEGER NOT NULL DEFAULT 0,
    rotated_from        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_token_lookup_index
    ON sessions(token_lookup_index);

CREATE TABLE IF NOT EXISTS user_mfa (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    method          TEXT NOT NULL,  -- 'totp' or 'webauthn'
    secret          TEXT NOT NULL DEFAULT '',
    credential      TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL DEFAULT '',
    verified        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used       TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_mfa_user ON user_mfa(user_id);

CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    code_hash       TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    used_at         TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mfa_backup_user ON mfa_backup_codes(user_id);

-- K7: password history for reuse prevention
CREATE TABLE IF NOT EXISTS password_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_password_history_user ON password_history(user_id);

CREATE TABLE IF NOT EXISTS github_installations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id     INTEGER NOT NULL UNIQUE,
    account_login       TEXT NOT NULL,
    account_type        TEXT NOT NULL DEFAULT 'User',
    target_type         TEXT NOT NULL DEFAULT 'Repository',
    repos_json          TEXT NOT NULL DEFAULT '[]',
    permissions_json    TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    suspended_at        TEXT
);

-- Phase 63-C: Prompt registry. Each row is a versioned snapshot of an
-- agent system prompt under backend/agents/prompts/. At most one row per
-- `path` may have role='active'; canary candidates use role='canary';
-- retired versions stay as role='archive' for rollback.
CREATE TABLE IF NOT EXISTS prompt_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    role            TEXT NOT NULL DEFAULT 'archive',  -- active | canary | archive
    body            TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    promoted_at     REAL,
    rolled_back_at  REAL,
    rollback_reason TEXT,
    UNIQUE(path, version)
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_path_role
    ON prompt_versions(path, role);

-- Phase 56-DAG-B: DAG plan storage. One row per submitted DAG; the
-- mutation chain (planner → validator fail → orchestrator regenerate
-- → planner again) creates additional rows linked via mutation_round
-- and parent_plan_id. Status transitions:
--   pending → validated → executing → completed
--                                  → mutated  (parent of next plan)
--                                  → exhausted (mutation budget hit)
CREATE TABLE IF NOT EXISTS dag_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_id          TEXT NOT NULL,
    run_id          TEXT,
    parent_plan_id  INTEGER,
    json_body       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    mutation_round  INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dag_plans_dag_id ON dag_plans(dag_id);
CREATE INDEX IF NOT EXISTS idx_dag_plans_run_id ON dag_plans(run_id);
CREATE INDEX IF NOT EXISTS idx_dag_plans_status ON dag_plans(status);

-- Phase 63-D D3: per-night IQ benchmark results. One row per (run, model,
-- benchmark). `truncated_at_question` non-null when the token budget cap
-- aborted the run early — aggregator uses this to downweight the row.
CREATE TABLE IF NOT EXISTS iq_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    model           TEXT NOT NULL,
    benchmark       TEXT NOT NULL,
    weighted_score  REAL NOT NULL,
    pass_count      INTEGER NOT NULL,
    total_count     INTEGER NOT NULL,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    truncated_at_question TEXT
);
CREATE INDEX IF NOT EXISTS idx_iq_runs_model_ts ON iq_runs(model, ts);
CREATE INDEX IF NOT EXISTS idx_iq_runs_ts ON iq_runs(ts);

-- B7 (#207): project_run aggregation — groups workflow_runs into a
-- logical "project run" so the UI can show a parent row with summary
-- stats and expand to reveal the individual workflow_runs.
CREATE TABLE IF NOT EXISTS project_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    workflow_run_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_project_runs_project ON project_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_project_runs_created ON project_runs(created_at);

-- K6: Per-key bearer tokens replacing single OMNISIGHT_DECISION_BEARER env.
CREATE TABLE IF NOT EXISTS api_keys (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL,
    key_lookup_index TEXT,
    key_prefix      TEXT NOT NULL DEFAULT '',
    scopes          TEXT NOT NULL DEFAULT '["*"]',
    created_by      TEXT NOT NULL DEFAULT '',
    last_used_ip    TEXT,
    last_used_at    REAL,
    expires_at      REAL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_lookup
    ON api_keys(key_lookup_index);

-- J4: user preferences (per-user key/value)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pref_key    TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    tenant_id   TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    -- Y1 row 7 (#277): project_id (alembic 0038). NULLable; the
    -- composite PK is (user_id, pref_key) so adding project_id is
    -- independent of the PK structure.
    project_id  TEXT REFERENCES projects(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, pref_key)
);
CREATE INDEX IF NOT EXISTS idx_user_prefs_user ON user_preferences(user_id);
-- idx_user_prefs_tenant: created in _migrate() after ADD COLUMN tenant_id
-- (existing DBs may have user_preferences without tenant_id column).

-- I4: Tenant-scoped secrets (git_credentials, provider_keys, cloudflare_tokens…)
CREATE TABLE IF NOT EXISTS tenant_secrets (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
    secret_type     TEXT NOT NULL,  -- git_credential | provider_key | cloudflare_token | webhook_secret | custom
    key_name        TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, secret_type, key_name)
);
CREATE INDEX IF NOT EXISTS idx_tenant_secrets_tenant ON tenant_secrets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_secrets_type ON tenant_secrets(tenant_id, secret_type);

-- M6: per-tenant egress allowlist (one row per tenant)
CREATE TABLE IF NOT EXISTS tenant_egress_policies (
    tenant_id       TEXT PRIMARY KEY REFERENCES tenants(id),
    allowed_hosts   TEXT NOT NULL DEFAULT '[]',
    allowed_cidrs   TEXT NOT NULL DEFAULT '[]',
    default_action  TEXT NOT NULL DEFAULT 'deny',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      TEXT NOT NULL DEFAULT 'system'
);

-- M6: pending operator/viewer requests awaiting admin approval
CREATE TABLE IF NOT EXISTS tenant_egress_requests (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id),
    requested_by    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL,
    justification   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_by      TEXT,
    decided_at      TEXT,
    decision_note   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_egress_req_tenant ON tenant_egress_requests(tenant_id);
CREATE INDEX IF NOT EXISTS idx_egress_req_status ON tenant_egress_requests(status);

-- I1: tenant_id indexes on business tables are created in _migrate()
-- (after ALTER TABLE ADD COLUMN tenant_id), NOT here in _SCHEMA.
-- Placing them here would fail on existing DBs where the old tables
-- lack the tenant_id column. See _migrate() L166-177.

-- L1: bootstrap wizard step audit + finalize anchor
-- Each row is one wizard step recorded as completed. `step` is the
-- stable logical name (admin_password_set / llm_provider_configured /
-- cf_tunnel_configured / smoke_passed / finalized / vertical_setup);
-- `actor_user_id` is the admin who advanced the wizard; `metadata`
-- carries per-step context (e.g. selected provider, tunnel id, the
-- BS.9.1 ``verticals_selected`` payload). Upsert-by-step keeps the
-- table idempotent so replaying a step refreshes its timestamp
-- rather than piling up duplicate rows.
-- BS.9.1 / alembic 0054: PG promotes ``metadata`` to JSONB to allow
-- containment queries against ``metadata->'verticals_selected'``;
-- SQLite stays TEXT-of-JSON (no native JSONB; python-layer parsing
-- via :func:`backend.bootstrap._deserialise_metadata` is identical).
CREATE TABLE IF NOT EXISTS bootstrap_state (
    step            TEXT PRIMARY KEY,
    completed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    actor_user_id   TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}'
);

-- Q.3-SUB-6 (#297): per-user durable chat history. Replaces the
-- pre-Q.3 module-global ``_history: list`` in backend/routers/chat.py
-- which was per-worker, cleared on restart, and invisible across
-- ``uvicorn --workers N``. See alembic 0021 for the PG mirror + the
-- audit reference (docs/design/multi-device-state-sync.md Path 5).
CREATE TABLE IF NOT EXISTS chat_messages (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 't-default'
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_ts
    ON chat_messages(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp
    ON chat_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant
    ON chat_messages(tenant_id);

-- ZZ.B2 #304-2 checkbox 1: per-user chat-session metadata. One row per
-- (session_id, user_id, tenant_id). Stores the LLM-generated auto_title
-- and optional user_title inside ``metadata``. Hydrated on every chat
-- write via ``upsert_chat_session``; the 3-user-turn trigger in
-- ``backend.routers.chat`` fills ``metadata.auto_title`` once and emits
-- SSE ``session.titled`` so the sidebar re-labels without a refetch.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 't-default',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (session_id, user_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
    ON chat_sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant
    ON chat_sessions(tenant_id);

-- Q.6 #300 (alembic 0022): per-user draft slots. Backs the 500 ms
-- debounce write from the INVOKE command bar and the workspace chat
-- composer so an accidental refresh / device switch does not lose
-- in-flight typing. PK is (user_id, slot_key); PUT is upsert. The
-- 24 h GC sweep (Q.6 checkbox 3) prunes by ``updated_at``.
CREATE TABLE IF NOT EXISTS user_drafts (
    user_id     TEXT NOT NULL,
    slot_key    TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 't-default',
    PRIMARY KEY (user_id, slot_key)
);
CREATE INDEX IF NOT EXISTS idx_user_drafts_updated_at
    ON user_drafts(updated_at);
CREATE INDEX IF NOT EXISTS idx_user_drafts_tenant
    ON user_drafts(tenant_id);

-- Phase 5-1 (#multi-account-forge): one row per forge account.
-- Replaces the legacy ``Settings.{github,gitlab}_token{,_map}`` JSON
-- blobs that could not represent multiple accounts on the same host.
-- See docs/phase-5-multi-account/01-design.md for the full rationale
-- and alembic 0027 for the PG mirror. The SQLite schema below is the
-- dialect-shifted dev-parity version (JSONB → TEXT-of-JSON, BOOLEAN
-- → INTEGER 0/1, DOUBLE PRECISION → REAL).
CREATE TABLE IF NOT EXISTS git_accounts (
    id                       TEXT PRIMARY KEY,
    tenant_id                TEXT NOT NULL DEFAULT 't-default'
                                    REFERENCES tenants(id) ON DELETE CASCADE,
    platform                 TEXT NOT NULL
                                    CHECK (platform IN ('github','gitlab','gerrit','jira')),
    instance_url             TEXT NOT NULL DEFAULT '',
    label                    TEXT NOT NULL DEFAULT '',
    username                 TEXT NOT NULL DEFAULT '',
    encrypted_token          TEXT NOT NULL DEFAULT '',
    encrypted_ssh_key        TEXT NOT NULL DEFAULT '',
    ssh_host                 TEXT NOT NULL DEFAULT '',
    ssh_port                 INTEGER NOT NULL DEFAULT 0,
    project                  TEXT NOT NULL DEFAULT '',
    encrypted_webhook_secret TEXT NOT NULL DEFAULT '',
    url_patterns             TEXT NOT NULL DEFAULT '[]',
    auth_type                TEXT NOT NULL DEFAULT 'pat',
    is_default               INTEGER NOT NULL DEFAULT 0,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    metadata                 TEXT NOT NULL DEFAULT '{}',
    last_used_at             REAL,
    created_at               REAL NOT NULL,
    updated_at               REAL NOT NULL,
    version                  INTEGER NOT NULL DEFAULT 0,
    -- Phase 5-12 (#multi-account-forge, alembic 0028): OAuth prep.
    -- JSONB on PG; TEXT-of-JSON here for dev parity. Reserved for
    -- future PKCE ``code_verifier`` + refresh-token metadata;
    -- unread + unwritten by PAT-only MVP.
    code_verifier            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant
    ON git_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant_platform
    ON git_accounts(tenant_id, platform);
CREATE INDEX IF NOT EXISTS idx_git_accounts_last_used
    ON git_accounts(tenant_id, last_used_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_git_accounts_default_per_platform
    ON git_accounts(tenant_id, platform)
    WHERE is_default = 1;

-- Phase 5b-1 (#llm-credentials): one row per LLM-provider credential.
-- Replaces the legacy in-memory-only ``Settings.{provider}_api_key``
-- scalar fields so operator-rotated keys survive ``docker compose
-- restart`` and rolling redeploys. See docs/phase-5b-llm-credentials/
-- 01-design.md for the full rationale and alembic 0029 for the PG
-- mirror. SQLite schema below is the dialect-shifted dev-parity
-- version (JSONB → TEXT-of-JSON, BOOLEAN → INTEGER 0/1, DOUBLE
-- PRECISION → REAL). Empty until rows 5b-2 / 5b-5 land.
CREATE TABLE IF NOT EXISTS llm_credentials (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 't-default'
                            REFERENCES tenants(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL
                            CHECK (provider IN (
                                'anthropic','google','openai','xai',
                                'groq','deepseek','together',
                                'openrouter','ollama'
                            )),
    label             TEXT NOT NULL DEFAULT '',
    encrypted_value   TEXT NOT NULL DEFAULT '',
    metadata          TEXT NOT NULL DEFAULT '{}',
    auth_type         TEXT NOT NULL DEFAULT 'pat',
    is_default        INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_used_at      REAL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant
    ON llm_credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant_provider
    ON llm_credentials(tenant_id, provider);
CREATE INDEX IF NOT EXISTS idx_llm_credentials_last_used
    ON llm_credentials(tenant_id, last_used_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_credentials_default_per_provider
    ON llm_credentials(tenant_id, provider)
    WHERE is_default = 1;

-- H4a row 2582: last-known-good AIMD budget for cold-start carry-over.
-- Singleton row keyed by id='global'; upserted on every budget-
-- changing tick so a ``uvicorn`` restart can re-seed the AIMD
-- controller with the previous run's calibration instead of the
-- static ``OMNISIGHT_AIMD_INIT_BUDGET=6`` default. See alembic 0030
-- for the PG mirror and backend/adaptive_budget.py for the load/save
-- hooks.
CREATE TABLE IF NOT EXISTS adaptive_budget_state (
    id           TEXT PRIMARY KEY,
    budget       INTEGER NOT NULL,
    last_reason  TEXT NOT NULL DEFAULT 'init',
    updated_at   REAL NOT NULL
);

-- Y9 #285 row 3 (alembic 0039): per-(tenant_id, project_id) billing
-- usage fact table. Append-only; emitter writes one row per LLM call
-- / workflow_run completion / workspace-GC sweep. PG side has BIGINT
-- IDENTITY id; SQLite uses ``INTEGER PRIMARY KEY AUTOINCREMENT`` (the
-- rowid alias) so a fresh dev DB matches the alembic-PG shape and the
-- migrator drift guard in ``test_migrator_schema_coverage`` stays
-- green. CHECK on ``kind`` mirrors the alembic CHECK constraint and is
-- pinned by ``test_billing_usage_y9_row3.test_kind_check_constraint_
-- matches_module_constants``. Mirror added BS.1.4 (#TBD) — fixing the
-- pre-existing Y9-row-3 oversight in the same row that lands the
-- catalog tables below.
CREATE TABLE IF NOT EXISTS billing_usage_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at         REAL NOT NULL,
    tenant_id           TEXT NOT NULL DEFAULT 't-default',
    project_id          TEXT NOT NULL DEFAULT 'p-default-default',
    kind                TEXT NOT NULL,
    model               TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_create_tokens INTEGER,
    workflow_run_id     TEXT,
    workflow_kind       TEXT,
    workflow_status     TEXT,
    cost_usd            REAL NOT NULL DEFAULT 0.0,
    quantity            REAL NOT NULL DEFAULT 1.0,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    CHECK (kind IN ('llm_call', 'workflow_run', 'workspace_gb_hour'))
);
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_tenant_project_time
    ON billing_usage_events(tenant_id, project_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_tenant_kind_time
    ON billing_usage_events(tenant_id, kind, occurred_at);

-- BS.1.1 (alembic 0051): three-source catalog of installable platforms
-- (``shipped`` / ``operator`` / ``override`` / ``subscription``).
-- Resolver picks ``override > operator > shipped`` per (id, tenant);
-- ``id`` is shared across the source layers by design — this table
-- has NO single TEXT PK, uniqueness comes from the partial UNIQUE
-- index ``uq_catalog_entries_visible`` below. The dialect-shifted
-- SQLite mirror follows the alembic 0027 / 0029 / 0051 pattern:
-- JSONB → TEXT-of-JSON, TIMESTAMPTZ → REAL (epoch seconds via
-- strftime), BOOLEAN → INTEGER 0/1. Hidden tombstone rows are
-- allowed by the partial-unique exclusion (``WHERE hidden = 0``) so
-- soft-retiring an operator/override row keeps audit history
-- without colliding with its replacement. CHECK constraints lock
-- the source / family / install_method enums to the same closed
-- sets the alembic CHECKs use.
CREATE TABLE IF NOT EXISTS catalog_entries (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    tenant_id       TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL,
    family          TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    version         TEXT NOT NULL,
    install_method  TEXT NOT NULL,
    install_url     TEXT,
    sha256          TEXT,
    size_bytes      INTEGER,
    depends_on      TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{}',
    hidden          INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (source IN ('shipped','operator','override','subscription')),
    CHECK (family IN ('mobile','embedded','web','software',
                      'rtos','cross-toolchain','custom')),
    CHECK (install_method IN ('noop','docker_pull',
                              'shell_script','vendor_installer')),
    CHECK (hidden IN (0, 1)),
    CHECK (
        (source = 'shipped'  AND tenant_id IS NULL)
        OR
        (source IN ('operator','override','subscription')
            AND tenant_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_entries_visible
    ON catalog_entries(id, source, COALESCE(tenant_id, ''))
    WHERE hidden = 0;
CREATE INDEX IF NOT EXISTS idx_catalog_entries_family
    ON catalog_entries(family);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_tenant
    ON catalog_entries(tenant_id)
    WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_catalog_entries_source
    ON catalog_entries(source);

-- BS.1.1 (alembic 0051): one row per install attempt. The
-- ``omnisight-installer`` sidecar long-poll-claims rows via
-- ``SELECT … FOR UPDATE SKIP LOCKED`` on PG; SQLite dev path doesn't
-- have skip-locked but the partial idx on ``state IN ('queued',
-- 'running')`` keeps the claimable set tight. ``idempotency_key``
-- is UNIQUE for ``INSERT … ON CONFLICT DO NOTHING`` double-click
-- protection (R27). State machine ``queued → running →
-- {completed | failed | cancelled}`` enforced by the CHECK below.
CREATE TABLE IF NOT EXISTS install_jobs (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 't-default'
                            REFERENCES tenants(id) ON DELETE CASCADE,
    entry_id          TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'queued',
    idempotency_key   TEXT NOT NULL,
    sidecar_id        TEXT,
    protocol_version  INTEGER NOT NULL DEFAULT 1,
    bytes_done        INTEGER NOT NULL DEFAULT 0,
    bytes_total       INTEGER,
    eta_seconds       INTEGER,
    log_tail          TEXT NOT NULL DEFAULT '',
    result_json       TEXT,
    error_reason      TEXT,
    pep_decision_id   TEXT,
    requested_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    queued_at         REAL NOT NULL DEFAULT (strftime('%s','now')),
    claimed_at        REAL,
    started_at        REAL,
    completed_at      REAL,
    UNIQUE (idempotency_key),
    CHECK (state IN ('queued','running','completed','failed','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_install_jobs_state_queued
    ON install_jobs(state, queued_at)
    WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_install_jobs_tenant_queued
    ON install_jobs(tenant_id, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_install_jobs_sidecar
    ON install_jobs(sidecar_id, state)
    WHERE sidecar_id IS NOT NULL;

-- BS.1.1 (alembic 0051): per-tenant URL feed of third-party catalogs
-- (BS.8.5). ``auth_secret_ref`` is a key into the existing tenant
-- secret store, never the secret value itself. UNIQUE (tenant_id,
-- feed_url) so a tenant can't subscribe to the same feed twice.
-- Empty until BS.8.5 admin REST starts inserting.
CREATE TABLE IF NOT EXISTS catalog_subscriptions (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 't-default'
                              REFERENCES tenants(id) ON DELETE CASCADE,
    feed_url            TEXT NOT NULL,
    auth_method         TEXT NOT NULL DEFAULT 'none',
    auth_secret_ref     TEXT,
    refresh_interval_s  INTEGER NOT NULL DEFAULT 86400,
    last_synced_at      REAL,
    last_sync_status    TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at          REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE (tenant_id, feed_url),
    CHECK (auth_method IN ('none','basic','bearer','signed_url')),
    CHECK (enabled IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_catalog_subscriptions_due
    ON catalog_subscriptions(last_synced_at, refresh_interval_s)
    WHERE enabled = 1;

-- AS.2.2 (alembic 0057): per-(user, provider) OAuth credential vault.
-- Columns mirror alembic 0057's CREATE TABLE; see that file's docstring
-- for the column-by-column rationale.  Composite PK ``(user_id, provider)``
-- enforces the "one binding per user per provider" invariant at the
-- database layer.  ``access_token_enc`` / ``refresh_token_enc`` round-trip
-- through ``backend.security.token_vault`` (Fernet ciphertext, urlsafe-b64
-- ASCII).  ``key_version`` reserved for the AS.0.4 §3.1 KMS rotation
-- roadmap; today every row is written and read at
-- ``token_vault.KEY_VERSION_CURRENT = 1``.  Provider CHECK clause MUST
-- byte-equal ``token_vault.SUPPORTED_PROVIDERS`` and
-- ``account_linking._AS1_OAUTH_PROVIDERS``; the cross-module drift
-- guard tests fail red when the three diverge.  Empty until AS.6.1
-- OAuth router starts writing rows.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    user_id            TEXT NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL
                            CHECK (provider IN ('apple','bitbucket','discord','github','gitlab','google','hubspot','microsoft','notion','salesforce','slack')),
    access_token_enc   TEXT NOT NULL DEFAULT '',
    refresh_token_enc  TEXT NOT NULL DEFAULT '',
    expires_at         REAL,
    scope              TEXT NOT NULL DEFAULT '',
    key_version        INTEGER NOT NULL DEFAULT 1,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    version            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires
    ON oauth_tokens(provider, expires_at);

-- W14.10 (alembic 0059): per-launch audit log for the
-- ``omnisight-web-preview`` sidecar.  Holds the cross-worker
-- preview state that survives uvicorn worker restart and lets the
-- W14.5 idle reaper see sandboxes launched by sibling workers.
-- Columns mirror alembic 0059's CREATE TABLE; see that file's
-- docstring for the column-by-column rationale.  ``status`` CHECK
-- byte-equals :class:`backend.web_sandbox.WebSandboxStatus`.value;
-- ``killed_reason`` is free-form by design (literals owned by
-- multiple sibling rows W14.5 / W14.9).  Partial UNIQUE on
-- ``workspace_id`` WHERE status NOT IN ('stopped','failed')
-- enforces "at most one live sidecar per workspace" across the
-- worker fleet — that's the cross-worker invariant W14.10 exists
-- to deliver.  Empty until the WebSandboxManager swap to PG-backed
-- reads lands (separate row).
CREATE TABLE IF NOT EXISTS web_sandbox_instances (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id        TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    started_at        REAL NOT NULL,
    ingress_url       TEXT,
    status            TEXT NOT NULL
                            CHECK (status IN ('failed','installing','pending',
                                              'running','stopped','stopping')),
    last_request_at   REAL NOT NULL,
    killed_at         REAL,
    killed_reason     TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_web_sandbox_instances_workspace_live
    ON web_sandbox_instances (workspace_id)
    WHERE status NOT IN ('stopped', 'failed');
CREATE INDEX IF NOT EXISTS idx_web_sandbox_instances_idle
    ON web_sandbox_instances (status, last_request_at);
CREATE INDEX IF NOT EXISTS idx_web_sandbox_instances_sandbox
    ON web_sandbox_instances (sandbox_id);

-- FS.1.3 (alembic 0061): tenant-owned DB provisioning registry.
-- The encrypted connection URL is stored as ciphertext only; plaintext
-- DSNs stay in transient adapter / migration-runner memory.  Composite
-- PK ``(tenant_id, provider)`` mirrors alembic 0061 and keeps the table
-- within the five-column TODO surface while allowing one recorded DB per
-- tenant/provider pair.
CREATE TABLE IF NOT EXISTS provisioned_databases (
    tenant_id          TEXT NOT NULL
                            REFERENCES tenants(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL
                            CHECK (provider IN ('neon','planetscale','supabase')),
    connection_url_enc TEXT NOT NULL,
    created_at         REAL NOT NULL,
    status             TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- FS.3.2 (alembic 0062): tenant-owned object storage registry.
-- Bucket name is not secret material; credentials stay with provider
-- config / vault callers.  Composite PK ``(tenant_id, provider)``
-- mirrors alembic 0062 and allows one recorded bucket per tenant/provider
-- pair without adding a synthetic id beyond the TODO surface.
CREATE TABLE IF NOT EXISTS provisioned_storage (
    tenant_id   TEXT NOT NULL
                    REFERENCES tenants(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL
                    CHECK (provider IN ('r2','s3','supabase-storage')),
    bucket_name TEXT NOT NULL,
    bucket_lookup_index TEXT,
    created_at  REAL NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- FS.8.3 (alembic 0063): tenant-owned Stripe billing registry.
-- Stores Stripe customer/subscription identifiers and current
-- subscription state only; API keys and webhook secrets stay in env /
-- AS.2 vault paths.  Composite PK ``(tenant_id, provider)`` mirrors
-- alembic 0063 and allows one billing provider record per tenant.
CREATE TABLE IF NOT EXISTS provisioned_billing (
    tenant_id              TEXT NOT NULL
                                REFERENCES tenants(id) ON DELETE CASCADE,
    provider               TEXT NOT NULL
                                CHECK (provider IN ('stripe')),
    stripe_customer_id     TEXT NOT NULL,
    stripe_subscription_id TEXT NOT NULL,
    stripe_price_id        TEXT NOT NULL DEFAULT '',
    status                 TEXT NOT NULL,
    current_period_end     REAL,
    cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,
    created_at             REAL NOT NULL,
    updated_at             REAL NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- SC.10.1 (alembic 0064): data-subject access / erasure /
-- portability workflow queue.  Stores workflow state only; SC.10.2-
-- SC.10.5 own the endpoint payloads, erasure execution, JSON export,
-- and 30-day SLA timer.  TEXT PK avoids sequence-reset work during
-- SQLite -> PG cutover.
CREATE TABLE IF NOT EXISTS dsar_requests (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL
                         REFERENCES users(id) ON DELETE CASCADE,
    request_type  TEXT NOT NULL
                         CHECK (request_type IN ('access','erasure','portability')),
    status        TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('cancelled','completed','failed',
                                           'pending','processing')),
    requested_at  REAL NOT NULL,
    due_at        REAL NOT NULL,
    completed_at  REAL,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    result_json   TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dsar_requests_user_status
    ON dsar_requests(user_id, status);
CREATE INDEX IF NOT EXISTS idx_dsar_requests_tenant_due
    ON dsar_requests(tenant_id, due_at)
    WHERE status IN ('pending', 'processing');

-- SC.11.1 (alembic 0065): compliance evidence bundle queue/catalog.
-- Stores bundle lifecycle metadata only; SC.11.2-SC.11.4 own control
-- mappings, evidence collection, zip export, and signatures.  TEXT PK
-- avoids sequence-reset work during SQLite -> PG cutover.
CREATE TABLE IF NOT EXISTS compliance_evidence_bundles (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT NOT NULL
                                  REFERENCES tenants(id) ON DELETE CASCADE,
    requested_by           TEXT
                                  REFERENCES users(id) ON DELETE SET NULL,
    standard               TEXT NOT NULL
                                  CHECK (standard IN ('iso27001','soc2')),
    status                 TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('cancelled','collecting',
                                                    'completed','failed',
                                                    'pending')),
    requested_at           REAL NOT NULL,
    completed_at           REAL,
    control_mapping_json   TEXT NOT NULL DEFAULT '{}',
    evidence_manifest_json TEXT NOT NULL DEFAULT '{}',
    artifact_uri           TEXT NOT NULL DEFAULT '',
    signature_json         TEXT NOT NULL DEFAULT '{}',
    error                  TEXT NOT NULL DEFAULT '',
    version                INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_tenant_status
    ON compliance_evidence_bundles(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_requested_by
    ON compliance_evidence_bundles(requested_by);

-- KS.1.10 (alembic 0106): envelope-encryption persistence tables.
-- These mirror backend.security.{kms_adapters,envelope,
-- decryption_audit,spend_anomaly} without wiring runtime callers here.
-- Plaintext DEKs and plaintext customer secrets never enter these
-- tables; ``tenant_deks`` stores wrapped DEKs only.
CREATE TABLE IF NOT EXISTS kms_keys (
    key_id        TEXT PRIMARY KEY,
    provider      TEXT NOT NULL
                       CHECK (provider IN ('aws-kms','gcp-kms',
                                           'local-fernet','vault-transit')),
    key_version   TEXT NOT NULL DEFAULT '1',
    purpose       TEXT NOT NULL DEFAULT 'tenant-secret',
    status        TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','disabled','destroyed',
                                         'retiring')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    rotated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_kms_keys_provider_status
    ON kms_keys(provider, status);

CREATE TABLE IF NOT EXISTS tenant_deks (
    dek_id                  TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL
                                  REFERENCES tenants(id) ON DELETE CASCADE,
    key_id                  TEXT NOT NULL
                                  REFERENCES kms_keys(key_id) ON DELETE RESTRICT,
    provider                TEXT NOT NULL
                                  CHECK (provider IN ('aws-kms','gcp-kms',
                                                      'local-fernet',
                                                      'vault-transit')),
    wrapped_dek_b64         TEXT NOT NULL,
    key_version             TEXT,
    wrap_algorithm          TEXT NOT NULL DEFAULT '',
    encryption_context_json TEXT NOT NULL DEFAULT '{}',
    purpose                 TEXT NOT NULL DEFAULT 'tenant-secret',
    schema_version          INTEGER NOT NULL DEFAULT 1,
    created_at              REAL NOT NULL,
    rotated_at              REAL,
    revoked_at              REAL
);
CREATE INDEX IF NOT EXISTS idx_tenant_deks_tenant_purpose
    ON tenant_deks(tenant_id, purpose);
CREATE INDEX IF NOT EXISTS idx_tenant_deks_key_version
    ON tenant_deks(key_id, key_version);

CREATE TABLE IF NOT EXISTS decryption_audits (
    audit_id      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    key_id        TEXT NOT NULL,
    dek_id        TEXT,
    request_id    TEXT NOT NULL,
    purpose       TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL
                         CHECK (provider IN ('aws-kms','gcp-kms',
                                             'local-fernet','vault-transit')),
    audit_log_id  INTEGER,
    decrypted_at  REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decryption_audits_tenant_time
    ON decryption_audits(tenant_id, decrypted_at DESC);
CREATE INDEX IF NOT EXISTS idx_decryption_audits_request
    ON decryption_audits(request_id);
CREATE INDEX IF NOT EXISTS idx_decryption_audits_key_time
    ON decryption_audits(key_id, decrypted_at DESC);

CREATE TABLE IF NOT EXISTS spend_thresholds (
    tenant_id           TEXT PRIMARY KEY
                              REFERENCES tenants(id) ON DELETE CASCADE,
    token_rate_limit    INTEGER NOT NULL CHECK (token_rate_limit > 0),
    window_seconds      REAL NOT NULL CHECK (window_seconds > 0),
    throttle_seconds    REAL NOT NULL CHECK (throttle_seconds > 0),
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    alert_channels_json TEXT NOT NULL DEFAULT '[]',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kek_rotations (
    rotation_id      TEXT PRIMARY KEY,
    key_id           TEXT NOT NULL
                           REFERENCES kms_keys(key_id) ON DELETE RESTRICT,
    provider         TEXT NOT NULL
                           CHECK (provider IN ('aws-kms','gcp-kms',
                                               'local-fernet','vault-transit')),
    from_key_version TEXT NOT NULL,
    to_key_version   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'scheduled'
                           CHECK (status IN ('cancelled','completed','failed',
                                             'running','scheduled')),
    scheduled_for    REAL,
    started_at       REAL,
    completed_at     REAL,
    rotated_rows     INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    metadata_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_kek_rotations_status_schedule
    ON kek_rotations(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_kek_rotations_key
    ON kek_rotations(key_id, started_at DESC);

-- KS.2.11 (alembic 0107): Tier 2 CMEK persistence tables.
-- Runtime rows KS.2.1-KS.2.10 stay stateless until these tables are
-- wired by follow-up rows; this schema only makes wizard completions,
-- tier assignments, and revoke events durable.
CREATE TABLE IF NOT EXISTS cmek_configs (
    config_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL
                         CHECK (provider IN ('aws-kms','gcp-kms',
                                             'vault-transit')),
    key_id          TEXT NOT NULL,
    policy_principal TEXT NOT NULL DEFAULT '',
    verification_id TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('active','disabled','draft',
                                           'revoked','verifying')),
    verified_at     REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    disabled_at     REAL,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cmek_configs_tenant_status
    ON cmek_configs(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_cmek_configs_provider_key
    ON cmek_configs(provider, key_id);

CREATE TABLE IF NOT EXISTS tier_assignments (
    tenant_id       TEXT PRIMARY KEY
                         REFERENCES tenants(id) ON DELETE CASCADE,
    security_tier   TEXT NOT NULL DEFAULT 'tier-1'
                         CHECK (security_tier IN ('tier-1','tier-2')),
    cmek_config_id  TEXT
                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','downgrading',
                                           'fallback_to_tier1','revoked',
                                           'upgrading')),
    assigned_by     TEXT NOT NULL DEFAULT '',
    assigned_at     REAL NOT NULL,
    updated_at      REAL NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tier_assignments_security_tier
    ON tier_assignments(security_tier, status);
CREATE INDEX IF NOT EXISTS idx_tier_assignments_cmek_config
    ON tier_assignments(cmek_config_id);

CREATE TABLE IF NOT EXISTS cmek_revoke_events (
    event_id       TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    cmek_config_id TEXT
                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,
    provider       TEXT NOT NULL
                         CHECK (provider IN ('aws-kms','gcp-kms',
                                             'vault-transit')),
    key_id         TEXT NOT NULL,
    reason         TEXT NOT NULL
                         CHECK (reason IN ('describe_failed','key_disabled',
                                           'permission_revoked','restored',
                                           'unknown')),
    raw_state      TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'cmek_revoke_detector',
    detected_at    REAL NOT NULL,
    restored_at    REAL,
    detail_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cmek_revoke_events_tenant_time
    ON cmek_revoke_events(tenant_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_cmek_revoke_events_config_time
    ON cmek_revoke_events(cmek_config_id, detected_at DESC);

-- KS.3.12 (alembic 0108): Tier 3 BYOG proxy persistence tables.
-- These mirror proxy registration, heartbeat, and mTLS certificate
-- metadata only. Prompt / response bodies stay customer-owned inside
-- omnisight-proxy and private key material is never stored here.
CREATE TABLE IF NOT EXISTS proxy_registrations (
    proxy_id         TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL
                           REFERENCES tenants(id) ON DELETE CASCADE,
    display_name     TEXT NOT NULL DEFAULT '',
    proxy_url        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('active','disabled','pending',
                                             'revoked')),
    service          TEXT NOT NULL DEFAULT 'omnisight-proxy',
    provider_count   INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),
    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30
                           CHECK (heartbeat_interval_seconds > 0),
    stale_threshold_seconds INTEGER NOT NULL DEFAULT 60
                           CHECK (stale_threshold_seconds > 0),
    nonce_key_ref    TEXT NOT NULL DEFAULT '',
    client_cert_fingerprint_sha256 TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    disabled_at      REAL,
    metadata_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_registrations_tenant_status
    ON proxy_registrations(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_proxy_registrations_url
    ON proxy_registrations(proxy_url);

CREATE TABLE IF NOT EXISTS proxy_health_checks (
    check_id        TEXT PRIMARY KEY,
    proxy_id        TEXT NOT NULL
                          REFERENCES proxy_registrations(proxy_id)
                          ON DELETE CASCADE,
    tenant_id       TEXT NOT NULL
                          REFERENCES tenants(id) ON DELETE CASCADE,
    status          TEXT NOT NULL
                          CHECK (status IN ('mtls_failed','ok','stale',
                                            'unreachable')),
    service         TEXT NOT NULL DEFAULT 'omnisight-proxy',
    provider_count  INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),
    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30
                          CHECK (heartbeat_interval_seconds > 0),
    latency_ms      REAL,
    error           TEXT NOT NULL DEFAULT '',
    checked_at      REAL NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_health_checks_proxy_time
    ON proxy_health_checks(proxy_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_proxy_health_checks_tenant_status
    ON proxy_health_checks(tenant_id, status);

CREATE TABLE IF NOT EXISTS proxy_mtls_certs (
    cert_id        TEXT PRIMARY KEY,
    proxy_id       TEXT NOT NULL
                         REFERENCES proxy_registrations(proxy_id)
                         ON DELETE CASCADE,
    tenant_id      TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    cert_role      TEXT NOT NULL CHECK (cert_role IN ('ca','client','server')),
    fingerprint_sha256 TEXT NOT NULL,
    subject        TEXT NOT NULL DEFAULT '',
    issuer         TEXT NOT NULL DEFAULT '',
    serial_number  TEXT NOT NULL DEFAULT '',
    not_before     REAL,
    not_after      REAL,
    status         TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','expired','revoked',
                                           'rotating')),
    pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    material_ref   TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    rotated_at     REAL,
    revoked_at     REAL,
    metadata_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_mtls_certs_proxy_status
    ON proxy_mtls_certs(proxy_id, status);
CREATE INDEX IF NOT EXISTS idx_proxy_mtls_certs_fingerprint
    ON proxy_mtls_certs(fingerprint_sha256);

-- KS.4.13 (alembic 0187): durable LLM firewall review events.
-- Stores only suspicious / blocked decisions and input hashes; raw
-- input text is intentionally absent so persistence does not expand
-- the leakage surface.
CREATE TABLE IF NOT EXISTS firewall_events (
    event_id       TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL
                         REFERENCES tenants(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
                         CHECK (classification IN ('blocked','suspicious')),
    input_hash     TEXT NOT NULL,
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_firewall_events_tenant_class_time
    ON firewall_events(tenant_id, classification, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_firewall_events_input_hash
    ON firewall_events(input_hash);

-- OP-877 D5 (alembic 0207): durable audit row per daily auto-promote tick.
-- Schema rationale + cross-table separation from `deploy_audit` documented
-- in `backend/alembic/versions/0207_release_audit.py`.
CREATE TABLE IF NOT EXISTS release_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome         TEXT NOT NULL
                          CHECK (outcome IN ('promoted','noop',
                                             'milestone_not_accepted',
                                             'ff_not_possible',
                                             'push_rejected')),
    fix_version     TEXT,
    develop_sha     TEXT NOT NULL DEFAULT '',
    main_sha        TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_release_audit_ts
    ON release_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_release_audit_outcome_ts
    ON release_audit(outcome, ts DESC);

-- OP-879 D7 (alembic 0223): per-endpoint latency p50/p95/p99 baseline.
-- Schema rationale documented in
-- `backend/alembic/versions/0223_metric_baselines.py`.
CREATE TABLE IF NOT EXISTS metric_baselines (
    endpoint        TEXT PRIMARY KEY,
    p50_ms          REAL NOT NULL CHECK (p50_ms >= 0),
    p95_ms          REAL NOT NULL CHECK (p95_ms >= 0),
    p99_ms          REAL NOT NULL CHECK (p99_ms >= 0),
    sample_count    INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    deployment_tag  TEXT NOT NULL DEFAULT '',
    captured_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_metric_baselines_captured_at
    ON metric_baselines(captured_at DESC);

-- OP-881 D9 (alembic 0224): orchestrator-side bookkeeping ledger for
-- `POST /api/v1/prod/deploy`. One row per release_id (UNIQUE) so the
-- idempotence check is a single indexed lookup. Schema rationale +
-- separation from `deploy_audit` documented in
-- `backend/alembic/versions/0224_prod_deploy_audit.py`.
CREATE TABLE IF NOT EXISTS prod_deploy_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id      TEXT NOT NULL UNIQUE,
    image_tag       TEXT NOT NULL,
    actor           TEXT NOT NULL,
    status          TEXT NOT NULL
                          CHECK (status IN (
                              'running','completed',
                              'aborted_approval_refused',
                              'aborted_smoke_failed',
                              'aborted_timeout',
                              'aborted_error'
                          )),
    last_step       TEXT
                          CHECK (last_step IS NULL OR last_step IN (
                              'approval','image_pull','secrets_decrypt',
                              'smoke_pre_check','blue_green_switch','completed'
                          )),
    started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TEXT,
    elapsed_seconds REAL,
    error_class     TEXT,
    error_message   TEXT,
    context_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_prod_deploy_audit_status_started
    ON prod_deploy_audit(status, started_at DESC);

-- U6-2b (OP-2697): L2 episodic session-summary store (DORMANT — write-only, NOT
-- injected in v1, §2.C). PostgreSQL alembic 0272 is authoritative; this SQLite
-- subset keeps the exactly-once UNIQUE key + the write-once (never in-place
-- mutate) trigger. summary_outcome/source_message_hashes are jsonb on PG, json
-- TEXT on SQLite. Nothing writes this until the U6-8 scheduler wires the writer.
CREATE TABLE IF NOT EXISTS chat_session_summaries (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT NOT NULL REFERENCES tenants(id),
    user_id                TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    source_watermark       TEXT NOT NULL,
    source_message_hashes  TEXT NOT NULL DEFAULT '[]',
    summary_outcome        TEXT NOT NULL DEFAULT '{}',
    token_count            INTEGER NOT NULL DEFAULT 0,
    model_fingerprint      TEXT NOT NULL DEFAULT '',
    classifier_version     INTEGER NOT NULL DEFAULT 0,
    renderer_version       INTEGER NOT NULL DEFAULT 0,
    revision               INTEGER NOT NULL DEFAULT 0,
    session_end_reason     TEXT NOT NULL
                           CHECK (session_end_reason IN ('inactivity_timeout', 'explicit_close')),
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, user_id, session_id, source_watermark)
);
CREATE TRIGGER IF NOT EXISTS trg_chat_session_summaries_no_update
BEFORE UPDATE ON chat_session_summaries
BEGIN
    SELECT RAISE(ABORT, 'ChatSessionSummaryImmutable: write-once; late turns get a new revision');
END;
CREATE INDEX IF NOT EXISTS idx_chat_session_summaries_user
    ON chat_session_summaries(tenant_id, user_id, created_at DESC);

-- U6-4 (OP-2699): L3 per-user erasable semantic-fact store (DORMANT). PostgreSQL
-- alembic 0273 is authoritative + owns the REAL RLS; this SQLite subset has NO RLS
-- (SQLite lacks it — dev isolation is explicit app predicates only). The VALUE is
-- sealed (sealed_ciphertext + dek_ref = U6-0b crypto-shred); the semantic key is
-- clear + queryable. Nothing writes this until U6-5 produces / U6-6 confirms.
CREATE TABLE IF NOT EXISTS l3_facts (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    user_id           TEXT NOT NULL,
    fact_type         TEXT NOT NULL,
    subject           TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    sealed_ciphertext TEXT NOT NULL,
    dek_ref           TEXT NOT NULL DEFAULT '{}',
    sensitivity       TEXT NOT NULL DEFAULT 'normal'
                      CHECK (sensitivity IN ('normal', 'sensitive')),
    valid_from        TEXT,
    valid_until       TEXT,
    state             TEXT NOT NULL DEFAULT 'quarantined'
                      CHECK (state IN ('quarantined', 'promoted', 'superseded', 'rejected')),
    revision          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_l3_facts_one_current
    ON l3_facts (tenant_id, user_id, fact_type, subject, predicate) WHERE state = 'promoted';
CREATE INDEX IF NOT EXISTS idx_l3_facts_user ON l3_facts (tenant_id, user_id, state);
CREATE TABLE IF NOT EXISTS l3_erasure_audit (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    user_ref    TEXT NOT NULL,  -- one-way pseudonym of the scope; NEVER the raw user_id
    fact_count  INTEGER NOT NULL,
    erased_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- U6-5b (OP-2701): per-user L3 eval/approval ledger (DORMANT). PostgreSQL alembic
-- 0274 is authoritative + owns the RLS; this SQLite subset has none. NOT the U4
-- memory_eval_runs (which FKs the global ledger) — a NEW per-user ledger keyed on
-- l3_facts + (tenant,user). reasons is content-free (probe names, never content).
CREATE TABLE IF NOT EXISTS l3_eval_runs (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    user_id     TEXT NOT NULL,
    fact_id     TEXT NOT NULL,
    eval_kind   TEXT NOT NULL CHECK (eval_kind IN ('memory_safety')),
    decision    TEXT NOT NULL
                CHECK (decision IN ('promote', 'reject', 'insufficient_evidence', 'infra_invalid')),
    reasons     TEXT NOT NULL DEFAULT '[]',
    ran_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_l3_eval_runs_fact ON l3_eval_runs (tenant_id, user_id, fact_id);
CREATE TABLE IF NOT EXISTS l3_approvals (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    user_id     TEXT NOT NULL,
    eval_run_id TEXT NOT NULL REFERENCES l3_eval_runs(id),
    fact_id     TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_l3_approvals_fact ON l3_approvals (tenant_id, user_id, fact_id);
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision Rules persistence (Phase 50B-Fix / A1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def load_decision_rules(conn) -> list[dict]:
    """Load all persisted decision rules for the current tenant. Returns
    list of dicts matching the in-memory shape used by
    backend.decision_rules."""
    conditions: list[str] = []
    params: list = []
    tenant_where_pg(conditions, params)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        "SELECT id, kind_pattern, severity, auto_in_modes, "
        "default_option_id, priority, enabled, note FROM decision_rules"
        + where
    )
    rows = await conn.fetch(sql, *params)
    out: list[dict] = []
    for r in rows:
        try:
            modes = json.loads(r["auto_in_modes"])
        except Exception:
            modes = []
        out.append({
            "id": r["id"],
            "kind_pattern": r["kind_pattern"],
            "severity": r["severity"],
            "auto_in_modes": modes if isinstance(modes, list) else [],
            "default_option_id": r["default_option_id"],
            "priority": r["priority"],
            "enabled": bool(r["enabled"]),
            "note": r["note"] or "",
        })
    return out


async def replace_decision_rules(conn, rules: list[dict]) -> None:
    """Atomically swap the current tenant's decision_rules slice.

    Phase-3-Runtime-v2 SP-3.11 (2026-04-20): ported to native asyncpg.
    The old SQLite version used manual ``BEGIN IMMEDIATE`` / commit /
    rollback; asyncpg uses ``async with conn.transaction()`` — implicit
    COMMIT on block exit, implicit ROLLBACK on exception. This
    preserves the all-or-nothing contract the caller (decision_rules
    service) depends on: a partial INSERT loop failure cannot leave
    the tenant's rule set in a mixed state.

    Tenant scope: the DELETE uses ``tenant_where_pg`` so only the
    current tenant's rows are wiped; other tenants' rules survive.
    Every INSERT pins tenant_id to ``tenant_insert_value()`` (same
    anti-forge rule as every other tenant-scoped port — the caller
    cannot override).
    """
    tid = tenant_insert_value()
    async with conn.transaction():
        t_cond: list[str] = []
        t_params: list = []
        tenant_where_pg(t_cond, t_params)
        del_sql = "DELETE FROM decision_rules"
        if t_cond:
            del_sql += " WHERE " + " AND ".join(t_cond)
        await conn.execute(del_sql, *t_params)
        for r in rules:
            await conn.execute(
                """INSERT INTO decision_rules (id, kind_pattern, severity,
                     auto_in_modes, default_option_id, priority, enabled,
                     note, tenant_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                r["id"],
                r["kind_pattern"],
                r.get("severity"),
                json.dumps(r.get("auto_in_modes") or []),
                r.get("default_option_id"),
                int(r.get("priority", 100)),
                1 if r.get("enabled", True) else 0,
                (r.get("note") or "")[:240],
                tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adaptive-budget state (H4a row 2582)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Fixed primary-key value for the host-level AIMD budget row — the
#: table is semantically a singleton (budget is per-host, not
#: per-tenant). Centralised here so the loader and saver can't
#: disagree on spelling.
ADAPTIVE_BUDGET_SINGLETON_ID = "global"


async def load_adaptive_budget_state(conn) -> dict | None:
    """Return the last-persisted AIMD budget row or None if unset.

    Called once at lifespan startup (see
    :func:`backend.adaptive_budget.load_last_known_good`) to seed the
    controller with whatever the previous ``uvicorn`` process
    converged on — replaces the static ``OMNISIGHT_AIMD_INIT_BUDGET=6``
    default on warm restarts. Row shape:
    ``{budget: int, last_reason: str, updated_at: float}``.
    """
    row = await conn.fetchrow(
        "SELECT budget, last_reason, updated_at "
        "FROM adaptive_budget_state WHERE id = $1",
        ADAPTIVE_BUDGET_SINGLETON_ID,
    )
    if row is None:
        return None
    return {
        "budget": int(row["budget"]),
        "last_reason": row["last_reason"] or "init",
        "updated_at": float(row["updated_at"]),
    }


async def save_adaptive_budget_state(
    conn,
    *,
    budget: int,
    last_reason: str,
    updated_at: float,
) -> None:
    """Upsert the singleton ``adaptive_budget_state`` row.

    Multi-worker races write "last writer wins" semantics which is
    benign — every worker observes the same host CPU / mem, so the
    candidate budgets differ by at most one AIMD step. The caller
    should only invoke this on a state-changing tick (AI / MD);
    HOLD / CAP / FLOOR leave the budget unchanged and would be noise.
    """
    await conn.execute(
        """
        INSERT INTO adaptive_budget_state (id, budget, last_reason, updated_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (id) DO UPDATE SET
            budget      = EXCLUDED.budget,
            last_reason = EXCLUDED.last_reason,
            updated_at  = EXCLUDED.updated_at
        """,
        ADAPTIVE_BUDGET_SINGLETON_ID,
        int(budget),
        str(last_reason),
        float(updated_at),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ─── Agents domain (Phase-3-Runtime-v2 SP-3.1) ──────────────────────────
#
# Ported from compat-wrapper single-connection access to native asyncpg
# + request-scoped pool connection. Every caller MUST pass an
# asyncpg.Connection borrowed from backend.db_pool (typically via the
# ``Depends(get_conn)`` dependency for router handlers, or
# ``async with get_pool().acquire() as conn:`` for background/startup
# code).
#
# Dialect scope:
#   * Postgres: primary target; every statement runs natively.
#   * SQLite: deliberately NOT supported by these functions. During
#     Epics 3-6 SQLite dev mode is degraded for ported domains; Epic 7
#     removes the compat wrapper and SQLite is gone for runtime.
#     Callers on a SQLite dev box will see a clear error rather than
#     silent data loss because the pool is gated on a Postgres DSN
#     (``backend.main.lifespan`` gate + ``db_pool.get_pool()`` raises
#     RuntimeError when uninit).
#
# Row factory:
#   asyncpg.Record supports both ``row["col"]`` and ``row[0]`` so the
#   helper ``_agent_row_to_dict`` below works unchanged across aiosqlite
#   and asyncpg — it was defensive enough even before the port.


async def list_agents(conn) -> list[dict]:
    """List all agents. ``conn`` is an asyncpg.Connection from the pool."""
    rows = await conn.fetch("SELECT * FROM agents")
    return [_agent_row_to_dict(r) for r in rows]


async def get_agent(conn, agent_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM agents WHERE id = $1", agent_id,
    )
    return _agent_row_to_dict(row) if row else None


async def upsert_agent(conn, data: dict) -> None:
    """Insert or update an agent row. No explicit commit — asyncpg
    auto-commits each statement when no outer ``conn.transaction()`` is
    active. For atomic multi-statement flows, callers wrap the whole
    block in ``async with conn.transaction():`` themselves.
    """
    await conn.execute(
        """INSERT INTO agents
               (id, name, type, sub_type, status, progress, thought_chain,
                ai_model, sub_tasks, workspace)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           ON CONFLICT (id) DO UPDATE SET
               name          = EXCLUDED.name,
               type          = EXCLUDED.type,
               sub_type      = EXCLUDED.sub_type,
               status        = EXCLUDED.status,
               progress      = EXCLUDED.progress,
               thought_chain = EXCLUDED.thought_chain,
               ai_model      = EXCLUDED.ai_model,
               sub_tasks     = EXCLUDED.sub_tasks,
               workspace     = EXCLUDED.workspace
        """,
        data["id"],
        data["name"],
        data["type"],
        data.get("sub_type", ""),
        data.get("status", "idle"),
        json.dumps(data.get("progress", {"current": 0, "total": 0})),
        data.get("thought_chain", ""),
        data.get("ai_model"),
        json.dumps(data.get("sub_tasks", [])),
        json.dumps(data.get("workspace", {})),
    )


async def delete_agent(conn, agent_id: str) -> bool:
    """Delete an agent row. Returns True if a row was deleted, else False.

    asyncpg returns a status string like ``"DELETE 1"`` / ``"DELETE 0"``
    from ``conn.execute()``; we parse the trailing integer. Compare
    with the compat wrapper's ``_PgCursor.rowcount`` emulation which
    did the same parse — we're now inlining it here so the compat
    wrapper can be deleted in Epic 7 without losing behaviour.
    """
    status = await conn.execute(
        "DELETE FROM agents WHERE id = $1", agent_id,
    )
    # status: "DELETE <n>"; n is the row count.
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def agent_count(conn) -> int:
    n = await conn.fetchval("SELECT COUNT(*) FROM agents")
    return int(n) if n is not None else 0


def _agent_row_to_dict(row) -> dict:
    """Marshal a DB row (aiosqlite.Row legacy OR asyncpg.Record) into the
    dict shape ``routers/agents.py::_row_to_agent`` expects.

    Works on both row types because both support ``row["col"]`` lookup.
    Kept as a plain def (not async) so unit tests can marshal synthetic
    dicts too if needed.
    """
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "sub_type": row["sub_type"] if "sub_type" in keys else "",
        "status": row["status"],
        "progress": json.loads(row["progress"]),
        "thought_chain": row["thought_chain"],
        "ai_model": row["ai_model"],
        "sub_tasks": json.loads(row["sub_tasks"]),
        "workspace": json.loads(row["workspace"]),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _task_row_to_dict(row) -> dict:
    d = dict(row)
    for json_field in ("child_task_ids", "labels", "depends_on"):
        if isinstance(d.get(json_field), str):
            d[json_field] = json.loads(d[json_field])
    return d


async def list_tasks(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM tasks")
    return [_task_row_to_dict(r) for r in rows]


async def get_task(conn, task_id: str) -> dict | None:
    row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    return _task_row_to_dict(row) if row else None


async def upsert_task(conn, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.2: native asyncpg — positional
    # placeholders, ON CONFLICT DO UPDATE using EXCLUDED.*.
    # Pool auto-commits each statement outside an explicit transaction
    # block — no explicit commit needed.
    #
    # Q.7 #301 — ``version`` is intentionally NOT written from upsert.
    # The column's schema DEFAULT 0 fires on initial INSERT and the
    # optimistic-lock ``bump_version_pg`` owns every subsequent write.
    # Legacy non-HTTP writers (worker watchdogs, pipeline, seed defaults)
    # therefore keep writing through ``_persist`` without accidentally
    # clobbering the version a concurrent HTTP PATCH may have bumped.
    await conn.execute(
        """INSERT INTO tasks (id, title, description, priority, status, assigned_agent_id,
             created_at, completed_at, ai_analysis, suggested_agent_type, suggested_sub_type,
             parent_task_id, child_task_ids, external_issue_id, issue_url, acceptance_criteria,
             labels, depends_on, external_issue_platform, last_external_sync_at, npi_phase_id,
             rpg_campaign_id, rpg_campaign_title)
           VALUES ($1, $2, $3, $4, $5, $6,
                   $7, $8, $9, $10, $11,
                   $12, $13, $14, $15, $16,
                   $17, $18, $19, $20, $21,
                   $22, $23)
           ON CONFLICT (id) DO UPDATE SET
             title=EXCLUDED.title, description=EXCLUDED.description, priority=EXCLUDED.priority,
             status=EXCLUDED.status, assigned_agent_id=EXCLUDED.assigned_agent_id,
             completed_at=EXCLUDED.completed_at, ai_analysis=EXCLUDED.ai_analysis,
             suggested_agent_type=EXCLUDED.suggested_agent_type, suggested_sub_type=EXCLUDED.suggested_sub_type,
             parent_task_id=EXCLUDED.parent_task_id, child_task_ids=EXCLUDED.child_task_ids,
             external_issue_id=EXCLUDED.external_issue_id, issue_url=EXCLUDED.issue_url,
             acceptance_criteria=EXCLUDED.acceptance_criteria, labels=EXCLUDED.labels,
             depends_on=EXCLUDED.depends_on, external_issue_platform=EXCLUDED.external_issue_platform,
             last_external_sync_at=EXCLUDED.last_external_sync_at, npi_phase_id=EXCLUDED.npi_phase_id,
             rpg_campaign_id=EXCLUDED.rpg_campaign_id,
             rpg_campaign_title=EXCLUDED.rpg_campaign_title
        """,
        data["id"],
        data["title"],
        data.get("description"),
        data.get("priority", "medium"),
        data.get("status", "backlog"),
        data.get("assigned_agent_id"),
        data.get("created_at", ""),
        data.get("completed_at"),
        data.get("ai_analysis"),
        data.get("suggested_agent_type"),
        data.get("suggested_sub_type"),
        data.get("parent_task_id"),
        json.dumps(data.get("child_task_ids", [])),
        data.get("external_issue_id"),
        data.get("issue_url"),
        data.get("acceptance_criteria"),
        json.dumps(data.get("labels", [])),
        json.dumps(data.get("depends_on", [])),
        data.get("external_issue_platform"),
        data.get("last_external_sync_at"),
        data.get("npi_phase_id"),
        data.get("rpg_campaign_id"),
        data.get("rpg_campaign_title"),
    )


# ── Task comments ──

async def insert_task_comment(conn, data: dict) -> None:
    await conn.execute(
        """INSERT INTO task_comments (id, task_id, author, content, timestamp)
           VALUES ($1, $2, $3, $4, $5)""",
        data["id"],
        data["task_id"],
        data["author"],
        data["content"],
        data["timestamp"],
    )


async def list_task_comments(conn, task_id: str, limit: int = 20) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM task_comments WHERE task_id = $1 ORDER BY timestamp DESC LIMIT $2",
        task_id,
        limit,
    )
    return [dict(r) for r in rows]


async def delete_task(conn, task_id: str) -> bool:
    # asyncpg returns a status string like "DELETE 1"; parse the count.
    # Matches the SP-3.1 delete_agent pattern — inlines what the compat
    # wrapper's _PgCursor did so Epic 7 can delete the wrapper safely.
    status = await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def task_count(conn) -> int:
    n = await conn.fetchval("SELECT COUNT(*) FROM tasks")
    return int(n) if n is not None else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_token_usage(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM token_usage")
    return [dict(r) for r in rows]


async def upsert_token_usage(conn, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.5 (2026-04-20): ported to native asyncpg.
    # ZZ.A1 (#303-1, 2026-04-24): extended to persist prompt-cache
    # observability columns. The three cache_* fields are intentionally
    # NULLABLE — a caller that never observed cache activity (pre-ZZ
    # legacy worker, or a provider that doesn't report cache fields)
    # passes None through and the NULL is preserved end-to-end so the
    # UI can distinguish "no data" from "genuine 0".
    # ZZ.A3 (#303-3, 2026-04-24): extended again with the two
    # per-turn boundary stamps (``turn_started_at`` / ``turn_ended_at``,
    # ISO-8601 UTC). Same NULL-preservation contract — pre-ZZ.A3 rows
    # pass ``None`` through and the dashboard renders "—" instead of a
    # fabricated gap of 0ms. 13 positional placeholders now; ON
    # CONFLICT (model) DO UPDATE uses EXCLUDED.* per PG convention.
    # Caller (routers/system.py _persist_token_usage) is fire-and-forget
    # from the LLM callback — asyncpg auto-commits each statement
    # outside a tx block, matching the prior compat-wrapper's explicit
    # .commit().
    def _int_or_none(key: str):
        val = data.get(key)
        return None if val is None else int(val)

    def _float_or_none(key: str):
        val = data.get(key)
        return None if val is None else float(val)

    def _str_or_none(key: str):
        # ZZ.A3: empty-string is treated as "no stamp yet" and coerced
        # to SQL NULL so the DB column stays a clean NULL until a real
        # turn lands — avoids persisting a synthetic "" that the UI
        # would then have to special-case separately from None.
        val = data.get(key)
        if val is None or val == "":
            return None
        return str(val)

    await conn.execute(
        """INSERT INTO token_usage (model, input_tokens, output_tokens,
             total_tokens, cost, request_count, avg_latency, last_used,
             cache_read_tokens, cache_create_tokens, cache_hit_ratio,
             turn_started_at, turn_ended_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
           ON CONFLICT (model) DO UPDATE SET
             input_tokens = EXCLUDED.input_tokens,
             output_tokens = EXCLUDED.output_tokens,
             total_tokens = EXCLUDED.total_tokens,
             cost = EXCLUDED.cost,
             request_count = EXCLUDED.request_count,
             avg_latency = EXCLUDED.avg_latency,
             last_used = EXCLUDED.last_used,
             cache_read_tokens = EXCLUDED.cache_read_tokens,
             cache_create_tokens = EXCLUDED.cache_create_tokens,
             cache_hit_ratio = EXCLUDED.cache_hit_ratio,
             turn_started_at = EXCLUDED.turn_started_at,
             turn_ended_at = EXCLUDED.turn_ended_at
        """,
        data["model"],
        int(data.get("input_tokens", 0)),
        int(data.get("output_tokens", 0)),
        int(data.get("total_tokens", 0)),
        float(data.get("cost", 0.0)),
        int(data.get("request_count", 0)),
        int(data.get("avg_latency", 0)),
        data.get("last_used", ""),
        _int_or_none("cache_read_tokens"),
        _int_or_none("cache_create_tokens"),
        _float_or_none("cache_hit_ratio"),
        _str_or_none("turn_started_at"),
        _str_or_none("turn_ended_at"),
    )


async def clear_token_usage(conn) -> None:
    await conn.execute("DELETE FROM token_usage")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handoffs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def upsert_handoff(
    conn, task_id: str, agent_id: str, content: str,
) -> None:
    # Phase-3-Runtime-v2 SP-3.3 (2026-04-20): ported to native asyncpg.
    # ``created_at`` on CONFLICT UPDATE uses the same text format the
    # alembic-level ``alembic_pg_compat._translate_datetime_now`` rewrite
    # produces for the column DEFAULT — keeps newly-INSERTED rows and
    # updated-rows byte-identical in format, so ORDER BY created_at
    # keeps working after an upsert.
    #
    # Uses ``clock_timestamp()`` rather than ``now()``: PG's ``now()``
    # is ``transaction_timestamp()`` — fixed at tx start — which means
    # multiple upserts within the same outer tx (a common shape in
    # pg_test_conn savepoint fixtures, and also in any handler that
    # wraps multiple writes in ``async with conn.transaction()``)
    # collide on timestamp and break the "last-written-at" ordering
    # semantics the handoffs timeline UI depends on. clock_timestamp()
    # returns real wall-clock time regardless of tx state. Outside a
    # tx (auto-commit path, which is how production handlers operate)
    # the two are equivalent — so this change is strictly additive:
    # stronger guarantee for tx callers, no regression for others.
    #
    # We explicitly provide ``created_at`` on the INSERT path too,
    # rather than letting the column DEFAULT (``to_char(now(), ...)``)
    # fire. Otherwise the INSERT path would still use tx-scoped now()
    # while the UPDATE path uses clock_timestamp() — inconsistent
    # between the two branches and still collision-prone on multiple
    # fresh INSERTs in the same tx.
    await conn.execute(
        """INSERT INTO handoffs (task_id, agent_id, content, created_at)
           VALUES (
             $1, $2, $3,
             to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS')
           )
           ON CONFLICT (task_id) DO UPDATE SET
             agent_id = EXCLUDED.agent_id,
             content = EXCLUDED.content,
             created_at = to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS')
        """,
        task_id, agent_id, content,
    )


async def get_handoff(conn, task_id: str) -> str:
    row = await conn.fetchrow(
        "SELECT content FROM handoffs WHERE task_id = $1", task_id,
    )
    return row["content"] if row else ""


async def list_handoffs(conn) -> list[dict]:
    rows = await conn.fetch(
        "SELECT task_id, agent_id, created_at FROM handoffs "
        "ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_notification(conn, data: dict) -> None:
    # R9 row 2935 (#315): severity is the new tag column. Older callers
    # that don't pass it land NULL — `data.get("severity")` returns
    # None which asyncpg / aiosqlite both bind to SQL NULL. We do not
    # validate the value here against the Severity enum because
    # ``Notification`` (the Pydantic model used by ``notify()``) already
    # validates on construction; persisting whatever the caller already
    # serialised keeps this function symmetric with how level is
    # treated (also stored as TEXT, also not re-validated here).
    await conn.execute(
        """INSERT INTO notifications (id, level, title, message, source, timestamp,
             read, action_url, action_label, auto_resolved, severity,
             is_red_card)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        data["id"],
        data["level"],
        data["title"],
        data.get("message", ""),
        data.get("source", ""),
        data.get("timestamp", ""),
        1 if data.get("read") else 0,
        data.get("action_url"),
        data.get("action_label"),
        1 if data.get("auto_resolved") else 0,
        data.get("severity"),
        1 if data.get("is_red_card") else 0,
    )


def _notification_row_to_dict(row) -> dict:
    d = dict(row)
    d["read"] = bool(d.get("read", 0))
    d["auto_resolved"] = bool(d.get("auto_resolved", 0))
    d["is_red_card"] = bool(d.get("is_red_card", 0))
    return d


async def list_notifications(
    conn, limit: int = 50, level: str = "",
) -> list[dict]:
    if level:
        rows = await conn.fetch(
            "SELECT * FROM notifications WHERE level = $1 "
            "ORDER BY timestamp DESC LIMIT $2",
            level, limit,
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM notifications "
            "ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
    return [_notification_row_to_dict(r) for r in rows]


async def mark_notification_read(conn, notification_id: str) -> bool:
    status = await conn.execute(
        "UPDATE notifications SET read = 1 WHERE id = $1",
        notification_id,
    )
    # asyncpg status is "UPDATE <n>" — same pattern SP-3.1 / SP-3.2
    # delete_agent / delete_task use to recover rowcount without
    # depending on a .rowcount attribute.
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def count_unread_notifications(conn, min_level: str = "warning") -> int:
    # level values come from a hardcoded dict — no user input reaches
    # the SQL, so the placeholder-count f-string is injection-safe.
    # We still bind via positional parameters because that is what
    # asyncpg actually supports; the dynamic count just produces the
    # right number of ``$N`` tokens for the IN list.
    levels = {"info": 0, "warning": 1, "action": 2, "critical": 3}
    min_rank = levels.get(min_level, 1)
    valid_levels = [l for l, r in levels.items() if r >= min_rank]
    placeholders = ",".join(f"${i + 1}" for i in range(len(valid_levels)))
    n = await conn.fetchval(
        f"SELECT COUNT(*) FROM notifications WHERE read = 0 "
        f"AND level IN ({placeholders})",
        *valid_levels,
    )
    return int(n) if n is not None else 0


async def update_notification_dispatch(
    conn, notification_id: str, status: str,
    attempts: int = 0, error: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE notifications SET dispatch_status = $1, "
        "send_attempts = $2, last_error = $3 WHERE id = $4",
        status, attempts, error, notification_id,
    )


async def list_failed_notifications(conn, limit: int = 50) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM notifications WHERE dispatch_status = 'failed' "
        "ORDER BY timestamp DESC LIMIT $1",
        limit,
    )
    return [_notification_row_to_dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_artifact(conn, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.6a (2026-04-20): ported to native asyncpg.
    # tenant_id is auto-derived from request context via
    # tenant_insert_value() — caller's ``data`` dict is OVERRIDDEN if
    # it sets tenant_id, matching the pre-port behaviour. This is the
    # core isolation guarantee: a malicious caller cannot forge a
    # cross-tenant INSERT by supplying their own tenant_id.
    await conn.execute(
        """INSERT INTO artifacts (id, task_id, agent_id, name, type,
             file_path, size, created_at, version, checksum, tenant_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        data["id"],
        data.get("task_id", ""),
        data.get("agent_id", ""),
        data["name"],
        data.get("type", ""),
        data.get("file_path", ""),
        int(data.get("size", 0)),
        data.get("created_at", ""),
        data.get("version", ""),
        data.get("checksum", ""),
        tenant_insert_value(),
    )


async def list_artifacts(
    conn, task_id: str = "", agent_id: str = "", limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    tenant_where_pg(conditions, params)
    if task_id:
        conditions.append(f"task_id = ${len(params) + 1}")
        params.append(task_id)
    if agent_id:
        conditions.append(f"agent_id = ${len(params) + 1}")
        params.append(agent_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT * FROM artifacts"
        + where
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def get_artifact(conn, artifact_id: str) -> dict | None:
    conditions = ["id = $1"]
    params: list = [artifact_id]
    tenant_where_pg(conditions, params)
    sql = "SELECT * FROM artifacts WHERE " + " AND ".join(conditions)
    row = await conn.fetchrow(sql, *params)
    return dict(row) if row else None


async def delete_artifact(conn, artifact_id: str) -> bool:
    conditions = ["id = $1"]
    params: list = [artifact_id]
    tenant_where_pg(conditions, params)
    sql = "DELETE FROM artifacts WHERE " + " AND ".join(conditions)
    status = await conn.execute(sql, *params)
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPI Lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def get_npi_state(conn) -> dict:
    row = await conn.fetchrow(
        "SELECT data FROM npi_state WHERE id = 'current'",
    )
    if row:
        return json.loads(row["data"])
    return {}


async def save_npi_state(conn, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.7 (2026-04-20): ported to native asyncpg.
    # Single-row table keyed on id='current'. $1 is bound once; the
    # prior compat form used a named ``:data`` parameter referenced in
    # both INSERT VALUES and implicit EXCLUDED — PG's ON CONFLICT DO
    # UPDATE SET ``data = EXCLUDED.data`` reads the attempted-insert
    # row automatically, so the binding is single-shot here.
    await conn.execute(
        """INSERT INTO npi_state (id, data) VALUES ('current', $1)
           ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data""",
        json.dumps(data),
    )


async def get_npi_state_version(conn) -> int:
    """Q.7 #301 — current optimistic-lock version for the singleton row.

    Returns 0 if the row does not exist yet (same default the schema
    gives on first write).
    """
    row = await conn.fetchrow(
        "SELECT version FROM npi_state WHERE id = 'current'",
    )
    if row is None:
        return 0
    return int(row["version"])


async def save_npi_state_versioned(
    conn, data: dict, *, expected_version: int,
) -> int:
    """Q.7 #301 — PUT /runtime/npi optimistic-lock write path.

    On first-ever write the row is absent; we INSERT with version=1
    only when the client honestly sent ``If-Match: 0`` (so two
    first-PUTs race → one wins with version=1, the other's INSERT
    fails the UNIQUE pk and we re-read to report 409 with
    current_version=1).

    On subsequent writes we UPDATE with the ``version = $expected``
    guard; miss → raise ``VersionConflict`` carrying the live
    post-commit version the frontend hook will show in the toast.
    """
    from backend.optimistic_lock import VersionConflict
    payload = json.dumps(data)
    row = await conn.fetchrow(
        "UPDATE npi_state SET data = $1, version = version + 1 "
        "WHERE id = 'current' AND version = $2 RETURNING version",
        payload, expected_version,
    )
    if row is not None:
        return int(row["version"])
    # UPDATE missed — either the row doesn't exist yet (first write)
    # or the version drifted. Distinguish by probing the pk.
    probe = await conn.fetchrow(
        "SELECT version FROM npi_state WHERE id = 'current'",
    )
    if probe is None:
        # First write — only accept If-Match: 0 (otherwise the client
        # claims to have seen a version we never produced).
        if expected_version != 0:
            raise VersionConflict(
                current_version=0,
                your_version=expected_version,
                resource="npi_state",
            )
        try:
            inserted = await conn.fetchrow(
                "INSERT INTO npi_state (id, data, version) "
                "VALUES ('current', $1, 1) "
                "ON CONFLICT (id) DO NOTHING RETURNING version",
                payload,
            )
        except Exception:
            inserted = None
        if inserted is not None:
            return int(inserted["version"])
        # Another writer inserted between our UPDATE and INSERT →
        # race loss, re-read to populate 409 body.
        probe2 = await conn.fetchrow(
            "SELECT version FROM npi_state WHERE id = 'current'",
        )
        raise VersionConflict(
            current_version=int(probe2["version"]) if probe2 else None,
            your_version=expected_version,
            resource="npi_state",
        )
    raise VersionConflict(
        current_version=int(probe["version"]),
        your_version=expected_version,
        resource="npi_state",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_simulation(conn, data: dict) -> None:
    await conn.execute(
        """INSERT INTO simulations
           (id, task_id, agent_id, track, module, status,
            tests_total, tests_passed, tests_failed,
            coverage_pct, valgrind_errors, duration_ms,
            report_json, artifact_id, created_at)
           VALUES ($1, $2, $3, $4, $5, $6,
                   $7, $8, $9,
                   $10, $11, $12,
                   $13, $14, $15)""",
        data["id"],
        data.get("task_id"),
        data.get("agent_id"),
        data["track"],
        data["module"],
        data.get("status", "running"),
        int(data.get("tests_total", 0)),
        int(data.get("tests_passed", 0)),
        int(data.get("tests_failed", 0)),
        float(data.get("coverage_pct", 0.0)),
        int(data.get("valgrind_errors", 0)),
        int(data.get("duration_ms", 0)),
        data.get("report_json", "{}"),
        data.get("artifact_id"),
        data.get("created_at", ""),
    )


async def get_simulation(conn, sim_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM simulations WHERE id = $1", sim_id,
    )
    return dict(row) if row else None


async def list_simulations(
    conn,
    task_id: str = "", agent_id: str = "", status: str = "",
    limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if task_id:
        conditions.append(f"task_id = ${len(params) + 1}")
        params.append(task_id)
    if agent_id:
        conditions.append(f"agent_id = ${len(params) + 1}")
        params.append(agent_id)
    if status:
        conditions.append(f"status = ${len(params) + 1}")
        params.append(status)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT * FROM simulations"
        + where
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


_SIMULATION_COLUMNS = frozenset({
    "status", "tests_total", "tests_passed", "tests_failed",
    "coverage_pct", "valgrind_errors", "duration_ms",
    "report_json", "artifact_id",
    # NPU fields (Phase 36)
    "npu_latency_ms", "npu_throughput_fps", "accuracy_delta",
    "model_size_kb", "npu_framework",
})


async def update_simulation(conn, sim_id: str, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.8 (2026-04-20): whitelist-driven SET
    # clause built from _SIMULATION_COLUMNS. Column names are NEVER
    # taken from untrusted input — only keys that pass the frozenset
    # check become column tokens — so the f-string SET clause is
    # injection-safe. Values bind via positional ``$N`` placeholders.
    if not data:
        return
    safe = {k: v for k, v in data.items() if k in _SIMULATION_COLUMNS}
    if not safe:
        return
    cols = list(safe.keys())
    set_clause = ", ".join(
        f"{c} = ${i + 1}" for i, c in enumerate(cols)
    )
    id_idx = len(cols) + 1
    sql = f"UPDATE simulations SET {set_clause} WHERE id = ${id_idx}"
    await conn.execute(sql, *safe.values(), sim_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Debug Findings (Shared Blackboard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_debug_finding(conn, data: dict) -> None:
    # Phase-3-Runtime-v2 SP-3.9 (2026-04-20): ported to native asyncpg.
    # INSERT OR IGNORE → ON CONFLICT (id) DO NOTHING preserves the
    # duplicate-id-is-noop contract — the shared blackboard is
    # append-only and agents may legitimately re-log the same finding
    # id without failing their own flow.
    # tenant_id ALWAYS comes from context (tenant_insert_value), never
    # from the caller's data dict — anti-forge guarantee (same rule as
    # insert_artifact, SP-3.6a).
    from backend.agent_guild_dual_write import (
        guild_id_from_values,
        sync_agent_type_guild_id,
        sync_json_mapping,
    )

    synced_data = sync_agent_type_guild_id(data)
    synced_context = sync_json_mapping(synced_data.get("context", "{}"))
    await conn.execute(
        """INSERT INTO debug_findings
           (id, task_id, agent_id, finding_type, severity, content,
            context, status, created_at, tenant_id, guild_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
           ON CONFLICT (id) DO NOTHING""",
        synced_data["id"],
        synced_data["task_id"],
        synced_data["agent_id"],
        synced_data["finding_type"],
        synced_data.get("severity", "info"),
        synced_data["content"],
        synced_context,
        synced_data.get("status", "open"),
        synced_data.get("created_at", ""),
        tenant_insert_value(),
        guild_id_from_values(synced_data),
    )


async def list_debug_findings(
    conn,
    task_id: str = "", agent_id: str = "", status: str = "",
    limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    tenant_where_pg(conditions, params)
    if task_id:
        conditions.append(f"task_id = ${len(params) + 1}")
        params.append(task_id)
    if agent_id:
        conditions.append(f"agent_id = ${len(params) + 1}")
        params.append(agent_id)
    if status:
        conditions.append(f"status = ${len(params) + 1}")
        params.append(status)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT * FROM debug_findings"
        + where
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def update_debug_finding(conn, finding_id: str, status: str) -> bool:
    # WHERE clause MUST include the current tenant filter — otherwise
    # a caller holding a known finding id from Tenant A could mutate
    # Tenant B's finding. Tenant filter is applied BEFORE the SET
    # value placeholder so the $N positions stay sequential.
    # resolved_at uses clock_timestamp() (not now()) — advances within
    # a single tx, matching the SP-3.3 handoffs fix.
    conditions = ["id = $1"]
    params: list = [finding_id]
    tenant_where_pg(conditions, params)
    status_idx = len(params) + 1
    sql = (
        f"UPDATE debug_findings SET "
        f"status = ${status_idx}, "
        f"resolved_at = to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS') "
        f"WHERE " + " AND ".join(conditions)
    )
    params.append(status)
    exec_status = await conn.execute(sql, *params)
    try:
        return int(exec_status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event Log (Persistence)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_event(conn, event_type: str, data_json: str) -> None:
    # Phase-3-Runtime-v2 SP-3.10 (2026-04-20): ported to native asyncpg.
    # tenant_id comes from context via tenant_insert_value() — same
    # anti-forge guarantee as insert_artifact / insert_debug_finding.
    await conn.execute(
        "INSERT INTO event_log (event_type, data_json, tenant_id) "
        "VALUES ($1, $2, $3)",
        event_type, data_json, tenant_insert_value(),
    )


async def list_events(
    conn,
    since: str = "",
    event_types: list[str] | None = None,
    limit: int = 200,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    tenant_where_pg(conditions, params)
    if since:
        conditions.append(f"created_at >= ${len(params) + 1}")
        params.append(since)
    if event_types:
        # Dynamic IN placeholder count; event_types values are bound
        # positionally so no injection risk from the list contents.
        start_idx = len(params) + 1
        placeholders = ",".join(
            f"${start_idx + i}" for i in range(len(event_types))
        )
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(event_types)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT * FROM event_log"
        + where
        + f" ORDER BY id DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def cleanup_old_events(conn, days: int = 7) -> int:
    # SP-3.10: SQLite ``datetime('now', '-N days')`` replaced with PG's
    # ``NOW() - INTERVAL '1 day' * $N``. The result is cast to the
    # same ``YYYY-MM-DD HH24:MI:SS`` text format the column stores
    # (via to_char) so the strict ``<`` text comparison is sortable.
    #
    # **Bug fix shipped alongside the port**: the old SQLite version
    # had NO tenant filter — a cleanup sweep on Tenant A's schedule
    # would delete Tenant B's events too. tenant_where_pg added so
    # each tenant's cleanup only touches its own rows. Safe even when
    # no tenant is set (cleanup defaults to t-default scope).
    conditions: list[str] = []
    params: list = []
    tenant_where_pg(conditions, params)
    # days is the LAST positional param so the tenant filter's $N is
    # stable regardless of context state.
    days_idx = len(params) + 1
    params.append(days)
    cutoff = (
        f"to_char(NOW() - INTERVAL '1 day' * ${days_idx}, "
        "'YYYY-MM-DD HH24:MI:SS')"
    )
    conditions.append(f"created_at < {cutoff}")
    sql = "DELETE FROM event_log WHERE " + " AND ".join(conditions)
    status = await conn.execute(sql, *params)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3 Episodic Memory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_episodic_memory(conn, data: dict) -> None:
    """Insert a new episodic memory entry (L3).

    Phase-3-Runtime-v2 SP-3.12 (2026-04-20): ported to native asyncpg.
    The FTS5 virtual-table sync that the SQLite version did after the
    INSERT is **gone** — alembic 0017 (SP-2.1) added a ``tsv tsvector
    GENERATED ALWAYS AS (...) STORED`` column that PG maintains
    automatically on INSERT/UPDATE. The search function
    (search_episodic_memory below) reads from ``tsv`` directly.

    Phase 63-E: decayed_score initialises to quality_score so a fresh
    row competes on its own merit; the nightly memory_decay worker
    decays it later when access stops. created_at / updated_at use
    clock_timestamp() — matches the SP-3.3 handoffs / SP-3.9
    debug_findings pattern (advances within a single tx, consistent
    YYYY-MM-DD HH:MM:SS text format).

    U6-0 T8-A (OP-2610): the six source-aware fields default to the
    QUARANTINED state — ``verified=False``/``source='legacy'`` — so an
    un-updated caller can never mint a verified row; the alembic 0260
    verified⇒gerrit CHECK backs this at the DB level on PG. T8-B's
    Gerrit-verified service path is the only writer that may pass
    ``verified=True`` (with source='service_gerrit_merge' +
    verification_authority='gerrit').
    """
    q = float(data.get("quality_score", 0.0))
    await conn.execute(
        """INSERT INTO episodic_memory
           (id, error_signature, solution, soc_vendor, sdk_version,
            hardware_rev, source_task_id, source_agent_id,
            gerrit_change_id, tags, quality_score, decayed_score,
            tenant_id, source, verification_authority, verified,
            owner_user_id, visibility,
            created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5,
                   $6, $7, $8,
                   $9, $10, $11, $12,
                   $13, $14, $15, $16,
                   $17, $18,
                   to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS'),
                   to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS'))""",
        data["id"],
        data["error_signature"],
        data["solution"],
        data.get("soc_vendor", ""),
        data.get("sdk_version", ""),
        data.get("hardware_rev", ""),
        data.get("source_task_id"),
        data.get("source_agent_id"),
        data.get("gerrit_change_id"),
        json.dumps(data.get("tags", [])),
        q,
        q,  # decayed_score seeded from quality_score
        data.get("tenant_id") or tenant_insert_value(),
        data.get("source", "legacy"),
        data.get("verification_authority"),
        bool(data.get("verified", False)),
        data.get("owner_user_id"),
        data.get("visibility", "tenant_shared"),
    )

    # RPG.W5.2 [OP-1354]: auto-distil a <=200 token summary for the L2
    # dim-memory layer (ADR-0008 *Memory hierarchy*).  Best-effort:
    # distillation failure must never poison the lesson write above.
    try:
        from backend.agents.lessons_distiller import on_lesson_written
        await on_lesson_written(data)
    except Exception as exc:
        logger.debug("lessons_distiller hook failed: %s", exc)


async def insert_verified_merge_solution(conn, data: dict) -> bool:
    """Atomically insert ONE Gerrit-verified merge solution (T8-B1, OP-2611).

    The verified-write path (T8-B2) calls this ONLY after
    ``mcp_gerrit.verify_merged_change`` independently confirmed the merge.
    The row's provenance is HARD-SET here — ``source='service_gerrit_merge'``,
    ``verification_authority='gerrit'``, ``verified=TRUE`` — so a caller can
    never route non-Gerrit content through this path (the alembic 0260
    verified⇒gerrit CHECK backs it at the DB level).

    Idempotent via the ``uq_episodic_verified_merge`` partial unique index
    (alembic 0261): ``ON CONFLICT DO NOTHING RETURNING id`` returns a row on
    a real insert and NOTHING on a replay. Returns True iff a row was
    inserted, so the caller only does post-insert work (distillation) once —
    a duplicate/replayed ``change-merged`` webhook cannot double-write.
    ``conn.execute`` returns only a status string, so the ``RETURNING`` read
    MUST go through ``fetchrow`` (mirrors the ``npi_state`` idiom).
    """
    q = float(data.get("quality_score", 1.0))
    row = await conn.fetchrow(
        """INSERT INTO episodic_memory
           (id, error_signature, solution, soc_vendor, sdk_version,
            hardware_rev, source_task_id, source_agent_id,
            gerrit_change_id, tags, quality_score, decayed_score,
            tenant_id, source, verification_authority, verified,
            owner_user_id, visibility,
            created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5,
                   $6, $7, $8,
                   $9, $10, $11, $12,
                   $13, 'service_gerrit_merge', 'gerrit', TRUE,
                   $14, 'tenant_shared',
                   to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS'),
                   to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS'))
           ON CONFLICT (gerrit_change_id)
               WHERE verified AND source = 'service_gerrit_merge'
           DO NOTHING
           RETURNING id""",
        data["id"],
        data["error_signature"],
        data["solution"],
        data.get("soc_vendor", ""),
        data.get("sdk_version", ""),
        data.get("hardware_rev", ""),
        data.get("source_task_id"),
        data.get("source_agent_id"),
        data["gerrit_change_id"],
        json.dumps(data.get("tags", [])),
        q,
        q,  # decayed_score seeded from quality_score
        data.get("tenant_id") or tenant_insert_value(),
        data.get("owner_user_id"),
    )
    inserted = row is not None
    if inserted:
        # Distil ONLY on a real insert — a replay must not re-run it.
        try:
            from backend.agents.lessons_distiller import on_lesson_written
            await on_lesson_written(data)
        except Exception as exc:
            logger.debug("lessons_distiller hook failed: %s", exc)
    return inserted


async def rebuild_episodic_fts(conn) -> int:
    """Reindex the episodic_memory GIN index on the tsvector column.

    SP-3.12: with the STORED generated tsv column, the *content* can't
    drift from the base columns (PG regenerates it on any UPDATE that
    touches the source expression). This function now exists only to
    rebuild the GIN index itself — useful if ops sees GIN bloat or
    has reason to believe the index is corrupted after hardware
    failures. Returns the number of rows in the table afterwards,
    matching the old function's return shape.
    """
    try:
        # REINDEX is a single-statement operation; outside any explicit
        # tx block asyncpg auto-commits it.
        await conn.execute("REINDEX INDEX episodic_memory_tsv_gin")
        n = await conn.fetchval("SELECT COUNT(*) FROM episodic_memory")
        count = int(n) if n is not None else 0
        logger.info("REINDEX episodic_memory_tsv_gin complete (%d rows)", count)
        return count
    except Exception as exc:
        logger.error("REINDEX episodic_memory_tsv_gin failed: %s", exc)
        return 0


async def search_episodic_memory(
    conn,
    query: str, soc_vendor: str = "", sdk_version: str = "", limit: int = 5,
    min_quality: float | None = None,
) -> list[dict]:
    """Search L3 episodic memory using PG full-text search.

    Returns matching memories sorted by ts_rank (relevance), filtered
    by vendor/SDK/quality if provided.

    SP-3.12 (2026-04-20): ported from SQLite FTS5 (``MATCH`` +
    BM25 ordering + LIKE fallback) to PG's ``tsv @@ plainto_tsquery``
    + ``ts_rank`` on the STORED tsvector column added in alembic 0017.

    Ranking drift from BM25 to ts_rank was pre-approved by the
    operator in the design doc (01-design-decisions.md §5). The
    contract this function preserves is **result-set equivalence**:
    the same rows match (modulo stop-word filtering by PG's English
    dictionary), but within the match set the order may differ.

    Phase 67-E: `min_quality` pushes the similarity-proxy floor into
    SQL so callers that want cosine-style gating (the Tier-1 sandbox
    path wants > 0.85) don't over-fetch and Python-filter.
    """
    conditions: list[str] = ["tsv @@ plainto_tsquery('english', $1)"]
    params: list = [query]
    if soc_vendor:
        conditions.append(f"soc_vendor = ${len(params) + 1}")
        params.append(soc_vendor)
    if sdk_version:
        conditions.append(f"sdk_version = ${len(params) + 1}")
        params.append(sdk_version)
    if min_quality is not None:
        conditions.append(f"quality_score >= ${len(params) + 1}")
        params.append(min_quality)
    # LIMIT bind is the final positional param.
    params.append(limit)
    sql = (
        "SELECT *, ts_rank(tsv, plainto_tsquery('english', $1)) AS rank "
        "FROM episodic_memory WHERE " + " AND ".join(conditions)
        + f" ORDER BY rank DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    results = [_episodic_row_to_dict(r) for r in rows]

    # Increment access count for returned rows (best-effort — a write
    # failure here must not hide search hits from the caller).
    for r in results:
        try:
            await conn.execute(
                "UPDATE episodic_memory SET access_count = access_count + 1 "
                "WHERE id = $1",
                r["id"],
            )
        except Exception:
            pass
    return results


async def search_verified_tenant_solutions(
    conn,
    query: str,
    *,
    tenant_id: str,
    soc_vendor: str = "",
    sdk_version: str = "",
    limit: int = 5,
    min_quality: float | None = None,
) -> list[dict]:
    """DEFAULT-SECURE episodic search for model-facing readers.

    U6-0 T8-A (OP-2610): same ``tsv @@ plainto_tsquery`` + ``ts_rank``
    ranking, access_count bump, and row shape as
    :func:`search_episodic_memory`, but ALWAYS filtered to
    ``verified AND tenant_id = $t AND source = 'service_gerrit_merge'
    AND visibility = 'tenant_shared'`` — a quarantined legacy row, a
    model-authored "solution", a private row, or another tenant's row
    can never reach an agent prompt through this function. The filters
    are not parameters: callers cannot opt out.

    DORMANT until T8-C wires the three model readers to it.
    ``search_episodic_memory`` stays unchanged as the admin/unverified
    path.
    """
    # Fail-closed BEFORE touching the connection: an unresolved tenant
    # must never fall through to a global (cross-tenant) read.
    if not tenant_id:
        raise ValueError("tenant_id is required")

    conditions: list[str] = ["tsv @@ plainto_tsquery('english', $1)"]
    params: list = [query]
    if soc_vendor:
        conditions.append(f"soc_vendor = ${len(params) + 1}")
        params.append(soc_vendor)
    if sdk_version:
        conditions.append(f"sdk_version = ${len(params) + 1}")
        params.append(sdk_version)
    if min_quality is not None:
        conditions.append(f"quality_score >= ${len(params) + 1}")
        params.append(min_quality)
    conditions.append("verified")
    conditions.append(f"tenant_id = ${len(params) + 1}")
    params.append(tenant_id)
    conditions.append("source = 'service_gerrit_merge'")
    conditions.append("visibility = 'tenant_shared'")
    # LIMIT bind is the final positional param.
    params.append(limit)
    sql = (
        "SELECT *, ts_rank(tsv, plainto_tsquery('english', $1)) AS rank "
        "FROM episodic_memory WHERE " + " AND ".join(conditions)
        + f" ORDER BY rank DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    results = [_episodic_row_to_dict(r) for r in rows]

    # Increment access count for returned rows (best-effort — a write
    # failure here must not hide search hits from the caller).
    for r in results:
        try:
            await conn.execute(
                "UPDATE episodic_memory SET access_count = access_count + 1 "
                "WHERE id = $1",
                r["id"],
            )
        except Exception:
            pass
    return results


async def search_owner_clarifications(
    conn,
    query: str,
    *,
    owner_user_id: str,
    tenant_id: str,
    limit: int = 5,
    min_quality: float | None = None,
) -> list[dict]:
    """U6-0 T8-C3: fetch an operator's OWN prior spec-conflict
    clarifications for the intent-parser hint path.

    Same ``tsv @@ plainto_tsquery`` + ``ts_rank`` match semantics as
    :func:`search_episodic_memory` (so "similar prompt" matching is
    unchanged), but ALWAYS filtered to
    ``source = 'user_clarification' AND owner_user_id = $op AND
    tenant_id = $t``. A private clarification hint only ever surfaces
    to its own author within their tenant; a legacy NULL-owner row is
    correctly excluded by the equality filter (blind-test B3). The
    filters are not opt-out parameters.

    Fails closed BEFORE touching the connection: an empty owner or
    tenant raises ``ValueError`` (never a global read). Read-only — no
    access_count bump (a hint surfacing must not mutate decay state)."""
    if not owner_user_id:
        raise ValueError("owner_user_id is required")
    if not tenant_id:
        raise ValueError("tenant_id is required")

    conditions: list[str] = [
        "tsv @@ plainto_tsquery('english', $1)",
        "source = 'user_clarification'",
    ]
    params: list = [query]
    conditions.append(f"owner_user_id = ${len(params) + 1}")
    params.append(owner_user_id)
    conditions.append(f"tenant_id = ${len(params) + 1}")
    params.append(tenant_id)
    if min_quality is not None:
        conditions.append(f"quality_score >= ${len(params) + 1}")
        params.append(min_quality)
    # LIMIT bind is the final positional param.
    params.append(limit)
    sql = (
        "SELECT *, ts_rank(tsv, plainto_tsquery('english', $1)) AS rank "
        "FROM episodic_memory WHERE " + " AND ".join(conditions)
        + f" ORDER BY rank DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [_episodic_row_to_dict(r) for r in rows]


async def get_episodic_memory(conn, memory_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM episodic_memory WHERE id = $1", memory_id,
    )
    return _episodic_row_to_dict(row) if row else None


async def list_episodic_memories(
    conn, soc_vendor: str = "", limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if soc_vendor:
        conditions.append(f"soc_vendor = ${len(params) + 1}")
        params.append(soc_vendor)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT * FROM episodic_memory"
        + where
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [_episodic_row_to_dict(r) for r in rows]


async def delete_episodic_memory(conn, memory_id: str) -> bool:
    # SP-3.12: no FTS5 virtual-table "magic delete" row needed — the
    # STORED tsv column disappears with the row.
    status = await conn.execute(
        "DELETE FROM episodic_memory WHERE id = $1", memory_id,
    )
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def episodic_memory_count(conn) -> int:
    n = await conn.fetchval("SELECT COUNT(*) FROM episodic_memory")
    return int(n) if n is not None else 0


def _episodic_row_to_dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("tags"), str):
        d["tags"] = json.loads(d["tags"])
    # Strip the tsv column (bytes/PG type — not JSON-serialisable
    # and not part of the public API) and the ts_rank alias when
    # present (only set by search_episodic_memory).
    d.pop("tsv", None)
    d.pop("rank", None)
    return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Durable provenance snapshots (U6-0 T9/T10 G0a)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_provenance_snapshot(
    conn,
    *,
    snapshot_id: str,
    tenant_id: str,
    model_call_id: str,
    request_id: str,
    records_json: str,
    omissions_json: str,
    manifest_digest: str,
    schema_version: str = "v1",
) -> bool:
    """Insert one immutable provenance snapshot, or idempotently no-op.

    ``snapshot_id`` is globally unique.  A same-id/same-manifest replay is
    harmless; a same-id/different-manifest replay is an integrity failure and
    never mutates the original row.
    """
    if not tenant_id or not model_call_id:
        raise ValueError("tenant_id and model_call_id must be non-empty")

    row = await conn.fetchrow(
        """INSERT INTO provenance_snapshots
           (snapshot_id, tenant_id, model_call_id, request_id, records,
            omissions, completeness, manifest_digest, schema_version)
           VALUES ($1, $2, $3, $4, $5::jsonb,
                   $6::jsonb, 'complete', $7, $8)
           ON CONFLICT (snapshot_id) DO NOTHING
           RETURNING snapshot_id""",
        snapshot_id,
        tenant_id,
        model_call_id,
        request_id,
        records_json,
        omissions_json,
        manifest_digest,
        schema_version,
    )
    if row is not None:
        return True

    existing = await conn.fetchrow(
        "SELECT manifest_digest FROM provenance_snapshots "
        "WHERE snapshot_id = $1",
        snapshot_id,
    )
    if existing is not None and existing["manifest_digest"] != manifest_digest:
        raise ValueError("snapshot manifest mismatch")
    return False


async def get_provenance_snapshot(
    conn,
    snapshot_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return a snapshot only within ``tenant_id``; fail closed if empty."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT snapshot_id, tenant_id, model_call_id, request_id, records, "
        "omissions, completeness, manifest_digest, schema_version, created_at "
        "FROM provenance_snapshots "
        "WHERE snapshot_id = $1 AND tenant_id = $2",
        snapshot_id,
        tenant_id,
    )
    return dict(row) if row is not None else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Durable prepared actions (U6-0 T9/T10 G2b)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def put_prepared_action(
    conn,
    *,
    action_instance_id: str,
    tenant_id: str,
    principal_type: str,
    actor_id: str,
    request_id: str,
    model_call_id: str,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    family: str,
    effect: str,
    canonical_target: str,
    args_hash: str,
    provenance_kind: str,
    model_snapshot_id: str | None,
    no_model_input_source: str | None,
    executable_args_json: str,
    human_rendering_json: str,
    prepared_action_digest: str,
    recovery_mode: str = "non_replayable",
) -> bool:
    """Insert once, accepting only same-id/same-digest replays."""
    if not tenant_id or not action_instance_id:
        raise ValueError("tenant_id and action_instance_id must be non-empty")

    row = await conn.fetchrow(
        """INSERT INTO prepared_actions
           (action_instance_id, tenant_id, principal_type, actor_id,
            request_id, model_call_id, adapter_namespace, tool_name,
            schema_version, family, effect, canonical_target, args_hash,
            provenance_kind, model_snapshot_id, no_model_input_source,
            executable_args, human_rendering, prepared_action_digest,
            recovery_mode)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                   $13, $14, $15, $16, $17::jsonb, $18::jsonb, $19, $20)
           ON CONFLICT (action_instance_id) DO NOTHING
           RETURNING action_instance_id""",
        action_instance_id,
        tenant_id,
        principal_type,
        actor_id,
        request_id,
        model_call_id,
        adapter_namespace,
        tool_name,
        schema_version,
        family,
        effect,
        canonical_target,
        args_hash,
        provenance_kind,
        model_snapshot_id,
        no_model_input_source,
        executable_args_json,
        human_rendering_json,
        prepared_action_digest,
        recovery_mode,
    )
    if row is not None:
        return True

    existing = await conn.fetchrow(
        "SELECT prepared_action_digest FROM prepared_actions "
        "WHERE action_instance_id = $1",
        action_instance_id,
    )
    if (
        existing is not None
        and existing["prepared_action_digest"] != prepared_action_digest
    ):
        raise ValueError("prepared_action digest mismatch")
    return False


async def get_prepared_action(
    conn,
    action_instance_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return a prepared action only within ``tenant_id``; fail closed."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT action_instance_id, tenant_id, principal_type, actor_id, "
        "request_id, model_call_id, adapter_namespace, tool_name, "
        "schema_version, family, effect, canonical_target, args_hash, "
        "provenance_kind, model_snapshot_id, no_model_input_source, "
        "executable_args, human_rendering, prepared_action_digest, "
        "recovery_mode, created_at "
        "FROM prepared_actions "
        "WHERE action_instance_id = $1 AND tenant_id = $2",
        action_instance_id,
        tenant_id,
    )
    return dict(row) if row is not None else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Durable challenges and action grants (U6-0 T9/T10 G4a-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def put_challenge(
    conn,
    *,
    challenge_id: str,
    tenant_id: str,
    action_instance_id: str,
    expires_at: str | datetime,
) -> bool:
    """Insert once, copying trusted identity from a prepared action."""
    if not tenant_id or not challenge_id or not action_instance_id:
        raise ValueError(
            "tenant_id, challenge_id, and action_instance_id must be non-empty"
        )

    row = await conn.fetchrow(
        """INSERT INTO challenges
           (challenge_id, tenant_id, action_instance_id, principal_type,
            actor_id, request_id, model_call_id, adapter_namespace, tool_name,
            schema_version, family, canonical_target, args_hash,
            provenance_kind, model_snapshot_id, no_model_input_source,
            prepared_action_digest, state, expires_at)
           SELECT $1, p.tenant_id, p.action_instance_id, p.principal_type,
                  p.actor_id, p.request_id, p.model_call_id,
                  p.adapter_namespace, p.tool_name, p.schema_version, p.family,
                  p.canonical_target, p.args_hash, p.provenance_kind,
                  p.model_snapshot_id, p.no_model_input_source,
                  p.prepared_action_digest, 'pending', $4
           FROM prepared_actions p
           WHERE p.tenant_id = $2 AND p.action_instance_id = $3
           ON CONFLICT (challenge_id) DO NOTHING
           RETURNING challenge_id""",
        challenge_id,
        tenant_id,
        action_instance_id,
        expires_at,
    )
    if row is not None:
        return True

    existing = await conn.fetchrow(
        "SELECT 1 FROM challenges WHERE challenge_id = $1 AND tenant_id = $2",
        challenge_id,
        tenant_id,
    )
    if existing is not None:
        return False
    raise ValueError("no prepared_action for challenge")


async def get_challenge(
    conn,
    challenge_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return a challenge only within ``tenant_id``; fail closed."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT challenge_id, tenant_id, action_instance_id, principal_type, "
        "actor_id, request_id, model_call_id, adapter_namespace, tool_name, "
        "schema_version, family, canonical_target, args_hash, provenance_kind, "
        "model_snapshot_id, no_model_input_source, prepared_action_digest, "
        "confirmer_actor, confirmer_principal_type, confirmed_at, "
        "confirmer_auth_event_id, confirm_reason, state, created_at, "
        "expires_at FROM challenges "
        "WHERE challenge_id = $1 AND tenant_id = $2",
        challenge_id,
        tenant_id,
    )
    return dict(row) if row is not None else None


async def get_action_grant(
    conn,
    grant_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return an action grant only within ``tenant_id``; fail closed."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT grant_id, tenant_id, challenge_id, action_instance_id, "
        "principal_type, actor_id, request_id, model_call_id, "
        "adapter_namespace, tool_name, schema_version, family, "
        "canonical_target, args_hash, provenance_kind, model_snapshot_id, "
        "no_model_input_source, prepared_action_digest, grant_issuer_source, "
        "idempotency_key, recovery_mode, state, created_at, expires_at "
        "FROM action_grants WHERE grant_id = $1 AND tenant_id = $2",
        grant_id,
        tenant_id,
    )
    return dict(row) if row is not None else None


# ── U6-2b: L2 chat_session_summaries store (DORMANT, PG-native) ───────────────
_SUMMARY_COLS = (
    "id, tenant_id, user_id, session_id, source_watermark, source_message_hashes, "
    "summary_outcome, token_count, model_fingerprint, classifier_version, "
    "renderer_version, revision, session_end_reason, created_at"
)
_SESSION_END_REASONS = ("inactivity_timeout", "explicit_close")


async def insert_session_summary(
    conn,
    *,
    summary_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    source_watermark: str,
    source_message_hashes: list,
    summary_outcome: dict,
    token_count: int,
    model_fingerprint: str,
    classifier_version: int,
    renderer_version: int,
    revision: int,
    session_end_reason: str,
) -> bool:
    """Insert one L2 session summary keyed by the exactly-once job key
    ``(tenant_id, user_id, session_id, source_watermark)``. Returns True if
    inserted, False if that key already exists (idempotent — a re-run of the same
    watermark is a no-op). Never mutates an existing row (late turns take a new
    watermark = a new revision)."""
    if not (summary_id and tenant_id and user_id and session_id and source_watermark):
        raise ValueError(
            "summary_id, tenant_id, user_id, session_id, source_watermark must be non-empty"
        )
    if session_end_reason not in _SESSION_END_REASONS:
        raise ValueError(f"invalid session_end_reason: {session_end_reason!r}")
    row = await conn.fetchrow(
        """INSERT INTO chat_session_summaries
           (id, tenant_id, user_id, session_id, source_watermark,
            source_message_hashes, summary_outcome, token_count, model_fingerprint,
            classifier_version, renderer_version, revision, session_end_reason)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10, $11, $12, $13)
           ON CONFLICT (tenant_id, user_id, session_id, source_watermark) DO NOTHING
           RETURNING id""",
        summary_id,
        tenant_id,
        user_id,
        session_id,
        source_watermark,
        json.dumps(source_message_hashes),
        json.dumps(summary_outcome),
        int(token_count),
        model_fingerprint,
        int(classifier_version),
        int(renderer_version),
        int(revision),
        session_end_reason,
    )
    return row is not None


async def get_session_summary(
    conn,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    source_watermark: str,
) -> dict | None:
    """Return one summary by its exactly-once key, tenant+user scoped; fail closed."""
    if not (tenant_id and user_id):
        raise ValueError("tenant_id and user_id must be non-empty")
    row = await conn.fetchrow(
        f"SELECT {_SUMMARY_COLS} FROM chat_session_summaries "
        "WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3 AND source_watermark = $4",
        tenant_id,
        user_id,
        session_id,
        source_watermark,
    )
    return dict(row) if row is not None else None


async def list_session_summaries(
    conn,
    *,
    tenant_id: str,
    user_id: str,
    limit: int = 50,
) -> list[dict]:
    """List a user's summaries (newest first) — the UI 'what we did last time'
    surface. Tenant+user scoped; never crosses users."""
    if not (tenant_id and user_id):
        raise ValueError("tenant_id and user_id must be non-empty")
    rows = await conn.fetch(
        f"SELECT {_SUMMARY_COLS} FROM chat_session_summaries "
        "WHERE tenant_id = $1 AND user_id = $2 "
        "ORDER BY created_at DESC, revision DESC LIMIT $3",
        tenant_id,
        user_id,
        max(1, int(limit)),
    )
    return [dict(r) for r in rows]


async def latest_revision_for_session(
    conn,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> int:
    """The highest revision written for a session, or -1 if none — the writer
    assigns ``latest + 1`` when a late turn produces a new watermark."""
    if not (tenant_id and user_id and session_id):
        raise ValueError("tenant_id, user_id, session_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT COALESCE(MAX(revision), -1) AS r FROM chat_session_summaries "
        "WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3",
        tenant_id,
        user_id,
        session_id,
    )
    return int(row["r"]) if row is not None else -1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Challenge decision lifecycle (U6-0 T9/T10 G4b; PostgreSQL only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These helpers deliberately require asyncpg's transaction API and
# ``clock_timestamp()``.  The dormant SQLite development path never decides
# challenges.  All cross-worker coordination is durable PostgreSQL state; no
# module-global state is read or written here.

async def confirm_challenge(
    conn,
    *,
    tenant_id: str,
    challenge_id: str,
    confirmer_actor: str,
    confirmer_principal_type: str,
    confirmer_auth_event_id: str,
    reason: str,
    grant_id: str,
    resume_id: str,
    grant_expires_at: str | datetime,
) -> str:
    """Confirm one live challenge and issue its grant and resume job.

    PostgreSQL only: the pending-state CAS, strict inserts, and live wall clock
    run in one asyncpg transaction.  Operation identity and recovery policy
    are copied from the immutable prepared action, never from caller input.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "UPDATE challenges SET state = 'confirmed', "
            "confirmer_actor = $3, confirmer_principal_type = $4, "
            "confirmer_auth_event_id = $5, "
            "confirmed_at = clock_timestamp(), confirm_reason = $6 "
            "WHERE tenant_id = $1 AND challenge_id = $2 "
            "AND state = 'pending' AND expires_at > clock_timestamp() "
            "RETURNING action_instance_id",
            tenant_id,
            challenge_id,
            confirmer_actor,
            confirmer_principal_type,
            confirmer_auth_event_id,
            reason,
        )
        if row is None:
            return "not_confirmable"
        action_instance_id = row["action_instance_id"]

        grant = await conn.fetchrow(
            """INSERT INTO action_grants
               (grant_id, tenant_id, challenge_id, action_instance_id,
                principal_type, actor_id, request_id, model_call_id,
                adapter_namespace, tool_name, schema_version, family,
                canonical_target, args_hash, provenance_kind,
                model_snapshot_id, no_model_input_source,
                prepared_action_digest, recovery_mode, grant_issuer_source,
                idempotency_key, state, expires_at)
               SELECT $1, p.tenant_id, $2, p.action_instance_id,
                      p.principal_type, p.actor_id, p.request_id,
                      p.model_call_id, p.adapter_namespace, p.tool_name,
                      p.schema_version, p.family, p.canonical_target,
                      p.args_hash, p.provenance_kind, p.model_snapshot_id,
                      p.no_model_input_source, p.prepared_action_digest,
                      p.recovery_mode, 'ui_confirm',
                      CASE WHEN p.recovery_mode = 'sink_idempotency_key'
                           THEN p.action_instance_id ELSE NULL END,
                      'pending', $3
               FROM prepared_actions p
               WHERE p.tenant_id = $4 AND p.action_instance_id = $5
               RETURNING grant_id""",
            grant_id,
            challenge_id,
            grant_expires_at,
            tenant_id,
            action_instance_id,
        )
        if grant is None:
            raise RuntimeError("confirm_challenge: prepared_action missing")

        resume = await conn.fetchrow(
            """INSERT INTO resume_jobs
               (resume_id, tenant_id, grant_id, action_instance_id, state)
               VALUES ($1, $2, $3, $4, 'queued')
               RETURNING resume_id""",
            resume_id,
            tenant_id,
            grant_id,
            action_instance_id,
        )
        if resume is None:
            raise RuntimeError("confirm_challenge: resume enqueue failed")
        return "confirmed"


async def auto_grant_from_prepared(
    conn,
    *,
    tenant_id: str,
    action_instance_id: str,
    challenge_id: str,
    grant_id: str,
    resume_id: str,
    confirmer_actor: str,
    confirmer_principal_type: str,
    confirmer_auth_event_id: str,
    reason: str,
    grant_expires_at: str | datetime,
    challenge_expires_at: str | datetime,
) -> str:
    """U6-0 B-autoauth (AA-2): server-side auto-authorization grant.

    Create an ALREADY-CONFIRMED challenge (synthetic server confirmer), issue its
    grant with ``grant_issuer_source='server_auto_grant'``, and enqueue the
    resume job — one PostgreSQL transaction.  Operation identity, target, args,
    and recovery policy are COPIED from the immutable ``prepared_actions`` row
    (``SELECT p.*``), NEVER from caller/model input; the caller supplies only the
    synthetic confirmer + fresh ids.  This is an INTEGRITY property (no identity
    substitution), NOT content safety — the auto-auth policy (AA-1) is what
    decides the action is safe to grant.  A near-clone of ``put_challenge`` +
    ``confirm_challenge`` (no new trust surface).  PostgreSQL only.
    """
    if not (
        tenant_id
        and action_instance_id
        and challenge_id
        and grant_id
        and resume_id
    ):
        raise ValueError(
            "tenant_id/action_instance_id/challenge_id/grant_id/resume_id "
            "must be non-empty"
        )
    if not reason:
        raise ValueError("reason must be non-empty")
    async with conn.transaction():
        chal = await conn.fetchrow(
            """INSERT INTO challenges
               (challenge_id, tenant_id, action_instance_id, principal_type,
                actor_id, request_id, model_call_id, adapter_namespace, tool_name,
                schema_version, family, canonical_target, args_hash,
                provenance_kind, model_snapshot_id, no_model_input_source,
                prepared_action_digest, confirmer_actor, confirmer_principal_type,
                confirmed_at, confirmer_auth_event_id, confirm_reason, state,
                expires_at)
               SELECT $1, p.tenant_id, p.action_instance_id, p.principal_type,
                      p.actor_id, p.request_id, p.model_call_id,
                      p.adapter_namespace, p.tool_name, p.schema_version, p.family,
                      p.canonical_target, p.args_hash, p.provenance_kind,
                      p.model_snapshot_id, p.no_model_input_source,
                      p.prepared_action_digest, $5, $6, clock_timestamp(), $7, $8,
                      'confirmed', $4
               FROM prepared_actions p
               WHERE p.tenant_id = $2 AND p.action_instance_id = $3
               RETURNING challenge_id""",
            challenge_id,
            tenant_id,
            action_instance_id,
            challenge_expires_at,
            confirmer_actor,
            confirmer_principal_type,
            confirmer_auth_event_id,
            reason,
        )
        if chal is None:
            raise RuntimeError("auto_grant_from_prepared: prepared_action missing")

        grant = await conn.fetchrow(
            """INSERT INTO action_grants
               (grant_id, tenant_id, challenge_id, action_instance_id,
                principal_type, actor_id, request_id, model_call_id,
                adapter_namespace, tool_name, schema_version, family,
                canonical_target, args_hash, provenance_kind,
                model_snapshot_id, no_model_input_source,
                prepared_action_digest, recovery_mode, grant_issuer_source,
                idempotency_key, state, expires_at)
               SELECT $1, p.tenant_id, $2, p.action_instance_id,
                      p.principal_type, p.actor_id, p.request_id,
                      p.model_call_id, p.adapter_namespace, p.tool_name,
                      p.schema_version, p.family, p.canonical_target,
                      p.args_hash, p.provenance_kind, p.model_snapshot_id,
                      p.no_model_input_source, p.prepared_action_digest,
                      p.recovery_mode, 'server_auto_grant',
                      CASE WHEN p.recovery_mode = 'sink_idempotency_key'
                           THEN p.action_instance_id ELSE NULL END,
                      'pending', $3
               FROM prepared_actions p
               WHERE p.tenant_id = $4 AND p.action_instance_id = $5
               RETURNING grant_id""",
            grant_id,
            challenge_id,
            grant_expires_at,
            tenant_id,
            action_instance_id,
        )
        if grant is None:
            raise RuntimeError("auto_grant_from_prepared: prepared_action missing (grant)")

        resume = await conn.fetchrow(
            """INSERT INTO resume_jobs
               (resume_id, tenant_id, grant_id, action_instance_id, state)
               VALUES ($1, $2, $3, $4, 'queued')
               RETURNING resume_id""",
            resume_id,
            tenant_id,
            grant_id,
            action_instance_id,
        )
        if resume is None:
            raise RuntimeError("auto_grant_from_prepared: resume enqueue failed")
        return "auto_granted"


async def reject_challenge(
    conn,
    *,
    tenant_id: str,
    challenge_id: str,
    confirmer_actor: str,
    confirmer_principal_type: str,
    confirmer_auth_event_id: str,
    reason: str,
) -> str:
    """Reject one live pending challenge without issuing work (PG-only)."""
    async with conn.transaction():
        row = await conn.fetchrow(
            "UPDATE challenges SET state = 'rejected', "
            "confirmer_actor = $3, confirmer_principal_type = $4, "
            "confirmer_auth_event_id = $5, "
            "confirmed_at = clock_timestamp(), confirm_reason = $6 "
            "WHERE tenant_id = $1 AND challenge_id = $2 "
            "AND state = 'pending' AND expires_at > clock_timestamp() "
            "RETURNING challenge_id",
            tenant_id,
            challenge_id,
            confirmer_actor,
            confirmer_principal_type,
            confirmer_auth_event_id,
            reason,
        )
        if row is None:
            return "not_rejectable"
        return "rejected"


async def expire_stale(conn) -> dict[str, int]:
    """Expire all pending challenges and grants past wall time (PG-only)."""
    async with conn.transaction():
        challenge_tag = await conn.execute(
            "UPDATE challenges SET state = 'expired' "
            "WHERE state = 'pending' "
            "AND expires_at <= clock_timestamp()"
        )
        grant_tag = await conn.execute(
            "UPDATE action_grants SET state = 'expired' "
            "WHERE state = 'pending' "
            "AND expires_at <= clock_timestamp()"
        )
        return {
            "challenges": int(challenge_tag.split()[-1]),
            "grants": int(grant_tag.split()[-1]),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Durable resume jobs, execution attempts, and results (U6-0 T9/T10 G4a/G5a)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def put_resume_job(
    conn,
    *,
    resume_id: str,
    tenant_id: str,
    grant_id: str,
    action_instance_id: str,
) -> bool:
    """Insert one dormant resume job, or idempotently no-op."""
    if not tenant_id or not resume_id:
        raise ValueError("tenant_id and resume_id must be non-empty")

    row = await conn.fetchrow(
        """INSERT INTO resume_jobs
           (resume_id, tenant_id, grant_id, action_instance_id)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (resume_id) DO NOTHING
           RETURNING resume_id""",
        resume_id,
        tenant_id,
        grant_id,
        action_instance_id,
    )
    return row is not None


async def get_resume_job(
    conn,
    resume_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return a resume job only within ``tenant_id``; fail closed."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT resume_id, tenant_id, grant_id, action_instance_id, state, "
        "lease_owner, lease_expires_at, created_at FROM resume_jobs "
        "WHERE resume_id = $1 AND tenant_id = $2",
        resume_id,
        tenant_id,
    )
    return dict(row) if row is not None else None


async def lease_next_resume_job(
    conn,
    *,
    worker_id: str,
    lease_ttl_seconds: int,
) -> dict | None:
    """Lease the oldest runnable resume job with a fresh fence (PG-only).

    Call on an autocommit connection so the lease is durable before work uses
    other pooled connections.
    """
    if not worker_id:
        raise ValueError("worker_id must be non-empty")
    if lease_ttl_seconds <= 0:
        raise ValueError("lease_ttl_seconds must be greater than zero")

    row = await conn.fetchrow(
        """UPDATE resume_jobs
           SET state = 'claimed', lease_owner = $1,
               lease_epoch = lease_epoch + 1,
               lease_expires_at = clock_timestamp()
                   + ($2 * interval '1 second')
           WHERE resume_id = (
               SELECT resume_id FROM resume_jobs
               WHERE state = 'queued'
                  OR (state = 'claimed'
                      AND lease_expires_at < clock_timestamp())
               ORDER BY created_at
               FOR UPDATE SKIP LOCKED
               LIMIT 1
           )
           RETURNING resume_id, tenant_id, grant_id, action_instance_id,
                     lease_owner, lease_epoch""",
        worker_id,
        lease_ttl_seconds,
    )
    return dict(row) if row is not None else None


async def finalize_resume(
    conn,
    *,
    resume_id: str,
    tenant_id: str,
    lease_owner: str,
    lease_epoch: int,
    new_state: str,
) -> bool:
    """Apply a claimed resume transition only while its lease fence is held.

    PostgreSQL only.  A false result means the lease was lost or the job is
    no longer claimed; callers must not retry the stale transition blindly.
    """
    if new_state not in {"done", "manual", "failed", "queued"}:
        raise ValueError(f"invalid resume final state: {new_state}")

    row = await conn.fetchrow(
        "UPDATE resume_jobs SET state = $5 "
        "WHERE resume_id = $1 AND tenant_id = $2 AND lease_owner = $3 "
        "AND lease_epoch = $4 AND state = 'claimed' "
        "RETURNING resume_id",
        resume_id,
        tenant_id,
        lease_owner,
        lease_epoch,
        new_state,
    )
    return row is not None


async def put_execution_result(
    conn,
    *,
    grant_id: str,
    tenant_id: str,
    result_json: str,
    ambiguous: bool,
) -> bool:
    """Insert once, accepting only same-grant/same-result replays."""
    if not tenant_id or not grant_id:
        raise ValueError("tenant_id and grant_id must be non-empty")

    row = await conn.fetchrow(
        """INSERT INTO execution_results
           (grant_id, tenant_id, result, ambiguous)
           VALUES ($1, $2, $3::jsonb, $4)
           ON CONFLICT (grant_id) DO NOTHING
           RETURNING grant_id""",
        grant_id,
        tenant_id,
        result_json,
        ambiguous,
    )
    if row is not None:
        return True

    existing = await conn.fetchrow(
        "SELECT result FROM execution_results "
        "WHERE grant_id = $1 AND tenant_id = $2",
        grant_id,
        tenant_id,
    )
    if (
        existing is not None
        and json.loads(existing["result"]) != json.loads(result_json)
    ):
        raise ValueError("execution_result mismatch")
    return False


async def get_execution_result(
    conn,
    grant_id: str,
    *,
    tenant_id: str,
) -> dict | None:
    """Return an execution result only within ``tenant_id``; fail closed."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    row = await conn.fetchrow(
        "SELECT grant_id, tenant_id, result, ambiguous, completed_at "
        "FROM execution_results WHERE grant_id = $1 AND tenant_id = $2",
        grant_id,
        tenant_id,
    )
    if row is None:
        return None
    result = dict(row)
    result["ambiguous"] = bool(result["ambiguous"])
    return result


async def put_execution_attempt(
    conn,
    *,
    attempt_id: str,
    tenant_id: str,
    grant_id: str,
    outcome: str,
    evidence: str = "",
    error: str = "",
    lease_owner: str = "",
    lease_epoch: int = 0,
) -> bool:
    """Append one execution attempt, idempotently keyed by attempt ID."""
    if not tenant_id or not attempt_id or not grant_id:
        raise ValueError("tenant_id, attempt_id, and grant_id must be non-empty")

    row = await conn.fetchrow(
        """INSERT INTO execution_attempts
           (attempt_id, tenant_id, grant_id, outcome, evidence, error,
            lease_owner, lease_epoch)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (attempt_id) DO NOTHING
           RETURNING attempt_id""",
        attempt_id,
        tenant_id,
        grant_id,
        outcome,
        evidence,
        error,
        lease_owner,
        lease_epoch,
    )
    return row is not None


async def get_execution_attempts(
    conn,
    grant_id: str,
    *,
    tenant_id: str,
) -> list[dict]:
    """Return a grant's execution attempts only within ``tenant_id``."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    rows = await conn.fetch(
        "SELECT attempt_id, grant_id, tenant_id, outcome, evidence, error, "
        "lease_owner, lease_epoch, created_at FROM execution_attempts "
        "WHERE grant_id = $1 AND tenant_id = $2 ORDER BY created_at",
        grant_id,
        tenant_id,
    )
    return [dict(row) for row in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Chat messages (Q.3-SUB-6 #297)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Replaces the pre-Q.3 ``_history: list`` module-global in
# ``backend/routers/chat.py``. Three reads:
#   * ``list_chat_messages`` — the ``/chat/history`` snapshot call.
#   * ``insert_chat_message`` — append on POST /chat + /chat/stream.
#   * ``prune_chat_messages`` — 30-day-per-user retention sweep.
#   * ``clear_chat_messages`` — DELETE /chat/history.
#
# Module-global audit (SOP Step 1, 2026-04-21 rule):
#   Pool-native via ``asyncpg.Connection``; no module-global state
#   added to ``backend.db`` by this port. The SQLite fallback path
#   uses ``db._conn()`` which is the same per-process connection
#   every other SQLite helper uses. See the Q.3-SUB-6 commit for
#   the cross-worker story (Redis pub/sub fan-out via
#   ``bus.publish``).

RETENTION_DAYS = 30
_RETENTION_SECONDS = RETENTION_DAYS * 86400


async def insert_chat_message(conn, msg: dict) -> None:
    """Insert a single chat_messages row.

    ``msg`` shape mirrors :class:`backend.models.OrchestratorMessage`
    plus ``user_id`` + ``session_id`` (the caller supplies both from
    the request context).
    """
    await conn.execute(
        "INSERT INTO chat_messages (id, user_id, session_id, role, "
        "content, timestamp, tenant_id) VALUES "
        "($1, $2, $3, $4, $5, $6, $7)",
        msg["id"],
        msg["user_id"],
        msg.get("session_id", "") or "",
        msg["role"],
        msg["content"],
        float(msg["timestamp"]),
        msg.get("tenant_id") or tenant_insert_value(),
    )


async def list_chat_messages(
    conn, user_id: str, *, session_id: str | None = None, limit: int = 200,
) -> list[dict]:
    """Return the most-recent ``limit`` messages for ``user_id`` in
    chronological (oldest-first) order so the chat UI can ``setMessages``
    directly without reversing.

    Pre-Q.3 the handler returned the module-global list ordered by
    append-order (chronological). We preserve that contract here.
    Tenant scope is enforced via :func:`tenant_where_pg` so a
    cross-tenant token can't read another tenant's chat log even if
    the ``user_id`` matched.

    When ``session_id`` is supplied the result is additionally pinned
    to that conversation thread — this is the path the orchestrator
    uses to load *just the current session's* prior turns as LLM
    context (Gap-A memory wiring, dogfood 2026-06-30), so unrelated
    threads under the same user don't bleed into the prompt. The
    history view (``GET /chat/history``) keeps calling without it and
    still gets the whole user log.
    """
    conditions: list[str] = ["user_id = $1"]
    params: list = [user_id]
    if session_id:
        params.append(session_id)
        conditions.append(f"session_id = ${len(params)}")
    tenant_where_pg(conditions, params)
    params.append(int(limit))
    sql = (
        "SELECT id, user_id, session_id, role, content, timestamp, "
        "tenant_id FROM chat_messages WHERE "
        + " AND ".join(conditions)
        + " ORDER BY timestamp DESC LIMIT $" + str(len(params))
    )
    rows = await conn.fetch(sql, *params)
    # Flip to oldest-first for the UI.
    out: list[dict] = [dict(r) for r in reversed(rows)]
    return out


async def clear_chat_messages(conn, user_id: str) -> int:
    """DELETE /chat/history — wipe all messages for ``user_id``.

    Returns the number of rows deleted. Tenant-scoped via
    :func:`tenant_where_pg` so admins can't trigger cross-tenant
    deletes by swapping user_id.
    """
    conditions: list[str] = ["user_id = $1"]
    params: list = [user_id]
    tenant_where_pg(conditions, params)
    sql = "DELETE FROM chat_messages WHERE " + " AND ".join(conditions)
    status = await conn.execute(sql, *params)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def prune_chat_messages(conn, user_id: str, *, days: int = RETENTION_DAYS) -> int:
    """30-day retention sweep. Called opportunistically after every
    INSERT so the table bounds itself without a dedicated cron job.

    A best-effort write amplification: ~one extra indexed-range DELETE
    per append, scoped to rows strictly older than ``days``. On a
    user with no stale rows the DELETE is a no-op (index seek + empty
    range) — cheap enough to be tolerable on the hot path.
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    conditions: list[str] = ["user_id = $1", "timestamp < $2"]
    params: list = [user_id, cutoff]
    tenant_where_pg(conditions, params)
    sql = "DELETE FROM chat_messages WHERE " + " AND ".join(conditions)
    status = await conn.execute(sql, *params)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  orchestrator_tasks (Gap C — chat-filed ticket delivery loop, 0252)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def upsert_orchestrator_task(conn, data: dict) -> None:
    """Record (or refresh) the user↔ticket link for a chat-filed Story.

    Keyed on ``ticket_key`` (unique) so a re-file of the same ticket is an
    upsert, not a duplicate. Timestamps are app-set epoch seconds.
    """
    await conn.execute(
        "INSERT INTO orchestrator_tasks "
        "(id, tenant_id, user_id, session_id, ticket_key, title, area, "
        " browse_url, status, last_jira_status, filed_at) VALUES "
        "($1,$2,$3,$4,$5,$6,$7,$8,'open','',$9) "
        "ON CONFLICT (ticket_key) DO UPDATE SET "
        "  user_id=EXCLUDED.user_id, session_id=EXCLUDED.session_id, "
        "  title=EXCLUDED.title, area=EXCLUDED.area, "
        "  browse_url=EXCLUDED.browse_url",
        data["id"],
        data.get("tenant_id") or tenant_insert_value(),
        data["user_id"],
        data.get("session_id", "") or "",
        data["ticket_key"],
        data.get("title", "") or "",
        data.get("area", "") or "",
        data.get("browse_url", "") or "",
        float(data.get("filed_at") or 0),
    )


async def find_recent_orchestrator_task_by_title(
    conn, *, tenant_id: str, session_id: str, title: str, since: float,
) -> dict | None:
    """Cross-turn idempotency for chat-filed Stories (audit r2 codex#2).

    Return the most recent orchestrator-filed row with the SAME exact title in
    the SAME tenant + session filed at/after ``since`` (epoch seconds), else
    None. Lets create_task return an already-filed ticket instead of creating a
    duplicate when the user refreshes / double-submits / says "file that again"
    within the window. Deliberately tight (exact title + same session) so it
    never swallows a genuinely-distinct request.
    """
    row = await conn.fetchrow(
        "SELECT ticket_key, browse_url, title FROM orchestrator_tasks "
        "WHERE tenant_id=$1 AND session_id=$2 AND title=$3 AND filed_at >= $4 "
        "ORDER BY filed_at DESC LIMIT 1",
        tenant_id, session_id or "", title, float(since),
    )
    return dict(row) if row else None


async def list_open_orchestrator_tasks(conn, *, limit: int = 200) -> list[dict]:
    """Return still-open chat-filed tickets for the delivery poller.

    NOT tenant-scoped: the poller is a system process that must see every
    tenant's open rows (each row already carries its own tenant_id/user_id
    for the fan-out target).
    """
    rows = await conn.fetch(
        "SELECT id, tenant_id, user_id, session_id, ticket_key, title, "
        "browse_url, last_jira_status FROM orchestrator_tasks "
        "WHERE status = 'open' ORDER BY filed_at ASC LIMIT $1",
        int(limit),
    )
    return [dict(r) for r in rows]


async def claim_orchestrator_task_delivery(conn, ticket_key: str, *, at: float) -> bool:
    """Atomically flip an open row → delivered. Returns True iff THIS caller
    won the claim (so exactly one backend replica delivers the message).
    """
    row = await conn.fetchrow(
        "UPDATE orchestrator_tasks SET status='delivered', delivered_at=$2 "
        "WHERE ticket_key=$1 AND status='open' RETURNING id",
        ticket_key, float(at),
    )
    return row is not None


async def update_orchestrator_task_jira_status(
    conn, ticket_key: str, jira_status: str,
) -> None:
    """Best-effort cache of the last-seen JIRA status (observability only)."""
    await conn.execute(
        "UPDATE orchestrator_tasks SET last_jira_status=$2 "
        "WHERE ticket_key=$1 AND status='open'",
        ticket_key, jira_status or "",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  proposed_actions — Sora P5 propose-and-approve gate (0256)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_proposed_action(conn, data: dict) -> None:
    """Persist a PENDING dangerous-action proposal. Sora NEVER executes — a human
    must approve first. Durable so a proposal survives a restart (a human may
    decide minutes later)."""
    await conn.execute(
        "INSERT INTO proposed_actions "
        "(id, tenant_id, user_id, session_id, action_kind, params, title, "
        " preview, blast_radius, status, proposed_by, proposed_at) VALUES "
        "($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending',$10,$11)",
        data["id"],
        data.get("tenant_id") or "",
        data.get("user_id") or "",
        data.get("session_id") or "",
        data["action_kind"],
        data.get("params") or "{}",
        data.get("title") or "",
        data.get("preview") or "",
        data.get("blast_radius") or "",
        data.get("proposed_by") or "sora",
        float(data.get("proposed_at") or 0),
    )


async def get_proposed_action(conn, action_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, action_kind, params, title, preview, blast_radius, status, "
        "proposed_by, proposed_at, decided_by, decided_at, decision_reason, "
        "executed_at, result FROM proposed_actions WHERE id=$1",
        action_id,
    )
    return dict(row) if row else None


async def list_proposed_actions(conn, *, status: str | None = None, limit: int = 50) -> list[dict]:
    """List proposals, newest-first, optionally filtered by status (e.g. 'pending')."""
    if status:
        rows = await conn.fetch(
            "SELECT id, action_kind, title, blast_radius, status, proposed_by, "
            "proposed_at, decided_by, decision_reason FROM proposed_actions WHERE status=$1 "
            "ORDER BY proposed_at DESC LIMIT $2",
            status, int(limit),
        )
    else:
        rows = await conn.fetch(
            "SELECT id, action_kind, title, blast_radius, status, proposed_by, "
            "proposed_at, decided_by, decision_reason FROM proposed_actions "
            "ORDER BY proposed_at DESC LIMIT $1",
            int(limit),
        )
    return [dict(r) for r in rows]


async def decide_proposed_action(
    conn, action_id: str, *, decision: str, decided_by: str, at: float, reason: str = "",
) -> bool:
    """Operator gate: flip a PENDING proposal → 'approved' or 'rejected'. Atomic
    (only transitions from 'pending', so a double-decide / race can't re-decide).
    Persists the operator's ``reason`` in the same write for durable audit
    (r3 fix). Returns True iff THIS caller won the transition."""
    if decision not in {"approved", "rejected"}:
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
    row = await conn.fetchrow(
        "UPDATE proposed_actions SET status=$2, decided_by=$3, decided_at=$4, "
        "decision_reason=$5 WHERE id=$1 AND status='pending' RETURNING id",
        action_id, decision, decided_by or "", float(at), (reason or "")[:1000],
    )
    return row is not None


async def mark_proposed_action_executing(conn, action_id: str, *, at: float) -> None:
    """Write-ahead the 'executing' intent BEFORE the real side effect (r3 SR-1),
    so a crash in the restart window leaves a recoverable 'executing' row rather
    than an ambiguous 'approved' one. Only flips an 'approved' row."""
    await conn.execute(
        "UPDATE proposed_actions SET status='executing', executed_at=$2 "
        "WHERE id=$1 AND status='approved'",
        action_id, float(at),
    )


async def set_proposed_action_result(
    conn, action_id: str, *, status: str, result: str, at: float,
    expected_prior: str | None = None,
) -> None:
    """Record an execution outcome ('executing'/'executed'/'failed'/'refused'…).
    Writes ``executed_at`` (NOT decided_at, r3 RS-2 — decided_at stays the durable
    operator-decision time). If ``expected_prior`` is given, this is a compare-and-
    set that only writes when the row is still in that status (r5 CIR-02 — so the
    container's terminal write can never clobber a host-written terminal)."""
    if expected_prior is not None:
        await conn.execute(
            "UPDATE proposed_actions SET status=$2, result=$3, executed_at=$4 "
            "WHERE id=$1 AND status=$5",
            action_id, status, (result or "")[:2000], float(at), expected_prior,
        )
    else:
        await conn.execute(
            "UPDATE proposed_actions SET status=$2, result=$3, executed_at=$4 WHERE id=$1",
            action_id, status, (result or "")[:2000], float(at),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Chat sessions (ZZ.B2 #304-2, checkbox 1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# One row per originator session hash that has produced at least one
# chat_messages row. Stores a JSON metadata blob the UI reads for the
# left-sidebar title (``auto_title`` LLM-generated; ``user_title``
# operator-set takes precedence; raw ``session_id[:8]`` hash is the
# fallback owned entirely by the frontend).
#
# Module-global audit (SOP Step 1): pool-native asyncpg; no module
# state added. Tenant scope enforced via ``tenant_where_pg`` /
# ``tenant_insert_value`` mirroring ``chat_messages``. The 3-turn
# trigger in ``backend.routers.chat._maybe_schedule_auto_title`` is
# advisory — at-most-once per session is enforced by the
# ``auto_title`` field already being present (checked under a PG
# advisory-ish conditional UPDATE, see ``set_session_auto_title``).

import json as _json_chat_sessions


async def upsert_chat_session(
    conn,
    *,
    session_id: str,
    user_id: str,
    now: float | None = None,
) -> None:
    """Idempotent insert-or-touch for a chat session.

    Called from the chat router on every persisted ``chat_messages``
    write so the ``chat_sessions`` row is guaranteed to exist by the
    time the 3-user-turn trigger fires. ``updated_at`` is refreshed on
    every call so the sidebar can order sessions by recency. The
    ``metadata`` column is left untouched — only ``set_session_auto_title``
    / user-title writers mutate it.
    """
    import time as _time
    ts = _time.time() if now is None else float(now)
    tenant = tenant_insert_value()
    await conn.execute(
        "INSERT INTO chat_sessions (session_id, user_id, tenant_id, "
        "metadata, created_at, updated_at) VALUES "
        "($1, $2, $3, '{}'::jsonb, $4, $5) "
        "ON CONFLICT (session_id, user_id, tenant_id) "
        "DO UPDATE SET updated_at = EXCLUDED.updated_at",
        session_id,
        user_id,
        tenant,
        ts,
        ts,
    )


async def count_user_turns_in_session(
    conn,
    *,
    session_id: str,
    user_id: str,
) -> int:
    """Count ``role='user'`` messages for a (user, session) pair.

    Drives the 3-user-turn trigger in
    ``backend.routers.chat._maybe_schedule_auto_title``. Tenant-scoped
    because ``chat_messages`` is tenant-scoped; a stray cross-tenant
    row must not tip the count over 3.
    """
    conditions: list[str] = ["user_id = $1", "session_id = $2", "role = $3"]
    params: list = [user_id, session_id, "user"]
    tenant_where_pg(conditions, params)
    sql = (
        "SELECT COUNT(*) AS c FROM chat_messages WHERE "
        + " AND ".join(conditions)
    )
    row = await conn.fetchrow(sql, *params)
    return int(row["c"]) if row else 0


async def get_chat_session_metadata(
    conn,
    *,
    session_id: str,
    user_id: str,
) -> dict | None:
    """Return the ``metadata`` dict for a session or ``None`` on miss.

    ``metadata`` is stored as JSONB on PG (decoded to dict by asyncpg)
    / TEXT-of-JSON on SQLite (decoded here). Returns ``None`` when the
    row doesn't exist so callers branch once.
    """
    conditions: list[str] = ["session_id = $1", "user_id = $2"]
    params: list = [session_id, user_id]
    tenant_where_pg(conditions, params)
    sql = (
        "SELECT metadata FROM chat_sessions WHERE "
        + " AND ".join(conditions)
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        return None
    raw = row["metadata"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return _json_chat_sessions.loads(raw) if raw else {}
        except ValueError:
            return {}
    return {}


async def list_chat_sessions_for_user(
    conn,
    user_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return the most-recent ``limit`` sessions for ``user_id``.

    Ordered by ``updated_at DESC`` (most-active first) to match the
    sidebar's top-of-list convention. ``metadata`` is decoded to a
    dict in every row regardless of backend dialect so the API
    endpoint + frontend don't need to probe.
    """
    conditions: list[str] = ["user_id = $1"]
    params: list = [user_id]
    tenant_where_pg(conditions, params)
    params.append(int(limit))
    sql = (
        "SELECT session_id, user_id, tenant_id, metadata, "
        "created_at, updated_at FROM chat_sessions WHERE "
        + " AND ".join(conditions)
        + " ORDER BY updated_at DESC LIMIT $" + str(len(params))
    )
    rows = await conn.fetch(sql, *params)
    out: list[dict] = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = _json_chat_sessions.loads(meta) if meta else {}
            except ValueError:
                meta = {}
        elif meta is None:
            meta = {}
        out.append({
            "session_id": r["session_id"],
            "user_id": r["user_id"],
            "tenant_id": r["tenant_id"],
            "metadata": meta,
            "created_at": float(r["created_at"]),
            "updated_at": float(r["updated_at"]),
        })
    return out


async def set_session_auto_title(
    conn,
    *,
    session_id: str,
    user_id: str,
    title: str,
) -> bool:
    """Write ``metadata.auto_title`` iff it hasn't been set yet.

    Returns ``True`` when a row was updated (this caller won the race
    to generate the title), ``False`` when the field was already
    present (another background task beat us to it, or this session
    already had an auto_title from a prior run). The race is covered
    by a conditional UPDATE that matches only rows where the JSONB
    ``auto_title`` field is missing, so concurrent 3-turn triggers
    from two uvicorn workers converge to one winner without an
    explicit advisory lock.

    ``user_title`` is deliberately untouched — if an operator has set
    it, we still record the auto_title alongside (so the system
    preserves both facets; the sidebar precedence rule lives in the
    UI per the checkbox-2 spec).
    """
    import time as _time
    title_clean = (title or "").strip()[:120]  # defensive cap on LLM output
    if not title_clean:
        return False
    ts = _time.time()
    # Condition: only set if auto_title is not yet present. The JSON
    # ``?`` operator tests key existence on JSONB; the NOT means "no
    # auto_title yet".
    conditions: list[str] = [
        "session_id = $1",
        "user_id = $2",
        "NOT (metadata ? 'auto_title')",
    ]
    params: list = [session_id, user_id]
    tenant_where_pg(conditions, params)
    params.extend([title_clean, ts])
    sql = (
        "UPDATE chat_sessions SET "
        "metadata = metadata || jsonb_build_object('auto_title', $"
        + str(len(params) - 1)
        + "::text), "
        "updated_at = $" + str(len(params))
        + " WHERE " + " AND ".join(conditions)
    )
    status = await conn.execute(sql, *params)
    try:
        affected = int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        affected = 0
    return affected > 0


async def set_session_user_title(
    conn,
    *,
    session_id: str,
    user_id: str,
    title: str | None,
) -> bool:
    """Write / clear ``metadata.user_title`` — operator-authored override.

    Unlike :func:`set_session_auto_title` (at-most-once, LLM-produced),
    the user title is editable: an empty / ``None`` value removes the
    key so the sidebar falls back to ``auto_title`` / hash per the
    ZZ.B2 checkbox-2 fallback chain. A non-empty value is upserted
    (``metadata || jsonb_build_object`` merges without touching other
    keys, notably preserving any ``auto_title`` the background task
    has already written).

    Returns ``True`` when the target row existed (and therefore the
    caller's intent was applied), ``False`` when no row matched —
    callers map that to a 404 response.

    Tenant-scoped via :func:`tenant_where_pg` so a swapped auth token
    cannot rename another tenant's session.
    """
    import time as _time
    ts = _time.time()
    cleaned = (title or "").strip()[:120]  # defensive cap mirroring auto-title
    conditions: list[str] = ["session_id = $1", "user_id = $2"]
    params: list = [session_id, user_id]
    tenant_where_pg(conditions, params)
    if cleaned:
        params.extend([cleaned, ts])
        sql = (
            "UPDATE chat_sessions SET "
            "metadata = metadata || jsonb_build_object('user_title', $"
            + str(len(params) - 1)
            + "::text), "
            "updated_at = $" + str(len(params))
            + " WHERE " + " AND ".join(conditions)
        )
    else:
        params.append(ts)
        sql = (
            "UPDATE chat_sessions SET "
            "metadata = metadata - 'user_title', "
            "updated_at = $" + str(len(params))
            + " WHERE " + " AND ".join(conditions)
        )
    status = await conn.execute(sql, *params)
    try:
        affected = int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        affected = 0
    return affected > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  User drafts (Q.6 #300, checkbox 1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Backs the 500 ms debounce write from the INVOKE command bar
# (``components/omnisight/invoke-core.tsx``) and the workspace chat
# composer (``components/omnisight/workspace-chat.tsx``). Three
# helpers cover the lifecycle:
#
#   * ``upsert_user_draft`` — PUT /user/drafts/{slot_key} write path,
#     idempotent ON CONFLICT. Last-writer-wins per the Q.6 conflict
#     spec ("draft is ephemeral, no optimistic lock").
#   * ``get_user_draft`` — GET /user/drafts/{slot_key} read path
#     (Q.6 checkbox 2). Returns ``None`` on miss instead of raising
#     so the new-device restore flow handles the empty case
#     branch-free.
#   * ``prune_user_drafts`` — 24 h retention sweep (Q.6 checkbox 3).
#     Called opportunistically after every PUT, same pattern as
#     ``prune_chat_messages`` so we don't need a dedicated cron.
#
# Module-global audit (SOP Step 1, 2026-04-21 rule): pool-native via
# ``asyncpg.Connection``; no module-global state added. Cross-worker
# safety is whatever PG gives us — UPSERT under read-committed
# isolation. The ephemeral / last-writer-wins contract makes a
# cross-tab race deliberately tolerated (acceptable answer #3 below
# the audit's three valid patterns: rate-limit-ish state intentionally
# cheap, with PG as the only durable source of truth).

DRAFT_RETENTION_SECONDS = 24 * 3600


async def upsert_user_draft(
    conn,
    user_id: str,
    slot_key: str,
    content: str,
    *,
    now: float | None = None,
) -> float:
    """Write (insert-or-replace) a draft slot. Returns the
    ``updated_at`` timestamp committed for the row so the caller can
    echo it back to the client (Q.6 checkbox 4 conflict-detection
    relies on the timestamp on restore).

    Tenant scope is captured via :func:`tenant_insert_value` on
    insert; updates do not touch ``tenant_id`` so a token swap
    cannot rebind an existing row to another tenant.
    """
    import time as _time
    ts = float(now if now is not None else _time.time())
    await conn.execute(
        "INSERT INTO user_drafts (user_id, slot_key, content, "
        "updated_at, tenant_id) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (user_id, slot_key) DO UPDATE SET "
        "  content = EXCLUDED.content, "
        "  updated_at = EXCLUDED.updated_at",
        user_id, slot_key, content, ts, tenant_insert_value(),
    )
    return ts


async def get_user_draft(
    conn,
    user_id: str,
    slot_key: str,
) -> dict | None:
    """Return ``{slot_key, content, updated_at}`` or ``None`` if
    no row exists for the (user, slot) pair.

    Tenant-scoped via :func:`tenant_where_pg` so a cross-tenant token
    cannot read another tenant's draft even if the ``user_id``
    happened to match (paranoid: the PK is (user_id, slot_key) so
    same-id-cross-tenant collision is theoretical, but the rest of
    the per-user helpers in this module enforce tenant scoping
    uniformly and we follow the convention).
    """
    conditions: list[str] = ["user_id = $1", "slot_key = $2"]
    params: list = [user_id, slot_key]
    tenant_where_pg(conditions, params)
    sql = (
        "SELECT slot_key, content, updated_at FROM user_drafts WHERE "
        + " AND ".join(conditions)
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        return None
    return {
        "slot_key": row["slot_key"],
        "content": row["content"],
        "updated_at": float(row["updated_at"]),
    }


async def prune_user_drafts(
    conn,
    *,
    older_than_seconds: int = DRAFT_RETENTION_SECONDS,
    now: float | None = None,
) -> int:
    """24 h retention sweep — drop rows whose ``updated_at`` is
    strictly older than ``older_than_seconds``. Called opportunistically
    after every PUT so the table bounds itself without a dedicated
    cron job. Same pattern as :func:`prune_chat_messages` — cheap
    indexed-range DELETE that is a no-op on a clean table.
    """
    import time as _time
    cutoff = float(now if now is not None else _time.time()) - older_than_seconds
    sql = "DELETE FROM user_drafts WHERE updated_at < $1"
    status = await conn.execute(sql, cutoff)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0
