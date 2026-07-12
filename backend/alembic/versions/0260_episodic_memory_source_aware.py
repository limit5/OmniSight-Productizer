"""OP-2610 U6-0 T8-A — source-aware episodic_memory substrate (DORMANT).

Backwards-compat: safe (additive; every new column has a quarantining
default and no reader/writer changes behaviour until T8-B/C).

``episodic_memory`` is the WORKER error→solution table that gets read
back into agent prompts. Today it is GLOBAL: no tenant scoping, no
provenance, no verification state — model-authored and
forgeable-webhook "solutions" are indistinguishable from
Gerrit-verified service writes (design-of-record
``docs/design/2026-07-11-phase-u6-sora-3tier-persistent-memory-design-v2.md``
§1). This migration lays the schema substrate:

  * 6 new columns, all quarantined-by-default: ``tenant_id``
    (``'t-default'`` + FK → tenants), ``source`` (``'legacy'``),
    ``verification_authority`` (NULL), ``verified`` (FALSE),
    ``owner_user_id`` (NULL), ``visibility`` (``'tenant_shared'``).
  * CHECK constraints (PG only): source-IN, visibility-IN, and the
    load-bearing cross-column ``verified ⇒ (source =
    'service_gerrit_merge' AND verification_authority = 'gerrit')`` —
    a buggy or model-driven writer can NEVER mint ``verified=TRUE``;
    only T8-B's independent Gerrit-verified service path can.
  * ``idx_episodic_tenant_verified`` on ``(tenant_id, verified)`` —
    the hot filter for T8-C's default-secure reads.
  * Seeds the ``omnisight-self`` tenant BEFORE the FK lands so the
    runner's own writes (P-ID-C identity) cannot violate it.
  * Belt-and-braces backfill: every pre-existing row →
    ``verified=FALSE, source='legacy', tenant_id='t-default',
    visibility='tenant_shared', verification_authority=NULL``. We
    deliberately do NOT grandfather ``quality_score=1.0`` rows —
    that score was mintable by the unauthenticated webhook with a
    forged ``gerrit_change_id``.

CHECKs are added via SEPARATE ``ADD CONSTRAINT`` statements (0244
style, not inline) because the verified⇒gerrit CHECK references three
columns and must come after all three exist.

SQLite branch: no-op (0017 pattern). The dev/test SQLite schema is a
deliberate SUBSET (no tsv/GIN/CHECKs); ``backend/db.py::_SCHEMA`` +
``_migrate()`` add the same 6 columns there — the ALTER path adds
``tenant_id`` WITHOUT the FK because SQLite cannot ``ADD COLUMN …
REFERENCES`` with a non-NULL default while FKs are on, and a
table-rebuild of the FTS5-backed table is not worth the risk. The
runtime boundary on SQLite is the insert defaults + T8-B's verified
path; the CHECKs bind on PG (prod).

Revision ID: 0260
Revises: 0259
Create Date: 2026-07-12
"""
from __future__ import annotations

from alembic import op


revision = "0260"
down_revision = "0259"
branch_labels = None
depends_on = None


_COLUMNS = (
    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "tenant_id TEXT NOT NULL DEFAULT 't-default'",

    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "source TEXT NOT NULL DEFAULT 'legacy'",

    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "verification_authority TEXT",

    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "verified BOOLEAN NOT NULL DEFAULT FALSE",

    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "owner_user_id TEXT",

    "ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS "
    "visibility TEXT NOT NULL DEFAULT 'tenant_shared'",
)

# (name, definition) — dropped-then-added so re-runs stay idempotent.
_CONSTRAINTS = (
    (
        "episodic_memory_source_check",
        "CHECK (source IN ('legacy', 'service_gerrit_merge', "
        "'model_save_solution', 'user_clarification'))",
    ),
    (
        "episodic_memory_visibility_check",
        "CHECK (visibility IN ('tenant_shared', 'private'))",
    ),
    (
        # The poisoning gate: a verified row can ONLY be a
        # Gerrit-verified service write. IS NOT DISTINCT FROM, not
        # ``=``: PG CHECKs PASS on NULL, so a bare equality would let
        # ``verified=TRUE, source='service_gerrit_merge',
        # verification_authority=NULL`` through the gate.
        "episodic_memory_verified_gerrit_check",
        "CHECK (NOT verified OR (source = 'service_gerrit_merge' "
        "AND verification_authority IS NOT DISTINCT FROM 'gerrit'))",
    ),
    (
        "episodic_memory_tenant_id_fkey",
        "FOREIGN KEY (tenant_id) REFERENCES tenants(id)",
    ),
)


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"
    if not is_pg:
        # SQLite gets the same columns from db.py::_SCHEMA/_migrate()
        # (dev SQLite does not run alembic; see module docstring).
        return

    # (a) Seed omnisight-self BEFORE the FK below — the runner's own
    # episodic writes carry this tenant once P-ID-C identity threading
    # reaches the writers.
    conn.exec_driver_sql(
        "INSERT INTO tenants (id, name, plan) "
        "VALUES ('omnisight-self', 'OmniSight Internal', 'free') "
        "ON CONFLICT DO NOTHING"
    )

    # (b) Columns — all defaults quarantine existing + un-updated rows.
    for stmt in _COLUMNS:
        conn.exec_driver_sql(stmt)

    # (c)+(d) CHECKs + FK — separate ADD CONSTRAINT after all columns
    # exist (the verified⇒gerrit CHECK spans three of them).
    for name, definition in _CONSTRAINTS:
        conn.exec_driver_sql(
            f"ALTER TABLE episodic_memory DROP CONSTRAINT IF EXISTS {name}"
        )
        conn.exec_driver_sql(
            f"ALTER TABLE episodic_memory ADD CONSTRAINT {name} {definition}"
        )

    # (e) Hot read-filter index for T8-C's verified-only tenant reads.
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_episodic_tenant_verified "
        "ON episodic_memory (tenant_id, verified)"
    )

    # (f) Belt-and-braces backfill (the column DEFAULTs already did
    # this for pre-existing rows; being explicit is the contract).
    conn.exec_driver_sql(
        "UPDATE episodic_memory SET "
        "verified = FALSE, source = 'legacy', tenant_id = 't-default', "
        "visibility = 'tenant_shared', verification_authority = NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"
    if not is_pg:
        return

    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_episodic_tenant_verified")
    for name, _definition in reversed(_CONSTRAINTS):
        conn.exec_driver_sql(
            f"ALTER TABLE episodic_memory DROP CONSTRAINT IF EXISTS {name}"
        )
    for col in (
        "visibility", "owner_user_id", "verified",
        "verification_authority", "source", "tenant_id",
    ):
        conn.exec_driver_sql(
            f"ALTER TABLE episodic_memory DROP COLUMN IF EXISTS {col}"
        )
    # The seeded omnisight-self tenant stays — harmless, and other
    # rows may reference it by the time anyone downgrades.
