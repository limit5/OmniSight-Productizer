"""OP-1127 (Boreas-A2) — Alembic image-DB drift gate.

Refuses backend start when the migration head shipped in the image differs
from the alembic_version recorded in the live DB.

Background incident: a container built at alembic head ``0200`` started
against a DB that had been hot-fix migrated to ``0202`` out-of-band.
``/readyz`` reported "healthy" briefly, then logic errors surfaced because
code expected the pre-``0202`` schema. Silent drift produced real bugs
before the readiness probe caught it.

The gate runs **before** uvicorn binds the listen port (wired in
``Dockerfile.backend`` CMD and ``deploy/systemd/omnisight-backend.service``
ExecStartPre) and:

* Compares the script-directory heads (image's view of the migration tree)
  against ``MigrationContext.get_current_heads()`` (DB's view).
* On mismatch: prints a structured JSON diagnostic to stderr
  (``image_head``, ``db_head``, ``drift_direction``) and exits ``1``.
  **No recovery is attempted** — operator decides whether to upgrade the
  DB (``alembic upgrade head``) or roll the image back to a version whose
  heads match the DB. Recovery here would mask the divergence and was the
  exact failure mode the incident exposed.
* On match: exits ``0`` so the next stage in CMD/ExecStart can run.

Exit codes:

* ``0`` — heads match, OR the gate was intentionally bypassed via
  ``OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE=1`` (emergency escape hatch), OR
  the DB has no ``alembic_version`` table yet (fresh install — migrations
  have not been run, no drift possible).
* ``1`` — drift detected. Structured diagnostic on stderr.
* ``2`` — drift check itself failed (script dir unreadable, DB
  unreachable). Operator must investigate before letting backend start.

Drift direction classification (sets of head revisions):

* ``image_ahead``  — every DB head is also an image head, image has extras
  (DB needs ``alembic upgrade head``).
* ``db_ahead``     — every image head is also a DB head, DB has extras
  (image is older than DB; upgrade image or downgrade DB).
* ``divergent``    — neither set is a subset of the other (rare; usually
  a botched merge or a partial restore from a backup).

Where to look for the diagnostic:

* docker compose: ``docker compose logs backend-a backend-b`` (the gate's
  stderr line goes to the container's combined log stream).
* systemd:        ``journalctl -u omnisight-backend -n 200 --no-pager``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# These imports are local to keep ``python -m backend.alembic_drift_gate``
# fast on the happy path (no Postgres driver needed if we exit early).


def _emit(payload: dict[str, Any]) -> None:
    """Single-line JSON to stderr. Format pinned so operators / log scrapers
    can grep ``"alembic_drift_gate"``.
    """
    payload.setdefault("event", "alembic_drift_gate")
    payload.setdefault("ts", time.time())
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stderr.flush()


def _resolve_db_url() -> str | None:
    """Mirror ``backend/alembic/env.py::_resolve_db_url`` resolution order.

    Returns ``None`` when no DB target is configured (SQLite dev mode with
    no ``OMNISIGHT_DATABASE_PATH`` and no remote DSN) — the gate then
    exits 0 because there is no DB to drift against.
    """
    full = os.environ.get("SQLALCHEMY_URL", "").strip()
    if full:
        return full
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL"):
        url = os.environ.get(key, "").strip()
        if url:
            try:
                from backend.db_url import parse  # type: ignore
            except Exception:
                return url
            return parse(url).sqlalchemy_url(sync=True)
    env = os.environ.get("OMNISIGHT_DATABASE_PATH", "").strip()
    if env:
        return f"sqlite:///{env}"
    return None


def _script_dir_path() -> Path:
    """Locate ``backend/alembic`` relative to this file so the gate works
    both from the source tree and from inside the image (where ``/app/backend``
    is the package root).
    """
    return Path(__file__).resolve().parent / "alembic"


def _classify_drift(
    image_heads: Iterable[str], db_heads: Iterable[str]
) -> str:
    image_set = set(image_heads)
    db_set = set(db_heads)
    if image_set == db_set:
        return "match"
    if db_set and db_set.issubset(image_set):
        return "image_ahead"
    if image_set and image_set.issubset(db_set):
        return "db_ahead"
    return "divergent"


def check_drift(db_url: str, script_dir: Path) -> tuple[int, dict[str, Any]]:
    """Return ``(exit_code, diagnostic)``.

    Pure function for easy unit-testing — ``main()`` is the thin wrapper
    that ``_emit``s the diagnostic and exits.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, pool
    from sqlalchemy.exc import SQLAlchemyError

    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    try:
        script = ScriptDirectory.from_config(cfg)
        image_heads = tuple(sorted(script.get_heads()))
    except Exception as exc:
        return 2, {
            "level": "error",
            "reason": "script_directory_unreadable",
            "script_dir": str(script_dir),
            "error_type": type(exc).__name__,
            "error_msg": str(exc),
        }

    try:
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "", 1)
            if db_path and not Path(db_path).exists():
                # Fresh dev install — no DB file at all means no drift to
                # check (migrations will create the file + version row on
                # first ``alembic upgrade head``). Gate stays open.
                return 0, {
                    "level": "info",
                    "reason": "sqlite_db_file_absent",
                    "db_path": db_path,
                    "image_heads": list(image_heads),
                }
        engine = create_engine(db_url, poolclass=pool.NullPool)
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            db_heads = tuple(sorted(ctx.get_current_heads()))
    except SQLAlchemyError as exc:
        return 2, {
            "level": "error",
            "reason": "db_unreachable",
            "db_url_redacted": _redact(db_url),
            "error_type": type(exc).__name__,
            "error_msg": str(exc),
        }
    except Exception as exc:
        return 2, {
            "level": "error",
            "reason": "db_check_failed",
            "db_url_redacted": _redact(db_url),
            "error_type": type(exc).__name__,
            "error_msg": str(exc),
        }

    if not db_heads:
        # Empty ``alembic_version`` table (or table absent) ⇒ DB has
        # never been migrated. Treat as fresh — the backend's own
        # startup-time migration runner (or operator) will populate it.
        return 0, {
            "level": "info",
            "reason": "db_has_no_alembic_version",
            "image_heads": list(image_heads),
            "db_heads": [],
        }

    drift_direction = _classify_drift(image_heads, db_heads)
    if drift_direction == "match":
        return 0, {
            "level": "info",
            "reason": "heads_match",
            "image_heads": list(image_heads),
            "db_heads": list(db_heads),
        }

    return 1, {
        "level": "fatal",
        "reason": "alembic_head_drift",
        "image_head": list(image_heads),
        "db_head": list(db_heads),
        "drift_direction": drift_direction,
        "remediation_hint": (
            "Run `alembic upgrade head` against the DB"
            if drift_direction == "image_ahead"
            else "Re-deploy a backend image whose alembic head matches the DB"
            if drift_direction == "db_ahead"
            else "Heads diverge — inspect alembic_version rows and the "
            "script tree, then merge or restore from backup"
        ),
    }


def _redact(url: str) -> str:
    """Strip the password from a SQLAlchemy URL for log output."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE", "").strip() == "1":
        _emit({"level": "warning", "reason": "skip_gate_env_set"})
        return 0

    db_url = _resolve_db_url()
    if db_url is None:
        _emit({"level": "info", "reason": "no_db_configured_skip"})
        return 0

    code, payload = check_drift(db_url, _script_dir_path())
    _emit(payload)
    return code


if __name__ == "__main__":  # pragma: no cover — entrypoint
    sys.exit(main())
