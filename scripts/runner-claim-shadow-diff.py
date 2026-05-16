#!/usr/bin/env python3
"""OP-1168 runner claim shadow diff.

Compares JIRA ``claim:*`` labels with active ``runner_coordination`` rows.
By default the JSON report is printed to stdout. With ``--apply`` it is
also written to ``docs/audit/runner-claim-shadow/<YYYY-MM-DD>.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch, runner_coordination  # noqa: E402

CLAIM_LABEL_PREFIX = "claim:"
OUTPUT_DIR = REPO_ROOT / "docs" / "audit" / "runner-claim-shadow"


@dataclass(frozen=True)
class JiraClaim:
    ticket_key: str
    instance_id: str
    fencing_token: str | None
    label: str


def _parse_claim_label(label: str) -> tuple[str, str | None] | None:
    if not label.startswith(CLAIM_LABEL_PREFIX):
        return None
    rest = label[len(CLAIM_LABEL_PREFIX):]
    if not rest:
        return None
    instance_id, sep, fencing_token = rest.partition(":")
    if not instance_id:
        return None
    return instance_id, fencing_token if sep else None


def _ticket_from_resource_key(resource_key: str) -> str | None:
    prefix = "ticket:"
    if not resource_key.startswith(prefix):
        return None
    ticket_key = resource_key[len(prefix):]
    return ticket_key or None


def fetch_jira_claims(client: jira_dispatch.DispatchClient) -> list[JiraClaim]:
    """Read JIRA issues with labels, then keep only ``claim:*`` labels."""
    resp = jira_dispatch._request(client, "POST", "/search/jql", {
        "jql": (
            f'project = "{client.project_key}" '
            "AND labels is not EMPTY "
            "ORDER BY key ASC"
        ),
        "fields": ["labels"],
        "maxResults": 1000,
    })
    claims: list[JiraClaim] = []
    for issue in resp.get("issues", []):
        ticket_key = issue.get("key")
        labels = ((issue.get("fields") or {}).get("labels")) or []
        if not ticket_key:
            continue
        for label in labels:
            parsed = _parse_claim_label(label)
            if parsed is None:
                continue
            instance_id, fencing_token = parsed
            claims.append(JiraClaim(ticket_key, instance_id, fencing_token, label))
    return claims


def _lease_label_token(lease: runner_coordination.ClaimLease) -> str | None:
    token = (lease.external_refs or {}).get("label_fencing_token")
    if isinstance(token, str) and token:
        return token
    parsed = _parse_claim_label(lease.fencing_token)
    if parsed is None:
        return None
    return parsed[1]


def build_report(
    jira_claims: Iterable[JiraClaim],
    active_leases: Iterable[runner_coordination.ClaimLease],
    *,
    generated_at: str | None = None,
) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    jira_by_ticket = {claim.ticket_key: claim for claim in jira_claims}
    table_by_ticket = {
        ticket_key: lease
        for lease in active_leases
        if (ticket_key := _ticket_from_resource_key(lease.resource_key)) is not None
    }

    label_without_table = []
    for ticket_key, claim in sorted(jira_by_ticket.items()):
        if ticket_key not in table_by_ticket:
            label_without_table.append({
                "ticket_key": ticket_key,
                "label": claim.label,
            })

    table_without_label = []
    for ticket_key, lease in sorted(table_by_ticket.items()):
        if ticket_key not in jira_by_ticket:
            table_without_label.append({
                "ticket_key": ticket_key,
                "lease_id": lease.lease_id,
                "resource_key": lease.resource_key,
                "fencing_token": lease.fencing_token,
            })

    token_mismatch = []
    for ticket_key, claim in sorted(jira_by_ticket.items()):
        lease = table_by_ticket.get(ticket_key)
        if lease is None:
            continue
        table_label_token = _lease_label_token(lease)
        if claim.fencing_token != table_label_token:
            token_mismatch.append({
                "ticket_key": ticket_key,
                "label": claim.label,
                "jira_fencing_token": claim.fencing_token,
                "table_label_fencing_token": table_label_token,
                "lease_id": lease.lease_id,
                "table_fencing_token": lease.fencing_token,
            })

    return {
        "generated_at": generated_at,
        "label_without_table": label_without_table,
        "table_without_label": table_without_label,
        "token_mismatch": token_mismatch,
        "summary": {
            "jira_claim_count": len(jira_by_ticket),
            "active_table_claim_count": len(table_by_ticket),
            "label_without_table_count": len(label_without_table),
            "table_without_label_count": len(table_without_label),
            "token_mismatch_count": len(token_mismatch),
        },
    }


def write_report(report: dict, output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).date().isoformat()
    path = output_dir / f"{date}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write docs/audit/runner-claim-shadow/YYYY-MM-DD.json")
    parser.add_argument("--agent-class", default="subscription-codex",
                        help="JIRA credential class passed to jira_dispatch.make_client")
    args = parser.parse_args(argv)

    client = jira_dispatch.make_client(args.agent_class)
    report = build_report(
        fetch_jira_claims(client),
        runner_coordination.find_active_holders(),
    )
    if args.apply:
        write_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
