"""BS.1.1 — Catalog DB schema (catalog_entries + install_jobs + catalog_subscriptions).

Three tables for the BS Bootstrap Vertical-Aware Setup epic
(``docs/design/bs-bootstrap-vertical-aware.md`` §3 + §4 + §7.1).

1.  ``catalog_entries`` — three-source (``shipped|operator|override``)
    catalog of installable platforms (Mobile / Embedded / Web /
    Software / RTOS / cross-toolchain / custom). Per ADR §3 the
    resolved entry is ``override > operator > shipped``; ``override``
    rows store a partial JSONB diff against the ``shipped`` base.

2.  ``install_jobs`` — one row per install attempt. Long-poll claimed
    by the ``omnisight-installer`` sidecar (BS.4); idempotent via
    ``idempotency_key``; state machine
    ``queued → running → {completed | failed | cancelled}``. The
    sidecar never writes PG directly — every state transition is
    recorded by the backend in response to the sidecar's
    ``progress`` / ``result`` HTTP callbacks (ADR §4.2).

3.  ``catalog_subscriptions`` — per-tenant URL feed of third-party
    catalogs (BS.8.5). Future feature; created empty. ``auth_secret_ref``
    is a key into the existing tenant secret store, never the secret
    itself (ADR §7.1).

PK / unique-index decisions
───────────────────────────

* ``catalog_entries`` does NOT carry a single TEXT PRIMARY KEY. The
  ``id`` (e.g. ``nxp-mcuxpresso-imxrt1170``) is shared across the
  three source layers by design — an ``override`` row patches the
  ``shipped`` row of the same ``id``. Uniqueness is enforced by a
  **partial** UNIQUE index on
  ``(id, source, COALESCE(tenant_id, ''))`` ``WHERE hidden = false``.
  A hidden tombstone row may shadow a prior live row without
  violating the constraint, which lets us soft-retire ``operator`` /
  ``override`` rows without losing audit history. ``shipped`` rows
  carry ``tenant_id = NULL`` (no tenant scope); ``operator`` /
  ``override`` rows carry ``tenant_id`` set (per-tenant scope; the
  app layer enforces NOT NULL on those source values, the DB CHECK
  is below).

* ``install_jobs`` carries a TEXT PK (``ij-…`` prefix convention) to
  match the rest of the project's app-generated id pattern
  (``t-`` / ``u-`` / ``p-`` / ``inv-`` / ``psh-``). UUID PKs were
  considered (per ADR §7.1's draft text) but adopted nowhere else
  in this codebase, so the consistency win argues for TEXT. The
  ``idempotency_key`` is a separate UUID UNIQUE column for
  double-click protection on ``POST /installer/jobs``.

* ``catalog_subscriptions`` carries a TEXT PK (``sub-…`` prefix)
  same convention. ``UNIQUE (tenant_id, feed_url)`` so a tenant
  can't subscribe to the same feed twice.

Dialect handling
────────────────

JSONB / TIMESTAMPTZ / BOOLEAN are PG-native; SQLite has no direct
equivalents. Following the established ``0027_git_accounts``
pattern we **branch the DDL** on ``conn.dialect.name``:

* PG path: ``JSONB`` for ``metadata`` and ``depends_on``,
  ``TIMESTAMPTZ`` (``DEFAULT now()``) for timestamps, ``BOOLEAN``
  for ``hidden`` / ``enabled``, ``BIGINT`` for byte sizes,
  ``SMALLINT`` for ``schema_version`` / ``protocol_version``.
* SQLite dev parity: ``TEXT``-of-JSON for the JSONB columns,
  ``REAL`` (UNIX seconds) for timestamps, ``INTEGER 0/1`` for
  booleans, ``INTEGER`` for the size / version columns.

The ``alembic_pg_compat`` shim (``backend/alembic_pg_compat.py``)
does **not** translate ``JSONB`` / ``BOOLEAN`` / ``TIMESTAMPTZ``;
those would fail on SQLite if written naively, hence the branch.
The shim still applies on the PG branch (rewrites
``REAL → DOUBLE PRECISION`` etc.) but the PG branch is already
in PG-flavor so the shim is a no-op for the visible tokens.

Forward-compat (R24 — catalog feed)
────────────────────────────────────

* ``schema_version`` is per-row (not catalog-level). Old entries
  remain readable forever; the resolver picks the right validator
  by row.
* ``CHECK (source IN ('shipped','operator','override','subscription'))``
  reserves ``'subscription'`` for the future feed-imported layer
  (ADR §3.1) without committing to the feed protocol now.
* ``metadata`` is open: validator code (BS.1.5 / BS.2 layer) is
  closed-list on top-level columns, open-list on JSONB sub-keys —
  unknown vendor-specific keys round-trip without rejection.

Indexes (rationale per index)
─────────────────────────────

``catalog_entries``:

  * ``uq_catalog_entries_visible`` — partial UNIQUE on
    ``(id, source, COALESCE(tenant_id, ''))`` ``WHERE hidden = false``.
    The load-bearing uniqueness invariant: at most one live row per
    (id, source, tenant) tuple.
  * ``idx_catalog_entries_family`` — primary read path (``GET
    /catalog/entries?family=embedded``).
  * ``idx_catalog_entries_tenant`` — partial on
    ``WHERE tenant_id IS NOT NULL`` so the ``shipped`` rows (most
    rows by row count) don't bloat the index.
  * ``idx_catalog_entries_source`` — exists primarily so the
    resolver's ``ROW_NUMBER() OVER (PARTITION BY id ORDER BY
    CASE source WHEN 'override' …)`` query (ADR §3.2) can use an
    index scan when the catalog grows past a few hundred rows.

``install_jobs``:

  * ``id`` PK — auto-creates the per-row lookup index (sidecar's
    ``POST /installer/jobs/{id}/progress``).
  * UNIQUE ``idempotency_key`` — ``INSERT … ON CONFLICT
    (idempotency_key) DO NOTHING RETURNING id`` (ADR §4.4).
  * ``idx_install_jobs_state_queued`` — partial on
    ``WHERE state IN ('queued', 'running')``. Sidecar's
    ``GET /installer/jobs/poll`` (``SELECT … FOR UPDATE SKIP LOCKED
    LIMIT 1``) needs a tight index over the small claimable set;
    completed / failed rows accumulate forever and would bloat a
    plain index.
  * ``idx_install_jobs_tenant_queued`` — admin-side "list my
    in-flight installs" query.
  * ``idx_install_jobs_sidecar`` — sidecar restart-recovery (ADR
    §4.4 step 2): ``GET /installer/jobs?sidecar_id=self&state=running``.

``catalog_subscriptions``:

  * ``id`` PK — auto-creates per-row lookup.
  * UNIQUE ``(tenant_id, feed_url)`` — at most one active row per
    (tenant, feed) pair. Doubles as the per-tenant listing index
    (leading column = ``tenant_id``).
  * ``idx_catalog_subscriptions_due`` — partial on
    ``WHERE enabled AND last_synced_at IS NOT NULL``. The cron job
    that picks the next subscription to refresh scans this index.

Module-global / cross-worker state audit
────────────────────────────────────────

Pure DDL migration. No in-memory cache, no module-level singleton.
Every worker reads from the same PG (or local SQLite in dev), so
cross-worker consistency is the database's problem. Sidecar claim
serialisation is enforced by ``SELECT … FOR UPDATE SKIP LOCKED``
on PG (asyncpg pool serializes per-row; multiple sidecars can poll
concurrently, only one wins per job).

Read-after-write timing audit
─────────────────────────────

Empty tables created in this commit; no reader code ships with
this migration. The first reader is BS.2 (``backend/routers/
catalog.py``); the first sidecar long-poll lands with BS.4.

Production readiness gate
─────────────────────────

* No new Python / OS package — production image needs no rebuild.
* ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER`` is **not**
  updated in this commit. BS.1.4 owns the SQLite ``_SCHEMA``
  mirror + migrator ``TABLES_IN_ORDER`` updates and the drift
  guard test (BS.1.5). The migrator drift test
  ``test_migrator_schema_coverage`` is already failing on master
  for ``billing_usage_events`` (Y9 row 3 oversight); BS.1.4 will
  fix both at once. Production status of THIS commit:
  **dev-only**.
* Next gate: ``deployed-inactive`` — operator runs ``alembic
  upgrade head`` once Y migrations 0040–0050 land and the chain
  has been retargeted (see "Revision chain note" below). The
  tables stay empty until BS.1.2 seeds shipped catalog entries.

Revision chain note
───────────────────

Alembic chain points back at 0039 (see ``down_revision`` below).
The TODO reserves 0040–0050 for Priority Y and 0051–0055 for
Priority BS; as of this commit Y has not landed any migration
past 0039, so this is the next forward step. **When Y migrations
land, the linear chain must be re-stitched**:

* Y's first migration sets its predecessor to 0039
* Each subsequent Y migration chains forward
* This file's ``down_revision`` is retargeted to the last Y rev
  (a one-line edit per Y rebase)

The CI alembic-graph guard (``alembic heads`` returns a single
value) catches any double-head introduced by forgetting the
retarget.

Revision ID: 0051
Revises: 0039
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "0051"
down_revision = "0039"
branch_labels = None
depends_on = None


# ─── PG branch ────────────────────────────────────────────────────────────

_PG_CATALOG_ENTRIES = """
CREATE TABLE IF NOT EXISTS catalog_entries (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    schema_version  SMALLINT NOT NULL DEFAULT 1,
    tenant_id       TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL,
    family          TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    version         TEXT NOT NULL,
    install_method  TEXT NOT NULL,
    install_url     TEXT,
    sha256          TEXT,
    size_bytes      BIGINT,
    depends_on      JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    hidden          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source IN ('shipped','operator','override','subscription')),
    CHECK (family IN ('mobile','embedded','web','software',
                      'rtos','cross-toolchain','custom')),
    CHECK (install_method IN ('noop','docker_pull',
                              'shell_script','vendor_installer')),
    CHECK (
        (source = 'shipped'  AND tenant_id IS NULL)
        OR
        (source IN ('operator','override','subscription')
            AND tenant_id IS NOT NULL)
    )
)
"""

_PG_CATALOG_ENTRIES_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_entries_visible "
    "ON catalog_entries(id, source, COALESCE(tenant_id, '')) "
    "WHERE hidden = FALSE",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_family "
    "ON catalog_entries(family)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_tenant "
    "ON catalog_entries(tenant_id) "
    "WHERE tenant_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_source "
    "ON catalog_entries(source)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_metadata_gin "
    "ON catalog_entries USING GIN (metadata)",
)

_PG_INSTALL_JOBS = """
CREATE TABLE IF NOT EXISTS install_jobs (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 't-default'
                            REFERENCES tenants(id) ON DELETE CASCADE,
    entry_id          TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'queued',
    idempotency_key   TEXT NOT NULL,
    sidecar_id        TEXT,
    protocol_version  SMALLINT NOT NULL DEFAULT 1,
    bytes_done        BIGINT NOT NULL DEFAULT 0,
    bytes_total       BIGINT,
    eta_seconds       INTEGER,
    log_tail          TEXT NOT NULL DEFAULT '',
    result_json       JSONB,
    error_reason      TEXT,
    pep_decision_id   TEXT,
    requested_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    queued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at        TIMESTAMPTZ,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    UNIQUE (idempotency_key),
    CHECK (state IN ('queued','running','completed','failed','cancelled'))
)
"""

_PG_INSTALL_JOBS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_state_queued "
    "ON install_jobs(state, queued_at) "
    "WHERE state IN ('queued','running')",
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_tenant_queued "
    "ON install_jobs(tenant_id, queued_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_sidecar "
    "ON install_jobs(sidecar_id, state) "
    "WHERE sidecar_id IS NOT NULL",
)

_PG_CATALOG_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS catalog_subscriptions (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 't-default'
                              REFERENCES tenants(id) ON DELETE CASCADE,
    feed_url            TEXT NOT NULL,
    auth_method         TEXT NOT NULL DEFAULT 'none',
    auth_secret_ref     TEXT,
    refresh_interval_s  INTEGER NOT NULL DEFAULT 86400,
    last_synced_at      TIMESTAMPTZ,
    last_sync_status    TEXT,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, feed_url),
    CHECK (auth_method IN ('none','basic','bearer','signed_url'))
)
"""

