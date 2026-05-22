"""OP-1591 / RT-13a -- release_train_status.py resolver tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import release_train_status as rts  # noqa: E402


SHA = "0123456789abcdef0123456789abcdef01234567"
BACKEND_DIGEST = "sha256:" + "1" * 64
FRONTEND_DIGEST = "sha256:" + "2" * 64
VERSION = "v1.2.3"


def _version_payload() -> dict:
    return {
        "build_git_sha": SHA,
        "build_git_ref": "refs/heads/develop",
        "deployed_tag": VERSION,
        "deployed_digest_backend": BACKEND_DIGEST,
        "deployed_digest_frontend": FRONTEND_DIGEST,
        "promotion_audit_id": "99",
    }


def _compose_lock() -> dict:
    return {
        "env": "prod",
        "images": {
            "backend": {
                "repository": "registry.example/omnisight-backend",
                "digest": BACKEND_DIGEST,
            },
            "frontend": {
                "repository": "registry.example/omnisight-frontend",
                "digest": FRONTEND_DIGEST,
            },
        },
    }


def _audit_row() -> dict:
    return {
        "id": 99,
        "ts": "2026-05-22T00:00:00Z",
        "outcome": "promoted",
        "fix_version": VERSION,
        "develop_sha": SHA,
        "main_sha": "",
        "detail": json.dumps(
            {
                "version": VERSION,
                "git_sha": SHA,
                "digests": {
                    "backend": BACKEND_DIGEST,
                    "frontend": FRONTEND_DIGEST,
                },
            }
        ),
    }


def test_status_report_joins_overlay_lock_registry_and_audit() -> None:
    audit = rts._audit_release_from_row(_audit_row(), VERSION)
    assert audit is not None

    def registry(ref: str) -> str:
        if ref == f"registry.example/omnisight-backend:{VERSION}":
            return BACKEND_DIGEST
        if ref == f"registry.example/omnisight-frontend:{VERSION}":
            return FRONTEND_DIGEST
        raise AssertionError(ref)

    report = rts.build_status_report(
        env="prod",
        version_payload=_version_payload(),
        compose_lock=_compose_lock(),
        audit_release=audit,
        registry_resolver=registry,
    )

    assert report.version == VERSION
    assert report.git_sha == SHA
    assert report.digests.as_dict() == {
        "backend": BACKEND_DIGEST,
        "frontend": FRONTEND_DIGEST,
    }
    assert rts.render_status(report) == (
        f"prod {VERSION} == {SHA} == "
        f"{{backend:{BACKEND_DIGEST}, frontend:{FRONTEND_DIGEST}}}"
    )


def test_status_report_rejects_registry_drift() -> None:
    audit = rts._audit_release_from_row(_audit_row(), VERSION)
    assert audit is not None

    with pytest.raises(rts.IdentityMismatch, match="registry digest pair"):
        rts.build_status_report(
            env="prod",
            version_payload=_version_payload(),
            compose_lock=_compose_lock(),
            audit_release=audit,
            registry_resolver=lambda _ref: "sha256:" + "9" * 64,
        )


def test_rollback_to_version_resolves_digest_pair_from_sqlite_audit(
    tmp_path: Path,
) -> None:
    db = tmp_path / "release_audit.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE release_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            outcome TEXT NOT NULL,
            fix_version TEXT,
            develop_sha TEXT NOT NULL DEFAULT '',
            main_sha TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    row = _audit_row()
    conn.execute(
        """
        INSERT INTO release_audit (
            id, ts, outcome, fix_version, develop_sha, main_sha, detail
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            row["ts"],
            row["outcome"],
            row["fix_version"],
            row["develop_sha"],
            row["main_sha"],
            row["detail"],
        ),
    )
    conn.commit()
    conn.close()

    release = rts.load_audit_release(VERSION, audit_db=db)

    assert release.version == VERSION
    assert release.git_sha == SHA
    assert release.digests.backend == BACKEND_DIGEST
    assert release.digests.frontend == FRONTEND_DIGEST
    assert rts.render_rollback(release) == (
        f"rollback {VERSION} -> backend={BACKEND_DIGEST} "
        f"frontend={FRONTEND_DIGEST} git_sha={SHA}"
    )
