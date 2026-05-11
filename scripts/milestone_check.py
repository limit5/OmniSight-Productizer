#!/usr/bin/env python3
"""OP-868 fixVersion release milestone acceptance checker."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch
from backend.agents.milestone_query import (
    ERR_VERIFIED_LABEL_MISSING,
    MilestoneTicket,
    ReleaseMilestoneStore,
    VerifiedRun,
    create_engine,
    ensure_release_milestones_table_for_dry_run,
    evaluate_milestone,
    write_json_report,
)


DEFAULT_AGENT_CLASS = "subscription-codex"
DEFAULT_AUDIT_LOG = Path("logs/release-milestones/audit.jsonl")
GREEN_VERIFIED_VALUES = {"1", "+1"}


class MilestoneJiraClient(Protocol):
    def tickets_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        ...

    def open_blockers_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        ...


class MilestoneGerritClient(Protocol):
    def verified_runs_for_tickets(self, tickets: list[MilestoneTicket]) -> list[VerifiedRun]:
        ...


@dataclass(frozen=True)
class DispatchMilestoneJiraClient:
    client: jira_dispatch.DispatchClient

    def _search(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        payload = jira_dispatch._request(
            self.client,
            "POST",
            "/search/jql",
            {"jql": jql, "fields": fields, "maxResults": 500},
        )
        return list(payload.get("issues", []))

    @staticmethod
    def _ticket(issue: dict[str, Any]) -> MilestoneTicket:
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        return MilestoneTicket(
            key=str(issue.get("key") or ""),
            status=str(status),
            labels=tuple(str(label) for label in fields.get("labels") or ()),
        )

    def tickets_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        jql = (
            f'project = "{self.client.project_key}" '
            f'AND fixVersion = "{version}" ORDER BY key ASC'
        )
        return [self._ticket(issue) for issue in self._search(jql, ["status", "labels"])]

    def open_blockers_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        jql = (
            f'project = "{self.client.project_key}" '
            f'AND fixVersion = "{version}" '
            'AND priority = Blocker '
            'AND statusCategory != Done '
            "ORDER BY key ASC"
        )
        return [self._ticket(issue) for issue in self._search(jql, ["status", "labels"])]


@dataclass(frozen=True)
class SshMilestoneGerritClient:
    host: str
    port: int
    key_path: Path
    project: str

    @classmethod
    def from_env(cls) -> "SshMilestoneGerritClient":
        return cls(
            host=os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", "codex-bot@sora.services"),
            port=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")),
            key_path=Path(
                os.environ.get(
                    "OMNISIGHT_GIT_SSH_KEY_PATH",
                    "~/.config/omnisight/gerrit-codex-bot-ed25519",
                )
            ).expanduser(),
            project=os.environ.get("OMNISIGHT_GERRIT_PROJECT", "omnisight/OmniSight-Productizer"),
        )

    def _query(self, query: str) -> list[dict[str, Any]]:
        cmd = [
            "ssh",
            "-i",
            str(self.key_path),
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            self.host,
            "gerrit",
            "query",
            "--format=JSON",
            "--current-patch-set",
            "--all-approvals",
            query,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        rows: list[dict[str, Any]] = []
        for raw in proc.stdout.splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "stats":
                rows.append(row)
        return rows

    def verified_runs_for_tickets(self, tickets: list[MilestoneTicket]) -> list[VerifiedRun]:
        runs: list[VerifiedRun] = []
        for ticket in tickets:
            rows = self._query(
                f'project:{self.project} branch:develop status:merged message:{ticket.key}'
            )
            if not rows:
                runs.append(
                    VerifiedRun(
                        ticket_key=ticket.key,
                        commit="",
                        state="missing_merged_commit",
                        label_present=True,
                    )
                )
                continue
            for row in rows:
                patchset = row.get("currentPatchSet") or {}
                approvals = patchset.get("approvals") or []
                verified = [a for a in approvals if a.get("type") == "Verified"]
                if not verified:
                    runs.append(
                        VerifiedRun(
                            ticket_key=ticket.key,
                            commit=str(patchset.get("revision") or ""),
                            state="missing",
                            label_present=False,
                        )
                    )
                    continue
                value = str(verified[-1].get("value") or "")
                runs.append(
                    VerifiedRun(
                        ticket_key=ticket.key,
                        commit=str(patchset.get("revision") or ""),
                        state="green" if value in GREEN_VERIFIED_VALUES else "red",
                        label_present=True,
                    )
                )
        return runs


class DryRunJiraClient:
    def tickets_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        labels = ("milestone:force-accept",) if version.endswith("-force") else ()
        return [MilestoneTicket(key="OP-868", status="公開済み", labels=labels)]

    def open_blockers_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        return []


class DryRunGerritClient:
    def verified_runs_for_tickets(self, tickets: list[MilestoneTicket]) -> list[VerifiedRun]:
        return [
            VerifiedRun(ticket_key=ticket.key, commit="dry-run-commit", state="green")
            for ticket in tickets
        ]


def append_audit(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def check_one(
    version: str,
    *,
    jira: MilestoneJiraClient,
    gerrit: MilestoneGerritClient,
    verified_required: bool = True,
    audit_log: Path = DEFAULT_AUDIT_LOG,
) -> dict[str, Any]:
    tickets = jira.tickets_for_fix_version(version)
    blockers = jira.open_blockers_for_fix_version(version)
    verified_runs = gerrit.verified_runs_for_tickets(tickets)
    result = evaluate_milestone(
        version,
        tickets=tickets,
        blockers=blockers,
        verified_runs=verified_runs,
        verified_required=verified_required,
    )
    report = result.to_report()
    if result.force_accept:
        append_audit(
            audit_log,
            {
                "event": "milestone_force_accept",
                "version": version,
                "tickets": [ticket.key for ticket in tickets],
                "reason": "milestone:force-accept skipped Verified-label requirement",
            },
        )
    return report


def _post_status_change(
    *,
    client: jira_dispatch.DispatchClient,
    meta_ticket: str,
    version: str,
    previous: str | None,
    current: str,
) -> None:
    jira_dispatch.add_comment(
        client,
        meta_ticket,
        (
            f"Milestone {version} status changed: "
            f"{previous or 'untracked'} -> {current}."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="FixVersion name, e.g. v1.2.3")
    parser.add_argument("--out", required=True, help="JSON report path")
    parser.add_argument(
        "--agent-class",
        default=os.environ.get("OMNISIGHT_JIRA_AGENT_CLASS", DEFAULT_AGENT_CLASS),
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verified-label-missing",
        action="store_true",
        help="Soft-gate acceptance while OP-739 Verified label support is unavailable.",
    )
    parser.add_argument(
        "--nightly",
        action="store_true",
        help="Check current+next release_milestones rows and comment on status changes.",
    )
    parser.add_argument("--meta-ticket", default=os.environ.get("OMNISIGHT_MILESTONE_META", "OP-761"))
    args = parser.parse_args(argv)

    if not args.version and not args.nightly:
        parser.error("--version is required unless --nightly is set")

    engine = create_engine(args.database_url)
    if args.dry_run:
        ensure_release_milestones_table_for_dry_run(engine)
    store = ReleaseMilestoneStore(engine)
    jira_client: MilestoneJiraClient
    gerrit_client: MilestoneGerritClient
    dispatch_client = None
    if args.dry_run:
        jira_client = DryRunJiraClient()
        gerrit_client = DryRunGerritClient()
    else:
        dispatch_client = jira_dispatch.make_client(args.agent_class)
        jira_client = DispatchMilestoneJiraClient(dispatch_client)
        gerrit_client = SshMilestoneGerritClient.from_env()

    versions = store.current_next_versions() if args.nightly else [str(args.version)]
    reports = []
    exit_code = 0
    for version in versions:
        try:
            report = check_one(
                version,
                jira=jira_client,
                gerrit=gerrit_client,
                verified_required=not args.verified_label_missing,
                audit_log=Path(args.audit_log),
            )
            previous = store.update_status(version, status=str(report["status"]), report=report)
            if (
                args.nightly
                and dispatch_client is not None
                and previous is not None
                and previous != report["status"]
            ):
                _post_status_change(
                    client=dispatch_client,
                    meta_ticket=args.meta_ticket,
                    version=version,
                    previous=previous,
                    current=str(report["status"]),
                )
            reports.append(report)
            exit_code = max(exit_code, int(report["exit_code"]))
        except Exception as exc:  # noqa: BLE001 - CLI must return code 2 on operational errors.
            report = {
                "version": version,
                "ready": False,
                "status": "error",
                "exit_code": 2,
                "errors": [{"code": exc.__class__.__name__, "detail": str(exc)}],
                "warnings": [],
            }
            reports.append(report)
            exit_code = 2

    payload: dict[str, Any] = reports[0] if len(reports) == 1 else {"reports": reports}
    write_json_report(Path(args.out), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if exit_code == 1:
        codes = {
            err.get("code")
            for report in reports
            for err in report.get("errors", [])
        } | {
            warn.get("code")
            for report in reports
            for warn in report.get("warnings", [])
        }
        if ERR_VERIFIED_LABEL_MISSING in codes:
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
