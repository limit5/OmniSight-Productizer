"""OP-1166 -- startup-time Alembic upgrade hook with advisory lock."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from backend import alembic_drift_gate
from backend.pg_integrity_probe import (
    AlembicVersionIntegrityError,
    verify_alembic_version_table_integrity,
)

SCRIPT_DIR = Path(__file__).resolve().parent / "alembic"
MANIFEST_HEAD_KEY = "alembic_head_in_image"
BACKWARD_REMEDIATION = (
    "deploy image >= db_head or run rescue CLI to downgrade DB"
)
LOCK_POLL_INTERVAL_S = 1.0

# Deterministic signed int64 derived from the literal lock namespace.  Every
# backend image must converge on the same key so rolling workers serialize
# ``alembic upgrade head`` across processes.
ALEMBIC_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"alembic_upgrade").digest()[:8],
    byteorder="big",
    signed=True,
)


class AlembicLockTimeout(RuntimeError):
    """Raised when the Alembic advisory lock cannot be acquired in time."""


class AlembicBackwardDrift(RuntimeError):
    """Raised when the DB is ahead of the image."""

    def __init__(self, *, image_head: str, db_head: str) -> None:
        self.image_head = image_head
        self.db_head = db_head
        self.remediation = BACKWARD_REMEDIATION
        super().__init__(json.dumps(self.to_log_payload(), sort_keys=True))

    def to_log_payload(self) -> dict[str, str]:
        return {
            "event": "alembic_drift_backward",
            "image_head": self.image_head,
            "db_head": self.db_head,
            "remediation": self.remediation,
        }


class AlembicManifestError(RuntimeError):
    """Raised when the image manifest is missing or malformed."""


def maybe_run_startup_upgrade(
    *,
    db_url: str,
    image_head_path: str = "/app/MANIFEST.json",
    lock_timeout_s: int = 60,
) -> str:
    """Run ``alembic upgrade head`` on forward drift, then return DB head."""

    image_head = _read_image_head(Path(image_head_path))
    try:
        verify_alembic_version_table_integrity(db_url)
    except AlembicVersionIntegrityError as exc:
        _exit_78_on_integrity_failure(exc)
    engine = create_engine(db_url, poolclass=NullPool)
    with engine.connect() as conn:
        _acquire_advisory_lock(conn, lock_timeout_s=lock_timeout_s)
        try:
            first = _check_drift(db_url)
            if _needs_upgrade(first):
                _upgrade_head(db_url)
                second = _check_drift(db_url)
                if _is_backward(second):
                    _raise_backward(second, image_head=image_head)
                if not _is_aligned(second):
                    raise RuntimeError(
                        "alembic startup upgrade did not align DB head: "
                        f"{second!r}"
                    )
                if not _manifest_matches_db(second, image_head=image_head):
                    _raise_backward(second, image_head=image_head)
                return _db_head(second, fallback=image_head)

            if _is_backward(first):
                _raise_backward(first, image_head=image_head)
            if _is_aligned(first):
                if not _manifest_matches_db(first, image_head=image_head):
                    _raise_backward(first, image_head=image_head)
                return _db_head(first, fallback=image_head)
            raise RuntimeError(f"alembic drift check failed: {first!r}")
        finally:
            _release_advisory_lock(conn)


def _read_image_head(path: Path) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AlembicManifestError(
            f"image manifest missing: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AlembicManifestError(
            f"image manifest is not valid JSON: {path}: {exc}"
        ) from exc
    head = manifest.get(MANIFEST_HEAD_KEY)
    if not isinstance(head, str) or not head.strip():
        raise AlembicManifestError(
            f"image manifest missing non-empty {MANIFEST_HEAD_KEY}: {path}"
        )
    return head.strip()


def _acquire_advisory_lock(
    conn: Connection, *, lock_timeout_s: int,
) -> None:
    deadline = time.monotonic() + lock_timeout_s
    while True:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": ALEMBIC_LOCK_KEY},
        ).scalar()
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise AlembicLockTimeout(
                "timed out waiting for alembic advisory lock "
                f"after {lock_timeout_s}s"
            )
        remaining = max(0.0, deadline - time.monotonic())
        time.sleep(min(LOCK_POLL_INTERVAL_S, remaining))


def _release_advisory_lock(conn: Connection) -> None:
    conn.execute(
        text("SELECT pg_advisory_unlock(:lock_key)"),
        {"lock_key": ALEMBIC_LOCK_KEY},
    )


def _check_drift(db_url: str) -> dict[str, Any]:
    code, payload = alembic_drift_gate.check_drift(db_url, SCRIPT_DIR)
    payload = dict(payload)
    payload["exit_code"] = code
    return payload


def _exit_78_on_integrity_failure(exc: AlembicVersionIntegrityError) -> None:
    sys.stderr.write(json.dumps(exc.to_log_payload(), sort_keys=True) + "\n")
    sys.stderr.flush()
    raise SystemExit(78) from exc


def _upgrade_head(db_url: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    prior = os.environ.get("SQLALCHEMY_URL")
    os.environ["SQLALCHEMY_URL"] = db_url
    try:
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("SQLALCHEMY_URL", None)
        else:
            os.environ["SQLALCHEMY_URL"] = prior


def _needs_upgrade(payload: dict[str, Any]) -> bool:
    return (
        payload.get("drift_direction") == "image_ahead"
        or payload.get("reason") == "db_has_no_alembic_version"
    )


def _is_aligned(payload: dict[str, Any]) -> bool:
    return (
        payload.get("drift_direction") == "match"
        or payload.get("reason") == "heads_match"
    )


def _is_backward(payload: dict[str, Any]) -> bool:
    return payload.get("drift_direction") in {"db_ahead", "divergent"}


def _manifest_matches_db(payload: dict[str, Any], *, image_head: str) -> bool:
    return _db_head(payload, fallback=image_head) == image_head


def _raise_backward(payload: dict[str, Any], *, image_head: str) -> None:
    raise AlembicBackwardDrift(
        image_head=image_head,
        db_head=_db_head(payload, fallback="unknown"),
    )


def _db_head(payload: dict[str, Any], *, fallback: str) -> str:
    return _head(
        payload.get("db_head") or payload.get("db_heads"),
        fallback=fallback,
    )


def _head(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (list, tuple)) and value:
        return ",".join(str(item) for item in value)
    return fallback