_PG_CATALOG_SUBSCRIPTIONS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_catalog_subscriptions_due "
    "ON catalog_subscriptions(last_synced_at NULLS FIRST, refresh_interval_s) "
    "WHERE enabled = TRUE",
)


# ─── SQLite branch ────────────────────────────────────────────────────────

_SQLITE_CATALOG_ENTRIES = """
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
)
"""

_SQLITE_CATALOG_ENTRIES_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_entries_visible "
    "ON catalog_entries(id, source, COALESCE(tenant_id, '')) "
    "WHERE hidden = 0",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_family "
    "ON catalog_entries(family)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_tenant "
    "ON catalog_entries(tenant_id) "
    "WHERE tenant_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_source "
    "ON catalog_entries(source)",
)

_SQLITE_INSTALL_JOBS = """
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
)
"""

_SQLITE_INSTALL_JOBS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_state_queued "
    "ON install_jobs(state, queued_at) "
    "WHERE state IN ('queued','running')",
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_tenant_queued "
    "ON install_jobs(tenant_id, queued_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_install_jobs_sidecar "
    "ON install_jobs(sidecar_id, state) "
    "WHERE sidecar_id IS NOT NULL",
)

_SQLITE_CATALOG_SUBSCRIPTIONS = """
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
)
"""

