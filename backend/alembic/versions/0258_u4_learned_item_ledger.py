"""OP-2565 U4-A1 — immutable learned-item ledger (append-only).

Backwards-compat: safe

Implements the U4-A0 contract freeze §G2/V3.1-V3.3 schema
(``docs/design/2026-07-10-phase-u4-a0-contract-freeze.md``): the eight
tables of the eval-gated promotion substrate. Ships DORMANT — the tables
are created empty and nothing reads or writes them yet (producers move in
U4-I, the publisher/loader in U4-C). Zero runtime behaviour change.

Immutability contract: an "edit" is a NEW version row (new canonical
hash). Seven of the eight tables are append-only ledgers — UPDATE and
DELETE are blocked at the database level by trigger on both Postgres and
SQLite (the 0234 release_compliance_ledger pattern). State transitions
are append-only events; there is NO mutable ``status`` column anywhere.
``learned_item_snapshots`` is the one MUTABLE table: the U4-C publisher
rewrites it.

Scope dedupe uses PARTIAL unique indexes per audience (NOT a plain
multi-column UNIQUE — Postgres treats NULLs as DISTINCT, which would
silently break global-item dedupe; freeze decision V3.2).

Revision ID: 0258
Revises: 0257
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op


revision = "0258"
down_revision = "0257"
branch_labels = None
depends_on = None


# Ledger tables guarded by the append-only trigger. Order matters for
# downgrade: children before parents.
_LEDGER_TABLES = (
    "memory_transition_events",
    "memory_publications",
    "memory_approvals",
    "memory_eval_cases",
    "memory_eval_runs",
    "learned_item_evidence",
    "learned_item_versions",
)


# ━━ Postgres DDL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PG_TABLES = (
    """
