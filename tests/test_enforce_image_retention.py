"""[OP-1480] Tests for per-tag-class GHCR retention decisions."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "enforce_image_retention.py"


def _load_retention_module():
    spec = importlib.util.spec_from_file_location(
        "enforce_image_retention_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["enforce_image_retention_under_test"] = module
    spec.loader.exec_module(module)
    return module


retention = _load_retention_module()


NOW = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)


def _version(version_id: int, tags: tuple[str, ...], updated_at: str):
    return retention.PackageVersion(
        version_id=version_id,
        tags=tags,
        updated_at=retention.parse_timestamp(updated_at),
    )


def _decisions(versions):
    return {
        row.version_id: row
        for row in retention.retention_decisions(versions, now=NOW)
    }


def test_release_tag_is_kept_forever():
    decision = _decisions([
        _version(1, ("v1.2.3",), "2025-01-01T00:00:00Z"),
    ])[1]
    assert decision.action == "keep"
    assert decision.tag_class == "release"


def test_hotfix_tag_is_kept_forever():
    decision = _decisions([
        _version(1, ("v1.2.3-hotfix-1",), "2025-01-01T00:00:00Z"),
    ])[1]
    assert decision.action == "keep"
    assert decision.tag_class == "hotfix"


def test_release_candidate_is_kept_until_parent_window_expires():
    decisions = _decisions([
        _version(1, ("v1.2.3-rc1",), "2026-01-01T00:00:00Z"),
        _version(2, ("v1.2.3",), "2026-04-01T00:00:00Z"),
    ])
    assert decisions[1].action == "keep"
    assert decisions[1].tag_class == "release-candidate"


def test_release_candidate_is_deleted_after_parent_window_expires():
    decisions = _decisions([
        _version(1, ("v1.2.3-rc1",), "2025-01-01T00:00:00Z"),
        _version(2, ("v1.2.3",), "2025-12-01T00:00:00Z"),
    ])
    assert decisions[1].action == "delete"
    assert "parent release shipped" in decisions[1].reason


def test_release_candidate_without_parent_uses_thirty_day_window():
    decisions = _decisions([
        _version(1, ("v1.2.3-rc1",), "2026-05-01T00:00:00Z"),
        _version(2, ("v1.2.4-rc1",), "2026-03-01T00:00:00Z"),
    ])
    assert decisions[1].action == "keep"
    assert decisions[2].action == "delete"


def test_develop_tags_keep_newest_n_even_when_old():
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    versions = [
        retention.PackageVersion(
            version_id=i,
            tags=(f"develop-{i:012x}",),
            updated_at=base + timedelta(days=i),
        )
        for i in range(1, 33)
    ]
    decisions = {
        row.version_id: row
        for row in retention.retention_decisions(
            versions,
            now=NOW,
            develop_keep_days=14,
            develop_min_keep=30,
        )
    }
    assert decisions[32].action == "keep"
    assert decisions[3].action == "keep"
    assert decisions[2].action == "delete"
    assert decisions[1].action == "delete"


def test_develop_tags_keep_by_age_outside_newest_n():
    versions = [
        _version(i, (f"develop-{i:012x}",), "2026-05-10T00:00:00Z")
        for i in range(1, 33)
    ]
    decisions = {
        row.version_id: row
        for row in retention.retention_decisions(
            versions,
            now=NOW,
            develop_keep_days=14,
            develop_min_keep=30,
        )
    }
    assert decisions[1].action == "keep"
    assert decisions[2].action == "keep"


def test_feature_and_untagged_versions_expire_after_seven_days():
    decisions = _decisions([
        _version(1, ("feature-abcdef123456",), "2026-05-01T00:00:00Z"),
        _version(2, tuple(), "2026-05-01T00:00:00Z"),
        _version(3, ("feature-fedcba654321",), "2026-05-15T00:00:00Z"),
        _version(4, tuple(), "2026-05-15T00:00:00Z"),
    ])
    assert decisions[1].action == "delete"
    assert decisions[2].action == "delete"
    assert decisions[3].action == "keep"
    assert decisions[4].action == "keep"


def test_mutable_alias_is_not_deleted():
    decision = _decisions([
        _version(1, ("canary",), "2025-01-01T00:00:00Z"),
    ])[1]
    assert decision.action == "keep"
    assert decision.tag_class == "mutable-alias"


def test_unknown_tagged_version_is_kept_for_manual_review():
    decision = _decisions([
        _version(1, ("sha-abcdef123456",), "2025-01-01T00:00:00Z"),
    ])[1]
    assert decision.action == "keep"
    assert decision.tag_class == "unknown-tagged"


def test_infer_owner_from_gerrit_ssh_remote(monkeypatch):
    monkeypatch.setattr(
        retention,
        "_run",
        lambda argv: "ssh://codex-bot@sora.services:29418/omnisight/OmniSight-Productizer\n",
    )
    assert retention.infer_owner_from_git_remote() == "omnisight"


def test_dry_run_cli_outputs_per_tag_decision_table(tmp_path, capsys):
    fixture = tmp_path / "versions.json"
    fixture.write_text(
        """
        [
          {"id": 1, "tags": ["v1.2.3"], "updated_at": "2025-01-01T00:00:00Z"},
          {"id": 2, "tags": [], "updated_at": "2025-01-01T00:00:00Z"}
        ]
        """
    )
    summary = tmp_path / "summary.txt"

    assert retention.main([
        "--dry-run",
        "--package",
        "omnisight-backend",
        "--input-json",
        str(fixture),
        "--summary-file",
        str(summary),
    ]) == 0

    output = capsys.readouterr().out
    assert "package=omnisight-backend" in output
    assert "version_id\tupdated_at\ttag_class\taction\ttags\treason" in output
    assert "release\tkeep\tv1.2.3" in output
    assert "untagged\tdelete\t<untagged>" in output
    assert summary.read_text() == output