_SQLITE_CATALOG_SUBSCRIPTIONS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_catalog_subscriptions_due "
    "ON catalog_subscriptions(last_synced_at, refresh_interval_s) "
    "WHERE enabled = 1",
)


# ─── upgrade / downgrade ──────────────────────────────────────────────────

def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(_PG_CATALOG_ENTRIES)
        for stmt in _PG_CATALOG_ENTRIES_INDEXES:
            conn.exec_driver_sql(stmt)
        conn.exec_driver_sql(_PG_INSTALL_JOBS)
        for stmt in _PG_INSTALL_JOBS_INDEXES:
            conn.exec_driver_sql(stmt)
        conn.exec_driver_sql(_PG_CATALOG_SUBSCRIPTIONS)
        for stmt in _PG_CATALOG_SUBSCRIPTIONS_INDEXES:
            conn.exec_driver_sql(stmt)
    else:
        conn.exec_driver_sql(_SQLITE_CATALOG_ENTRIES)
        for stmt in _SQLITE_CATALOG_ENTRIES_INDEXES:
            conn.exec_driver_sql(stmt)
        conn.exec_driver_sql(_SQLITE_INSTALL_JOBS)
        for stmt in _SQLITE_INSTALL_JOBS_INDEXES:
            conn.exec_driver_sql(stmt)
        conn.exec_driver_sql(_SQLITE_CATALOG_SUBSCRIPTIONS)
        for stmt in _SQLITE_CATALOG_SUBSCRIPTIONS_INDEXES:
            conn.exec_driver_sql(stmt)


def downgrade() -> None:
    # Drop in reverse FK order: install_jobs and catalog_subscriptions
    # reference tenants(id) but nothing references back into them, so
    # the order between catalog_entries / install_jobs / catalog_subscriptions
    # is cosmetic. Indexes are dropped automatically with the table.
    op.execute("DROP TABLE IF EXISTS catalog_subscriptions")
    op.execute("DROP TABLE IF EXISTS install_jobs")
    op.execute("DROP TABLE IF EXISTS catalog_entries")
