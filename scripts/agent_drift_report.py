#!/usr/bin/env python3
"""OP-909 monthly agent drift report.

Compares runner_metrics over rolling 30-day windows by
``(agent_class, ticket_type)`` and writes ``docs/audit/agent-drift-YYYY-MM.md``.
"""
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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.db_url import parse as parse_db_url  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "audit"
WINDOW_DAYS = 30
SUCCESS_RATE_DROP_PAGE = -0.10
TIME_TO_COMPLETE_WARN = 0.50
LESSONS_USED_DROP_WARN = -0.30


class MonthlyReportInsufficientData(RuntimeError):
    """runner_metrics has less than 30 days of data."""


class AlertThresholdTunable(RuntimeError):
    """First two months are warn-only baseline establishment."""


@dataclass(frozen=True)
class MetricRow:
    agent_class: str
    ticket_type: str
    time_to_complete_seconds: float | None
    outcome: str | None
    lessons_used_count: int
    ts: datetime


@dataclass(frozen=True)
class WindowStats:
    count: int
    avg_time_to_complete_seconds: float
    success_rate: float
    avg_lessons_used: float


@dataclass(frozen=True)
class TrendRow:
    agent_class: str
    ticket_type: str
    current: WindowStats
    prior: WindowStats
    time_delta: float | None
    success_delta: float | None
    lessons_delta: float | None
    alerts: tuple[str, ...]


