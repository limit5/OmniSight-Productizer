#!/usr/bin/env python3
"""OP-873 daily JIRA stale-ticket audit.

Finds OP tickets that have remained in To Do for more than 30 days,
writes a markdown report, and posts a runner-audit comment on tickets
older than 60 days.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd  # noqa: E402


LOG = logging.getLogger("jira_stale_audit")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "audit"
AUTO_COMMENT_TEXT = (
    "[runner-audit] ticket aged 60d in To Do — consider close-as-Wont-Do or revive"
)
STALE_DAYS = 30
AUTO_COMMENT_DAYS = 60
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 1.0


class JIRAQueryRateLimited(RuntimeError):
    """JIRA returned a rate-limit response; caller should retry."""


class MarkdownReportWriteFailed(RuntimeError):
    """Markdown report could not be written."""


@dataclass(frozen=True)
class StaleTicket:
    key: str
    summary: str
    created: datetime
    labels: tuple[str, ...]
    tier: str
    sprint: str
    agent_class: str

    def age_days(self, now: datetime) -> int:
        return int((now - self.created).total_seconds() // 86400)


def _parse_jira_datetime(raw: str) -> datetime:
    cleaned = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).astimezone(timezone.utc)


def _class_from_labels(labels: tuple[str, ...]) -> str:
    classes = {label.removeprefix("class:") for label in labels if label.startswith("class:")}
    if classes & {"subscription-claude", "api-anthropic", "claude"}:
        return "claude"
    if classes & {"subscription-codex", "api-openai", "codex"}:
        return "codex"
    return "unassigned"


def _tier_from_labels(labels: tuple[str, ...]) -> str:
    for label in labels:
        if label.startswith("tier:"):
            return label.split(":", 1)[1] or "unassigned"
    return "unassigned"


def _sprint_from_fields(fields: dict[str, Any], labels: tuple[str, ...]) -> str:
    for label in labels:
        if label.startswith(("sprint:", "sprint-")):
            return label.split(":", 1)[-1].removeprefix("sprint-") or "unassigned"
    versions = fields.get("fixVersions") or []
    if versions:
        return versions[0].get("name") or "unassigned"
    return "unassigned"


def _ticket_from_issue(issue: dict[str, Any]) -> StaleTicket:
    fields = issue.get("fields") or {}
    labels = tuple(str(label) for label in fields.get("labels") or ())
    created = _parse_jira_datetime(fields.get("created") or "")
    return StaleTicket(
        key=str(issue.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        created=created,
        labels=labels,
        tier=_tier_from_labels(labels),
        sprint=_sprint_from_fields(fields, labels),
        agent_class=_class_from_labels(labels),
    )


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate-limited" in text


def _with_jira_backoff(fn: Callable[[], Any]) -> Any:
    delay = BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classify JIRA helper failures
            if not _is_rate_limit(exc):
                raise
            last_exc = exc
            LOG.warning("JIRAQueryRateLimited attempt=%s/%s err=%s", attempt, MAX_ATTEMPTS, exc)
            if attempt == MAX_ATTEMPTS:
                break
            time.sleep(delay)
            delay *= 2
    raise JIRAQueryRateLimited(str(last_exc)) from last_exc


def fetch_stale_tickets(client: jd.DispatchClient, *, max_results: int = 100) -> tuple[StaleTicket, ...]:
    jql = (
        f'project = "{client.project_key}" '
        'AND status = "To Do" '
        f"AND created < -{STALE_DAYS}d "
        "ORDER BY created ASC"
    )
    resp = _with_jira_backoff(
        lambda: jd._request(
            client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": ["summary", "labels", "created", "fixVersions"],
                "maxResults": max_results,
            },
        )
    )
    return tuple(_ticket_from_issue(issue) for issue in resp.get("issues", []) or [])


def _counter_lines(title: str, counts: Counter[str]) -> list[str]:
    lines = [f"## {title}"]
    if not counts:
        return lines + ["- none: 0"]
    return lines + [f"- {key}: {counts[key]}" for key in sorted(counts)]


def _previous_total(output_dir: Path, report_date: datetime) -> int | None:
    previous = output_dir / f"jira-stale-tickets-{(report_date - timedelta(days=7)).date()}.md"
    if not previous.exists():
        return None
    match = re.search(r"Total stale tickets: (\d+)", previous.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else None


def render_report(tickets: tuple[StaleTicket, ...], *, now: datetime, output_dir: Path) -> str:
    total = len(tickets)
    previous = _previous_total(output_dir, now)
    delta = "n/a" if previous is None else f"{total - previous:+d}"
    class_counts = Counter(ticket.agent_class for ticket in tickets)
    tier_counts = Counter(ticket.tier for ticket in tickets)
    sprint_counts = Counter(ticket.sprint for ticket in tickets)

    lines = [
        f"# JIRA stale-ticket audit - {now.date()}",
        "",
        f"Dashboard: total stale tickets={total}; delta_vs_last_week={delta}",
        f"Total stale tickets: {total}",
        "",
        *_counter_lines("By class", class_counts),
        "",
        *_counter_lines("By tier", tier_counts),
        "",
        *_counter_lines("By sprint", sprint_counts),
        "",
        "## Tickets",
    ]
    if not tickets:
        lines.append("- none")
    for ticket in tickets:
        lines.append(
            f"- {ticket.key}: age={ticket.age_days(now)}d; class={ticket.agent_class}; "
            f"tier={ticket.tier}; sprint={ticket.sprint}; {ticket.summary}"
        )
    return "\n".join(lines) + "\n"


def write_report(report: str, *, output_dir: Path, report_date: datetime) -> Path:
    path = output_dir / f"jira-stale-tickets-{report_date.date()}.md"
    latest = output_dir / "jira-stale-tickets-latest.md"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(path.name)
    except OSError as exc:
        LOG.exception("MarkdownReportWriteFailed path=%s", path)
        raise MarkdownReportWriteFailed(str(exc)) from exc
    return path


def auto_comment_aged_tickets(
    client: jd.DispatchClient,
    tickets: tuple[StaleTicket, ...],
    *,
    now: datetime,
) -> tuple[str, ...]:
    commented: list[str] = []
    for ticket in tickets:
        if ticket.age_days(now) < AUTO_COMMENT_DAYS:
            continue
        _with_jira_backoff(lambda key=ticket.key: jd.add_comment(client, key, AUTO_COMMENT_TEXT))
        commented.append(ticket.key)
    return tuple(commented)


def run(
    *,
    client: jd.DispatchClient,
    now: datetime,
    output_dir: Path,
    dry_run: bool = False,
) -> tuple[Path, tuple[str, ...]]:
    tickets = fetch_stale_tickets(client)
    report = render_report(tickets, now=now, output_dir=output_dir)
    path = write_report(report, output_dir=output_dir, report_date=now)
    commented = () if dry_run else auto_comment_aged_tickets(client, tickets, now=now)
    LOG.info("jira_stale_audit_done stale=%s commented=%s report=%s", len(tickets), len(commented), path)
    return path, commented


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    return _parse_jira_datetime(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp for tests/manual replay")
    parser.add_argument("--dry-run", action="store_true", help="Write report but do not comment")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    try:
        run(
            client=jd.make_client(args.agent_class),
            now=_parse_now(args.now),
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except MarkdownReportWriteFailed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
