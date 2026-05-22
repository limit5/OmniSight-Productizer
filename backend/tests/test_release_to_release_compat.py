"""OP-1588 [RT-11] -- previous-final → candidate rollback-compat gate.

Covers the script half of RT-11 (``scripts.check_migration_compat``
release-to-release mode):

* :func:`classify_rollback_safety` -- per-migration rollback verdict,
  fail-closed on a missing tag, unsafe for breaking / deprecation-window
  / statically-destructive migrations.
* :func:`migrations_between` -- the previous-final → candidate delta walk.
* :func:`check_release_to_release_compat` -- aggregate verdict.
* ``main(--release-to-release ...)`` -- a rollback-unsafe migration
  exits non-zero (BLOCK); ``--break-glass --approved-by sora`` flips it
  to exit 0 and appends an audit row.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from scripts import check_migration_compat as cmc


def _write(path: Path, source: str) -> Path:
    path.write_text(dedent(source), encoding="utf-8")
    return path


def _migration(rev: str, down: str | None, tag: str, body: str = "pass") -> str:
    down_lit = "None" if down is None else f'"{down}"'
    return dedent(
        f'''
        """migration {rev}.

        Revision ID: {rev}
        Revises: {down}
        backwards-compat: {tag}
        """
        from alembic import op

        revision = "{rev}"
        down_revision = {down_lit}
        branch_labels = None
        depends_on = None

        def upgrade() -> None:
            {body}

        def downgrade() -> None:
            pass
        '''
    )


# ── classify_rollback_safety ──────────────────────────────────────────


def test_safe_additive_migration_is_rollback_safe() -> None:
    source = _migration(
        "0002", "0001", "safe",
        'op.add_column("users", sa.Column("nickname", sa.Text(), nullable=True))',
    )
    result = cmc.classify_rollback_safety(source)
    assert result.ok, result.reason


def test_breaking_tag_is_rollback_unsafe() -> None:
    source = _migration("0002", "0001", "breaking", "pass")
    result = cmc.classify_rollback_safety(source)
    assert not result.ok
    assert "rollback-unsafe" in result.reason
    assert "breaking" in result.reason


@pytest.mark.parametrize(
    "tag", ["deprecation-window-1of2", "deprecation-window-2of2"]
)
def test_deprecation_window_tags_are_rollback_unsafe(tag: str) -> None:
    source = _migration("0002", "0001", tag, "pass")
    result = cmc.classify_rollback_safety(source)
    assert not result.ok
    assert "rollback-unsafe" in result.reason


def test_safe_tag_but_destructive_op_is_rollback_unsafe() -> None:
    # tag says safe, but the static scan catches a drop -> still unsafe.
    source = _migration(
        "0002", "0001", "safe", 'op.drop_column("users", "legacy")'
    )
    result = cmc.classify_rollback_safety(source)
    assert not result.ok
    assert "rollback-unsafe" in result.reason


def test_missing_tag_is_rollback_unsafe_fail_closed() -> None:
    source = dedent(
        '''
        """no compat tag.

        Revision ID: 0002
        Revises: 0001
        """
        revision = "0002"
        down_revision = "0001"
        '''
    )
    result = cmc.classify_rollback_safety(source)
    assert not result.ok
    assert "fail-closed" in result.reason


# ── migrations_between ────────────────────────────────────────────────


def test_migrations_between_returns_only_the_delta(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration("0002", "0001", "safe"))
    _write(tmp_path / "0003.py", _migration("0003", "0002", "safe"))
    _write(tmp_path / "0004.py", _migration("0004", "0003", "safe"))

    delta = cmc.migrations_between("0002", "0004", versions_dir=tmp_path)
    assert [s.revision for s in delta] == ["0003", "0004"]


def test_migrations_between_equal_heads_is_empty(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration("0002", "0001", "safe"))
    assert cmc.migrations_between("0002", "0002", versions_dir=tmp_path) == []


def test_migrations_between_unknown_candidate_raises(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    with pytest.raises(ValueError, match="candidate head"):
        cmc.migrations_between("0001", "9999", versions_dir=tmp_path)


def test_migrations_between_unknown_previous_raises(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration("0002", "0001", "safe"))
    with pytest.raises(ValueError, match="previous-final head"):
        cmc.migrations_between("9999", "0002", versions_dir=tmp_path)


# ── check_release_to_release_compat ───────────────────────────────────


def test_all_safe_delta_is_rollback_safe(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration(
        "0002", "0001", "safe",
        'op.add_column("users", sa.Column("nick", sa.Text(), nullable=True))',
    ))
    _write(tmp_path / "0003.py", _migration("0003", "0002", "safe"))

    result = cmc.check_release_to_release_compat(
        "0001", "0003", versions_dir=tmp_path
    )
    assert result.rollback_safe, result.reason
    assert result.checked_migrations == ("0002.py", "0003.py") or set(
        Path(m).name for m in result.checked_migrations
    ) == {"0002.py", "0003.py"}


def test_unsafe_migration_in_delta_blocks(tmp_path: Path) -> None:
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration("0002", "0001", "safe"))
    _write(tmp_path / "0003.py", _migration(
        "0003", "0002", "breaking", 'op.drop_table("legacy")'
    ))

    result = cmc.check_release_to_release_compat(
        "0001", "0003", versions_dir=tmp_path
    )
    assert not result.rollback_safe
    assert any(Path(m).name == "0003.py" for m in result.unsafe_migrations)


def test_unsafe_migration_already_in_previous_is_not_rechecked(tmp_path: Path) -> None:
    # 0002 is breaking but is part of the *previous-final* release; only
    # the 0001->0003 *new* delta is evaluated for the promote.
    _write(tmp_path / "0001.py", _migration("0001", None, "safe"))
    _write(tmp_path / "0002.py", _migration("0002", "0001", "breaking"))
    _write(tmp_path / "0003.py", _migration("0003", "0002", "safe"))

    result = cmc.check_release_to_release_compat(
        "0002", "0003", versions_dir=tmp_path
    )
    assert result.rollback_safe, result.reason


# ── CLI: --release-to-release block + break-glass ─────────────────────


def _seed_unsafe_tree(tmp_path: Path) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions / "0001.py", _migration("0001", None, "safe"))
    _write(versions / "0002.py", _migration(
        "0002", "0001", "breaking", 'op.drop_column("users", "email")'
    ))
    return versions


def test_main_release_to_release_blocks_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = _seed_unsafe_tree(tmp_path)
    monkeypatch.setattr(cmc, "VERSIONS_DIR", versions)

    rc = cmc.main([
        "--release-to-release",
        "--previous-final-head", "0001",
        "--candidate-head", "0002",
    ])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["rollback_safe"] is False
    assert report["verdict"] == "Verified -1"
    assert any(Path(m).name == "0002.py" for m in report["unsafe_migrations"])


def test_main_break_glass_requires_sora(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = _seed_unsafe_tree(tmp_path)
    monkeypatch.setattr(cmc, "VERSIONS_DIR", versions)
    audit_log = tmp_path / "audit.jsonl"

    rc = cmc.main([
        "--release-to-release",
        "--previous-final-head", "0001",
        "--candidate-head", "0002",
        "--break-glass",
        "--approved-by", "operator",
        "--audit-log", str(audit_log),
    ])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert "requires --approved-by sora" in report["break_glass"]
    assert not audit_log.exists()


def test_main_break_glass_with_sora_passes_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = _seed_unsafe_tree(tmp_path)
    monkeypatch.setattr(cmc, "VERSIONS_DIR", versions)
    audit_log = tmp_path / "audit.jsonl"

    rc = cmc.main([
        "--release-to-release",
        "--previous-final-head", "0001",
        "--candidate-head", "0002",
        "--break-glass",
        "--approved-by", "sora",
        "--audit-log", str(audit_log),
        "--ticket", "OP-1588",
    ])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "break-glass"

    row = json.loads(audit_log.read_text(encoding="utf-8"))
    assert row["event"] == "release_to_release_break_glass"
    assert row["approved_by"] == "sora"
    assert row["ticket"] == "OP-1588"
    assert row["candidate_head"] == "0002"
    assert any(Path(m).name == "0002.py" for m in row["unsafe_migrations"])