def parse_as_of(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct_delta(current: float, prior: float) -> float | None:
    if prior == 0:
        return None
    return (current - prior) / prior


def row_from_mapping(raw: dict[str, Any]) -> MetricRow:
    ts_raw = raw.get("ts")
    if isinstance(ts_raw, datetime):
        ts = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
    else:
        ts = parse_as_of(str(ts_raw))
    elapsed = raw.get("time_to_complete_seconds")
    return MetricRow(
        agent_class=str(raw.get("agent_class") or "unknown"),
        ticket_type=str(raw.get("ticket_type") or "unknown"),
        time_to_complete_seconds=None if elapsed is None else float(elapsed),
        outcome=None if raw.get("outcome") is None else str(raw.get("outcome")),
        lessons_used_count=int(raw.get("lessons_used_count") or 0),
        ts=ts,
    )


def _stats(rows: list[MetricRow]) -> WindowStats:
    completed = [r for r in rows if r.outcome]
    elapsed = [r.time_to_complete_seconds for r in completed if r.time_to_complete_seconds is not None]
    success = sum(1 for r in completed if r.outcome == "success")
    return WindowStats(
        count=len(completed),
        avg_time_to_complete_seconds=sum(elapsed) / len(elapsed) if elapsed else 0.0,
        success_rate=success / len(completed) if completed else 0.0,
        avg_lessons_used=sum(r.lessons_used_count for r in completed) / len(completed)
        if completed else 0.0,
    )


def build_trends(rows: list[MetricRow], *, as_of: datetime) -> tuple[TrendRow, ...]:
    if not rows or min(row.ts for row in rows) > as_of - timedelta(days=WINDOW_DAYS):
        raise MonthlyReportInsufficientData("need at least 30 days of runner_metrics history")

    current_start = as_of - timedelta(days=WINDOW_DAYS)
    prior_start = as_of - timedelta(days=WINDOW_DAYS * 2)
    grouped: dict[tuple[str, str], dict[str, list[MetricRow]]] = defaultdict(lambda: {"current": [], "prior": []})
    for row in rows:
        key = (row.agent_class, row.ticket_type)
        if current_start <= row.ts <= as_of:
            grouped[key]["current"].append(row)
        elif prior_start <= row.ts < current_start:
            grouped[key]["prior"].append(row)

    trends: list[TrendRow] = []
    baseline_warn_only = min(row.ts for row in rows) > as_of - timedelta(days=WINDOW_DAYS * 2)
    for (agent_class, ticket_type), buckets in sorted(grouped.items()):
        current = _stats(buckets["current"])
        prior = _stats(buckets["prior"])
        if not current.count or not prior.count:
            continue
        time_delta = _pct_delta(current.avg_time_to_complete_seconds, prior.avg_time_to_complete_seconds)
        lessons_delta = _pct_delta(current.avg_lessons_used, prior.avg_lessons_used)
        success_delta = current.success_rate - prior.success_rate
        alerts: list[str] = []
        if success_delta < SUCCESS_RATE_DROP_PAGE:
            level = "warn" if baseline_warn_only else "page"
            alerts.append(f"{level}: success_rate_drop={success_delta:.1%}")
        if time_delta is not None and time_delta > TIME_TO_COMPLETE_WARN:
            alerts.append(f"warn: time_to_complete_increase={time_delta:.1%}")
        if lessons_delta is not None and lessons_delta < LESSONS_USED_DROP_WARN:
            alerts.append(f"warn: lessons_used_drop={lessons_delta:.1%}")
        trends.append(
            TrendRow(
                agent_class=agent_class,
                ticket_type=ticket_type,
                current=current,
                prior=prior,
                time_delta=time_delta,
                success_delta=success_delta,
                lessons_delta=lessons_delta,
                alerts=tuple(alerts),
            )
        )
    return tuple(trends)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_report(trends: tuple[TrendRow, ...], *, as_of: datetime) -> str:
    lines = [
        f"# Agent Drift Report - {as_of:%Y-%m}",
        "",
        f"Window: rolling {WINDOW_DAYS}d ending {as_of.date()} vs prior {WINDOW_DAYS}d",
        "",
        "| agent_class | ticket_type | current_n | prior_n | time_delta | success_delta | lessons_delta | alerts |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    if not trends:
        lines.append("| none | none | 0 | 0 | n/a | n/a | n/a | no comparable buckets |")
    for trend in trends:
        lines.append(
            "| "
            f"{trend.agent_class} | {trend.ticket_type} | "
            f"{trend.current.count} | {trend.prior.count} | "
            f"{_fmt_pct(trend.time_delta)} | {_fmt_pct(trend.success_delta)} | "
            f"{_fmt_pct(trend.lessons_delta)} | {', '.join(trend.alerts) or 'none'} |"
        )
    lines.extend([
        "",
        "## Thresholds",
        "- Success rate drop >10% MoM: page operator after baseline, warn during first 2 months.",
        "- Time-to-complete increase >50% MoM: warn.",
        "- Lessons-used drop >30% MoM: warn.",
    ])
    return "\n".join(lines) + "\n"


def write_report(report: str, *, output_dir: Path, as_of: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"agent-drift-{as_of:%Y-%m}.md"
    path.write_text(report, encoding="utf-8")
    return path


async def fetch_rows(*, dsn: str, since: datetime, until: datetime) -> list[MetricRow]:
    parsed = parse_db_url(dsn)
    if not parsed.is_postgres:
        raise RuntimeError("agent drift report requires a Postgres database URL")
    import asyncpg  # type: ignore[import-not-found]

    conn = await asyncpg.connect(**parsed.asyncpg_connect_kwargs())
    try:
        records = await conn.fetch(
            """
            SELECT agent_class, ticket_type, time_to_complete_seconds,
                   outcome, lessons_used_count, ts
            FROM runner_metrics
            WHERE ts >= $1 AND ts <= $2
            ORDER BY ts ASC
            """,
            since,
            until,
        )
    finally:
        await conn.close()
    return [row_from_mapping(dict(record)) for record in records]


def load_fixture(path: Path) -> list[MetricRow]:
    return [row_from_mapping(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def post_page(*, text: str) -> None:
    token = os.environ.get("OMNISIGHT_SLACK_TOKEN", "").strip()
    channel = os.environ.get("OMNISIGHT_AGENT_DRIFT_ALERT_CHANNEL", "#omnisight-ops")
    if not token:
        return
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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fixture-jsonl", type=Path)
    args = parser.parse_args(argv)

    as_of = parse_as_of(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if args.fixture_jsonl:
        rows = load_fixture(args.fixture_jsonl)
    else:
        dsn = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("OMNISIGHT_DATABASE_URL or DATABASE_URL must be set")
        rows = await fetch_rows(dsn=dsn, since=as_of - timedelta(days=WINDOW_DAYS * 2), until=as_of)
    trends = build_trends(rows, as_of=as_of)
    path = write_report(render_report(trends, as_of=as_of), output_dir=args.output_dir, as_of=as_of)
    pages = [alert for trend in trends for alert in trend.alerts if alert.startswith("page:")]
    if pages:
        post_page(text=f"Agent drift threshold fired: {path}\n" + "\n".join(pages))
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(sys.argv[1:] if argv is None else argv))
    except MonthlyReportInsufficientData as exc:
        print(f"MonthlyReportInsufficientData: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
