#!/usr/bin/env python3
"""[OP-888] Generate ops release notes from a JIRA fixVersion milestone."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_AGENT_CLASS = "subscription-codex"
SPRINT_ORDER = ("A", "B", "C", "D", "E")
PRIORITY_ORDER = ("Highest", "High", "Medium", "Low", "Lowest")
LESSON_PLACEHOLDER = "<lesson-link-missing>"


class JIRAFixVersionNotFound(RuntimeError):
    """Raised when the requested JIRA fixVersion does not exist."""


class TicketDescriptionMalformed(RuntimeError):
    """Raised when a ticket description lacks parseable acceptance criteria."""


class LessonReferenceBroken(RuntimeError):
    """Raised when a ticket references a lesson file that is missing."""


@dataclass(frozen=True)
class MilestoneTicket:
    key: str
    summary: str
    priority: str
    labels: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class ParsedTicket:
    ticket: MilestoneTicket
    sprint: str
    acceptance_criteria: tuple[str, ...]
    lessons: tuple[str, ...]
    broken_lessons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkippedTicket:
    key: str
    summary: str
    reason: str


@dataclass(frozen=True)
class ReleaseNotesResult:
    version: str
    output_path: Path
    rendered: str
    included: tuple[ParsedTicket, ...]
    skipped: tuple[SkippedTicket, ...]


class JiraClient(Protocol):
    def fix_version_exists(self, version: str) -> bool:
        ...

    def suggest_versions(self, version: str) -> list[str]:
        ...

    def tickets_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        ...


class JiraDispatchMilestoneClient:
    """JIRA REST adapter mirroring the runner's ``jira_dispatch`` usage."""

    def __init__(self, agent_class: str) -> None:
        from backend.agents import jira_dispatch

        self.jira_dispatch = jira_dispatch
        self.client = jira_dispatch.make_client(agent_class)

    def fix_version_exists(self, version: str) -> bool:
        versions = self.jira_dispatch._request(
            self.client,
            "GET",
            f"/project/{self.client.project_key}/versions",
            None,
        )
        rows = versions if isinstance(versions, list) else versions.get("values", [])
        return any(str(row.get("name") or "") == version for row in rows)

    def suggest_versions(self, version: str) -> list[str]:
        versions = self.jira_dispatch._request(
            self.client,
            "GET",
            f"/project/{self.client.project_key}/versions",
            None,
        )
        rows = versions if isinstance(versions, list) else versions.get("values", [])
        wanted = version.lower().lstrip("v")
        names = [str(row.get("name") or "") for row in rows if row.get("name")]
        ranked = [name for name in names if wanted and wanted in name.lower()]
        return (ranked or names)[0:5]

    def tickets_for_fix_version(self, version: str) -> list[MilestoneTicket]:
        jql = (
            f'project = "{self.client.project_key}" '
            f'AND fixVersion = "{version}" '
            "ORDER BY labels ASC, priority DESC, key ASC"
        )
        payload = self.jira_dispatch._request(
            self.client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": ["summary", "description", "labels", "priority"],
                "maxResults": 500,
            },
        )
        tickets: list[MilestoneTicket] = []
        for issue in payload.get("issues", []):
            fields = issue.get("fields") or {}
            priority = fields.get("priority") or {}
            tickets.append(
                MilestoneTicket(
                    key=str(issue.get("key") or ""),
                    summary=str(fields.get("summary") or "").strip(),
                    priority=str(priority.get("name") or "Medium"),
                    labels=tuple(str(label) for label in fields.get("labels") or ()),
                    description=description_to_text(fields.get("description")),
                )
            )
        return tickets


def description_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        _collect_adf_text(value, parts)
        return "\n".join(part for part in parts if part).strip()
    return str(value)


def _collect_adf_text(node: object, parts: list[str]) -> None:
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str):
            parts.append(text)
        for child in node.get("content") or ():
            _collect_adf_text(child, parts)
        if node.get("type") in {"paragraph", "heading", "listItem"}:
            parts.append("")
    elif isinstance(node, list):
        for child in node:
            _collect_adf_text(child, parts)


