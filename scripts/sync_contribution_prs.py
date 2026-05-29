#!/usr/bin/env python3
"""One-shot camviewpro contribution PR-state sync (OP-1847)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.agents import contribution_pr_tracker as tracker
from backend.agents import jira_dispatch


def _default_jql(project: str) -> str:
    return (
        f'project = "{project}" '
        'AND statusCategory != Done '
        'AND labels is not EMPTY '
        'ORDER BY updated ASC'
    )


def _fetch_open_contributions(
    client: jira_dispatch.DispatchClient,
    *,
    jql: str,
    max_results: int,
):
    response = jira_dispatch._request(  # noqa: SLF001 - thin operator entrypoint
        client,
        "POST",
        "/search/jql",
        {
            "jql": jql,
            "fields": [
                "summary",
                "labels",
                "status",
                "issuetype",
                "fixVersions",
                "created",
                "components",
                "issuelinks",
                "parent",
            ],
            "maxResults": max_results,
        },
    )
    snapshots = [jira_dispatch.to_snapshot(issue) for issue in response.get("issues", [])]
    return [
        snapshot
        for snapshot in snapshots
        if tracker._extract_pr_number(snapshot.labels) is not None  # noqa: SLF001
    ]


async def _run(args: argparse.Namespace) -> list[tracker.PrSyncOutcome]:
    client = jira_dispatch.make_client(args.agent_class)
    tickets = _fetch_open_contributions(
        client,
        jql=args.jql or _default_jql(client.project_key),
        max_results=args.max_results,
    )
    return await tracker.sync_all_open_contributions(
        tickets,
        git_account_ref=args.git_account_ref,
        tenant_id=args.tenant_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class",
        default="subscription-codex",
        help="JIRA dispatch credential class.",
    )
    parser.add_argument(
        "--git-account-ref",
        required=True,
        help="git_accounts id for the camviewpro GitHub source credential.",
    )
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument(
        "--jql",
        default=None,
        help="Override the open contribution ticket search JQL.",
    )
    args = parser.parse_args(argv)

    outcomes = asyncio.run(_run(args))
    for outcome in outcomes:
        print(json.dumps(outcome.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
