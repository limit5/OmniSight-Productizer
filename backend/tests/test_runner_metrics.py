"""OP-909 runner_metrics telemetry and agent drift report tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from backend.agents import runner_metrics_recorder as rec


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0231 = REPO_ROOT / "backend" / "alembic" / "versions" / "0231_runner_metrics.py"
REPORT_SCRIPT = REPO_ROOT / "scripts" / "agent_drift_report.py"
RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"


def _load_module(path: Path, name: str) -> Any:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        if self.fail:
            raise RuntimeError("db down")
        self.fetchval_calls.append((sql, args))
        return 42

    async def execute(self, sql: str, *args: Any) -> str:
        if self.fail:
            raise RuntimeError("db down")
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


@asynccontextmanager
async def fake_factory(conn: FakeConn):
    yield conn


@pytest.fixture(scope="module")
def m0231():
    return _load_module(MIGRATION_0231, "_alembic_test_0231")


@pytest.fixture()
def sqlite_conn(m0231):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0231.upgrade()
        yield conn


def test_alembic_0231_creates_runner_metrics_contract(sqlite_conn) -> None:
    columns = {
        row[1]: row
        for row in sqlite_conn.exec_driver_sql("PRAGMA table_info(runner_metrics)")
    }
    expected = {
        "agent_class",
        "instance_id",
        "ticket_key",
        "ticket_type",
        "tier",
        "area",
        "time_to_complete_seconds",
        "outcome",
        "lessons_used_count",
        "mcp_calls_count",
        "claude_model_used",
        "ts",
    }
    assert expected.issubset(columns)
    indexes = {
        row[1]
        for row in sqlite_conn.exec_driver_sql("PRAGMA index_list(runner_metrics)")
    }
    assert "idx_runner_metrics_task_window" in indexes
    assert "idx_runner_metrics_ticket_open" in indexes


def test_alembic_0231_outcome_check_rejects_unknown(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO runner_metrics (
                agent_class, instance_id, ticket_key, ticket_type, tier, area, outcome
            ) VALUES ('subscription-codex', 'default', 'OP-909', 'Task', 'M', 'backend', 'mystery')
            """
        )


@pytest.mark.asyncio
async def test_pickup_logs_runner_metrics_row() -> None:
    conn = FakeConn()
    metric = rec.RunnerMetricStart(
        agent_class="subscription-codex",
        instance_id="default",
        ticket_key="OP-909",
        ticket_type="Task",
        tier="M",
        area="backend,db",
        lessons_used_count=2,
        mcp_calls_count=3,
        claude_model_used="claude-sonnet",
    )
    row_id = await rec.record_pickup(metric, conn_factory=lambda: fake_factory(conn))

    assert row_id == 42
    sql, args = conn.fetchval_calls[0]
    assert "INSERT INTO runner_metrics" in sql
    assert args[:9] == (
        "subscription-codex",
        "default",
        "OP-909",
        "Task",
        "M",
        "backend,db",
        2,
        3,
        "claude-sonnet",
    )


@pytest.mark.asyncio
async def test_completion_updates_runner_metrics_row() -> None:
    conn = FakeConn()
    started = datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=90)

    await rec.record_completion(
        metric_id=42,
        ticket_key="OP-909",
        agent_class="subscription-codex",
        instance_id="default",
        outcome="success",
        started_at=started,
        now=completed,
        conn_factory=lambda: fake_factory(conn),
    )

    sql, args = conn.execute_calls[0]
    assert "UPDATE runner_metrics" in sql
    assert args[:4] == ("success", 90.0, completed, 42)


@pytest.mark.asyncio
async def test_metrics_fail_open_on_db_error(caplog: pytest.LogCaptureFixture) -> None:
    conn = FakeConn(fail=True)
    metric = rec.RunnerMetricStart(
        agent_class="subscription-codex",
        instance_id="default",
        ticket_key="OP-909",
        ticket_type="Task",
        tier="M",
        area="backend",
    )

    row_id = await rec.record_pickup(metric, conn_factory=lambda: fake_factory(conn))

    assert row_id is None
    assert "MetricsInsertFailed" in caplog.text


def test_monthly_report_generates_valid_markdown(tmp_path: Path) -> None:
    report = _load_module(REPORT_SCRIPT, "_agent_drift_report_test")
    as_of = datetime(2026, 5, 31, tzinfo=timezone.utc)
    rows = [
        report.MetricRow("subscription-codex", "Task", 100.0, "success", 3, as_of - timedelta(days=5)),
        report.MetricRow("subscription-codex", "Task", 80.0, "failure", 2, as_of - timedelta(days=6)),
        report.MetricRow("subscription-codex", "Task", 70.0, "success", 4, as_of - timedelta(days=35)),
        report.MetricRow("subscription-codex", "Task", 60.0, "success", 4, as_of - timedelta(days=36)),
    ]

    trends = report.build_trends(rows, as_of=as_of)
    rendered = report.render_report(trends, as_of=as_of)
    path = report.write_report(rendered, output_dir=tmp_path, as_of=as_of)

    assert path.name == "agent-drift-2026-05.md"
    assert "# Agent Drift Report - 2026-05" in path.read_text()
    assert "subscription-codex | Task" in path.read_text()


def test_alert_threshold_fires_and_baseline_suppresses_page() -> None:
    report = _load_module(REPORT_SCRIPT, "_agent_drift_report_threshold_test")
    as_of = datetime(2026, 5, 31, tzinfo=timezone.utc)
    rows = [
        report.MetricRow("subscription-codex", "Task", 100.0, "failure", 1, as_of - timedelta(days=5)),
        report.MetricRow("subscription-codex", "Task", 100.0, "failure", 1, as_of - timedelta(days=6)),
        report.MetricRow("subscription-codex", "Task", 100.0, "success", 3, as_of - timedelta(days=35)),
        report.MetricRow("subscription-codex", "Task", 100.0, "success", 3, as_of - timedelta(days=36)),
    ]

    trend = report.build_trends(rows, as_of=as_of)[0]

    assert "warn: success_rate_drop=-100.0%" in trend.alerts
    assert "warn: lessons_used_drop=-66.7%" in trend.alerts


def test_monthly_report_refuses_when_under_30_days() -> None:
    report = _load_module(REPORT_SCRIPT, "_agent_drift_report_insufficient_test")
    as_of = datetime(2026, 5, 31, tzinfo=timezone.utc)
    rows = [
        report.MetricRow("subscription-codex", "Task", 100.0, "success", 1, as_of - timedelta(days=5)),
    ]

    with pytest.raises(report.MonthlyReportInsufficientData):
        report.build_trends(rows, as_of=as_of)


def test_auto_runner_wires_metrics_hooks() -> None:
    source = RUNNER_PATH.read_text()
    assert "runner_metrics_recorder.record_pickup_sync" in source
    assert "runner_metrics_recorder.record_completion_sync" in source
    assert "_LAST_TICKET_METADATA" in source
