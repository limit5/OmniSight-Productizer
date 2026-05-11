#!/usr/bin/env python3
"""OP-868 fixVersion-backed release milestone definer."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy.exc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch
from backend.agents.milestone_query import (
    ERR_ALREADY_EXISTS,
    ReleaseMilestoneStore,
    create_engine,
    ensure_release_milestones_table_for_dry_run,
)


DEFAULT_AGENT_CLASS = "subscription-codex"
DEFAULT_DRY_RUN_JIRA_VERSION_ID = "dry-run-version-id"


class JiraVersionClient(Protocol):
    project_key: str

    def find_version(self, version: str) -> dict[str, Any] | None:
        ...

    def create_version(self, version: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class DispatchJiraVersionClient:
    client: jira_dispatch.DispatchClient

    @property
    def project_key(self) -> str:
        return self.client.project_key

    def find_version(self, version: str) -> dict[str, Any] | None:
        payload = jira_dispatch._request(self.client, "GET", f"/project/{self.project_key}/versions")
        rows = payload if isinstance(payload, list) else payload.get("values", [])
        for row in rows:
            if str(row.get("name") or "") == version:
                return row
        return None

    def create_version(self, version: str) -> dict[str, Any]:
        return jira_dispatch._request(
            self.client,
            "POST",
            "/version",
            {
                "name": version,
                "project": self.project_key,
                "released": False,
                "archived": False,
            },
        )


@dataclass
class DryRunJiraVersionClient:
    project_key: str = "OP"
    created: dict[str, Any] | None = None
    existing: set[str] | None = None

    def find_version(self, version: str) -> dict[str, Any] | None:
        if self.existing and version in self.existing:
            return {"id": DEFAULT_DRY_RUN_JIRA_VERSION_ID, "name": version}
        return None

    def create_version(self, version: str) -> dict[str, Any]:
        self.created = {"id": DEFAULT_DRY_RUN_JIRA_VERSION_ID, "name": version}
        return self.created


def define_milestone(
    version: str,
    *,
    jira: JiraVersionClient,
    store: ReleaseMilestoneStore,
) -> dict[str, Any]:
    existing = jira.find_version(version)
    if existing is not None or store.exists(version):
        raise RuntimeError(ERR_ALREADY_EXISTS)
    created = jira.create_version(version)
    jira_version_id = str(created.get("id") or created.get("name") or version)
    try:
        store.create(
            version=version,
            jira_version_id=jira_version_id,
            project_key=jira.project_key,
        )
    except sqlalchemy.exc.IntegrityError as exc:
        raise RuntimeError(ERR_ALREADY_EXISTS) from exc
    return {
        "version": version,
        "jira_version_id": jira_version_id,
        "jira_project_key": jira.project_key,
        "status": "defined",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="FixVersion name, e.g. v1.2.3")
    parser.add_argument(
        "--agent-class",
        default=os.environ.get("OMNISIGHT_JIRA_AGENT_CLASS", DEFAULT_AGENT_CLASS),
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    if args.dry_run:
        ensure_release_milestones_table_for_dry_run(engine)
    store = ReleaseMilestoneStore(engine)
    jira: JiraVersionClient
    if args.dry_run:
        jira = DryRunJiraVersionClient()
    else:
        jira = DispatchJiraVersionClient(jira_dispatch.make_client(args.agent_class))

    try:
        result = define_milestone(args.version, jira=jira, store=store)
    except RuntimeError as exc:
        if str(exc) == ERR_ALREADY_EXISTS:
            print(
                json.dumps(
                    {"ok": False, "code": ERR_ALREADY_EXISTS, "version": args.version},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        raise

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
