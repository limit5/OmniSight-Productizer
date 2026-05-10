#!/usr/bin/env python3
"""OP-821 - daily SDK CostGuard spend report."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.db_url import parse as parse_db_url  # noqa: E402

DEFAULT_CHANNEL = "#omnisight-cost"
DEFAULT_WINDOW_HOURS = 24


@dataclass(frozen=True)
class CostRow:
    call_id: str
    model: str
    ticket_key: str
    estimated_usd: float
    actual_usd: float | None
    observed_at: datetime

    @property
    def spend_usd(self) -> float:
        return self.actual_usd if self.actual_usd is not None else self.estimated_usd


def parse_as_of(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ticket_from_metadata(raw: Any) -> str | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    for key in ("ticket_key", "ticket", "jira_key", "task_id"):
        if raw.get(key):
            return str(raw[key])
    return None


def row_from_mapping(raw: dict[str, Any]) -> CostRow:
    observed = raw.get("observed_at") or raw.get("completed_at") or raw.get("created_at")
    if isinstance(observed, datetime):
        observed_at = observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)
    else:
        observed_at = parse_as_of(str(observed)) if observed else datetime.now(timezone.utc)
    actual = raw.get("actual_usd", raw.get("cost_usd_actual"))
    return CostRow(
        call_id=str(raw.get("call_id") or ""),
        model=str(raw.get("model") or "unknown"),
        ticket_key=str(
            raw.get("ticket_key")
            or _ticket_from_metadata(raw.get("metadata"))
            or raw.get("workspace")
            or raw.get("task_type")
            or "unknown"
        ),
        estimated_usd=float(
            raw.get("estimated_usd", raw.get("cost_usd_estimated") or 0.0)
        ),
        actual_usd=None if actual is None else float(actual),
        observed_at=observed_at,
    )


def build_report(rows: list[CostRow], *, as_of: datetime) -> dict[str, Any]:
    by_ticket: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    estimated = actual = 0.0
    actual_count = 0
    for row in rows:
        by_ticket[row.ticket_key] += row.spend_usd
        by_model[row.model] += row.spend_usd
        estimated += row.estimated_usd
        if row.actual_usd is not None:
            actual += row.actual_usd
            actual_count += 1
    total = sum(by_ticket.values())
    ticket_count = len(by_ticket)
    return {
        "generated_at": as_of.isoformat(),
        "window_hours": DEFAULT_WINDOW_HOURS,
        "row_count": len(rows),
        "ticket_count": ticket_count,
        "total_sdk_spend_usd": round(total, 6),
        "estimated_usd": round(estimated, 6),
        "actual_usd": round(actual, 6),
        "actual_row_count": actual_count,
        "per_ticket_avg_usd": round(total / ticket_count, 6) if ticket_count else 0.0,
        "top_expensive_tickets": [
            {"ticket_key": key, "spend_usd": round(value, 6)}
            for key, value in sorted(by_ticket.items(), key=lambda item: item[1], reverse=True)[:5]
        ],
        "cost_by_model": {
            key: round(value, 6)
            for key, value in sorted(by_model.items(), key=lambda item: item[0])
        },
    }


def render_slack_text(report: dict[str, Any]) -> str:
    top = ", ".join(
        f"{item['ticket_key']}=${item['spend_usd']:.4f}"
        for item in report["top_expensive_tickets"]
    ) or "none"
    models = ", ".join(
        f"{model}=${amount:.4f}" for model, amount in report["cost_by_model"].items()
    ) or "none"
    return (
        "Daily SDK cost report\n"
        f"Window: {report['window_hours']}h, rows: {report['row_count']}, "
        f"tickets: {report['ticket_count']}\n"
        f"Total spend: ${report['total_sdk_spend_usd']:.4f}; "
        f"per-ticket avg: ${report['per_ticket_avg_usd']:.4f}\n"
        f"Top tickets: {top}\n"
        f"By model: {models}"
    )


async def fetch_cost_rows(*, since: datetime, dsn: str) -> list[CostRow]:
    parsed = parse_db_url(dsn)
    if not parsed.is_postgres:
        raise RuntimeError("sdk cost report requires a Postgres database URL")
    import asyncpg  # type: ignore[import-not-found]

    conn = await asyncpg.connect(**parsed.asyncpg_connect_kwargs())
    try:
        records = await conn.fetch(
            """
            SELECT call_id, model, cost_usd_estimated, cost_usd_actual,
                   workspace, task_type, metadata,
                   COALESCE(completed_at, created_at) AS observed_at
            FROM cost_estimates
            WHERE COALESCE(completed_at, created_at) >= $1
            ORDER BY observed_at DESC
            """,
            since,
        )
    finally:
        await conn.close()
    return [row_from_mapping(dict(record)) for record in records]


def load_fixture(path: Path, *, since: datetime) -> list[CostRow]:
    rows = [row_from_mapping(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row.observed_at >= since]


def post_to_slack(*, token: str, channel: str, text: str) -> None:
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"slack post failed: {body.get('error', 'unknown')}")


async def async_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of")
    parser.add_argument("--fixture-jsonl", type=Path)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    as_of = parse_as_of(args.as_of) if args.as_of else datetime.now(timezone.utc)
    since = as_of - timedelta(hours=DEFAULT_WINDOW_HOURS)
    if args.fixture_jsonl:
        rows = load_fixture(args.fixture_jsonl, since=since)
    else:
        dsn = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("OMNISIGHT_DATABASE_URL or DATABASE_URL must be set")
        rows = await fetch_cost_rows(since=since, dsn=dsn)

    report = build_report(rows, as_of=as_of)
    token = os.environ.get("OMNISIGHT_SLACK_TOKEN", "").strip()
    if token and not args.stdout:
        post_to_slack(
            token=token,
            channel=os.environ.get("OMNISIGHT_SLACK_CHANNEL", DEFAULT_CHANNEL),
            text=render_slack_text(report),
        )
    else:
        print(json.dumps(report, sort_keys=True))
    return 0


def main(argv: list[str]) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
