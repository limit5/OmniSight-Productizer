"""Sora 統帥交付戰功 endpoint — GET /agents/orchestrator/command-stats.

Covers: route registration, real-delivery aggregation (grouped by brain +
weighted avg + latest), and the fail-open contract (missing runner telemetry
tables → zeros, never a 500). See backend/routers/agents.py.
"""

import asyncio
from datetime import datetime, timezone

import asyncpg
import pytest

from backend.routers.agents import get_orchestrator_command_stats, router


def test_route_registered():
    paths = {r.path for r in router.routes}
    assert "/agents/orchestrator/command-stats" in paths


class _FailConn:
    async def fetch(self, *a, **k):
        raise asyncpg.PostgresError("no such table")

    async def fetchrow(self, *a, **k):
        raise asyncpg.PostgresError("no such table")

    async def fetchval(self, *a, **k):
        raise asyncpg.PostgresError("no such table")


def test_fail_open_when_tables_absent():
    res = asyncio.run(get_orchestrator_command_stats(conn=_FailConn()))
    assert res == {
        "delivered_total": 0,
        "by_brain": [],
        "avg_seconds": None,
        "latest": None,
        "incidents_30d": 0,
        "incidents_total": 0,
        "top_incident_classes": [],
    }


class _RealConn:
    async def fetch(self, q, *a):
        if "GROUP BY agent_class" in q:
            return [
                {"agent_class": "subscription-claude", "n": 28, "avg_s": 532.6},
                {"agent_class": "subscription-codex", "n": 16, "avg_s": 383.9},
                {"agent_class": "subscription-grok", "n": 1, "avg_s": 125.4},
                {"agent_class": "subscription-gemini", "n": 1, "avg_s": 63.8},
            ]
        if "failure_class" in q:
            return [{"failure_class": "OTHER", "n": 1415}]
        return []

    async def fetchrow(self, q, *a):
        return {
            "ticket_key": "OP-2530",
            "agent_class": "subscription-claude",
            "completed_at": datetime(2026, 7, 4, tzinfo=timezone.utc),
        }

    async def fetchval(self, q, *a):
        return 142 if "30 days" in q else 1935


def test_aggregates_real_delivery_counts():
    res = asyncio.run(get_orchestrator_command_stats(conn=_RealConn()))
    assert res["delivered_total"] == 46
    # brains sorted by count desc, agent_class mapped to brain
    assert res["by_brain"][0] == {"brain": "claude", "count": 28}
    assert {b["brain"] for b in res["by_brain"]} == {"claude", "codex", "grok", "gemini"}
    # weighted average, not a naive mean
    expected = round((28 * 532.6 + 16 * 383.9 + 125.4 + 63.8) / 46, 1)
    assert res["avg_seconds"] == expected
    assert res["latest"] == {
        "ticket_key": "OP-2530",
        "brain": "claude",
        "at": "2026-07-04T00:00:00+00:00",
    }
    assert res["incidents_30d"] == 142
    assert res["incidents_total"] == 1935
