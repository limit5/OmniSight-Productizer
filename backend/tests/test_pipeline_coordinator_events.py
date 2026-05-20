"""Event-source tests for the pipeline-coordinator daemon (OP-1550 / 29f-coord).

AC mapping:

  Code AC → test_event_deduper_coalesces_by_window_and_key,
            test_bridge_event_tailer_reads_from_offset_and_skips_bad_lines,
            test_bridge_event_tailer_resets_offset_after_truncation,
            test_jira_event_poller_due_interval_and_issue_mapping,
            test_jira_event_poller_failure_is_non_fatal,
            test_coordinator_jql_contains_required_event_clauses,
            test_refresh_work_graph_stages_events_and_provider_graph,
            test_hourly_sweep_event_fires_at_most_once_per_interval
  Deploy AC → standard pytest module under backend/tests, no infra needed.
  Integration AC → exercised with the same coordinator suite files.
  Exercised AC → direct pytest of this file exits 0.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents.pipeline_coordinator import (
    BridgeEventTailer,
    CoordinatorConfig,
    EventDeduper,
    JiraEventPoller,
    PipelineCoordinator,
    coordinator_jql,
)
from backend.agents.pipeline_coordinator_rules import Ticket, WorkGraph


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeBridgeTailer:
    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)

    def poll(self) -> list[dict[str, Any]]:
        if not self.batches:
            return []
        return self.batches.pop(0)


class FakeJiraPoller:
    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)

    def poll(self) -> list[dict[str, Any]]:
        if not self.batches:
            return []
        return self.batches.pop(0)


class FakeJiraClient:
    project_key = "OP"


def _config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        heartbeat_interval_seconds=60.0,
        tick_interval_seconds=60.0,
        sweep_interval_seconds=3600.0,
    )


def _event(key: str = "OP-1550") -> dict[str, Any]:
    return {
        "source": "jira",
        "trigger": "jira-poll",
        "ticket_key": key,
        "payload": {"type": "issue", "id": key, "key": key},
    }


def test_event_deduper_coalesces_by_window_and_key() -> None:
    """Code AC: dedupe window, distinct keys, and JSON fallback are covered."""
    clock = FakeClock()
    deduper = EventDeduper(window_seconds=300.0, clock=clock)

    assert deduper.fresh(_event("OP-1")) is True
    assert deduper.fresh(_event("OP-1")) is False
    assert deduper.fresh(_event("OP-2")) is True

    clock.advance(301)
    assert deduper.fresh(_event("OP-1")) is True

    fallback = {"source": "bridge", "payload": ["not", "a", "dict"]}
    assert deduper.fresh(fallback) is True
    assert deduper.fresh({"source": "bridge", "payload": ["not", "a", "dict"]}) is False
    assert deduper.fresh({"source": "jira", "payload": ["not", "a", "dict"]}) is True


def test_bridge_event_tailer_reads_from_offset_and_skips_bad_lines(tmp_path: Path) -> None:
    """Code AC: JSONL tail reads only new records; malformed lines are skipped."""
    path = tmp_path / "bridge-events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "t1", "event": {"type": "claim", "key": "OP-1"}}),
                "{malformed",
                json.dumps({"event": {"type": "done", "key": "OP-2"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tailer = BridgeEventTailer(path)

    first = tailer.poll()

    assert [e["payload"]["key"] for e in first] == ["OP-1", "OP-2"]
    assert first[0]["source"] == "bridge"
    assert first[0]["trigger"] == "bridge-event:claim"
    assert first[0]["bridge_record_ts"] == "t1"
    assert tailer.poll() == []

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": {"type": "updated", "key": "OP-3"}}) + "\n")

    assert [e["payload"]["key"] for e in tailer.poll()] == ["OP-3"]


def test_bridge_event_tailer_resets_offset_after_truncation(tmp_path: Path) -> None:
    """Code AC: truncation resets the tail offset; missing path returns empty."""
    path = tmp_path / "bridge-events.jsonl"
    tailer = BridgeEventTailer(path)

    assert tailer.poll() == []

    first_record = json.dumps(
        {"event": {"type": "first", "key": "OP-1", "padding": "x" * 80}}
    )
    path.write_text(first_record + "\n", encoding="utf-8")
    assert [e["payload"]["key"] for e in tailer.poll()] == ["OP-1"]

    path.write_text(json.dumps({"event": {"type": "r", "key": "OP-2"}}) + "\n",
                    encoding="utf-8")

    assert [e["payload"]["key"] for e in tailer.poll()] == ["OP-2"]


def test_jira_event_poller_due_interval_and_issue_mapping() -> None:
    """Code AC: due interval, issue mapping, and factory/search injection."""
    clock = FakeClock()
    factory_calls: list[str] = []
    search_calls: list[str] = []

    def _factory(agent_class: str) -> FakeJiraClient:
        factory_calls.append(agent_class)
        return FakeJiraClient()

    def _search(client: FakeJiraClient, jql: str) -> list[dict[str, Any]]:
        search_calls.append(jql)
        assert client.project_key == "OP"
        return [{"key": "OP-1", "fields": {"summary": "one"}}, {"fields": {}}]

    poller = JiraEventPoller(
        agent_class="subscription-codex",
        clock=clock,
        interval_seconds=60.0,
        client_factory=_factory,
        search=_search,
    )

    assert poller.due() is True
    events = poller.poll()
    assert poller.due() is False
    assert factory_calls == ["subscription-codex"]
    assert len(search_calls) == 1
    assert 'project = "OP"' in search_calls[0]
    assert events == [
        {
            "source": "jira",
            "trigger": "jira-poll",
            "ticket_key": "OP-1",
            "payload": {"key": "OP-1", "fields": {"summary": "one"}},
        }
    ]
    assert poller.poll() == []

    clock.advance(60)
    assert poller.due() is True
    assert [e["ticket_key"] for e in poller.poll()] == ["OP-1"]
    assert factory_calls == ["subscription-codex"]


def test_jira_event_poller_failure_is_non_fatal() -> None:
    """Code AC: poll failure returns [] and still obeys the interval gate."""
    clock = FakeClock()

    def _search(_client: FakeJiraClient, _jql: str) -> list[dict[str, Any]]:
        raise RuntimeError("jira unavailable")

    poller = JiraEventPoller(
        agent_class="subscription-codex",
        clock=clock,
        interval_seconds=60.0,
        client_factory=lambda _agent_class: FakeJiraClient(),
        search=_search,
    )

    assert poller.poll() == []
    assert poller.due() is False
    clock.advance(60)
    assert poller.due() is True


def test_coordinator_jql_contains_required_event_clauses() -> None:
    """Code AC: JQL includes all coordinator-triggering clauses."""
    jql = coordinator_jql("OP")

    assert 'project = "OP"' in jql
    assert 'labels = "needs-coordinator"' in jql
    assert 'status in ("Done", "Closed", "Won\'t Do", "却下")' in jql
    assert "statusCategoryChangedDate >= -1d" in jql
    assert 'status = "進行中" AND assignee is EMPTY' in jql


def test_refresh_work_graph_stages_events_and_provider_graph(tmp_path: Path) -> None:
    """Code AC: refresh stages source events, dedupes, and returns provider graph."""
    clock = FakeClock()
    duplicate = _event("OP-1550")
    ticket = Ticket(key="OP-1550", labels=("needs-coordinator",))
    graph = WorkGraph(focal=ticket, tickets={"OP-1550": ticket})
    provider_calls = 0

    def _provider() -> WorkGraph:
        nonlocal provider_calls
        provider_calls += 1
        return graph

    coord = PipelineCoordinator(
        _config(tmp_path),
        clock=clock,
        bridge_tailer=FakeBridgeTailer([duplicate, dict(duplicate)]),
        jira_poller=FakeJiraPoller([]),
        deduper=EventDeduper(window_seconds=300.0, clock=clock),
        work_graph_provider=_provider,
    )

    assert coord._refresh_work_graph() is graph
    assert coord._pending_events == [duplicate]
    assert coord._work_graph is graph
    assert provider_calls == 1


def test_hourly_sweep_event_fires_at_most_once_per_interval(tmp_path: Path) -> None:
    """Code AC: hourly sweep event fires once per configured interval."""
    clock = FakeClock()
    coord = PipelineCoordinator(
        _config(tmp_path),
        clock=clock,
        bridge_tailer=FakeBridgeTailer([], [], []),
        jira_poller=FakeJiraPoller([], [], []),
        deduper=EventDeduper(window_seconds=300.0, clock=clock),
    )

    assert coord._collect_source_events() == []

    clock.advance(3599)
    assert coord._collect_source_events() == []

    clock.advance(1)
    events = coord._collect_source_events()
    assert len(events) == 1
    assert events[0]["source"] == "timer"
    assert events[0]["trigger"] == "hourly-sweep"
    assert events[0]["payload"]["elapsed_seconds"] == 3600.0

    assert coord._collect_source_events() == []
    clock.advance(3600)
    assert [e["trigger"] for e in coord._collect_source_events()] == ["hourly-sweep"]
