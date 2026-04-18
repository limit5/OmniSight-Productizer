"""SQLite persistence layer for agents, tasks, and token usage.

Uses aiosqlite for async access.  The database file lives at
``data/omnisight.db`` relative to the project root (auto-created).

All public functions are thin wrappers around ``_conn()`` so the
rest of the application stays unaware of SQL details.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from backend.db_context import tenant_insert_value, tenant_where

logger = logging.getLogger(__name__)

def _resolve_db_path() -> Path:
    from backend.config import settings
    if settings.database_path:
        return Path(settings.database_path).expanduser()
    return Path(__file__).resolve().parents[1] / "data" / "omnisight.db"

_DB_PATH = _resolve_db_path()
_db: aiosqlite.Connection | None = None


async def init() -> None:
    """Open the database and create tables if they don't exist."""
    global _db
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


def _conn() -> aiosqlite.Connection:
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

async def load_decision_rules() -> list[dict]:
    """Load all persisted decision rules. Returns list of dicts matching
    the in-memory shape used by backend.decision_rules."""
    sql = ("SELECT id, kind_pattern, severity, auto_in_modes, default_option_id, "
           "priority, enabled, note FROM decision_rules")
    conditions: list[str] = []
    params: list = []
    tenant_where(conditions, params)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    async with _conn().execute(sql, params) as cur:
        rows = await cur.fetchall()
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


async def replace_decision_rules(rules: list[dict]) -> None:
    """Atomically swap the decision_rules table. Used when the editor PUTs
    the whole list."""
    tid = tenant_insert_value()
    db = _conn()
    async with db.execute("BEGIN IMMEDIATE"):
        pass
    try:
        t_cond: list[str] = []
        t_params: list = []
        tenant_where(t_cond, t_params)
        del_sql = "DELETE FROM decision_rules"
        if t_cond:
            del_sql += " WHERE " + " AND ".join(t_cond)
        await db.execute(del_sql, t_params)
        for r in rules:
            await db.execute(
                "INSERT INTO decision_rules (id, kind_pattern, severity, "
                "auto_in_modes, default_option_id, priority, enabled, note, tenant_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    r["id"],
                    r["kind_pattern"],
                    r.get("severity"),
                    json.dumps(r.get("auto_in_modes") or []),
                    r.get("default_option_id"),
                    int(r.get("priority", 100)),
                    1 if r.get("enabled", True) else 0,
                    (r.get("note") or "")[:240],
                    tid,
                ),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_agents() -> list[dict]:
    async with _conn().execute("SELECT * FROM agents") as cur:
        rows = await cur.fetchall()
    return [_agent_row_to_dict(r) for r in rows]