def sprint_from_labels(labels: Iterable[str]) -> str:
    for label in labels:
        match = re.fullmatch(r"(?:sprint[:_-]?|phase[:_-]?|batch[:_-]?)([A-E])", label, re.I)
        if match:
            return match.group(1).upper()
        match = re.fullmatch(r"([A-E])", label, re.I)
        if match:
            return match.group(1).upper()
    return "Unsorted"


def priority_rank(priority: str) -> int:
    try:
        return PRIORITY_ORDER.index(priority)
    except ValueError:
        return len(PRIORITY_ORDER)


def parse_acceptance_criteria(description: str) -> tuple[str, ...]:
    lines = description.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        if re.match(r"^\s*#{1,4}\s*Acceptance criteria\s*$", line, re.I):
            start = idx + 1
            break
    if start is None:
        raise TicketDescriptionMalformed("missing Acceptance criteria section")

    items: list[str] = []
    for line in lines[start:]:
        if re.match(r"^\s*#{1,4}\s+\S+", line):
            break
        match = re.match(r"^\s*(?:[-*]|\d+[.)])\s*(?:\[[ xX~!]\]\s*)?(.*\S)\s*$", line)
        if match:
            items.append(match.group(1).strip())
    if not items:
        raise TicketDescriptionMalformed("Acceptance criteria section has no bullet items")
    return tuple(items)


def extract_lesson_refs(description: str) -> tuple[str, ...]:
    refs: set[str] = set()
    for match in re.finditer(r"docs/sop/lessons/(L-[A-Z]+-\d+[-A-Za-z0-9_.]*\.md)", description):
        refs.add(match.group(1))
    for match in re.finditer(r"\b(L-[A-Z]+-\d+[-A-Za-z0-9_.]*)(?:\.md)?\b", description):
        stem = match.group(1)
        refs.add(stem if stem.endswith(".md") else f"{stem}.md")
    return tuple(sorted(refs))


