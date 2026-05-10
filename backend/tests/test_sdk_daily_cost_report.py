"""OP-821 - tests for the daily SDK CostGuard spend report."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.cron.sdk_daily_cost_report import (
    build_report,
    load_fixture,
    parse_as_of,
    render_slack_text,
    row_from_mapping,
)


def _row(
    ticket_key: str,
    *,
    call_id: str,
    model: str = "claude-sonnet-4-6",
    estimated: float = 0.10,
    actual: float | None = None,
    observed_at: str = "2026-05-10T00:00:00+00:00",
) -> dict:
    return {
        "call_id": call_id,
        "model": model,
        "cost_usd_estimated": estimated,
        "cost_usd_actual": actual,
        "metadata": {"ticket_key": ticket_key},
        "workspace": "s1-launcher",
        "task_type": "sprint-impl",
        "observed_at": observed_at,
    }


def test_row_from_mapping_prefers_actual_and_metadata_ticket() -> None:
    row = row_from_mapping(_row("OP-100", call_id="c1", estimated=0.20, actual=0.33))

    assert row.ticket_key == "OP-100"
    assert row.estimated_usd == pytest.approx(0.20)
    assert row.actual_usd == pytest.approx(0.33)
    assert row.spend_usd == pytest.approx(0.33)


def test_row_from_mapping_falls_back_to_estimate_and_workspace() -> None:
    row = row_from_mapping({
        "call_id": "c2",
        "model": "claude-haiku-4-5-20251001",
        "cost_usd_estimated": 0.07,
        "workspace": "s1-launcher",
        "observed_at": "2026-05-10T00:00:00Z",
    })

    assert row.ticket_key == "s1-launcher"
    assert row.spend_usd == pytest.approx(0.07)


def test_24h_fixture_report_shape_verified(tmp_path) -> None:
    as_of = parse_as_of("2026-05-11T01:00:00+00:00")
    fresh_at = (as_of - timedelta(hours=2)).isoformat()
    old_at = (as_of - timedelta(hours=30)).isoformat()
    rows = [
        _row("OP-101", call_id="c1", estimated=0.10, actual=0.12, observed_at=fresh_at),
        _row("OP-101", call_id="c2", estimated=0.20, actual=0.25, observed_at=fresh_at),
        _row(
            "OP-102",
            call_id="c3",
            model="claude-opus-4-7",
            estimated=0.70,
            actual=None,
            observed_at=fresh_at,
        ),
        _row(
            "OP-103",
            call_id="old",
            model="claude-opus-4-7",
            estimated=9.99,
            actual=9.99,
            observed_at=old_at,
        ),
    ]
    fixture = tmp_path / "cost.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = build_report(
        load_fixture(fixture, since=as_of - timedelta(hours=24)),
        as_of=as_of,
    )

    assert report == {
        "generated_at": "2026-05-11T01:00:00+00:00",
        "window_hours": 24,
        "row_count": 3,
        "ticket_count": 2,
        "total_sdk_spend_usd": 1.07,
        "estimated_usd": 1.0,
        "actual_usd": 0.37,
        "actual_row_count": 2,
        "per_ticket_avg_usd": 0.535,
        "top_expensive_tickets": [
            {"ticket_key": "OP-102", "spend_usd": 0.7},
            {"ticket_key": "OP-101", "spend_usd": 0.37},
        ],
        "cost_by_model": {
            "claude-opus-4-7": 0.7,
            "claude-sonnet-4-6": 0.37,
        },
    }


def test_render_slack_text_contains_summary_sections() -> None:
    report = build_report([
        row_from_mapping(_row("OP-101", call_id="c1", estimated=0.10, actual=0.12)),
    ], as_of=datetime(2026, 5, 11, 1, tzinfo=timezone.utc))

    text = render_slack_text(report)

    assert "Daily SDK cost report" in text
    assert "Total spend: $0.1200" in text
    assert "Top tickets: OP-101=$0.1200" in text
    assert "By model: claude-sonnet-4-6=$0.1200" in text
