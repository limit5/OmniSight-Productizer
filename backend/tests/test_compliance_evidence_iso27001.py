"""SC.11.3 -- ISO 27001 control mapping and evidence collection tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.compliance_evidence import (
    ISO27001_MAPPING_VERSION,
    collect_iso27001_evidence,
    collect_iso27001_evidence_for_bundle,
    iso27001_control_mapping,
    list_iso27001_controls,
)


class _FakeConn:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []
        self.updated: tuple[str, tuple] | None = None
        self.bundle = {
            "id": "ceb-iso27001",
            "tenant_id": "tenant-a",
            "standard": "iso27001",
        }

    async def fetch(self, sql: str, *params):
        self.queries.append((sql, params))
        if "FROM audit_log" in sql:
            tenant_id, entity_kinds, actions, limit = params
            assert tenant_id == "tenant-a"
            assert limit == 2
            if "user" in entity_kinds or "login" in actions:
                return [
                    {
                        "id": 41,
                        "ts": 1_770_000_001.0,
                        "action": "login",
                        "entity_kind": "user",
                        "entity_id": "u-a",
                    },
                ]
            return []
        if "FROM event_log" in sql:
            tenant_id, event_types, limit = params
            assert tenant_id == "tenant-a"
            assert limit == 2
            if "debug.finding" in event_types:
                return [
                    {
                        "id": 9,
                        "created_at": "2026-05-03 00:00:00",
                        "event_type": "debug.finding",
                    },
                ]
            return []
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql: str, *params):
        self.queries.append((sql, params))
        if "FROM compliance_evidence_bundles" in sql:
            return self.bundle if params == ("ceb-iso27001",) else None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *params):
        self.updated = (sql, params)
        return "UPDATE 1"


def _write(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_iso27001_control_mapping_shape() -> None:
    mapping = iso27001_control_mapping()

    assert mapping["standard"] == "iso27001"
    assert mapping["version"] == ISO27001_MAPPING_VERSION
    control_ids = {item["control_id"] for item in mapping["controls"]}
    assert {"A.5.1", "A.5.15", "A.8.15", "A.8.24"}.issubset(control_ids)
    assert len(mapping["controls"]) == len(list_iso27001_controls())
    for item in mapping["controls"]:
        sources = item["evidence_sources"]
        assert item["domain"] in {
            "organizational_controls",
            "technological_controls",
        }
        assert (
            sources["policy_paths"]
            or sources["audit_entity_kinds"]
            or sources["event_types"]
        )


@pytest.mark.asyncio
async def test_collect_iso27001_evidence_reads_policy_files_and_log_summaries(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "docs/ops/security_baseline.md", "# Security baseline\n")
    _write(tmp_path, "docs/security/as_0_1_auth_surface_inventory.md", "# Auth\n")
    conn = _FakeConn()

    manifest = await collect_iso27001_evidence(
        conn,
        "tenant-a",
        root=tmp_path,
        limit_per_source=2,
    )

    assert manifest["standard"] == "iso27001"
    assert manifest["tenant_id"] == "tenant-a"
    assert manifest["summary"]["controls_total"] == len(list_iso27001_controls())
    assert manifest["summary"]["controls_with_policy"] >= 2
    assert manifest["summary"]["controls_with_logs"] >= 1

    a515 = next(c for c in manifest["controls"] if c["control_id"] == "A.5.15")
    assert a515["status"] == "collected"
    policy = next(p for p in a515["policy_evidence"] if p["available"])
    assert policy["path"] == "docs/security/as_0_1_auth_surface_inventory.md"
    assert len(policy["sha256"]) == 64
    assert any(
        item["table"] == "audit_log" and item["row_count"] == 1
        for item in a515["log_evidence"]
    )


@pytest.mark.asyncio
async def test_collect_iso27001_evidence_for_bundle_updates_mapping_and_manifest(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "docs/ops/security_baseline.md", "# Security baseline\n")
    conn = _FakeConn()

    manifest = await collect_iso27001_evidence_for_bundle(
        conn,
        "ceb-iso27001",
        root=tmp_path,
        limit_per_source=2,
    )

    assert manifest["standard"] == "iso27001"
    assert conn.updated is not None
    sql, params = conn.updated
    assert "UPDATE compliance_evidence_bundles" in sql
    assert "status = 'collecting'" in sql
    assert params[0] == "ceb-iso27001"

    mapping_json = json.loads(params[1])
    manifest_json = json.loads(params[2])
    assert mapping_json["standard"] == "iso27001"
    assert manifest_json["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_collect_iso27001_evidence_for_bundle_rejects_soc2_bundle() -> None:
    conn = _FakeConn()
    conn.bundle["standard"] = "soc2"

    with pytest.raises(ValueError, match="only collects iso27001"):
        await collect_iso27001_evidence_for_bundle(conn, "ceb-iso27001")