def resolve_lesson_refs(repo: Path, refs: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    resolved: list[str] = []
    broken: list[str] = []
    lessons_dir = repo / "docs" / "sop" / "lessons"
    for ref in refs:
        path = lessons_dir / ref
        if path.exists():
            resolved.append(f"docs/sop/lessons/{ref}")
        else:
            broken.append(ref)
            resolved.append(LESSON_PLACEHOLDER)
    return tuple(resolved), tuple(broken)


def parse_ticket(ticket: MilestoneTicket, *, repo: Path) -> ParsedTicket:
    acceptance_criteria = parse_acceptance_criteria(ticket.description)
    lessons, broken = resolve_lesson_refs(repo, extract_lesson_refs(ticket.description))
    if broken:
        _ = LessonReferenceBroken(f"{ticket.key}: {', '.join(broken)}")
    return ParsedTicket(
        ticket=ticket,
        sprint=sprint_from_labels(ticket.labels),
        acceptance_criteria=acceptance_criteria,
        lessons=lessons,
        broken_lessons=broken,
    )


def _group_key(parsed: ParsedTicket) -> tuple[int, int, str]:
    try:
        sprint_idx = SPRINT_ORDER.index(parsed.sprint)
    except ValueError:
        sprint_idx = len(SPRINT_ORDER)
    return (sprint_idx, priority_rank(parsed.ticket.priority), parsed.ticket.key)


def render_release_notes(
    *,
    version: str,
    today: date,
    included: Iterable[ParsedTicket],
    skipped: Iterable[SkippedTicket],
) -> str:
    included_rows = sorted(included, key=_group_key)
    skipped_rows = tuple(skipped)
    lines = [
        f"# OmniSight {version} Release Notes",
        "",
        f"Generated: {today.isoformat()}",
        "Source: JIRA fixVersion milestone",
        "",
        "## Ops Handoff",
        "",
        f"- Included tickets: {len(included_rows)}",
        f"- Skipped tickets: {len(skipped_rows)}",
        "",
    ]

    current: tuple[str, str] | None = None
    for parsed in included_rows:
        group = (parsed.sprint, parsed.ticket.priority)
        if group != current:
            if current is not None:
                lines.append("")
            lines.append(f"## Sprint {parsed.sprint} / {parsed.ticket.priority}")
            lines.append("")
            current = group
        lines.append(f"### {parsed.ticket.key} - {parsed.ticket.summary}")
        lines.append("")
        lines.append("Acceptance criteria:")
        for item in parsed.acceptance_criteria:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Lessons:")
        if parsed.lessons:
            for lesson in parsed.lessons:
                lines.append(f"- {lesson}")
        else:
            lines.append("- None")
        if parsed.broken_lessons:
            lines.append("")
            lines.append("Broken lesson references:")
            for lesson in parsed.broken_lessons:
                lines.append(f"- {lesson}")
        lines.append("")

    if skipped_rows:
        lines.extend(["## Skipped tickets", ""])
        for skipped_ticket in skipped_rows:
            lines.append(f"- {skipped_ticket.key}: {skipped_ticket.reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate_release_notes(
    *,
    version: str,
    repo: Path,
    jira: JiraClient,
    today: date,
) -> ReleaseNotesResult:
    if not jira.fix_version_exists(version):
        suggestions = ", ".join(jira.suggest_versions(version)) or "no similar versions found"
        raise JIRAFixVersionNotFound(f"{version} was not found; try one of: {suggestions}")

    included: list[ParsedTicket] = []
    skipped: list[SkippedTicket] = []
    for ticket in jira.tickets_for_fix_version(version):
        try:
            included.append(parse_ticket(ticket, repo=repo))
        except TicketDescriptionMalformed as exc:
            skipped.append(SkippedTicket(ticket.key, ticket.summary, f"TicketDescriptionMalformed: {exc}"))

    output_path = repo / "release-notes" / f"{version}.md"
    rendered = render_release_notes(
        version=version,
        today=today,
        included=included,
        skipped=skipped,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return ReleaseNotesResult(
        version=version,
        output_path=output_path,
        rendered=rendered,
        included=tuple(included),
        skipped=tuple(skipped),
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def checkout_release_branch(repo: Path, version: str) -> None:
    branch = f"release/{version}"
    existing = _git(repo, "branch", "--list", branch).stdout.strip()
    if existing:
        _git(repo, "checkout", branch)
    else:
        _git(repo, "checkout", "-b", branch)


def commit_release_notes(repo: Path, output_path: Path, version: str) -> None:
    rel_path = output_path.relative_to(repo).as_posix()
    _git(repo, "add", rel_path, "release-notes/.gitkeep")
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if diff.returncode == 0:
        return

    env_name = _git(repo, "config", "user.name").stdout.strip()
    env_email = _git(repo, "config", "user.email").stdout.strip()
    global_name = _git(repo, "config", "--global", "user.name").stdout.strip()
    global_email = _git(repo, "config", "--global", "user.email").stdout.strip()
    _git(
        repo,
        "commit",
        "-m",
        f"[OP-888] Generate release notes for {version}",
        "-m",
        "\n".join(
            [
                "Generated from JIRA fixVersion tickets for ops handoff.",
                "",
                "[Tier-B]",
                "",
                "Co-Authored-By: GPT-5.5 (codex-cli) <noreply@openai.com>",
                f"Co-Authored-By: {env_name} <{env_email}>",
                f"Co-Authored-By: {global_name} <{global_email}>",
            ]
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="JIRA fixVersion, e.g. v1.2.3")
    parser.add_argument("--agent-class", default=DEFAULT_AGENT_CLASS)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--no-commit", action="store_true", help="Write notes without switching/committing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    jira = JiraDispatchMilestoneClient(args.agent_class)
    if not args.no_commit:
        checkout_release_branch(repo, args.version)
    try:
        result = generate_release_notes(
            version=args.version,
            repo=repo,
            jira=jira,
            today=date.today(),
        )
    except JIRAFixVersionNotFound as exc:
        print(json.dumps({"status": "refused", "error_code": "JIRAFixVersionNotFound", "error": str(exc)}))
        return 2

    if not args.no_commit:
        commit_release_notes(repo, result.output_path, result.version)

    print(
        json.dumps(
            {
                "status": "generated",
                "version": result.version,
                "output": result.output_path.relative_to(repo).as_posix(),
                "included": len(result.included),
                "skipped": len(result.skipped),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
