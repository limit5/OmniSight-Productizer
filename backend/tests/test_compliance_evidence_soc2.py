"""SC.11.2 -- SOC 2 control mapping and evidence collection tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.compliance_evidence import (
    SOC2_MAPPING_VERSION,
    collect_soc2_evidence,
    collect_soc2_evidence_for_bundle,
    list_soc2_controls,
    soc2_control_mapping,
)


class _FakeConn:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []
        self.updated: tuple[str, tuple] | None = None
        self.bundle = {
            "id": "ceb-soc2",
            "tenant_id": "tenant-a",
            "standard": "soc2",
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
            return self.bundle if params == ("ceb-soc2",) else None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *params):
        self.updated = (sql, params)
        return "UPDATE 1"


def _write(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_soc2_control_mapping_shape() -> None:
    mapping = soc2_control_mapping()

    assert mapping["standard"] == "soc2"
    assert mapping["version"] == SOC2_MAPPING_VERSION
    control_ids = {item["control_id"] for item in mapping["controls"]}
    assert {"CC1.1", "CC6.1", "CC7.2", "CC8.1"}.issubset(control_ids)
    assert len(mapping["controls"]) == len(list_soc2_controls())
    for item in mapping["controls"]:
        sources = item["evidence_sources"]
        assert (
            sources["policy_paths"]
            or sources["audit_entity_kinds"]
            or sources["event_types"]
        )


@pytest.mark.asyncio
async def test_collect_soc2_evidence_reads_policy_files_and_log_summaries(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "docs/ops/security_baseline.md", "# Security baseline\n")
    _write(tmp_path, "docs/security/as_0_1_auth_surface_inventory.md", "# Auth\n")
    conn = _FakeConn()

    manifest = await collect_soc2_evidence(
        conn,
        "tenant-a",
        root=tmp_path,
        limit_per_source=2,
    )

    assert manifest["standard"] == "soc2"
    assert manifest["tenant_id"] == "tenant-a"
    assert manifest["summary"]["controls_total"] == len(list_soc2_controls())
    assert manifest["summary"]["controls_with_policy"] >= 2
    assert manifest["summary"]["controls_with_logs"] >= 1

    cc61 = next(c for c in manifest["controls"] if c["control_id"] == "CC6.1")
    assert cc61["status"] == "collected"
    policy = next(p for p in cc61["policy_evidence"] if p["available"])
    assert policy["path"] == "docs/security/as_0_1_auth_surface_inventory.md"
    assert len(policy["sha256"]) == 64
    assert any(
        item["table"] == "audit_log" and item["row_count"] == 1
        for item in cc61["log_evidence"]
    )


@pytest.mark.asyncio
async def test_collect_soc2_evidence_for_bundle_updates_mapping_and_manifest(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "docs/ops/security_baseline.md", "# Security baseline\n")
    conn = _FakeConn()

    manifest = await collect_soc2_evidence_for_bundle(
        conn,
        "ceb-soc2",
        root=tmp_path,
        limit_per_source=2,
    )

    assert manifest["standard"] == "soc2"
    assert conn.updated is not None
    sql, params = conn.updated
    assert "UPDATE compliance_evidence_bundles" in sql
    assert "status = 'collecting'" in sql
    assert params[0] == "ceb-soc2"

    mapping_json = json.loads(params[1])
    manifest_json = json.loads(params[2])
    assert mapping_json["standard"] == "soc2"
    assert manifest_json["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_collect_soc2_evidence_for_bundle_rejects_iso_bundle() -> None:
    conn = _FakeConn()
    conn.bundle["standard"] = "iso27001"

    with pytest.raises(ValueError, match="only collects soc2"):
        await collect_soc2_evidence_for_bundle(conn, "ceb-soc2")
