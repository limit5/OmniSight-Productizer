"""SC.11.4 -- compliance evidence bundle zip export and signature tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backend.compliance_evidence import (
    EVIDENCE_EXPORT_VERSION,
    EvidenceSigningKey,
    export_compliance_evidence_bundle,
    verify_evidence_bundle_signature,
)


class _FakeConn:
    def __init__(self) -> None:
        self.updated: tuple[str, tuple] | None = None
        self.bundle = {
            "id": "ceb-export",
            "tenant_id": "tenant-a",
            "requested_by": "user-a",
            "standard": "soc2",
            "status": "collecting",
            "control_mapping_json": {
                "standard": "soc2",
                "version": "test",
                "controls": [{"control_id": "CC6.1"}],
            },
            "evidence_manifest_json": {
                "standard": "soc2",
                "tenant_id": "tenant-a",
                "controls": [
                    {
                        "control_id": "CC6.1",
                        "policy_evidence": [
                            {
                                "available": True,
                                "path": "docs/ops/security_baseline.md",
                            },
                            {
                                "available": False,
                                "path": "docs/ops/missing.md",
                            },
                        ],
                        "log_evidence": [
                            {"table": "audit_log", "row_count": 1},
                        ],
                    },
                ],
                "summary": {"controls_total": 1},
            },
            "version": 3,
        }

    async def fetchrow(self, sql: str, *params):
        if "FROM compliance_evidence_bundles" in sql:
            return self.bundle if params == ("ceb-export",) else None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *params):
        self.updated = (sql, params)
        return "UPDATE 1"


def _write(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_export_compliance_evidence_bundle_writes_zip_signature_and_row(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "docs/ops/security_baseline.md", "# Security baseline\n")
    conn = _FakeConn()
    key = EvidenceSigningKey(key_id="test-k1", secret=b"test-secret-value")

    result = await export_compliance_evidence_bundle(
        conn,
        "ceb-export",
        output_dir=tmp_path / "exports",
        signing_key=key,
        root=tmp_path,
    )

    artifact_path = Path(result["artifact_uri"])
    signature_path = Path(result["signature_uri"])
    assert artifact_path.is_file()
    assert signature_path.is_file()
    assert result["signature"]["version"] == EVIDENCE_EXPORT_VERSION
    assert result["signature"]["key_id"] == "test-k1"
    assert verify_evidence_bundle_signature(
        artifact_path,
        result["signature"],
        key,
    )

    with zipfile.ZipFile(artifact_path) as archive:
        names = set(archive.namelist())
        assert names == {
            "bundle.json",
            "control_mapping.json",
            "evidence_manifest.json",
            "policies/docs/ops/security_baseline.md",
        }
        bundle_json = json.loads(archive.read("bundle.json"))
        manifest_json = json.loads(archive.read("evidence_manifest.json"))
        assert bundle_json["id"] == "ceb-export"
        assert bundle_json["source_status"] == "collecting"
        assert manifest_json["tenant_id"] == "tenant-a"

    assert conn.updated is not None
    sql, params = conn.updated
    assert "UPDATE compliance_evidence_bundles" in sql
    assert "status = 'completed'" in sql
    assert params[0] == "ceb-export"
    assert params[2] == str(artifact_path)
    signature_json = json.loads(params[3])
    assert signature_json["artifact_sha256"] == result["signature"]["artifact_sha256"]


@pytest.mark.asyncio
async def test_export_rejects_uncollected_bundle(tmp_path: Path) -> None:
    conn = _FakeConn()
    conn.bundle["evidence_manifest_json"] = {}
    key = EvidenceSigningKey(key_id="test-k1", secret=b"test-secret-value")

    with pytest.raises(ValueError, match="not collected"):
        await export_compliance_evidence_bundle(
            conn,
            "ceb-export",
            output_dir=tmp_path,
            signing_key=key,
        )


@pytest.mark.asyncio
async def test_export_rejects_manifest_standard_mismatch(tmp_path: Path) -> None:
    conn = _FakeConn()
    conn.bundle["evidence_manifest_json"]["standard"] = "iso27001"
    key = EvidenceSigningKey(key_id="test-k1", secret=b"test-secret-value")

    with pytest.raises(ValueError, match="manifest standard"):
        await export_compliance_evidence_bundle(
            conn,
            "ceb-export",
            output_dir=tmp_path,
            signing_key=key,
        )
