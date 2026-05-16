from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit-coordination-table-vs-labels.py"
spec = importlib.util.spec_from_file_location("audit_coordination_table_vs_labels", SCRIPT)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def test_compare_claim_states_reports_zero_mismatches_when_labels_match_table() -> None:
    report = audit.compare_claim_states(
        label_claims=[
            audit.LabelClaim(
                ticket_key="OP-1107",
                label="claim:default:1715000000000000-aaaaaaaa",
                owner_instance_id="default",
            )
        ],
        table_claims=[
            audit.TableClaim(
                ticket_key="OP-1107",
                resource_key="ticket:OP-1107",
                owner_instance_id="default",
                owner_agent_class="subscription-codex",
                lease_id="lease-1",
            )
        ],
    )

    assert report["summary"]["ok"] is True
    assert report["summary"]["mismatch_count"] == 0
    assert report["mismatches"] == []


def test_compare_claim_states_reports_label_and_table_divergence() -> None:
    report = audit.compare_claim_states(
        label_claims=[
            audit.LabelClaim(
                ticket_key="OP-label-only",
                label="claim:default:1715000000000000-aaaaaaaa",
                owner_instance_id="default",
            ),
            audit.LabelClaim(
                ticket_key="OP-owner-mismatch",
                label="claim:default:1715000000000000-bbbbbbbb",
                owner_instance_id="default",
            ),
        ],
        table_claims=[
            audit.TableClaim(
                ticket_key="OP-table-only",
                resource_key="ticket:OP-table-only",
                owner_instance_id="default",
                owner_agent_class="subscription-codex",
                lease_id="lease-table",
            ),
            audit.TableClaim(
                ticket_key="OP-owner-mismatch",
                resource_key="ticket:OP-owner-mismatch",
                owner_instance_id="other",
                owner_agent_class="subscription-claude",
                lease_id="lease-mismatch",
            ),
        ],
    )

    reasons = {
        item["ticket_key"]: item["reasons"]
        for item in report["mismatches"]
    }
    assert report["summary"]["ok"] is False
    assert reasons["OP-label-only"] == ["label_without_table"]
    assert reasons["OP-table-only"] == ["table_without_label"]
    assert reasons["OP-owner-mismatch"] == ["owner_instance_mismatch"]


def test_label_claims_for_issue_ignores_non_claim_and_legacy_bare_labels() -> None:
    claims = audit._label_claims_for_issue({
        "key": "OP-1107",
        "fields": {
            "labels": [
                "area:backend",
                "claim:default",
                "claim:default:1715000000000000-aaaaaaaa",
            ],
        },
    })

    assert claims == [
        audit.LabelClaim(
            ticket_key="OP-1107",
            label="claim:default:1715000000000000-aaaaaaaa",
            owner_instance_id="default",
        )
    ]
