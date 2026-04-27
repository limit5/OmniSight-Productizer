"""BS.9.1 — bootstrap_state.metadata: TEXT-of-JSON → JSONB on PG.

The BS.9 epic adds an optional intermediate wizard step
``vertical_setup`` whose payload — operator's vertical multi-pick
(D/W/P/S/X) and the per-vertical sub-step choices (Android API
range, etc.) — lives at ``bootstrap_state.metadata.verticals_selected``.

To support targeted JSONB queries against that path
(``WHERE metadata @> '{"verticals_selected": [...]}'`` or
``metadata->'verticals_selected'`` containment lookups when the
admin UI later filters install jobs by chosen vertical), this
migration promotes the ``bootstrap_state.metadata`` column from
``TEXT`` to ``JSONB`` on PostgreSQL.

Why now and not at table creation
─────────────────────────────────
The 0016 ``pg_schema_sync`` migration shipped ``metadata`` as
``TEXT`` to keep parity with the SQLite dev path (no native
JSONB). That parity is preserved here — SQLite stays TEXT-of-JSON
since SQLite has no JSONB type and this column is small enough
that string round-tripping at the python layer is cheap. PG gains
the structured-column win without breaking SQLite dev.

Existing rows store JSON strings (e.g. ``'{"source": "wizard"}'``)
written by :func:`backend.bootstrap._serialise_metadata`, so the
PG-side ``USING metadata::jsonb`` cast is total. The catalog of
upstream writers is closed:

* :func:`record_bootstrap_step` — already serialises through
  :func:`_serialise_metadata` which always emits valid JSON or
  the literal ``'{}'`` fallback.
* No other writer touches the column directly (verified via
  ``grep -rn "INSERT INTO bootstrap_state\|UPDATE bootstrap_state"``).

Default value
─────────────
PG default goes from ``'{}'`` (TEXT) to ``'{}'::jsonb`` so a row
inserted with metadata omitted is still a valid empty JSONB
object. SQLite default stays ``'{}'`` (TEXT-of-JSON).

Read path
─────────
asyncpg returns JSONB columns as **strings** by default (no
codec registered for the JSONB OID), so existing call sites that
go through :func:`_deserialise_metadata` keep working unchanged
— ``json.loads`` on the raw column value, regardless of whether
the underlying column is TEXT or JSONB. No reader refactor
needed in this migration.

Write path
──────────
INSERT/UPDATE in :func:`record_bootstrap_step` is now passed
``$3::jsonb`` so asyncpg's textual binding lands in the new JSONB
column without an explicit codec — the cast happens in PG. See
the corresponding bootstrap.py edit landed in this same row.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL migration; no in-memory cache, no module-level
singleton. Every worker reads from the same PG so the column-type
flip is visible atomically post-commit. Answer #1 for the
cross-worker question — every worker reads the same DDL state
from the same DB.

Read-after-write timing audit
─────────────────────────────
The TEXT → JSONB conversion is a one-shot DDL inside the alembic
upgrade transaction. Writers and readers see consistent column
types before and after, with no concurrent writes during the
migration window (alembic upgrade holds an exclusive ACCESS
EXCLUSIVE lock on the table for the ALTER TYPE statement). The
asyncpg pool is closed before alembic upgrade per the standard
deploy flow (``scripts/deploy.sh`` step 3); reopening it after
alembic finishes refreshes asyncpg's catalog.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new schema artefact (the existing ``bootstrap_state`` table is
  modified in place; ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  doesn't change since the table is still listed by name).
* TODO.md predicted alembic 0053 for this row; 0053 was claimed
  by BS.2.4 (``catalog_override_nullables``) shortly before BS.9
  landed. This file uses 0054 — the next free slot. The TODO
  schema-migration table updated in the same row.
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once BS.9 lands and the alembic chain
  (0051 + 0052 + 0053 + 0054) is run against prod PG.

Revision ID: 0054
Revises: 0053
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


# ─── PG branch ────────────────────────────────────────────────────────────


_PG_DROP_METADATA_DEFAULT = (
    "ALTER TABLE bootstrap_state "
    "ALTER COLUMN metadata DROP DEFAULT"
)

_PG_PROMOTE_METADATA_TO_JSONB = (
    "ALTER TABLE bootstrap_state "
    "ALTER COLUMN metadata TYPE jsonb USING metadata::jsonb"
)

_PG_RESET_METADATA_DEFAULT_JSONB = (
    "ALTER TABLE bootstrap_state "
    "ALTER COLUMN metadata SET DEFAULT '{}'::jsonb"
)

_PG_RESTORE_METADATA_TEXT = (
    "ALTER TABLE bootstrap_state "
    "ALTER COLUMN metadata TYPE TEXT USING metadata::text"
)

_PG_RESTORE_METADATA_DEFAULT_TEXT = (
    "ALTER TABLE bootstrap_state "
    "ALTER COLUMN metadata SET DEFAULT '{}'"
)


# ─── upgrade / downgrade ──────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # SQLite path: no-op. SQLite has no JSONB; metadata remains
        # TEXT-of-JSON, decoded at the python layer. The
        # ``verticals_selected`` sub-key still works — it's just
        # parsed by ``json.loads`` instead of indexed by GIN.
        return
    # PG cannot auto-cast a TEXT default ('{}') to a JSONB column.
    # Drop the default first, retype the column (existing rows
    # round-trip through ``USING metadata::jsonb`` because every row
    # holds valid JSON via :func:`_serialise_metadata`'s '{}' fallback),
    # then re-establish the default as the JSONB literal.
    conn.exec_driver_sql(_PG_DROP_METADATA_DEFAULT)
    conn.exec_driver_sql(_PG_PROMOTE_METADATA_TO_JSONB)
    conn.exec_driver_sql(_PG_RESET_METADATA_DEFAULT_JSONB)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Symmetric: drop JSONB default, retype back to TEXT, restore the
    # original TEXT default.
    conn.exec_driver_sql(_PG_DROP_METADATA_DEFAULT)
    conn.exec_driver_sql(_PG_RESTORE_METADATA_TEXT)
    conn.exec_driver_sql(_PG_RESTORE_METADATA_DEFAULT_TEXT)
