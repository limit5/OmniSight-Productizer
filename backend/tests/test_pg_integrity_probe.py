"""OP-1172 -- alembic_version integrity probe tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend import alembic_startup_hook as hook
from backend.pg_integrity_probe import (
    AlembicVersionIntegrityError,
    verify_alembic_version_table_integrity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "backend" / "alembic"


def _write_db(path: Path, rows: list[str] | None) -> None:
    conn = sqlite3.connect(path)
    try:
        if rows is not None:
            conn.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                [(row,) for row in rows],
            )
        else:
            conn.execute("CREATE TABLE placeholder (x INTEGER)")
        conn.commit()
    finally:
        conn.close()


def test_probe_passes_on_valid_alembic_version_row(tmp_path: Path) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_DIR))
    head = ScriptDirectory.from_config(cfg).get_heads()[0]
    db = tmp_path / "valid.db"
    _write_db(db, [head])

    verify_alembic_version_table_integrity(f"sqlite:///{db}")


def test_probe_fails_on_missing_table(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    _write_db(db, None)

    with pytest.raises(AlembicVersionIntegrityError) as exc_info:
        verify_alembic_version_table_integrity(f"sqlite:///{db}")

    assert exc_info.value.to_log_payload()["reason"] == "missing_table"


def test_probe_fails_on_multiple_rows(tmp_path: Path) -> None:
    db = tmp_path / "multiple.db"
    _write_db(db, ["0203", "0204"])

    with pytest.raises(AlembicVersionIntegrityError) as exc_info:
        verify_alembic_version_table_integrity(f"sqlite:///{db}")

    payload = exc_info.value.to_log_payload()
    assert payload["reason"] == "unexpected_row_count"
    assert payload["row_count"] == 2


def test_probe_fails_on_unknown_revision_id(tmp_path: Path) -> None:
    db = tmp_path / "unknown.db"
    _write_db(db, ["not_a_revision"])

    with pytest.raises(AlembicVersionIntegrityError) as exc_info:
        verify_alembic_version_table_integrity(f"sqlite:///{db}")

    payload = exc_info.value.to_log_payload()
    assert payload["reason"] == "unknown_revision_id"
    assert payload["version_num"] == "not_a_revision"


def test_startup_hook_exits_78_on_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(
        '{"alembic_head_in_image":"rev_image"}',
        encoding="utf-8",
    )
    err = AlembicVersionIntegrityError(reason="missing_table")
    monkeypatch.setattr(
        hook,
        "verify_alembic_version_table_integrity",
        lambda db_url: (_ for _ in ()).throw(err),
    )

    with pytest.raises(SystemExit) as exc_info:
        hook.maybe_run_startup_upgrade(
            db_url="postgresql://db",
            image_head_path=str(manifest),
        )

    assert exc_info.value.code == 78
    stderr = capsys.readouterr().err
    assert '"event": "alembic_version_corrupt"' in stderr
    assert '"remediation": "run pg integrity probe + rescue CLI"' in stderr