async def get_agent(agent_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM agents WHERE id = ?", (agent_id,)) as cur:
        row = await cur.fetchone()
    return _agent_row_to_dict(row) if row else None


async def upsert_agent(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO agents (id, name, type, sub_type, status, progress, thought_chain, ai_model, sub_tasks, workspace)
           VALUES (:id, :name, :type, :sub_type, :status, :progress, :thought_chain, :ai_model, :sub_tasks, :workspace)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, type=excluded.type, sub_type=excluded.sub_type, status=excluded.status,
             progress=excluded.progress, thought_chain=excluded.thought_chain,
             ai_model=excluded.ai_model, sub_tasks=excluded.sub_tasks, workspace=excluded.workspace
        """,
        {
            "id": data["id"],
            "name": data["name"],
            "type": data["type"],
            "sub_type": data.get("sub_type", ""),
            "status": data.get("status", "idle"),
            "progress": json.dumps(data.get("progress", {"current": 0, "total": 0})),
            "thought_chain": data.get("thought_chain", ""),
            "ai_model": data.get("ai_model"),
            "sub_tasks": json.dumps(data.get("sub_tasks", [])),
            "workspace": json.dumps(data.get("workspace", {})),
        },
    )
    await _conn().commit()


async def delete_agent(agent_id: str) -> bool:
    cur = await _conn().execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def agent_count() -> int:
    async with _conn().execute("SELECT COUNT(*) FROM agents") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


def _agent_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "sub_type": row["sub_type"] if "sub_type" in row.keys() else "",
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


async def list_tasks() -> list[dict]:
    async with _conn().execute("SELECT * FROM tasks") as cur:
        rows = await cur.fetchall()
    return [_task_row_to_dict(r) for r in rows]


async def get_task(task_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return _task_row_to_dict(row) if row else None


async def upsert_task(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO tasks (id, title, description, priority, status, assigned_agent_id,
             created_at, completed_at, ai_analysis, suggested_agent_type, suggested_sub_type,
             parent_task_id, child_task_ids, external_issue_id, issue_url, acceptance_criteria,
             labels, depends_on, external_issue_platform, last_external_sync_at, npi_phase_id)
           VALUES (:id, :title, :description, :priority, :status, :assigned_agent_id,
                   :created_at, :completed_at, :ai_analysis, :suggested_agent_type, :suggested_sub_type,
                   :parent_task_id, :child_task_ids, :external_issue_id, :issue_url, :acceptance_criteria,
                   :labels, :depends_on, :external_issue_platform, :last_external_sync_at, :npi_phase_id)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, description=excluded.description, priority=excluded.priority,
             status=excluded.status, assigned_agent_id=excluded.assigned_agent_id,
             completed_at=excluded.completed_at, ai_analysis=excluded.ai_analysis,
             suggested_agent_type=excluded.suggested_agent_type, suggested_sub_type=excluded.suggested_sub_type,
             parent_task_id=excluded.parent_task_id, child_task_ids=excluded.child_task_ids,
             external_issue_id=excluded.external_issue_id, issue_url=excluded.issue_url,
             acceptance_criteria=excluded.acceptance_criteria, labels=excluded.labels,
             depends_on=excluded.depends_on, external_issue_platform=excluded.external_issue_platform,
             last_external_sync_at=excluded.last_external_sync_at, npi_phase_id=excluded.npi_phase_id
        """,
        {
            "id": data["id"],
            "title": data["title"],
            "description": data.get("description"),
            "priority": data.get("priority", "medium"),
            "status": data.get("status", "backlog"),
            "assigned_agent_id": data.get("assigned_agent_id"),
            "created_at": data.get("created_at", ""),
            "completed_at": data.get("completed_at"),
            "ai_analysis": data.get("ai_analysis"),
            "suggested_agent_type": data.get("suggested_agent_type"),
            "suggested_sub_type": data.get("suggested_sub_type"),
            "parent_task_id": data.get("parent_task_id"),
            "child_task_ids": json.dumps(data.get("child_task_ids", [])),
            "external_issue_id": data.get("external_issue_id"),
            "issue_url": data.get("issue_url"),
            "acceptance_criteria": data.get("acceptance_criteria"),
            "labels": json.dumps(data.get("labels", [])),
            "depends_on": json.dumps(data.get("depends_on", [])),
            "external_issue_platform": data.get("external_issue_platform"),
            "last_external_sync_at": data.get("last_external_sync_at"),
            "npi_phase_id": data.get("npi_phase_id"),
        },
    )
    await _conn().commit()


# ── Task comments ──

async def insert_task_comment(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO task_comments (id, task_id, author, content, timestamp)
           VALUES (:id, :task_id, :author, :content, :timestamp)""",
        data,
    )
    await _conn().commit()


async def list_task_comments(task_id: str, limit: int = 20) -> list[dict]:
    async with _conn().execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?",
        (task_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_task(task_id: str) -> bool:
    cur = await _conn().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def task_count() -> int:
    async with _conn().execute("SELECT COUNT(*) FROM tasks") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_token_usage() -> list[dict]:
    async with _conn().execute("SELECT * FROM token_usage") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_token_usage(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO token_usage (model, input_tokens, output_tokens, total_tokens, cost, request_count, avg_latency, last_used)
           VALUES (:model, :input_tokens, :output_tokens, :total_tokens, :cost, :request_count, :avg_latency, :last_used)
           ON CONFLICT(model) DO UPDATE SET
             input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
             total_tokens=excluded.total_tokens, cost=excluded.cost,
             request_count=excluded.request_count, avg_latency=excluded.avg_latency,
             last_used=excluded.last_used
        """,
        data,
    )
    await _conn().commit()


async def clear_token_usage() -> None:
    await _conn().execute("DELETE FROM token_usage")
    await _conn().commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handoffs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def upsert_handoff(task_id: str, agent_id: str, content: str) -> None:
    await _conn().execute(
        """INSERT INTO handoffs (task_id, agent_id, content)
           VALUES (?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
             agent_id=excluded.agent_id, content=excluded.content,
             created_at=datetime('now')
        """,
        (task_id, agent_id, content),
    )
    await _conn().commit()


async def get_handoff(task_id: str) -> str:
    async with _conn().execute("SELECT content FROM handoffs WHERE task_id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return row["content"] if row else ""


async def list_handoffs() -> list[dict]:
    async with _conn().execute("SELECT task_id, agent_id, created_at FROM handoffs ORDER BY created_at DESC") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_notification(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO notifications (id, level, title, message, source, timestamp, read, action_url, action_label, auto_resolved)
           VALUES (:id, :level, :title, :message, :source, :timestamp, :read, :action_url, :action_label, :auto_resolved)
        """,
        {
            "id": data["id"],
            "level": data["level"],
            "title": data["title"],
            "message": data.get("message", ""),
            "source": data.get("source", ""),
            "timestamp": data.get("timestamp", ""),
            "read": 1 if data.get("read") else 0,
            "action_url": data.get("action_url"),
            "action_label": data.get("action_label"),
            "auto_resolved": 1 if data.get("auto_resolved") else 0,
        },
    )
    await _conn().commit()


def _notification_row_to_dict(row) -> dict:
    d = dict(row)
    d["read"] = bool(d.get("read", 0))
    d["auto_resolved"] = bool(d.get("auto_resolved", 0))
    return d


async def list_notifications(limit: int = 50, level: str = "") -> list[dict]:
    query = "SELECT * FROM notifications"
    params: list = []
    if level:
        query += " WHERE level = ?"
        params.append(level)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(query, params) as cur:
        rows = await cur.fetchall()
    return [_notification_row_to_dict(r) for r in rows]


async def mark_notification_read(notification_id: str) -> bool:
    cur = await _conn().execute("UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def count_unread_notifications(min_level: str = "warning") -> int:
    levels = {"info": 0, "warning": 1, "action": 2, "critical": 3}
    min_rank = levels.get(min_level, 1)
    valid_levels = [l for l, r in levels.items() if r >= min_rank]
    placeholders = ",".join("?" * len(valid_levels))
    async with _conn().execute(
        f"SELECT COUNT(*) FROM notifications WHERE read = 0 AND level IN ({placeholders})",
        valid_levels,
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def update_notification_dispatch(
    notification_id: str, status: str, attempts: int = 0, error: str | None = None,
) -> None:
    await _conn().execute(
        "UPDATE notifications SET dispatch_status = ?, send_attempts = ?, last_error = ? WHERE id = ?",
        (status, attempts, error, notification_id),
    )
    await _conn().commit()


async def list_failed_notifications(limit: int = 50) -> list[dict]:
    async with _conn().execute(
        "SELECT * FROM notifications WHERE dispatch_status = 'failed' ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [_notification_row_to_dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_artifact(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO artifacts (id, task_id, agent_id, name, type, file_path, size, created_at, version, checksum, tenant_id)
           VALUES (:id, :task_id, :agent_id, :name, :type, :file_path, :size, :created_at,
                   :version, :checksum, :tenant_id)""",
        {
            **data,
            "version": data.get("version", ""),
            "checksum": data.get("checksum", ""),
            "tenant_id": tenant_insert_value(),
        },
    )
    await _conn().commit()


async def list_artifacts(task_id: str = "", agent_id: str = "", limit: int = 50) -> list[dict]:
    query = "SELECT * FROM artifacts"
    conditions: list[str] = []
    params: list = []
    tenant_where(conditions, params)
    if task_id:
        conditions.append("task_id = ?")
        params.append(task_id)
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_artifact(artifact_id: str) -> dict | None:
    conditions = ["id = ?"]
    params: list = [artifact_id]
    tenant_where(conditions, params)
    sql = "SELECT * FROM artifacts WHERE " + " AND ".join(conditions)
    async with _conn().execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_artifact(artifact_id: str) -> bool:
    conditions = ["id = ?"]
    params: list = [artifact_id]
    tenant_where(conditions, params)
    sql = "DELETE FROM artifacts WHERE " + " AND ".join(conditions)
    cur = await _conn().execute(sql, params)
    await _conn().commit()
    return cur.rowcount > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPI Lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def get_npi_state() -> dict:
    async with _conn().execute("SELECT data FROM npi_state WHERE id = 'current'") as cur:
        row = await cur.fetchone()
    if row:
        return json.loads(row["data"])
    return {}


async def save_npi_state(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO npi_state (id, data) VALUES ('current', :data)
           ON CONFLICT(id) DO UPDATE SET data=excluded.data""",
        {"data": json.dumps(data)},
    )
    await _conn().commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_simulation(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO simulations
           (id, task_id, agent_id, track, module, status,
            tests_total, tests_passed, tests_failed,
            coverage_pct, valgrind_errors, duration_ms,
            report_json, artifact_id, created_at)
           VALUES (:id, :task_id, :agent_id, :track, :module, :status,
                   :tests_total, :tests_passed, :tests_failed,
                   :coverage_pct, :valgrind_errors, :duration_ms,
                   :report_json, :artifact_id, :created_at)""",
        data,
    )
    await _conn().commit()


async def get_simulation(sim_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM simulations WHERE id = ?", (sim_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_simulations(
    task_id: str = "", agent_id: str = "", status: str = "", limit: int = 50,
) -> list[dict]:
    query = "SELECT * FROM simulations"
    conditions: list[str] = []
    params: list = []
    if task_id:
        conditions.append("task_id = ?")
        params.append(task_id)
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


_SIMULATION_COLUMNS = frozenset({
    "status", "tests_total", "tests_passed", "tests_failed",
    "coverage_pct", "valgrind_errors", "duration_ms",
    "report_json", "artifact_id",
    # NPU fields (Phase 36)
    "npu_latency_ms", "npu_throughput_fps", "accuracy_delta",
    "model_size_kb", "npu_framework",
})


async def update_simulation(sim_id: str, data: dict) -> None:
    if not data:
        return
    safe = {k: v for k, v in data.items() if k in _SIMULATION_COLUMNS}
    if not safe:
        return
    sets = ", ".join(f"{k} = :{k}" for k in safe)
    safe["_id"] = sim_id
    await _conn().execute(f"UPDATE simulations SET {sets} WHERE id = :_id", safe)
    await _conn().commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Debug Findings (Shared Blackboard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_debug_finding(data: dict) -> None:
    d = {**data, "tenant_id": tenant_insert_value()}
    await _conn().execute(
        """INSERT OR IGNORE INTO debug_findings
           (id, task_id, agent_id, finding_type, severity, content, context, status, created_at, tenant_id)
           VALUES (:id, :task_id, :agent_id, :finding_type, :severity, :content, :context, :status, :created_at, :tenant_id)""",
        d,
    )
    await _conn().commit()


async def list_debug_findings(
    task_id: str = "", agent_id: str = "", status: str = "", limit: int = 50,
) -> list[dict]:
    query = "SELECT * FROM debug_findings"
    conditions: list[str] = []
    params: list = []
    tenant_where(conditions, params)
    if task_id:
        conditions.append("task_id = ?")
        params.append(task_id)
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_debug_finding(finding_id: str, status: str) -> bool:
    conditions = ["id = ?"]
    params: list = [finding_id]
    tenant_where(conditions, params)
    sql = ("UPDATE debug_findings SET status = ?, resolved_at = datetime('now') WHERE "
           + " AND ".join(conditions))
    params_full = [status] + params
    cur = await _conn().execute(sql, params_full)
    await _conn().commit()
    return cur.rowcount > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event Log (Persistence)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_event(event_type: str, data_json: str) -> None:
    await _conn().execute(
        "INSERT INTO event_log (event_type, data_json, tenant_id) VALUES (?, ?, ?)",
        (event_type, data_json, tenant_insert_value()),
    )
    await _conn().commit()


async def list_events(
    since: str = "", event_types: list[str] | None = None, limit: int = 200,
) -> list[dict]:
    query = "SELECT * FROM event_log"
    conditions: list[str] = []
    params: list = []
    tenant_where(conditions, params)
    if since:
        conditions.append("created_at >= ?")
        params.append(since)
    if event_types:
        placeholders = ",".join("?" * len(event_types))
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(event_types)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def cleanup_old_events(days: int = 7) -> int:
    cur = await _conn().execute(
        "DELETE FROM event_log WHERE created_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    await _conn().commit()
    return cur.rowcount


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3 Episodic Memory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def insert_episodic_memory(data: dict) -> None:
    """Insert a new episodic memory entry (L3)."""
    # Phase 63-E: decayed_score initialises to quality_score so a
    # fresh row competes on its own merit; the nightly worker decays
    # it later when access stops.
    await _conn().execute(
        """INSERT INTO episodic_memory
           (id, error_signature, solution, soc_vendor, sdk_version, hardware_rev,
            source_task_id, source_agent_id, gerrit_change_id, tags,
            quality_score, decayed_score, created_at, updated_at)
           VALUES (:id, :error_signature, :solution, :soc_vendor, :sdk_version, :hardware_rev,
                   :source_task_id, :source_agent_id, :gerrit_change_id, :tags,
                   :quality_score, :quality_score,
                   datetime('now'), datetime('now'))""",
        {
            "id": data["id"],
            "error_signature": data["error_signature"],
            "solution": data["solution"],
            "soc_vendor": data.get("soc_vendor", ""),
            "sdk_version": data.get("sdk_version", ""),
            "hardware_rev": data.get("hardware_rev", ""),
            "source_task_id": data.get("source_task_id"),
            "source_agent_id": data.get("source_agent_id"),
            "gerrit_change_id": data.get("gerrit_change_id"),
            "tags": json.dumps(data.get("tags", [])),
            "quality_score": data.get("quality_score", 0.0),
        },
    )
    # Update FTS5 index (same transaction — committed together)
    try:
        await _conn().execute(
            """INSERT INTO episodic_memory_fts(rowid, error_signature, solution, soc_vendor, tags)
               SELECT rowid, error_signature, solution, soc_vendor, tags
               FROM episodic_memory WHERE id = ?""",
            (data["id"],),
        )
    except Exception as exc:
        logger.warning("FTS5 index update failed for %s (search will use LIKE fallback): %s", data["id"], exc)
    await _conn().commit()


async def rebuild_episodic_fts() -> int:
    """Rebuild the FTS5 index from the episodic_memory content table.

    Call this if the FTS5 index becomes out of sync (e.g., after a crash).
    Returns the number of rows reindexed.
    """
    try:
        # Drop and rebuild FTS content
        await _conn().execute("DELETE FROM episodic_memory_fts")
        await _conn().execute(
            """INSERT INTO episodic_memory_fts(rowid, error_signature, solution, soc_vendor, tags)
               SELECT rowid, error_signature, solution, soc_vendor, tags
               FROM episodic_memory"""
        )
        await _conn().commit()
        async with _conn().execute("SELECT COUNT(*) FROM episodic_memory") as cur:
            row = await cur.fetchone()
        count = row[0] if row else 0
        logger.info("Rebuilt FTS5 index: %d entries", count)
        return count
    except Exception as exc:
        logger.error("FTS5 rebuild failed: %s", exc)
        return 0


async def search_episodic_memory(
    query: str, soc_vendor: str = "", sdk_version: str = "", limit: int = 5,
    min_quality: float | None = None,
) -> list[dict]:
    """Search L3 episodic memory using FTS5 (with LIKE fallback).

    Returns matching memories sorted by relevance, filtered by vendor/SDK if provided.

    Phase 67-E: `min_quality` pushes the similarity-proxy floor into
    SQL so callers that want cosine-style gating (the Tier-1 sandbox
    path wants > 0.85) don't have to over-fetch then Python-filter.
    None = no floor (matches pre-67-E behaviour).
    """
    results: list[dict] = []

    # Try FTS5 first
    try:
        fts_query = query.replace('"', '""')  # Escape quotes for FTS5
        sql = """
            SELECT em.*, rank
            FROM episodic_memory_fts fts
            JOIN episodic_memory em ON em.rowid = fts.rowid
            WHERE episodic_memory_fts MATCH ?
        """
        params: list = [f'"{fts_query}"']
        if soc_vendor:
            sql += " AND em.soc_vendor = ?"
            params.append(soc_vendor)
        if sdk_version:
            sql += " AND em.sdk_version = ?"
            params.append(sdk_version)
        if min_quality is not None:
            sql += " AND em.quality_score >= ?"
            params.append(min_quality)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        async with _conn().execute(sql, params) as cur:
            rows = await cur.fetchall()
        results = [_episodic_row_to_dict(r) for r in rows]
    except Exception:
        # FTS5 not available — use LIKE fallback
        pass

    if not results:
        sql = "SELECT * FROM episodic_memory WHERE (error_signature LIKE ? OR solution LIKE ?)"
        like_param = f"%{query}%"
        params = [like_param, like_param]
        if soc_vendor:
            sql += " AND soc_vendor = ?"
            params.append(soc_vendor)
        if sdk_version:
            sql += " AND sdk_version = ?"
            params.append(sdk_version)
        if min_quality is not None:
            sql += " AND quality_score >= ?"
            params.append(min_quality)
        sql += " ORDER BY quality_score DESC, created_at DESC LIMIT ?"
        params.append(limit)
        async with _conn().execute(sql, params) as cur:
            rows = await cur.fetchall()
        results = [_episodic_row_to_dict(r) for r in rows]

    # Increment access count for returned results
    for r in results:
        try:
            await _conn().execute(
                "UPDATE episodic_memory SET access_count = access_count + 1 WHERE id = ?",
                (r["id"],),
            )
        except Exception:
            pass
    if results:
        await _conn().commit()

    return results


async def get_episodic_memory(memory_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM episodic_memory WHERE id = ?", (memory_id,)) as cur:
        row = await cur.fetchone()
    return _episodic_row_to_dict(row) if row else None


async def list_episodic_memories(
    soc_vendor: str = "", limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM episodic_memory"
    params: list = []
    if soc_vendor:
        sql += " WHERE soc_vendor = ?"
        params.append(soc_vendor)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with _conn().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_episodic_row_to_dict(r) for r in rows]


async def delete_episodic_memory(memory_id: str) -> bool:
    # Remove from FTS5 first
    try:
        await _conn().execute(
            """INSERT INTO episodic_memory_fts(episodic_memory_fts, rowid, error_signature, solution, soc_vendor, tags)
               SELECT 'delete', rowid, error_signature, solution, soc_vendor, tags
               FROM episodic_memory WHERE id = ?""",
            (memory_id,),
        )
    except Exception as exc:
        logger.warning("FTS5 delete failed for %s: %s", memory_id, exc)
    cur = await _conn().execute("DELETE FROM episodic_memory WHERE id = ?", (memory_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def episodic_memory_count() -> int:
    async with _conn().execute("SELECT COUNT(*) FROM episodic_memory") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


def _episodic_row_to_dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("tags"), str):
        d["tags"] = json.loads(d["tags"])
    # Remove FTS5 rank column if present
    d.pop("rank", None)
    return d
