"""OP-2166 R5.3 follow-up — seed rtsp-onvif-server catalog entry.

The R5.3 ipcam integration (OP-2164) deferred the catalog row for the
prebuilt ``omnisight/rtsp-onvif-server`` release to here because alembic
0052 is an immutable merged migration and ``backend/tests/test_catalog_schema.py``
(BS.1.5) drift-guards the yaml mirror ↔ 0052 ``_SEED_ENTRIES`` set
equality. Adding a row there would have either broken the drift guard
or required editing the frozen migration; this revision instead lands
the row as a follow-on insert and the drift-guard test combines this
migration's ``SEED_ENTRIES`` with 0052's so yaml/seed/DB stay
consistent.

Shape: ``install_method='noop'`` with ``metadata.manual_step=true`` and
a git source URL, mirroring the ``beaglebone-debian-image`` precedent
in 0052 — the server is not installed onto the registry host; the
on-device image bakes it in via the R5.1 Yocto meta-layer (SRCREV pin
to the release tag) or the R5.2 Buildroot external tree (package
version pin). The catalog row exists so the operator UI can render the
prebuilt server in the software-family browser and so the ipcam pack's
``prebuilt_server_integration`` task can resolve the pinned release
without hard-coding the repo URL.

Idempotency: same ``INSERT OR IGNORE`` pattern as 0052 (the
``alembic_pg_compat`` shim translates this to ``INSERT … ON CONFLICT
DO NOTHING`` on PG, matching the partial UNIQUE
``uq_catalog_entries_visible(id, source, COALESCE(tenant_id, ''))
WHERE hidden = false`` index from 0051). Re-running this migration on
an already-seeded DB is a no-op.

Dialect handling: same JSONB-vs-TEXT split as 0052. The
``::jsonb`` cast is emitted on PG; SQLite stores JSON columns as
TEXT-of-JSON.

Module-global / cross-worker state audit
----------------------------------------
Pure DML migration. No module-level cache, no singleton, no
ContextVar. Runs once at ``alembic upgrade head`` time; every worker
boot after the cutover sees the same row in the same PG database.

Revision ID: 0251
Revises: 0250
Create Date: 2026-06-28
backwards-compat: safe (additive)
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op


revision = "0251"
down_revision = "0250"
branch_labels = None
depends_on = None


# Single-entry follow-on seed. Public name mirrors 0052's ``SEED_ENTRIES``
# so the BS.1.5 drift-guard test in ``backend/tests/test_catalog_schema.py``
# can splice this list into its yaml ↔ alembic equality checks without
# special-casing each follow-on migration.
SEED_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "id": "rtsp-onvif-server",
        "vendor": "omnisight",
        "family": "software",
        "display_name": "OmniSight RTSP/ONVIF Server (prebuilt release)",
        "version": "0.1.0",
        "install_method": "noop",
        "install_url": "https://gitlab.com/omnisight/rtsp-onvif-server",
        "metadata": {
            "repo": "omnisight/rtsp-onvif-server",
            "gitlab_project_id": 45,
            "first_release_tag": "v0.1.0",
            "release_pinning": "yocto_srcrev_or_buildroot_version",
            "integrates_via": "ipcam_prebuilt_server_integration",
            "manual_step": True,
        },
    },
)


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _build_insert(entry: dict[str, Any], dialect: str) -> str:
    """Build a single ``INSERT OR IGNORE`` row, matching 0052's shape.

    Kept private + duplicated rather than imported from 0052 because
    alembic migrations should not import each other (a future refactor
    of 0052's internals must not be able to silently change this
    migration's wire format).
    """
    cols: list[str] = [
        "id", "source", "vendor", "family",
        "display_name", "version", "install_method",
    ]
    vals: list[str] = [
        f"'{_sql_escape(entry['id'])}'",
        "'shipped'",
        f"'{_sql_escape(entry['vendor'])}'",
        f"'{_sql_escape(entry['family'])}'",
        f"'{_sql_escape(entry['display_name'])}'",
        f"'{_sql_escape(entry['version'])}'",
        f"'{_sql_escape(entry['install_method'])}'",
    ]

    if entry.get("install_url") is not None:
        cols.append("install_url")
        vals.append(f"'{_sql_escape(entry['install_url'])}'")

    if entry.get("size_bytes") is not None:
        cols.append("size_bytes")
        vals.append(str(int(entry["size_bytes"])))

    if entry.get("sha256") is not None:
        cols.append("sha256")
        vals.append(f"'{_sql_escape(entry['sha256'])}'")

    depends_on_json = json.dumps(entry.get("depends_on", []))
    metadata_json = json.dumps(entry.get("metadata", {}), sort_keys=True)
    cols.append("depends_on")
    cols.append("metadata")
    if dialect == "postgresql":
        vals.append(f"'{_sql_escape(depends_on_json)}'::jsonb")
        vals.append(f"'{_sql_escape(metadata_json)}'::jsonb")
    else:
        vals.append(f"'{_sql_escape(depends_on_json)}'")
        vals.append(f"'{_sql_escape(metadata_json)}'")

    cols_sql = ", ".join(cols)
    vals_sql = ", ".join(vals)
    return (
        f"INSERT OR IGNORE INTO catalog_entries ({cols_sql}) "
        f"VALUES ({vals_sql})"
    )


def _seed_ids() -> tuple[str, ...]:
    return tuple(e["id"] for e in SEED_ENTRIES)


def upgrade() -> None:
    # Route through ``op.execute`` (not ``conn.exec_driver_sql``) for the
    # same SQLAlchemy 2.x + cython sqlite3 immutabledict reason 0052
    # documents in its FX.9.1 note.
    dialect = op.get_bind().dialect.name
    for entry in SEED_ENTRIES:
        op.execute(_build_insert(entry, dialect))


def downgrade() -> None:
    # Narrow downgrade: only delete shipped rows whose id is in this
    # migration's seed set. Operator-overridden rows at source='operator'
    # / 'override' for the same id are preserved.
    ids = ", ".join(f"'{_sql_escape(i)}'" for i in _seed_ids())
    op.execute(
        f"DELETE FROM catalog_entries "
        f"WHERE source = 'shipped' "
        f"AND id IN ({ids})"
    )
