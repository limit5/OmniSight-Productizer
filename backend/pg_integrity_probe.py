"""OP-1172 -- PostgreSQL alembic_version integrity probe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

SCRIPT_DIR = Path(__file__).resolve().parent / "alembic"
REMEDIATION = "run pg integrity probe + rescue CLI"


class AlembicVersionIntegrityError(RuntimeError):
    """Raised when the alembic_version table is missing or corrupt."""

    def __init__(self, *, reason: str, **details: Any) -> None:
        self.reason = reason
        self.details = details
        super().__init__(json.dumps(self.to_log_payload(), sort_keys=True))

    def to_log_payload(self) -> dict[str, Any]:
        return {
            "event": "alembic_version_corrupt",
            "reason": self.reason,
            "remediation": REMEDIATION,
            **self.details,
        }


def verify_alembic_version_table_integrity(
    db_url: str, script_dir: Path = SCRIPT_DIR,
) -> None:
    """Verify alembic_version exists, has one row, and names a script revision."""

    script = _script_directory(script_dir)
    heads = set(script.get_heads())
    engine = create_engine(db_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            if "alembic_version" not in inspect(conn).get_table_names():
                raise AlembicVersionIntegrityError(
                    reason="missing_table",
                    valid_heads=sorted(heads),
                )
            rows = [
                str(row[0])
                for row in conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchall()
            ]
    except AlembicVersionIntegrityError:
        raise
    except SQLAlchemyError as exc:
        raise AlembicVersionIntegrityError(
            reason="probe_failed",
            error_type=type(exc).__name__,
            error_msg=str(exc),
        ) from exc

    if len(rows) != 1:
        raise AlembicVersionIntegrityError(
            reason="unexpected_row_count",
            row_count=len(rows),
            valid_heads=sorted(heads),
        )
    try:
        script.get_revision(rows[0])
    except Exception as exc:
        raise AlembicVersionIntegrityError(
            reason="unknown_revision_id",
            version_num=rows[0],
            valid_heads=sorted(heads),
        ) from exc


def _script_directory(script_dir: Path) -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    try:
        return ScriptDirectory.from_config(cfg)
    except Exception as exc:
        raise AlembicVersionIntegrityError(
            reason="script_directory_unreadable",
            script_dir=str(script_dir),
            error_type=type(exc).__name__,
            error_msg=str(exc),
        ) from exc
