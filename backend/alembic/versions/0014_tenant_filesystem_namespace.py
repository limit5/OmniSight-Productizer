"""I5: Tenant filesystem namespace — migrate existing files to t-default.

Moves artifacts from the legacy ``.artifacts/`` directory into the
tenant-scoped ``data/tenants/t-default/artifacts/`` layout and updates
the ``file_path`` column in the ``artifacts`` table accordingly.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-16
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LEGACY_ARTIFACTS = _PROJECT_ROOT / ".artifacts"
_TENANTS_ROOT = _PROJECT_ROOT / "data" / "tenants"
_DEFAULT_TENANT = "t-default"


def _skip_fs() -> bool:
    # N8 dual-track validator sets this so the migration's SQL path
    # is exercised without shuffling real artifact files around the
    # workspace.  Production callers never set it.
    return os.environ.get("OMNISIGHT_SKIP_FS_MIGRATIONS", "").strip() in {"1", "true", "yes"}


def upgrade() -> None:
    conn = op.get_bind()

    default_root = _TENANTS_ROOT / _DEFAULT_TENANT
    default_artifacts = default_root / "artifacts"
    if not _skip_fs():
        for sub in ("artifacts", "backups", "workflow_runs"):
            (default_root / sub).mkdir(parents=True, exist_ok=True)

    if _skip_fs() or not _LEGACY_ARTIFACTS.is_dir():
        logger.info("No legacy .artifacts/ directory to migrate (or skip requested).")
        return

    migrated = 0
    for item in _LEGACY_ARTIFACTS.iterdir():
        dest = default_artifacts / item.name
        if dest.exists():
            logger.warning("Skipping %s — already exists in tenant dir", item.name)
            continue
        try:
            shutil.move(str(item), str(dest))
            migrated += 1
        except OSError as exc:
            logger.warning("Failed to move %s: %s", item.name, exc)

    logger.info("Migrated %d items from .artifacts/ to %s", migrated, default_artifacts)

    # SQLAlchemy 2.x requires `text()` around raw SQL on Connection.execute;
    # N8 dual-track validator found this bug on 2026-04-16 (rc=1 at rev 0014).
    rows = conn.execute(
        text("SELECT id, file_path FROM artifacts WHERE file_path IS NOT NULL")
    ).fetchall()

    legacy_prefix = str(_LEGACY_ARTIFACTS)
    updated = 0
    for row_id, file_path in rows:
        if file_path and file_path.startswith(legacy_prefix):
            new_path = str(default_artifacts / file_path[len(legacy_prefix):].lstrip(os.sep))
            conn.execute(
                text("UPDATE artifacts SET file_path = :p WHERE id = :i"),
                {"p": new_path, "i": row_id},
            )
            updated += 1

    logger.info("Updated %d artifact file_path records", updated)


def downgrade() -> None:
    conn = op.get_bind()

    default_artifacts = _TENANTS_ROOT / _DEFAULT_TENANT / "artifacts"
    if _skip_fs() or not default_artifacts.is_dir():
        return

    _LEGACY_ARTIFACTS.mkdir(parents=True, exist_ok=True)

    for item in default_artifacts.iterdir():
        dest = _LEGACY_ARTIFACTS / item.name
        if not dest.exists():
            try:
                shutil.move(str(item), str(dest))
            except OSError:
                pass

    rows = conn.execute(
        text("SELECT id, file_path FROM artifacts WHERE file_path IS NOT NULL")
    ).fetchall()

    tenant_prefix = str(default_artifacts)
    for row_id, file_path in rows:
        if file_path and file_path.startswith(tenant_prefix):
            new_path = str(_LEGACY_ARTIFACTS / file_path[len(tenant_prefix):].lstrip(os.sep))
            conn.execute(
                text("UPDATE artifacts SET file_path = :p WHERE id = :i"),
                {"p": new_path, "i": row_id},
            )