CREATE TABLE IF NOT EXISTS learned_item_versions (
    id                      UUID PRIMARY KEY,
    canonical_content_hash  TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    audience                TEXT NOT NULL,
    tenant_id               TEXT REFERENCES tenants(id),
    project_id              TEXT REFERENCES projects(id),
    payload                 JSONB NOT NULL,
    rendered_payload        TEXT,
    renderer_version        TEXT,
    rendered_payload_sha256 TEXT,
    delivery_mode           TEXT NOT NULL DEFAULT 'retrieved',
    name                    TEXT,
    description             TEXT,
    trigger_condition       TEXT,
    keywords                JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by              TEXT NOT NULL,
    CONSTRAINT learned_item_versions_kind_chk
        CHECK (kind IN ('lesson', 'skill', 'playbook')),
    CONSTRAINT learned_item_versions_audience_chk
        CHECK (audience IN ('tenant', 'project', 'global')),
    CONSTRAINT learned_item_versions_delivery_mode_chk
        CHECK (delivery_mode IN ('always_injected', 'retrieved')),
    CONSTRAINT learned_item_versions_hash_chk
        CHECK (canonical_content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT learned_item_versions_global_scope_chk
        CHECK (audience <> 'global' OR tenant_id IS NULL),
    CONSTRAINT learned_item_versions_tenant_scope_chk
        CHECK (audience <> 'tenant' OR tenant_id IS NOT NULL)
)
""",
    """
CREATE TABLE IF NOT EXISTS learned_item_evidence (
    id                UUID PRIMARY KEY,
    version_id        UUID NOT NULL REFERENCES learned_item_versions(id),
    ground_truth_kind TEXT,
    source_change_id  TEXT,
    verified_at       TIMESTAMPTZ,
    revert_state      TEXT DEFAULT 'none',
    evidence_span     JSONB,
    CONSTRAINT learned_item_evidence_gtk_chk
        CHECK (ground_truth_kind IN
               ('merged', 'ci_pass', 'review_plus2', 'reverted', 'stoploss')),
    CONSTRAINT learned_item_evidence_revert_state_chk
        CHECK (revert_state IN ('none', 'reverted'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_eval_runs (
    id                UUID PRIMARY KEY,
    version_id        UUID NOT NULL REFERENCES learned_item_versions(id),
    eval_kind         TEXT,
    suite_sha256      TEXT,
    live_set_hash     TEXT,
    model_fingerprint TEXT,
    decision          TEXT,
    stat_summary      JSONB,
    ran_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_eval_runs_eval_kind_chk
        CHECK (eval_kind IN ('plan_triage', 'replay')),
    CONSTRAINT memory_eval_runs_decision_chk
        CHECK (decision IN
               ('promote', 'reject', 'insufficient_evidence', 'infra_invalid'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_eval_cases (
    id             UUID PRIMARY KEY,
    eval_run_id    UUID NOT NULL REFERENCES memory_eval_runs(id),
    case_id        TEXT NOT NULL,
    baseline_pass  BOOLEAN NOT NULL,
    candidate_pass BOOLEAN NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_approvals (
    id            UUID PRIMARY KEY,
    version_id    UUID NOT NULL REFERENCES learned_item_versions(id),
    eval_run_id   UUID NOT NULL REFERENCES memory_eval_runs(id),
    live_set_hash TEXT NOT NULL,
    approved_by   TEXT NOT NULL,
    approved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    digest_id     TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_publications (
    id            UUID PRIMARY KEY,
    version_id    UUID NOT NULL REFERENCES learned_item_versions(id),
    approval_id   UUID REFERENCES memory_approvals(id),
    state         TEXT NOT NULL,
    event_seq     BIGINT GENERATED BY DEFAULT AS IDENTITY,
    published_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    revoked_by    TEXT,
    revoke_reason TEXT,
    CONSTRAINT memory_publications_state_chk
        CHECK (state IN ('approved', 'publishing', 'published',
                         'publish_failed', 'revoked', 'superseded'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_transition_events (
    id         UUID PRIMARY KEY,
    version_id UUID NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason     TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS learned_item_snapshots (
    scope_key       TEXT NOT NULL,
    live_set_head   BIGINT NOT NULL,
    membership      JSONB NOT NULL,
    rendered_bundle JSONB,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    built_by        TEXT,
    PRIMARY KEY (scope_key, live_set_head)
)
""",
)


# V3.2: partial unique indexes per audience — a plain multi-column
# UNIQUE would NOT dedupe global items (NULL tenant_id is DISTINCT).
_PARTIAL_UNIQUE_INDEXES = (
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_learned_item_versions_global_hash
    ON learned_item_versions (canonical_content_hash)
    WHERE audience = 'global'
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_learned_item_versions_tenant_hash
    ON learned_item_versions (tenant_id, canonical_content_hash)
    WHERE audience = 'tenant'
""",
)


_PG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION learned_item_ledger_block_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'LedgerTriggerBypassed: % is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql
"""


def _pg_triggers(table: str) -> tuple[str, str]:
    return (
        f"""
DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table};
CREATE TRIGGER trg_{table}_no_update
    BEFORE UPDATE ON {table}
    FOR EACH ROW EXECUTE FUNCTION learned_item_ledger_block_mutation()
""",
        f"""
DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table};
CREATE TRIGGER trg_{table}_no_delete
    BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION learned_item_ledger_block_mutation()
""",
    )


# ━━ SQLite DDL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SQLITE_TABLES = (
    """
CREATE TABLE IF NOT EXISTS learned_item_versions (
    id                      TEXT PRIMARY KEY,
    canonical_content_hash  TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    audience                TEXT NOT NULL,
    tenant_id               TEXT REFERENCES tenants(id),
    project_id              TEXT REFERENCES projects(id),
    payload                 JSONB NOT NULL,
    rendered_payload        TEXT,
    renderer_version        TEXT,
    rendered_payload_sha256 TEXT,
    delivery_mode           TEXT NOT NULL DEFAULT 'retrieved',
    name                    TEXT,
    description             TEXT,
    trigger_condition       TEXT,
    keywords                JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by              TEXT NOT NULL,
    CONSTRAINT learned_item_versions_kind_chk
        CHECK (kind IN ('lesson', 'skill', 'playbook')),
    CONSTRAINT learned_item_versions_audience_chk
        CHECK (audience IN ('tenant', 'project', 'global')),
    CONSTRAINT learned_item_versions_delivery_mode_chk
        CHECK (delivery_mode IN ('always_injected', 'retrieved')),
    CONSTRAINT learned_item_versions_hash_chk
        CHECK (length(canonical_content_hash) = 64),
    CONSTRAINT learned_item_versions_global_scope_chk
        CHECK (audience <> 'global' OR tenant_id IS NULL),
    CONSTRAINT learned_item_versions_tenant_scope_chk
        CHECK (audience <> 'tenant' OR tenant_id IS NOT NULL)
)
""",
    """
CREATE TABLE IF NOT EXISTS learned_item_evidence (
    id                TEXT PRIMARY KEY,
    version_id        TEXT NOT NULL REFERENCES learned_item_versions(id),
    ground_truth_kind TEXT,
    source_change_id  TEXT,
    verified_at       TIMESTAMPTZ,
    revert_state      TEXT DEFAULT 'none',
    evidence_span     JSONB,
    CONSTRAINT learned_item_evidence_gtk_chk
        CHECK (ground_truth_kind IN
               ('merged', 'ci_pass', 'review_plus2', 'reverted', 'stoploss')),
    CONSTRAINT learned_item_evidence_revert_state_chk
        CHECK (revert_state IN ('none', 'reverted'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_eval_runs (
    id                TEXT PRIMARY KEY,
    version_id        TEXT NOT NULL REFERENCES learned_item_versions(id),
    eval_kind         TEXT,
    suite_sha256      TEXT,
    live_set_hash     TEXT,
    model_fingerprint TEXT,
    decision          TEXT,
    stat_summary      JSONB,
    ran_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_eval_runs_eval_kind_chk
        CHECK (eval_kind IN ('plan_triage', 'replay')),
    CONSTRAINT memory_eval_runs_decision_chk
        CHECK (decision IN
               ('promote', 'reject', 'insufficient_evidence', 'infra_invalid'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_eval_cases (
    id             TEXT PRIMARY KEY,
    eval_run_id    TEXT NOT NULL REFERENCES memory_eval_runs(id),
    case_id        TEXT NOT NULL,
    baseline_pass  BOOLEAN NOT NULL,
    candidate_pass BOOLEAN NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_approvals (
    id            TEXT PRIMARY KEY,
    version_id    TEXT NOT NULL REFERENCES learned_item_versions(id),
    eval_run_id   TEXT NOT NULL REFERENCES memory_eval_runs(id),
    live_set_hash TEXT NOT NULL,
    approved_by   TEXT NOT NULL,
    approved_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    digest_id     TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_publications (
    id            TEXT PRIMARY KEY,
    version_id    TEXT NOT NULL REFERENCES learned_item_versions(id),
    approval_id   TEXT REFERENCES memory_approvals(id),
    state         TEXT NOT NULL,
    event_seq     BIGINT,
    published_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    revoked_by    TEXT,
    revoke_reason TEXT,
    CONSTRAINT memory_publications_state_chk
        CHECK (state IN ('approved', 'publishing', 'published',
                         'publish_failed', 'revoked', 'superseded'))
)
""",
    """
CREATE TABLE IF NOT EXISTS memory_transition_events (
    id         TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason     TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS learned_item_snapshots (
    scope_key       TEXT NOT NULL,
    live_set_head   BIGINT NOT NULL,
    membership      JSONB NOT NULL,
    rendered_bundle JSONB,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    built_by        TEXT,
    PRIMARY KEY (scope_key, live_set_head)
)
""",
)


def _sqlite_triggers(table: str) -> tuple[str, str]:
    return (
        f"""
CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, 'LedgerTriggerBypassed: {table} is append-only');
END
""",
        f"""
CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, 'LedgerTriggerBypassed: {table} is append-only');
END
""",
    )


# ━━ upgrade / downgrade ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for ddl in _PG_TABLES:
            bind.exec_driver_sql(ddl)
        for ddl in _PARTIAL_UNIQUE_INDEXES:
            bind.exec_driver_sql(ddl)
        bind.exec_driver_sql(_PG_TRIGGER_FN)
        for table in _LEDGER_TABLES:
            for ddl in _pg_triggers(table):
                bind.exec_driver_sql(ddl)
    else:
        for ddl in _SQLITE_TABLES:
            bind.exec_driver_sql(ddl)
        for ddl in _PARTIAL_UNIQUE_INDEXES:
            bind.exec_driver_sql(ddl)
        for table in _LEDGER_TABLES:
            for ddl in _sqlite_triggers(table):
                bind.exec_driver_sql(ddl)


def downgrade() -> None:
    # The tables ship dormant/empty, so a clean drop is correct.
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    for table in _LEDGER_TABLES:
        if is_pg:
            bind.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table}"
            )
            bind.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}"
            )
        else:
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS uq_learned_item_versions_tenant_hash"
    )
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS uq_learned_item_versions_global_hash"
    )
    bind.exec_driver_sql("DROP TABLE IF EXISTS learned_item_snapshots")
    for table in _LEDGER_TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
    if is_pg:
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS learned_item_ledger_block_mutation()"
        )
