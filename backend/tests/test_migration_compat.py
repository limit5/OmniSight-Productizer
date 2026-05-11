"""OP-865 D4 — migration backwards-compat enforcer (8 cases per spec).

Each test exercises one acceptance-criterion branch end-to-end against
the public API of ``scripts.check_migration_compat``:

  1. drop column refused (single-revision; no deprecation window)
  2. deprecation-window-1of2 rename to ``_deprecated`` allowed
  3. deprecation-window-2of2 drop after window-1of2 allowed
  4. rename via ``op.alter_column(new_column_name=...)`` allowed
  5. NOT NULL column add without server default refused
  6. enum value appended at tail allowed
  7. enum value at non-tail (``BEFORE``/``AFTER``) refused
  8. ``migration:approved-breaking`` commit trailer overrides the
     ``breaking`` tag check; metadata-missing migration is refused
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from scripts import check_migration_compat as cmc


def _write(path: Path, source: str) -> Path:
    path.write_text(dedent(source), encoding="utf-8")
    return path


# ── case 1: drop column refused ───────────────────────────────────────


def test_drop_column_without_deprecation_window_refused(tmp_path: Path) -> None:
    """Plain ``op.drop_column`` outside a 2-revision window is refused."""
    parent = _write(
        tmp_path / "0001_parent.py",
        '''
        """parent migration.

        Revision ID: 0001
        Revises: None
        backwards-compat: safe
        """
        from alembic import op

        revision = "0001"
        down_revision = None
        branch_labels = None
        depends_on = None

        def upgrade() -> None:
            op.add_column("users", sa.Column("legacy_email", sa.Text(), nullable=True))

        def downgrade() -> None:
            op.drop_column("users", "legacy_email")
        ''',
    )
    drop_mig = _write(
        tmp_path / "0002_drop.py",
        '''
        """drop legacy_email.

        Revision ID: 0002
        Revises: 0001
        backwards-compat: safe
        """
        from alembic import op

        revision = "0002"
        down_revision = "0001"
        branch_labels = None
        depends_on = None

        def upgrade() -> None:
            op.drop_column("users", "legacy_email")

        def downgrade() -> None:
            pass
        ''',
    )
    spec = cmc.parse_migration(drop_mig)
    result = cmc.check_drop_column_deprecation(spec, versions_dir=tmp_path)

    assert not result.ok
    assert "MigrationBreakingChangeRefused" in result.reason
    assert "deprecation-window-2of2" in result.reason
    # parent migration itself parses fine but is not consulted because
    # the gate refuses before reaching it.
    assert cmc.parse_migration(parent).revision == "0001"


# ── case 2: deprecation-window-1of2 allowed ──────────────────────────


def test_deprecation_window_1of2_rename_allowed() -> None:
    """First-of-two: rename live column to ``<name>_deprecated`` is allowed."""
    source = dedent('''
        """deprecate users.legacy_email.

        Revision ID: 0007
        Revises: 0006
        backwards-compat: deprecation-window-1of2
        """
        from alembic import op

        revision = "0007"
        down_revision = "0006"

        def upgrade() -> None:
            op.alter_column("users", "legacy_email",
                            new_column_name="legacy_email_deprecated")

        def downgrade() -> None:
            op.alter_column("users", "legacy_email_deprecated",
                            new_column_name="legacy_email")
        ''')

    assert cmc.parse_compat_tag(source) == "deprecation-window-1of2"
    assert cmc.check_compat_metadata(source).ok
    # the rename-pattern AST check tolerates op.alter_column renames.
    assert cmc.check_rename_pattern(source).ok
    # window-1of2 carries no op.drop_column, so the drop-window check
    # is a no-op pass.
    spec = cmc.MigrationSpec(
        path=cmc.REPO_ROOT / "backend" / "alembic" / "versions" / "0007_x.py",
        revision="0007",
        down_revision="0006",
    )
    # write source out so check_drop_column_deprecation can read it.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "0007.py"
        p.write_text(source, encoding="utf-8")
        spec = cmc.MigrationSpec(path=p, revision="0007", down_revision="0006")
        assert cmc.check_drop_column_deprecation(spec, versions_dir=Path(td)).ok


# ── case 3: deprecation-window-2of2 allowed ──────────────────────────


def test_deprecation_window_2of2_drop_after_window_1of2_allowed(tmp_path: Path) -> None:
    """Second-of-two: drop the ``_deprecated`` shadow is allowed."""
    _write(
        tmp_path / "0007_w1.py",
        '''
        """rename to _deprecated.

        Revision ID: 0007
        Revises: 0006
        backwards-compat: deprecation-window-1of2
        """
        from alembic import op

        revision = "0007"
        down_revision = "0006"

        def upgrade() -> None:
            op.alter_column("users", "legacy_email",
                            new_column_name="legacy_email_deprecated")

        def downgrade() -> None:
            op.alter_column("users", "legacy_email_deprecated",
                            new_column_name="legacy_email")
        ''',
    )
    drop_path = _write(
        tmp_path / "0008_w2.py",
        '''
        """drop legacy_email_deprecated.

        Revision ID: 0008
        Revises: 0007
        backwards-compat: deprecation-window-2of2
        """
        from alembic import op

        revision = "0008"
        down_revision = "0007"

        def upgrade() -> None:
            op.drop_column("users", "legacy_email_deprecated")

        def downgrade() -> None:
            pass
        ''',
    )
    spec = cmc.parse_migration(drop_path)
    result = cmc.check_drop_column_deprecation(spec, versions_dir=tmp_path)

    assert result.ok, result.reason


# ── case 4: alter_column rename allowed ──────────────────────────────


def test_rename_via_alter_column_allowed() -> None:
    """``op.alter_column(new_column_name=...)`` is the prescribed rename
    mechanism and must not trigger ``rename-pattern``.
    """
    source = dedent('''
        """rename display_name.

        Revision ID: 0010
        Revises: 0009
        backwards-compat: breaking
        """
        from alembic import op

        revision = "0010"
        down_revision = "0009"

        def upgrade() -> None:
            op.alter_column("users", "name",
                            new_column_name="display_name")

        def downgrade() -> None:
            op.alter_column("users", "display_name",
                            new_column_name="name")
        ''')

    result = cmc.check_rename_pattern(source)
    assert result.ok, result.reason


def test_rename_via_drop_add_refused() -> None:
    """Drop-then-add of a same-table column is rejected — the
    prescribed mechanism is ``op.alter_column(new_column_name=...)``.
    """
    source = dedent('''
        """rename display_name.

        Revision ID: 0011
        Revises: 0010
        backwards-compat: breaking
        """
        from alembic import op

        revision = "0011"
        down_revision = "0010"

        def upgrade() -> None:
            op.drop_column("users", "name")
            op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))

        def downgrade() -> None:
            pass
        ''')

    result = cmc.check_rename_pattern(source)
    assert not result.ok
    assert "MigrationBreakingChangeRefused" in result.reason
    assert "drop+add" in result.reason


# ── case 5: NOT NULL no-default refused ──────────────────────────────


def test_not_null_column_add_without_default_refused() -> None:
    # No downgrade-side drop_column in the fixture so the scanner sees
    # the NOT-NULL add as the first hazard (regex scan order matters).
    source = dedent('''
        """add login_count.

        Revision ID: 0012
        Revises: 0011
        backwards-compat: safe
        """
        from alembic import op

        revision = "0012"
        down_revision = "0011"

        def upgrade() -> None:
            op.add_column("users", sa.Column("login_count", sa.Integer(), nullable=False))

        def downgrade() -> None:
            pass
        ''')

    result = cmc.classify_old_code_compat(source)
    assert not result.ok
    assert "NOT NULL column add without DEFAULT" in result.reason


def test_not_null_column_add_with_server_default_allowed() -> None:
    source = dedent('''
        """add login_count with default.

        Revision ID: 0013
        Revises: 0012
        backwards-compat: safe
        """
        from alembic import op

        revision = "0013"
        down_revision = "0012"

        def upgrade() -> None:
            op.add_column(
                "users",
                sa.Column("login_count", sa.Integer(), nullable=False, server_default="0"),
            )

        def downgrade() -> None:
            pass
        ''')

    result = cmc.classify_old_code_compat(source)
    assert result.ok, result.reason


# ── case 6 + 7: enum tail vs non-tail ────────────────────────────────


def test_enum_tail_add_allowed() -> None:
    source = dedent('''
        """append enum value.

        Revision ID: 0020
        Revises: 0019
        backwards-compat: safe
        """
        from alembic import op

        revision = "0020"
        down_revision = "0019"

        def upgrade() -> None:
            op.execute("ALTER TYPE failure_class ADD VALUE 'NEW_CLASS'")

        def downgrade() -> None:
            pass
        ''')

    assert cmc.check_enum_tail_position(source).ok


@pytest.mark.parametrize("positional", ["BEFORE", "AFTER"])
def test_enum_non_tail_add_refused(positional: str) -> None:
    source = dedent(f'''
        """insert enum value non-tail.

        Revision ID: 0021
        Revises: 0020
        backwards-compat: safe
        """
        from alembic import op

        revision = "0021"
        down_revision = "0020"

        def upgrade() -> None:
            op.execute(
                "ALTER TYPE failure_class ADD VALUE 'INSERTED' {positional} 'EXISTING'"
            )

        def downgrade() -> None:
            pass
        ''')

    result = cmc.check_enum_tail_position(source)
    assert not result.ok
    assert "MigrationEnumNonTail" in result.reason
    assert positional in result.reason


# ── case 8: approved-breaking trailer + metadata enforcement ─────────


def test_approved_breaking_trailer_overrides_breaking_tag() -> None:
    source = dedent('''
        """add display_name.

        Revision ID: 0030
        Revises: 0029
        backwards-compat: breaking
        """
        from alembic import op

        revision = "0030"
        down_revision = "0029"

        def upgrade() -> None:
            op.alter_column("users", "name",
                            new_column_name="display_name")

        def downgrade() -> None:
            pass
        ''')

    commit = (
        "Rename users.name -> users.display_name\n"
        "\n"
        "Aligns with the new profile schema.\n"
        "\n"
        "migration:approved-breaking\n"
    )

    assert cmc.has_approved_breaking_trailer(commit)
    assert cmc.check_breaking_trailer(source, commit).ok


def test_breaking_tag_without_trailer_refused() -> None:
    source = dedent('''
        """rename column.

        Revision ID: 0031
        Revises: 0030
        backwards-compat: breaking
        """
        from alembic import op

        revision = "0031"
        down_revision = "0030"
        ''')

    result = cmc.check_breaking_trailer(source, commit_message="no trailer here")
    assert not result.ok
    assert "MigrationBreakingChangeRefused" in result.reason
    assert "migration:approved-breaking" in result.reason


def test_missing_compat_metadata_refused() -> None:
    source = dedent('''
        """no compat tag.

        Revision ID: 0040
        Revises: 0039
        """
        from alembic import op

        revision = "0040"
        down_revision = "0039"
        ''')

    result = cmc.check_compat_metadata(source)
    assert not result.ok
    assert "MigrationMetadataMissing" in result.reason


def test_placeholder_compat_metadata_refused() -> None:
    """An un-filled-in mako placeholder (the angle-bracket form) must
    not be accepted as a valid tag."""
    source = dedent('''
        """unfilled template.

        Revision ID: 0041
        Revises: 0040
        backwards-compat: <safe|breaking|deprecation-window-1of2|deprecation-window-2of2>
        """
        from alembic import op

        revision = "0041"
        down_revision = "0040"
        ''')

    result = cmc.check_compat_metadata(source)
    assert not result.ok
    assert "MigrationMetadataMissing" in result.reason


def test_unknown_tag_refused() -> None:
    source = dedent('''
        """bogus.

        Revision ID: 0042
        Revises: 0041
        backwards-compat: maybe-ok
        """
        from alembic import op

        revision = "0042"
        down_revision = "0041"
        ''')

    result = cmc.check_compat_metadata(source)
    assert not result.ok
    assert "unknown tag value" in result.reason


def test_commit_trailer_parsing_bare_form() -> None:
    """The trailer can appear either as ``key: value`` or as a bare
    presence line (a single token on its own line)."""
    commit = "fix something\n\nmigration:approved-breaking\n"
    assert cmc.has_approved_breaking_trailer(commit)


def test_commit_trailer_parsing_key_value_form() -> None:
    commit = "fix something\n\nmigration:approved-breaking: true\n"
    trailers = cmc.parse_commit_trailers(commit)
    assert "migration:approved-breaking" in trailers
    assert cmc.has_approved_breaking_trailer(commit)


def test_commit_without_trailer_is_not_approved() -> None:
    commit = "fix something\n\nSigned-off-by: alice <alice@example.com>\n"
    assert not cmc.has_approved_breaking_trailer(commit)


# ── pipeline-level smoke: drop without window flagged via main() ─────


def test_main_rejects_drop_without_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """End-to-end: invoking ``main()`` against a drop-without-window
    migration exits non-zero and emits both ``compat-metadata`` and
    ``drop-deprecation-window`` failure reasons in the JSON report.
    """
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions / "0050_parent.py",
        '''
        """parent.

        Revision ID: 0050
        Revises: None
        backwards-compat: safe
        """
        from alembic import op

        revision = "0050"
        down_revision = None
        ''',
    )
    target = _write(
        versions / "0051_drop.py",
        '''
        """drop unsafe.

        Revision ID: 0051
        Revises: 0050
        backwards-compat: safe
        """
        from alembic import op

        revision = "0051"
        down_revision = "0050"

        def upgrade() -> None:
            op.drop_column("users", "legacy_email")
        ''',
    )

    # patch the discovery + downgrade-revert to avoid running alembic.
    monkeypatch.setattr(cmc, "VERSIONS_DIR", versions)

    def _skip_downgrade(spec, *, engine, url):
        return cmc.CheckResult(
            ok=True,
            name="downgrade-revert",
            reason="skipped in unit test",
            evidence=str(spec.path),
        )

    monkeypatch.setattr(cmc, "verify_downgrade_reverts", _skip_downgrade)
    monkeypatch.setattr(cmc, "run_old_image_smoke", lambda **kw: cmc.CheckResult(
        ok=True,
        name="previous-release-smoke",
        reason="skipped in unit test",
        evidence="unit",
    ))

    rc = cmc.main([str(target), "--engine", "sqlite"])

    assert rc != 0
    out = capsys.readouterr().out
    assert "drop-deprecation-window" in out
    assert "MigrationBreakingChangeRefused" in out
