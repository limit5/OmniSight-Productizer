"""OP-2544 incident write-rate canary tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from scripts import incident_write_canary as canary


@pytest.fixture()
def engine(tmp_path: Path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'canary.db'}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE runner_incidents (
                incident_id TEXT PRIMARY KEY,
                ticket_key TEXT NOT NULL,
                failure_class TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                raw_traceback TEXT NOT NULL DEFAULT '',
                runner_class TEXT NOT NULL DEFAULT 'unknown',
                mutex_label TEXT,
                area TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE runner_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_class TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                ticket_key TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                tier TEXT NOT NULL,
                area TEXT NOT NULL,
                ts TEXT NOT NULL
            )
            """
        )
    return engine


def _insert_incident(engine, incident_id: str, created_at: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO runner_incidents (
                    incident_id, ticket_key, failure_class, created_at
                ) VALUES (:incident_id, 'OP-2537', 'TEST_FAILURE', :created_at)
                """
            ),
            {"incident_id": incident_id, "created_at": created_at},
        )


def _insert_metric(engine, ts: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO runner_metrics (
                    agent_class, instance_id, ticket_key, ticket_type, tier, area, ts
                ) VALUES (
                    'subscription-codex', 'default', 'OP-1', 'Task', 'M', 'backend', :ts
                )
                """
            ),
            {"ts": ts},
        )


class FakeEscalator:
    def __init__(self) -> None:
        self.calls: list[canary.CanaryCounts] = []

    def escalate(self, counts: canary.CanaryCounts) -> str:
        self.calls.append(counts)
        return "OP-9999"


def test_counts_live_v1_and_uuid_non_64_hex_ids(engine) -> None:
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    _insert_metric(engine, now - timedelta(days=1))
    _insert_incident(engine, "live-v1-" + "a" * 64, now - timedelta(days=1))
    _insert_incident(engine, "f" * 32, now - timedelta(days=1))
    _insert_incident(engine, "a" * 64, now - timedelta(days=1))
    _insert_incident(engine, "live-v1-" + "b" * 64, now - timedelta(days=9))

    counts = canary.collect_counts(engine, now=now)

    assert counts.incident_write_count == 2
    assert counts.fleet_activity_count == 1
    assert counts.should_escalate is False


def test_escalates_once_when_fleet_active_but_no_live_incidents(engine, capsys) -> None:
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    escalator = FakeEscalator()
    _insert_metric(engine, now - timedelta(hours=2))
    _insert_incident(engine, "a" * 64, now - timedelta(hours=1))

    counts = canary.run_canary(engine=engine, escalator=escalator, now=now)

    assert counts.should_escalate is True
    assert len(escalator.calls) == 1
    out = capsys.readouterr().out
    assert "incident_write_count=0" in out
    assert "fleet_activity_count=1" in out
    assert "escalated key=OP-9999" in out


def test_dry_run_prints_counts_without_escalating(engine, capsys) -> None:
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    escalator = FakeEscalator()
    _insert_metric(engine, now - timedelta(hours=2))

    counts = canary.run_canary(
        engine=engine,
        escalator=escalator,
        now=now,
        dry_run=True,
    )

    assert counts.should_escalate is True
    assert escalator.calls == []
    out = capsys.readouterr().out
    assert "should_escalate=true" in out
    assert "dry_run=true action=would_escalate" in out


def test_jira_dispatch_escalator_reuses_open_ticket(monkeypatch) -> None:
    client = SimpleNamespace(
        agent_class="subscription-codex",
        base_url="https://jira.example/rest/api/3",
        project_key="OP",
        auth_header="Basic token",
        bot_account_id="bot",
        bot_email="bot@example.com",
    )
    counts = canary.CanaryCounts(
        incident_write_count=0,
        fleet_activity_count=3,
        window_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 7, tzinfo=timezone.utc),
    )
    comments: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        canary,
        "jira_dispatch",
        SimpleNamespace(
            make_client=lambda agent_class: client,
            add_comment=lambda client, key, text, idem_key=None: comments.append(
                (key, text, idem_key)
            ),
        ),
    )
    monkeypatch.setattr(
        canary,
        "_jira_request",
        lambda client, method, path, body=None: {"issues": [{"key": "OP-4242"}]},
    )

    key = canary.JiraDispatchEscalator(agent_class="subscription-codex").escalate(counts)

    assert key == "OP-4242"
    assert comments == [
        (
            "OP-4242",
            canary.render_escalation_comment(counts),
            "incident-write-canary-2026-07-07",
        )
    ]


def test_jira_dispatch_escalator_creates_ticket_when_none_open(monkeypatch) -> None:
    client = SimpleNamespace(
        agent_class="subscription-codex",
        base_url="https://jira.example/rest/api/3",
        project_key="OP",
        auth_header="Basic token",
        bot_account_id="bot",
        bot_email="bot@example.com",
    )
    counts = canary.CanaryCounts(
        incident_write_count=0,
        fleet_activity_count=3,
        window_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 7, tzinfo=timezone.utc),
    )
    requests: list[tuple[str, str, dict | None]] = []

    monkeypatch.setattr(
        canary,
        "jira_dispatch",
        SimpleNamespace(
            make_client=lambda agent_class: client,
            add_comment=lambda *args, **kwargs: pytest.fail(
                "new ticket should not need add_comment"
            ),
        ),
    )

    def fake_request(client, method, path, body=None):
        requests.append((method, path, body))
        if path == "/search/jql":
            return {"issues": []}
        if path == "/issue":
            return {"key": "OP-5252"}
        raise AssertionError(path)

    monkeypatch.setattr(canary, "_jira_request", fake_request)

    key = canary.JiraDispatchEscalator(agent_class="subscription-codex").escalate(counts)

    assert key == "OP-5252"
    assert requests[0][1] == "/search/jql"
    assert requests[1][1] == "/issue"
    assert requests[1][2]["fields"]["labels"] == [
        canary.CANARY_LABEL,
        "component:devops",
        "OP-2544",
    ]
