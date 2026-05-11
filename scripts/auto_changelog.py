#!/usr/bin/env python3
"""[OP-880] Generate a release changelog section from a JIRA milestone.

D16 owns the full release-note exporter. Until that ships, this helper
keeps the release-cut path deterministic:

* with ``--jira`` it reads tickets from the JIRA fixVersion matching the
  release version and renders one bullet per ticket;
* without JIRA data, or when JIRA lookup fails and ``--allow-template``
  is set, it inserts a manual-fill template and reports
  ``ChangelogGenFailed`` in JSON output.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ChangelogTicket:
    key: str
    summary: str


@dataclass(frozen=True)
class ChangelogResult:
    version: str
    milestone: str
    status: str
    output: str
    ticket_count: int
    error_code: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "version": self.version,
            "milestone": self.milestone,
            "status": self.status,
            "output": self.output,
            "ticket_count": self.ticket_count,
        }
        if self.error_code:
            data["error_code"] = self.error_code
        if self.error:
            data["error"] = self.error
        return data


def fetch_jira_tickets(*, milestone: str, agent_class: str) -> list[ChangelogTicket]:
    from backend.agents import jira_dispatch

    client = jira_dispatch.make_client(agent_class)
    jql = (
        f'project = "{client.project_key}" '
        f'AND fixVersion = "{milestone}" '
        "ORDER BY key ASC"
    )
    payload = jira_dispatch._request(
        client,
        "POST",
        "/search/jql",
        {"jql": jql, "fields": ["summary"], "maxResults": 500},
    )
    tickets: list[ChangelogTicket] = []
    for issue in payload.get("issues", []):
        fields = issue.get("fields") or {}
        tickets.append(
            ChangelogTicket(
                key=str(issue.get("key") or ""),
                summary=str(fields.get("summary") or "").strip(),
            )
        )
    return tickets


def render_section(
    *,
    version: str,
    today: date,
    tickets: list[ChangelogTicket],
    template: bool,
) -> str:
    lines = [f"## {version} - {today.isoformat()}", ""]
    if template:
        lines.extend(
            [
                "### Added",
                "- TODO: Fill from JIRA milestone once D16 changelog export is available.",
                "",
                "### Changed",
                "- TODO: Fill release-impacting changes.",
                "",
                "### Fixed",
                "- TODO: Fill release-impacting fixes.",
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    lines.extend(["### Changes"])
    if tickets:
        for ticket in tickets:
            summary = f" - {ticket.summary}" if ticket.summary else ""
            lines.append(f"- {ticket.key}{summary}")
    else:
        lines.append("- No JIRA tickets found for this milestone.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def insert_section(existing: str, *, version: str, section: str) -> tuple[str, bool]:
    heading = f"## {version} - "
    if heading in existing:
        return existing, False

    marker = "## Unreleased"
    idx = existing.find(marker)
    if idx == -1:
        text = existing.rstrip() + "\n\n" + section
        return text, True

    next_idx = existing.find("\n## ", idx + len(marker))
    if next_idx == -1:
        text = existing.rstrip() + "\n\n" + section
        return text, True

    text = existing[:next_idx].rstrip() + "\n\n" + section + "\n" + existing[next_idx:].lstrip("\n")
    return text, True


def update_changelog(
    *,
    path: Path,
    version: str,
    tickets: list[ChangelogTicket],
    template: bool,
    today: date,
) -> tuple[bool, str]:
    existing = path.read_text() if path.exists() else "# Changelog\n\n## Unreleased\n"
    section = render_section(version=version, today=today, tickets=tickets, template=template)
    updated, inserted = insert_section(existing, version=version, section=section)
    if inserted:
        path.write_text(updated)
    return inserted, str(path)


def generate(args: argparse.Namespace) -> ChangelogResult:
    path = Path(args.output).resolve()
    milestone = args.milestone or args.version
    tickets: list[ChangelogTicket] = []
    template = not args.jira
    error: str | None = None

    if args.jira:
        try:
            tickets = fetch_jira_tickets(milestone=milestone, agent_class=args.agent_class)
        except Exception as exc:
            if not args.allow_template:
                raise
            template = True
            error = f"{type(exc).__name__}: {exc}"

    if args.template_only:
        template = True

    inserted, output = update_changelog(
        path=path,
        version=args.version,
        tickets=tickets,
        template=template,
        today=date.today(),
    )
    status = "inserted" if inserted else "already_present"
    return ChangelogResult(
        version=args.version,
        milestone=milestone,
        status=status,
        output=output,
        ticket_count=len(tickets),
        error_code="ChangelogGenFailed" if error or template else None,
        error=error,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--milestone")
    parser.add_argument("--output", default="CHANGELOG.md")
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--jira", action="store_true")
    parser.add_argument("--allow-template", action="store_true")
    parser.add_argument("--template-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = generate(args)
    except Exception as exc:
        print(f"auto_changelog: ChangelogGenFailed: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
