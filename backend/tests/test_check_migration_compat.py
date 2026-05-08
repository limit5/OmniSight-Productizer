from __future__ import annotations

import json
from pathlib import Path

from scripts import check_migration_compat as cmc


def test_column_rename_is_old_code_new_schema_regression() -> None:
    result = cmc.classify_old_code_compat(
        """
        def upgrade() -> None:
            op.execute("ALTER TABLE users RENAME COLUMN name TO display_name")
        """
    )

    assert not result.ok
    assert "old code + new schema regression" in result.reason
    assert "column rename" in result.reason


def test_alembic_op_column_rename_is_old_code_new_schema_regression() -> None:
    result = cmc.classify_old_code_compat(
        """
        def upgrade() -> None:
            op.alter_column("users", "name", new_column_name="display_name")
        """
    )

    assert not result.ok
    assert "old code + new schema regression" in result.reason
    assert "column rename" in result.reason


def test_nullable_column_add_is_compatible() -> None:
    result = cmc.classify_old_code_compat(
        """
        def upgrade() -> None:
            op.add_column("users", sa.Column("nickname", sa.Text(), nullable=True))
        """
    )

    assert result.ok


def test_not_null_column_add_without_default_is_regression() -> None:
    result = cmc.classify_old_code_compat(
        """
        def upgrade() -> None:
            op.execute("ALTER TABLE users ADD COLUMN nickname TEXT NOT NULL")
        """
    )

    assert not result.ok
    assert "NOT NULL column add without DEFAULT" in result.reason


def test_alembic_op_not_null_column_add_without_default_is_regression() -> None:
    result = cmc.classify_old_code_compat(
        """
        def upgrade() -> None:
            op.add_column("users", sa.Column("nickname", sa.Text(), nullable=False))
        """
    )

    assert not result.ok
    assert "NOT NULL column add without DEFAULT" in result.reason


def test_override_requires_sora_approval() -> None:
    labels = {"migration:break-allowed"}

    assert cmc.override_allowed(labels=labels, approved_by="sora")
    assert not cmc.override_allowed(labels=labels, approved_by="operator")
    assert not cmc.override_allowed(labels=set(), approved_by="sora")


def test_override_audit_log_records_ticket_and_migrations(tmp_path: Path) -> None:
    audit_log = tmp_path / "audit.jsonl"
    migration = cmc.MigrationSpec(
        path=cmc.REPO_ROOT / "backend" / "alembic" / "versions" / "0999_example.py",
        revision="0999",
        down_revision="0998",
    )

    cmc.append_override_audit(
        audit_log,
        ticket="OP-765",
        labels={"migration:break-allowed", "tier:M"},
        approved_by="sora",
        migrations=[migration],
    )

    row = json.loads(audit_log.read_text(encoding="utf-8"))
    assert row["ticket"] == "OP-765"
    assert row["event"] == "migration_compat_override"
    assert row["approved_by"] == "sora"
    assert row["migrations"] == ["backend/alembic/versions/0999_example.py"]


def test_migration_template_requires_backwards_compat_docstring() -> None:
    template = (
        cmc.REPO_ROOT / "backend" / "alembic" / "script.py.mako"
    ).read_text(encoding="utf-8")

    assert "Backwards-compat: <safe|requires-coordinated-deploy>" in template
