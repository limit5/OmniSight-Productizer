"""OP-1598 -- GitLab CR sha-tag retention and release-train protection."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RETENTION_SCRIPT = REPO_ROOT / "scripts" / "enforce_image_retention.py"

NOW = datetime(2026, 5, 22, tzinfo=timezone.utc)
OLD = datetime(2026, 4, 1, tzinfo=timezone.utc)
SHA_TAG = "sha-" + "a" * 40
DIGEST_OLD = "sha256:" + "1" * 64
DIGEST_PROMOTED = "sha256:" + "2" * 64
DIGEST_AUDIT = "sha256:" + "3" * 64


def _load_retention_module():
    spec = importlib.util.spec_from_file_location(
        "_test_enforce_image_retention",
        RETENTION_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_gitlab_sha_tag_older_than_window_is_deleted() -> None:
    retention = _load_retention_module()
    versions = retention.normalize_gitlab_tags(
        [
            {
                "name": SHA_TAG,
                "updated_at": "2026-04-01T00:00:00Z",
                "digest": DIGEST_OLD,
            }
        ]
    )

    decisions = retention.retention_decisions(versions, now=NOW, sha_keep_days=14)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.tag_class == "sha"
    assert decision.delete is True
    assert decision.reason == "unpromoted sha tag older than 14d"


def test_release_final_tag_is_kept_forever() -> None:
    retention = _load_retention_module()
    versions = [
        retention.PackageVersion(
            version_id="v2.5.0",
            tags=("v2.5.0",),
            updated_at=OLD,
            digest=DIGEST_OLD,
        )
    ]

    decisions = retention.retention_decisions(versions, now=NOW, sha_keep_days=14)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.tag_class == "release"
    assert decision.delete is False
    assert decision.reason == "immutable release-line tag"


def test_release_train_source_digest_protects_old_sha_tag() -> None:
    retention = _load_retention_module()
    versions = [
        retention.PackageVersion(
            version_id=SHA_TAG,
            tags=(SHA_TAG,),
            updated_at=OLD,
            digest=DIGEST_PROMOTED,
        )
    ]

    decisions = retention.retention_decisions(
        versions,
        now=NOW,
        protected_digests={DIGEST_PROMOTED},
        sha_keep_days=14,
    )

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.tag_class == "sha"
    assert decision.delete is False
    assert decision.reason == "digest referenced by release_train/release_audit protection input"


def test_release_audit_json_detail_digest_protects_old_sha_tag(tmp_path: Path) -> None:
    retention = _load_retention_module()
    audit_export = tmp_path / "release_audit.json"
    audit_export.write_text(
        '[{"id": 7, "detail": "{\\"backend_digest\\": \\"'
        + DIGEST_AUDIT
        + '\\"}"}]\n',
        encoding="utf-8",
    )
    versions = [
        retention.PackageVersion(
            version_id=SHA_TAG,
            tags=(SHA_TAG,),
            updated_at=OLD,
            digest=DIGEST_AUDIT,
        )
    ]

    protected = retention.protected_digests_from_json_documents([audit_export])
    decisions = retention.retention_decisions(
        versions,
        now=NOW,
        protected_digests=protected,
        sha_keep_days=14,
    )

    assert protected == {DIGEST_AUDIT}
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.tag_class == "sha"
    assert decision.delete is False
    assert decision.reason == "digest referenced by release_train/release_audit protection input"
