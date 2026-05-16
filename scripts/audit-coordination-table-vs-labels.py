#!/usr/bin/env python3
"""OP-1107 — compare JIRA claim labels with runner_claims shadow rows.

Outputs a JSON document suitable for the daily Atlas-X-2-Shadow report.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch, runner_coordination  # noqa: E402


@dataclass(frozen=True)
class LabelClaim:
    ticket_key: str
    label: str
    owner_instance_id: str


@dataclass(frozen=True)
class TableClaim:
    ticket_key: str
    resource_key: str
    owner_instance_id: str
    owner_agent_class: str
    lease_id: str


def _label_claims_for_issue(issue: dict[str, Any]) -> list[LabelClaim]:
    key = str(issue.get("key") or "")
    labels = ((issue.get("fields") or {}).get("labels")) or []
    claims: list[LabelClaim] = []
    for label in labels:
        parsed = jira_dispatch._parse_claim_label(str(label))
        if parsed is None:
            continue
        instance_id, token = parsed
        if token is None:
            continue
        claims.append(LabelClaim(key, str(label), instance_id))
    return claims


def compare_claim_states(
    *,
    label_claims: Iterable[LabelClaim],
    table_claims: Iterable[TableClaim],
) -> dict[str, Any]:
    labels_by_ticket: dict[str, list[LabelClaim]] = {}
    table_by_ticket: dict[str, list[TableClaim]] = {}
    for claim in label_claims:
        labels_by_ticket.setdefault(claim.ticket_key, []).append(claim)
    for claim in table_claims:
        table_by_ticket.setdefault(claim.ticket_key, []).append(claim)

    all_keys = sorted(set(labels_by_ticket) | set(table_by_ticket))
    mismatches: list[dict[str, Any]] = []
    for key in all_keys:
        labels = labels_by_ticket.get(key, [])
        rows = table_by_ticket.get(key, [])
        label_instances = sorted({claim.owner_instance_id for claim in labels})
        table_instances = sorted({claim.owner_instance_id for claim in rows})
        reasons: list[str] = []
        if labels and not rows:
            reasons.append("label_without_table")
        if rows and not labels:
            reasons.append("table_without_label")
        if len(labels) > 1:
            reasons.append("multiple_label_claims")
        if len(rows) > 1:
            reasons.append("multiple_table_claims")
        if labels and rows and label_instances != table_instances:
            reasons.append("owner_instance_mismatch")
        if reasons:
            mismatches.append({
                "ticket_key": key,
                "reasons": reasons,
                "label_claims": [claim.label for claim in labels],
                "table_claims": [
                    {
                        "lease_id": claim.lease_id,
                        "resource_key": claim.resource_key,
                        "owner_agent_class": claim.owner_agent_class,
                        "owner_instance_id": claim.owner_instance_id,
                    }
                    for claim in rows
                ],
            })

    return {
        "summary": {
            "label_ticket_count": len(labels_by_ticket),
            "table_ticket_count": len(table_by_ticket),
            "mismatch_count": len(mismatches),
            "ok": len(mismatches) == 0,
        },
        "mismatches": mismatches,
    }


def _fetch_claim_label_issues(client: jira_dispatch.DispatchClient) -> list[dict[str, Any]]:
    jql = f"project = {client.project_key} AND labels is not EMPTY ORDER BY updated DESC"
    params = urllib.parse.urlencode({
        "jql": jql,
        "fields": "labels,status",
        "maxResults": "100",
    })
    issues: list[dict[str, Any]] = []
    start_at = 0
    while True:
        path = f"/search?{params}&startAt={start_at}"
        resp = jira_dispatch._request(client, "GET", path)
        batch = resp.get("issues") or []
        issues.extend(batch)
        if start_at + len(batch) >= int(resp.get("total", len(issues))):
            break
        if not batch:
            break
        start_at += len(batch)
    return issues


def _active_table_claims() -> list[TableClaim]:
    return [
        TableClaim(
            ticket_key=lease.ticket_key,
            resource_key=lease.resource_key,
            owner_instance_id=lease.owner_instance_id,
            owner_agent_class=lease.owner_agent_class,
            lease_id=lease.lease_id,
        )
        for lease in runner_coordination.find_active_holders()
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-class",
        default="subscription-codex",
        help="JIRA credential class to use for label-state reads",
    )
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    client = jira_dispatch.make_client(args.agent_class)
    issues = _fetch_claim_label_issues(client)
    label_claims = [
        claim
        for issue in issues
        for claim in _label_claims_for_issue(issue)
    ]
    report = compare_claim_states(
        label_claims=label_claims,
        table_claims=_active_table_claims(),
    )
    print(json.dumps(report, indent=args.indent, sort_keys=True))
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
