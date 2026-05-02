"""FS.8.3 -- ``provisioned_billing`` table.

Persistent registry for tenant-owned Stripe billing state created by
the FS.8.1 checkout scaffold and updated by later FS.8 webhook sync.
This migration deliberately lands only the schema:

* ``tenant_id`` -- tenant that owns the Stripe billing relationship.
  FK cascades on tenant delete so a deleted tenant does not leave stale
  billing state in the control-plane DB.
* ``provider`` -- currently only ``stripe``.  The provider column keeps
  the same natural-key shape as ``provisioned_databases`` and
  ``provisioned_storage`` without inventing a synthetic app id.
* ``stripe_customer_id`` -- Stripe Customer id used by the billing
  portal endpoint and webhook lookup paths.
* ``stripe_subscription_id`` -- Stripe Subscription id used by
  subscription lifecycle webhooks.
* ``stripe_price_id`` -- Stripe Price id last observed for this tenant.
* ``status`` -- Stripe-normalized subscription status.  No CHECK
  constraint: Stripe may add new statuses and this table should not
  reject a legitimate provider state just because the catalog is stale.
* ``current_period_end`` -- nullable Stripe epoch seconds; not every
  event shape guarantees a period boundary.
* ``cancel_at_period_end`` -- boolean copy of Stripe subscription state.
* ``created_at`` / ``updated_at`` -- local epoch seconds used by the
  later FS.8.4 upsert path.

Why composite PK ``(tenant_id, provider)``
-----------------------------------------
Mirroring 0061 / 0062, the TODO row names a provisioned_* registry
table but no synthetic ``id`` column.  The natural pair is enough:
one tenant can have at most one Stripe billing record, and FS.8.4 can
upsert that pair as webhook state changes.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
``scripts/deploy.sh`` closes the asyncpg pool before alembic upgrade
and reopens it after, so runtime workers never see a half-shaped
schema.  No concurrent writer exists during the migration window.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``provisioned_billing``
  (replays AFTER ``tenants`` because of the FK).  PK is composite TEXT
  so the table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the FS.8.3 contract test
  enforce this.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0062 -> 0063) is run
  against prod PG and Stripe test-mode env knobs are wired.

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


# -- Provider whitelist -----------------------------------------------------


_PROVIDERS_SQL = "'stripe'"


# -- PG branch --------------------------------------------------------------


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_billing (\n"
    "    tenant_id              TEXT NOT NULL\n"
    "                                   REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider               TEXT NOT NULL\n"
    f"                                   CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    stripe_customer_id     TEXT NOT NULL,\n"
    "    stripe_subscription_id TEXT NOT NULL,\n"
    "    stripe_price_id        TEXT NOT NULL DEFAULT '',\n"
    "    status                 TEXT NOT NULL,\n"
    "    current_period_end     DOUBLE PRECISION,\n"
    "    cancel_at_period_end   BOOLEAN NOT NULL DEFAULT FALSE,\n"
    "    created_at             DOUBLE PRECISION NOT NULL,\n"
    "    updated_at             DOUBLE PRECISION NOT NULL,\n"
    "    PRIMARY KEY (tenant_id, provider)\n"
    ")"
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_billing (\n"
    "    tenant_id              TEXT NOT NULL\n"
    "                                   REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider               TEXT NOT NULL\n"
    f"                                   CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    stripe_customer_id     TEXT NOT NULL,\n"
    "    stripe_subscription_id TEXT NOT NULL,\n"
    "    stripe_price_id        TEXT NOT NULL DEFAULT '',\n"
    "    status                 TEXT NOT NULL,\n"
    "    current_period_end     REAL,\n"
    "    cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,\n"
    "    created_at             REAL NOT NULL,\n"
    "    updated_at             REAL NOT NULL,\n"
    "    PRIMARY KEY (tenant_id, provider)\n"
    ")"
)


# -- upgrade / downgrade ----------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        return

    conn.exec_driver_sql(_SQLITE_CREATE_TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provisioned_billing")
