"""Database persistence layer.

Originally SQLite-only via aiosqlite; Phase-3 Runtime (2026-04-20) adds
PostgreSQL support via an aiosqlite-compatible wrapper
(:mod:`backend.db_pg_compat`) so the 80+ ``_conn().execute(...)`` call
sites elsewhere in this file — and in ``tenant_secrets`` / ``audit`` /
``bootstrap`` / the dozen other modules that reach through ``_conn()``
— don't need to change. The wrapper translates SQLite-isms
(``INSERT OR IGNORE``, ``datetime('now')``, ``?`` placeholders) at
execute time on the PG path; SQLite connections are unchanged.

Dispatch on ``OMNISIGHT_DATABASE_URL``: empty / ``sqlite://`` →
aiosqlite.Connection; ``postgresql+asyncpg://...`` → PgCompatConnection.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from backend.db_context import (
    current_tenant_id,
    tenant_insert_value,
    tenant_where,
    tenant_where_pg,
)

logger = logging.getLogger(__name__)

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
            for prefix, canon in (
                ("postgresql+asyncpg://", "postgresql://"),
                ("postgres+asyncpg://", "postgresql://"),
                ("asyncpg://", "postgresql://"),
                ("postgres://", "postgresql://"),
            ):
                if low.startswith(prefix):
                    return canon + url[len(prefix):]
            return url  # already postgresql://
    return ""


_DB_PATH = _resolve_db_path()
# Typed as ``Any`` because it can hold either aiosqlite.Connection or
# PgCompatConnection depending on the runtime dispatch. The public
# surface ``_conn()`` + the 80 call sites are identical either way.
_db: Any = None
_IS_PG = False


async def init() -> None:
    """Open the database and create tables if they don't exist.

    On SQLite (default): create tables via CREATE TABLE IF NOT EXISTS,
    run ALTER TABLE ADD COLUMN migrations in ``_migrate()``, set WAL
    pragmas. This is the canonical schema path — alembic only catches
    up PG via migrations.

    On Postgres: the schema is owned by alembic. ``alembic upgrade
    head`` must have been run before this function fires. We skip all
    DDL (CREATE TABLE / _migrate() / FTS5 CREATE VIRTUAL TABLE / PRAGMA
    set) because (a) alembic already created it, (b) SQLite-specific
    statements wouldn't make sense on PG anyway.
    """
    global _db, _IS_PG
    pg_dsn = _resolve_pg_dsn()
    if pg_dsn:
        _IS_PG = True
        from backend.db_pg_compat import PgCompatConnection
        _db = await PgCompatConnection.open(pg_dsn)
        # Schema is alembic-managed on PG. We do NOT re-run CREATE TABLE
        # or _migrate here — the alembic upgrade_head that ran at deploy
        # time owns the schema. A sanity ping confirms reachability.
        async with await _db.execute("SELECT 1") as cur:
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("PG connection opened but SELECT 1 returned no row")
        logger.info("Database ready (PostgreSQL via asyncpg compat wrapper)")
        return

    _IS_PG = False
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
    """Add columns that may be missing in older databases."""
    # Collect existing columns per table
    migrations = [
        ("agents", "sub_type", "TEXT NOT NULL DEFAULT ''"),
        ("tasks", "suggested_sub_type", "TEXT"),
        ("tasks", "parent_task_id", "TEXT"),
        ("tasks", "child_task_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("tasks", "external_issue_id", "TEXT"),
        ("tasks", "issue_url", "TEXT"),
        ("tasks", "acceptance_criteria", "TEXT"),
        ("tasks", "labels", "TEXT NOT NULL DEFAULT '[]'"),
        ("tasks", "depends_on", "TEXT NOT NULL DEFAULT '[]'"),
        ("tasks", "external_issue_platform", "TEXT"),
        ("tasks", "last_external_sync_at", "TEXT"),
        # Pipeline linkage (Phase 46)
        ("tasks", "npi_phase_id", "TEXT"),
        ("notifications", "dispatch_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("notifications", "send_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("notifications", "last_error", "TEXT"),
        # Artifact version/checksum (Phase 39)
        ("artifacts", "version", "TEXT NOT NULL DEFAULT ''"),
        ("artifacts", "checksum", "TEXT NOT NULL DEFAULT ''"),
        # NPU simulation fields (Phase 36)
        ("simulations", "npu_latency_ms", "REAL NOT NULL DEFAULT 0.0"),
        ("simulations", "npu_throughput_fps", "REAL NOT NULL DEFAULT 0.0"),
        ("simulations", "accuracy_delta", "REAL NOT NULL DEFAULT 0.0"),
        ("simulations", "model_size_kb", "INTEGER NOT NULL DEFAULT 0"),
        ("simulations", "npu_framework", "TEXT NOT NULL DEFAULT ''"),
        # Phase 56-DAG-B — DAG planner ↔ workflow linkage.
        ("workflow_runs", "dag_plan_id", "INTEGER"),
        ("workflow_runs", "successor_run_id", "TEXT"),
        ("workflow_steps", "dag_task_id", "TEXT"),
        # Phase 63-E — Memory quality decay.
        ("episodic_memory", "decayed_score", "REAL NOT NULL DEFAULT 0.0"),
        ("episodic_memory", "last_used_at", "TEXT"),
        # S0 — session/audit enhancements.
        ("audit_log", "session_id", "TEXT"),
        ("sessions", "metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ("sessions", "mfa_verified", "INTEGER NOT NULL DEFAULT 0"),
        ("sessions", "rotated_from", "TEXT"),
        # K1 — force password change for default-credential admins.
        ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        # K2 — account lockout after consecutive login failures.
        ("users", "failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "locked_until", "REAL"),
        # K4 — session rotation + UA binding.
        ("sessions", "ua_hash", "TEXT NOT NULL DEFAULT ''"),
        # I1 — multi-tenancy: tenant_id on all business tables.
        ("users", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("workflow_runs", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("debug_findings", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("decision_rules", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("event_log", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("audit_log", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("artifacts", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        ("user_preferences", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
        # I4: tenant_id on api_keys
        ("api_keys", "tenant_id", "TEXT NOT NULL DEFAULT 't-default'"),
    ]
    # N6: critical columns the runtime hard-depends on. If post-migration
    # any of these are still missing, fail-fast at startup rather than
    # silently letting the ORM raise IntegrityError on every insert.
    REQUIRED = {("tasks", "npi_phase_id"), ("agents", "sub_type")}
    for table, column, typedef in migrations:
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
            logger.info("Migration: added %s.%s", table, column)
        except Exception as exc:
            if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                pass  # Column already exists — expected
            else:
                logger.warning("Migration %s.%s failed: %s", table, column, exc)

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
        # WAL checkpoint is SQLite-specific. PgCompatConnection handles
        # PRAGMA statements as no-ops so this loop is safe either way,
        # but we short-circuit on PG to skip the redundant PASSIVE
        # retry + the misleading "wal_checkpoint failed" log line.
        if not _IS_PG:
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
    """Execute raw SQL and return rows affected. For startup cleanup."""
    cur = await _conn().execute(sql, params)
    await _conn().commit()
    return cur.rowcount


def _conn() -> Any:
    """Return the open connection.

    Typed as ``Any`` because the return value is either
    ``aiosqlite.Connection`` (SQLite path) or
    ``PgCompatConnection`` (PG path). Both expose the same surface
    the rest of this module + the downstream modules
    (``tenant_secrets``, ``audit``, ``bootstrap``, ``dag_storage``,
    etc.) use: ``async with conn.execute(...) as cur``, ``commit()``,
    ``executescript()``.
    """
    if _db is None:
        raise RuntimeError("Database not initialized — call db.init() first")
    return _db


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCHEMA = """
-- I1: Multi-tenancy foundation
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    enabled     INTEGER NOT NULL DEFAULT 1
);

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
    labels              TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS task_comments (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
);

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
    tenant_id   TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
    task_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS token_usage (
    model           TEXT PRIMARY KEY,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost            REAL NOT NULL DEFAULT 0.0,
    request_count   INTEGER NOT NULL DEFAULT 0,
    avg_latency     INTEGER NOT NULL DEFAULT 0,
    last_used       TEXT NOT NULL DEFAULT ''
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
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

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
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    tenant_id           TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
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
    tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_oidc ON users(oidc_provider, oidc_subject);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    csrf_token      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    ip              TEXT NOT NULL DEFAULT '',
    user_agent      TEXT NOT NULL DEFAULT '',
    ua_hash         TEXT NOT NULL DEFAULT '',
    metadata        TEXT NOT NULL DEFAULT '{}',
    mfa_verified    INTEGER NOT NULL DEFAULT 0,
    rotated_from    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

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
    key_prefix      TEXT NOT NULL DEFAULT '',
    scopes          TEXT NOT NULL DEFAULT '["*"]',
    created_by      TEXT NOT NULL DEFAULT '',
    last_used_ip    TEXT,
    last_used_at    REAL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);

