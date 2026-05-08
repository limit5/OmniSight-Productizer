"""OP-777 release notes generator for release-tagged events.

Consumes ``release_tagged`` records from the Sprint D release log, builds
``docs/releases/vX.Y.Z.md`` from JIRA fixVersion tickets plus per-file
lessons, and pushes the result to Gerrit for operator polish. The module
keeps all mutable state in cursor files / git refs; module globals are
constants so multi-worker processes derive the same values independently.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from backend.agents import jira_dispatch

log = logging.getLogger(__name__)

EVENT_RELEASE_TAGGED = "release_tagged"
EVENT_RELEASE_NOTES_DRAFTED = "release_notes_drafted"
DEFAULT_AGENT_CLASS = "subscription-codex"
DEFAULT_REPO = Path("/home/user/sora-bridge")
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/systemd.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/release-notes.cursor")

NotifyFn = Callable[[str, str, str], None]
EventSink = Callable[[str, dict[str, Any]], None]
PrepareReviewFn = Callable[[Path], None]


@dataclass(frozen=True)
class ReleaseTicket:
    """JIRA ticket fields needed to draft release notes."""

    key: str
    summary: str
    component: str
    status: str
    issue_type: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class LessonLink:
    """Per-file lesson linked from a release note."""

    ticket_key: str
    title: str
    path: Path


@dataclass(frozen=True)
class DraftResult:
    """Outcome of one release-notes draft attempt."""

    status: str
    version: str
    notes_path: Path | None = None
    change_url: str | None = None
    detail: str = ""


class ReleaseNotesJiraClient(Protocol):
    def tickets_for_fix_version(self, version: str) -> list[ReleaseTicket]:
        ...


class GerritPusher(Protocol):
    def push(self, repo: Path, *, agent_class: str, target: str) -> jira_dispatch.GerritPushResult:
        ...


class JiraDispatchReleaseNotesClient:
    """JIRA REST adapter mirroring ``jira_dispatch`` authentication."""

    def __init__(self, client: jira_dispatch.DispatchClient) -> None:
        self.client = client

    @classmethod
    def from_agent_class(cls, agent_class: str) -> "JiraDispatchReleaseNotesClient":
        return cls(jira_dispatch.make_client(agent_class))

    def tickets_for_fix_version(self, version: str) -> list[ReleaseTicket]:
        jql = (
            f'project = "{self.client.project_key}" '
            f'AND fixVersion = "{version}" '
            "ORDER BY component ASC, key ASC"
        )
        payload = jira_dispatch._request(
            self.client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": ["summary", "components", "status", "issuetype", "labels"],
                "maxResults": 500,
            },
        )
        tickets: list[ReleaseTicket] = []
        for issue in payload.get("issues", []):
            fields = issue.get("fields") or {}
            components = fields.get("components") or []
            status = fields.get("status") or {}
            issue_type = fields.get("issuetype") or {}
            tickets.append(
                ReleaseTicket(
                    key=str(issue.get("key") or ""),
                    summary=str(fields.get("summary") or ""),
                    component=str((components[0] if components else {}).get("name") or "Unassigned"),
                    status=str(status.get("name") or ""),
                    issue_type=str(issue_type.get("name") or ""),
                    labels=tuple(str(label) for label in fields.get("labels") or []),
                )
            )
        return tickets


class JiraDispatchGerritPusher:
    """Gerrit pusher using the runner's existing dispatch helper."""

    def push(self, repo: Path, *, agent_class: str, target: str) -> jira_dispatch.GerritPushResult:
        return jira_dispatch.push_to_gerrit_for_review(repo, agent_class, target=target)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_event(event: str, payload: dict[str, Any]) -> None:
    record = {
        "timestamp": utc_now_iso(),
        "level": "INFO",
        "event": event,
        **payload,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def _default_notify(channel: str, severity: str, detail: str) -> None:
    jira_dispatch.notify_operator(channel=channel, severity=severity, detail=detail)


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def safe_release_version(raw: str) -> str:
    """Return a filesystem-safe release version string."""
    version = raw.strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?", version):
        raise ValueError(f"unsupported release version: {raw!r}")
    return version


def version_from_event(event: dict[str, Any]) -> str:
    raw = str(event.get("fixVersion") or event.get("tag") or event.get("version") or "")
    return safe_release_version(raw)


def ticket_area(ticket: ReleaseTicket) -> str:
    for label in ticket.labels:
        if label.startswith("area:"):
            return label.split(":", 1)[1]
    return ticket.component


def is_breaking(ticket: ReleaseTicket) -> bool:
    return "breaking-change" in ticket.labels


def is_bugfix(ticket: ReleaseTicket) -> bool:
    labels = set(ticket.labels)
    return ticket.issue_type.lower() == "bug" or bool(labels & {"bug", "bugfix", "type:bug"})


def is_internal_reliability(ticket: ReleaseTicket) -> bool:
    labels = set(ticket.labels)
    if ticket.component.upper() == "RUNNER":
        return True
    return bool(labels & {"runner", "internal", "reliability", "area:devops", "area:tests"})


def _grouped_ticket_lines(tickets: Iterable[ReleaseTicket]) -> list[str]:
    groups: dict[str, list[ReleaseTicket]] = {}
    for ticket in tickets:
        groups.setdefault(ticket_area(ticket), []).append(ticket)
    lines: list[str] = []
    for area in sorted(groups):
        lines.append(f"### {area}")
        for ticket in sorted(groups[area], key=lambda item: item.key):
            lines.append(f"- {ticket.key}: {ticket.summary}")
        lines.append("")
    return lines or ["- None", ""]


def _lesson_title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _lesson_href(path: Path) -> str:
    """Return a link target from ``docs/releases/vX.Y.Z.md`` to a lesson."""
    try:
        return "../" + path.relative_to("docs").as_posix()
    except ValueError:
        return "../" + path.as_posix()


def lessons_for_release(*, repo: Path, tickets: Iterable[ReleaseTicket]) -> list[LessonLink]:
    ticket_keys = {ticket.key for ticket in tickets}
    lessons_dir = repo / "docs" / "sop" / "lessons"
    if not lessons_dir.exists():
        return []
    lessons: list[LessonLink] = []
    for path in sorted(lessons_dir.glob("L-OP-*-*.md")):
        match = re.match(r"L-(OP-\d+)-", path.name)
        if match is None or match.group(1) not in ticket_keys:
            continue
        lessons.append(
            LessonLink(
                ticket_key=match.group(1),
                title=_lesson_title(path),
                path=path.relative_to(repo),
            )
        )
    return lessons


def render_release_notes(
    *,
    version: str,
    tickets: list[ReleaseTicket],
    lessons: list[LessonLink],
) -> str:
    """Render release notes with the OP-777 template sections."""
    breaking = [ticket for ticket in tickets if is_breaking(ticket)]
    bugfixes = [ticket for ticket in tickets if is_bugfix(ticket) and not is_breaking(ticket)]
    internal = [
        ticket
        for ticket in tickets
        if is_internal_reliability(ticket) and not is_bugfix(ticket) and not is_breaking(ticket)
    ]
    features = [
        ticket
        for ticket in tickets
        if ticket not in breaking and ticket not in bugfixes and ticket not in internal
    ]

    lines = [
        f"# OmniSight {version} Release Notes",
        "",
        "## Features",
        "",
        *_grouped_ticket_lines(features),
        "## Bug fixes",
        "",
        *_grouped_ticket_lines(bugfixes),
        "## Internal / runner reliability",
        "",
        *_grouped_ticket_lines(internal),
        "## Lessons learned",
        "",
    ]
    if lessons:
        for lesson in lessons:
            lines.append(f"- [{lesson.title}]({_lesson_href(lesson.path)}) ({lesson.ticket_key})")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Breaking changes",
            "",
            *_grouped_ticket_lines(breaking),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _assert_clean_worktree(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain").stdout.strip()
    if status:
        raise RuntimeError(f"release notes worktree is dirty:\n{status}")


def _commit_release_notes(repo: Path, notes_path: Path, version: str) -> None:
    rel_path = notes_path.relative_to(repo).as_posix()
    env_name = _git(repo, "config", "user.name").stdout.strip()
    env_email = _git(repo, "config", "user.email").stdout.strip()
    global_name = _git(repo, "config", "--global", "user.name").stdout.strip()
    global_email = _git(repo, "config", "--global", "user.email").stdout.strip()
    _git(repo, "add", rel_path)
    _git(
        repo,
        "commit",
        "-m",
        f"Draft release notes for {version}",
        "-m",
        "\n".join(
            [
                "Generated from JIRA fixVersion tickets and per-file lessons.",
                "",
                "[Tier-S]",
                "",
                "Co-Authored-By: GPT-5.5 (codex-cli) <noreply@openai.com>",
                f"Co-Authored-By: {env_name} <{env_email}>",
                f"Co-Authored-By: {global_name} <{global_email}>",
            ]
        ),
    )


def prepare_gerrit_review_commit(repo: Path) -> None:
    """Install Gerrit metadata and add Change-Id to the generated commit."""
    jira_dispatch.install_commit_msg_hook(repo)
    jira_dispatch.ensure_change_ids(repo, base_ref="HEAD~1")


def draft_release_notes_for_event(
    event: dict[str, Any],
    *,
    repo: Path = DEFAULT_REPO,
    jira: ReleaseNotesJiraClient,
    pusher: GerritPusher = JiraDispatchGerritPusher(),
    prepare_review: PrepareReviewFn = prepare_gerrit_review_commit,
    agent_class: str = DEFAULT_AGENT_CLASS,
    target_branch: str = "develop",
    notify: NotifyFn = _default_notify,
    event_sink: EventSink = emit_event,
) -> DraftResult:
    """Generate release notes for one ``release_tagged`` record."""
    if event.get("event") != EVENT_RELEASE_TAGGED:
        return DraftResult("ignored", "")

    try:
        version = version_from_event(event)
        _assert_clean_worktree(repo)
        tickets = jira.tickets_for_fix_version(version)
        lessons = lessons_for_release(repo=repo, tickets=tickets)
        notes_path = repo / "docs" / "releases" / f"{version}.md"
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(
            render_release_notes(version=version, tickets=tickets, lessons=lessons),
            encoding="utf-8",
        )
        _commit_release_notes(repo, notes_path, version)
        prepare_review(repo)
        push = pusher.push(repo, agent_class=agent_class, target=target_branch)
        if not push.success:
            detail = f"release notes Gerrit push failed for {version}: {push.detail}"
            notify("release-notes", "critical", detail)
            return DraftResult("push_failed", version, notes_path, None, detail)
        payload = {
            "fixVersion": version,
            "notes_path": notes_path.relative_to(repo).as_posix(),
            "ticket_count": len(tickets),
            "lesson_count": len(lessons),
            "gerrit_change_url": push.change_url,
        }
        event_sink(EVENT_RELEASE_NOTES_DRAFTED, payload)
        return DraftResult("drafted", version, notes_path, push.change_url or "", "")
    except Exception as exc:
        detail = f"release notes generation failed: {exc}"
        notify("release-notes", "critical", detail)
        return DraftResult("failed", str(event.get("fixVersion") or ""), None, None, detail)


def _parse_json_line(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(raw[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def iter_new_records(path: Path, cursor_path: Path) -> Iterable[dict[str, Any]]:
    """Yield JSON records appended since the last stored byte offset."""
    offset = 0
    if cursor_path.exists():
        try:
            offset = int(cursor_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            offset = 0
    if not path.exists():
        return
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        for raw in fh:
            record = _parse_json_line(raw)
            if record is not None:
                yield record
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(str(fh.tell()), encoding="utf-8")


def run_once(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    jira: ReleaseNotesJiraClient,
    pusher: GerritPusher = JiraDispatchGerritPusher(),
    prepare_review: PrepareReviewFn = prepare_gerrit_review_commit,
    agent_class: str = DEFAULT_AGENT_CLASS,
    target_branch: str = "develop",
    notify: NotifyFn = _default_notify,
    event_sink: EventSink = emit_event,
) -> list[DraftResult]:
    results: list[DraftResult] = []
    for record in iter_new_records(event_log, cursor):
        result = draft_release_notes_for_event(
            record,
            repo=repo,
            jira=jira,
            pusher=pusher,
            prepare_review=prepare_review,
            agent_class=agent_class,
            target_branch=target_branch,
            notify=notify,
            event_sink=event_sink,
        )
        if result.status != "ignored":
            results.append(result)
    return results


def follow(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    agent_class: str,
    target_branch: str,
    poll_seconds: float,
) -> None:
    jira = JiraDispatchReleaseNotesClient.from_agent_class(agent_class)
    pusher = JiraDispatchGerritPusher()
    while True:
        run_once(
            event_log=event_log,
            cursor=cursor,
            repo=repo,
            jira=jira,
            pusher=pusher,
            agent_class=agent_class,
            target_branch=target_branch,
        )
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--agent-class", default=DEFAULT_AGENT_CLASS)
    parser.add_argument("--target-branch", default="develop")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.once:
        jira = JiraDispatchReleaseNotesClient.from_agent_class(args.agent_class)
        run_once(
            event_log=args.event_log,
            cursor=args.cursor,
            repo=args.repo,
            jira=jira,
            agent_class=args.agent_class,
            target_branch=args.target_branch,
        )
        return 0
    follow(
        event_log=args.event_log,
        cursor=args.cursor,
        repo=args.repo,
        agent_class=args.agent_class,
        target_branch=args.target_branch,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
