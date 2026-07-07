#!/usr/bin/env python3
"""OP-2544 -- runner incident write-rate canary.

Daily read-only audit for the OP-2537 durable incident writer. The canary
counts live-v1/uuid-style ``runner_incidents`` rows written in the rolling
window and escalates to one JIRA ticket only when the fleet was active but
no durable incident writes landed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_WINDOW_DAYS = 7
DEFAULT_AGENT_CLASS = "subscription-codex"
CANARY_LABEL = "incident-write-canary"
CANARY_SUMMARY = "[CANARY] runner_incidents live write-rate is zero"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
jira_dispatch: Any | None = None


@dataclass(frozen=True)
class CanaryCounts:
    incident_write_count: int
    fleet_activity_count: int
    window_start: datetime
    window_end: datetime

    @property
    def should_escalate(self) -> bool:
        return self.incident_write_count == 0 and self.fleet_activity_count > 0


class JiraEscalator(Protocol):
    def escalate(self, counts: CanaryCounts) -> str:
        """Create/update the one open incident-write canary ticket."""


def database_url_from_env() -> str:
    url = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("OMNISIGHT_DATABASE_URL or DATABASE_URL is required")
    return url


def _is_live_incident_id(incident_id: str) -> bool:
    if incident_id.startswith("live-v1-"):
        return True
    return HEX64_RE.fullmatch(incident_id) is None


def count_live_incident_writes(
    engine: Engine,
    *,
    since: datetime,
    until: datetime,
) -> int:
    stmt = text(
        """
        SELECT incident_id
        FROM runner_incidents
        WHERE created_at >= :since AND created_at <= :until
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt, {"since": since, "until": until}).scalars().all()
    return sum(1 for incident_id in rows if _is_live_incident_id(str(incident_id)))


def count_fleet_activity(
    engine: Engine,
    *,
    since: datetime,
    until: datetime,
) -> int:
    stmt = text(
        """
        SELECT count(*)
        FROM runner_metrics
        WHERE ts >= :since AND ts <= :until
        """
    )
    with engine.begin() as conn:
        return int(conn.execute(stmt, {"since": since, "until": until}).scalar_one())


def collect_counts(
    engine: Engine,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CanaryCounts:
    window_end = now or datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=window_days)
    return CanaryCounts(
        incident_write_count=count_live_incident_writes(
            engine,
            since=window_start,
            until=window_end,
        ),
        fleet_activity_count=count_fleet_activity(
            engine,
            since=window_start,
            until=window_end,
        ),
        window_start=window_start,
        window_end=window_end,
    )


def render_escalation_comment(counts: CanaryCounts) -> str:
    return (
        "incident_write_canary fired\n"
        f"window_start={counts.window_start.isoformat()}\n"
        f"window_end={counts.window_end.isoformat()}\n"
        f"incident_write_count={counts.incident_write_count}\n"
        f"fleet_activity_count={counts.fleet_activity_count}\n"
        "action=no auto-remediation; investigate durable runner_incidents writer"
    )


def _adf_text(text_body: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line or " "}],
            }
            for line in text_body.splitlines()
        ],
    }


def _jira_request(
    client: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        client.base_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": client.auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _jira_dispatch() -> Any:
    global jira_dispatch
    if jira_dispatch is None:
        from backend.agents import jira_dispatch as imported

        jira_dispatch = imported
    return jira_dispatch


class JiraDispatchEscalator:
    """JIRA escalation backed by jira_dispatch's public client/comment API."""

    def __init__(self, *, agent_class: str = DEFAULT_AGENT_CLASS) -> None:
        self.agent_class = agent_class

    def escalate(self, counts: CanaryCounts) -> str:
        jd = _jira_dispatch()
        client = jd.make_client(self.agent_class)
        key = self._find_open_canary_ticket(client)
        comment = render_escalation_comment(counts)
        if key is None:
            key = self._create_canary_ticket(client, comment)
        else:
            jd.add_comment(
                client,
                key,
                comment,
                idem_key=f"incident-write-canary-{counts.window_end.date().isoformat()}",
            )
        return key

    def _find_open_canary_ticket(
        self,
        client: Any,
    ) -> str | None:
        jql = (
            f'project = "{client.project_key}" AND labels = "{CANARY_LABEL}" '
            "AND statusCategory != Done ORDER BY created DESC"
        )
        payload = _jira_request(
            client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "maxResults": 1,
                "fields": ["summary", "status", "labels"],
            },
        )
        issues = payload.get("issues") or []
        return str(issues[0]["key"]) if issues else None

    def _create_canary_ticket(
        self,
        client: Any,
        comment: str,
    ) -> str:
        payload = _jira_request(
            client,
            "POST",
            "/issue",
            {
                "fields": {
                    "project": {"key": client.project_key},
                    "summary": CANARY_SUMMARY,
                    "description": _adf_text(comment),
                    "issuetype": {"name": "Task"},
                    "labels": [CANARY_LABEL, "component:devops", "OP-2544"],
                }
            },
        )
        return str(payload["key"])


def run_canary(
    *,
    engine: Engine,
    escalator: JiraEscalator | None,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool = False,
) -> CanaryCounts:
    counts = collect_counts(engine, now=now, window_days=window_days)
    print(
        "incident_write_canary "
        f"window_days={window_days} "
        f"incident_write_count={counts.incident_write_count} "
        f"fleet_activity_count={counts.fleet_activity_count} "
        f"should_escalate={str(counts.should_escalate).lower()}"
    )
    if counts.should_escalate and dry_run:
        print("incident_write_canary dry_run=true action=would_escalate")
    elif counts.should_escalate and escalator is not None:
        key = escalator.escalate(counts)
        print(f"incident_write_canary escalated key={key}")
    return counts


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--jira-agent", default=DEFAULT_AGENT_CLASS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        engine = create_engine(args.database_url or database_url_from_env(), future=True)
        run_canary(
            engine=engine,
            escalator=JiraDispatchEscalator(agent_class=args.jira_agent),
            window_days=args.window_days,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - systemd needs a concise stderr failure.
        print(f"incident_write_canary failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