-- J4: user preferences (per-user key/value)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pref_key    TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    tenant_id   TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
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
-- cf_tunnel_configured / smoke_passed / finalized); `actor_user_id`
-- is the admin who advanced the wizard; `metadata` carries per-step
-- context (e.g. selected provider, tunnel id). Upsert-by-step keeps
-- the table idempotent so replaying a step refreshes its timestamp
-- rather than piling up duplicate rows.
CREATE TABLE IF NOT EXISTS bootstrap_state (
    step            TEXT PRIMARY KEY,
    completed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    actor_user_id   TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
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
    # Phase-3-Runtime-v2 SP-3.2: native asyncpg — 21 positional
    # placeholders ($1..$21), ON CONFLICT DO UPDATE using EXCLUDED.*.
    # Pool auto-commits each statement outside an explicit transaction
    # block — no explicit commit needed.
    await conn.execute(
        """INSERT INTO tasks (id, title, description, priority, status, assigned_agent_id,
             created_at, completed_at, ai_analysis, suggested_agent_type, suggested_sub_type,
             parent_task_id, child_task_ids, external_issue_id, issue_url, acceptance_criteria,
             labels, depends_on, external_issue_platform, last_external_sync_at, npi_phase_id)
           VALUES ($1, $2, $3, $4, $5, $6,
                   $7, $8, $9, $10, $11,
                   $12, $13, $14, $15, $16,
                   $17, $18, $19, $20, $21)
           ON CONFLICT (id) DO UPDATE SET
             title=EXCLUDED.title, description=EXCLUDED.description, priority=EXCLUDED.priority,
             status=EXCLUDED.status, assigned_agent_id=EXCLUDED.assigned_agent_id,
             completed_at=EXCLUDED.completed_at, ai_analysis=EXCLUDED.ai_analysis,
             suggested_agent_type=EXCLUDED.suggested_agent_type, suggested_sub_type=EXCLUDED.suggested_sub_type,
             parent_task_id=EXCLUDED.parent_task_id, child_task_ids=EXCLUDED.child_task_ids,
             external_issue_id=EXCLUDED.external_issue_id, issue_url=EXCLUDED.issue_url,
             acceptance_criteria=EXCLUDED.acceptance_criteria, labels=EXCLUDED.labels,
             depends_on=EXCLUDED.depends_on, external_issue_platform=EXCLUDED.external_issue_platform,
             last_external_sync_at=EXCLUDED.last_external_sync_at, npi_phase_id=EXCLUDED.npi_phase_id
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
    # 8 positional placeholders; ON CONFLICT (model) DO UPDATE uses
    # EXCLUDED.* per PG convention. Caller (routers/system.py
    # _persist_token_usage) is fire-and-forget from the LLM callback —
    # asyncpg auto-commits each statement outside a tx block, matching
    # the prior compat-wrapper's explicit .commit().
    await conn.execute(
        """INSERT INTO token_usage (model, input_tokens, output_tokens,
             total_tokens, cost, request_count, avg_latency, last_used)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (model) DO UPDATE SET
             input_tokens = EXCLUDED.input_tokens,
             output_tokens = EXCLUDED.output_tokens,
             total_tokens = EXCLUDED.total_tokens,
             cost = EXCLUDED.cost,
             request_count = EXCLUDED.request_count,
             avg_latency = EXCLUDED.avg_latency,
             last_used = EXCLUDED.last_used
        """,
        data["model"],
        int(data.get("input_tokens", 0)),
        int(data.get("output_tokens", 0)),
        int(data.get("total_tokens", 0)),
        float(data.get("cost", 0.0)),
        int(data.get("request_count", 0)),
        int(data.get("avg_latency", 0)),
        data.get("last_used", ""),
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
    await conn.execute(
        """INSERT INTO notifications (id, level, title, message, source, timestamp,
             read, action_url, action_label, auto_resolved)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
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
    )


def _notification_row_to_dict(row) -> dict:
    d = dict(row)
    d["read"] = bool(d.get("read", 0))
    d["auto_resolved"] = bool(d.get("auto_resolved", 0))
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
    await conn.execute(
        """INSERT INTO debug_findings
           (id, task_id, agent_id, finding_type, severity, content,
            context, status, created_at, tenant_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           ON CONFLICT (id) DO NOTHING""",
        data["id"],
        data["task_id"],
        data["agent_id"],
        data["finding_type"],
        data.get("severity", "info"),
        data["content"],
        data.get("context", "{}"),
        data.get("status", "open"),
        data.get("created_at", ""),
        tenant_insert_value(),
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
    """
    q = float(data.get("quality_score", 0.0))
    await conn.execute(
        """INSERT INTO episodic_memory
           (id, error_signature, solution, soc_vendor, sdk_version,
            hardware_rev, source_task_id, source_agent_id,
            gerrit_change_id, tags, quality_score, decayed_score,
            created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5,
                   $6, $7, $8,
                   $9, $10, $11, $12,
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
    )


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
