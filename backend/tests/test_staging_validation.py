"""OP-768 staging smoke + metric baseline comparator contract tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.agents.conflict_observations import fetch_recent_observations_sync
from backend.staging_validation import (
    MetricSample,
    PROMQL_BASELINE,
    PROMQL_CURRENT,
    SMOKE_TRANSACTION_COUNT,
    build_smoke_suite,
    collect_metric_samples,
    compare_metrics,
    emit_staging_regression_event,
    run_smoke_suite,
)


@pytest.fixture()
def conflict_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "conflict.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE conflict_observations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ps_change_id            TEXT,
            ps_change_number        INTEGER,
            ticket                  TEXT,
            files_in_conflict       TEXT NOT NULL DEFAULT '[]',
            cause_category          TEXT NOT NULL,
            pre_existing_open_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OMNI_TEST_PG_URL", raising=False)
    return db_path


def test_ac1_smoke_suite_covers_60_transactions_under_5_min() -> None:
    suite = build_smoke_suite()
    assert len(suite) == SMOKE_TRANSACTION_COUNT
    assert {tx.flow for tx in suite} == {
        "auth",
        "dashboard",
        "agent_invoke",
        "jira_pickup",
        "gerrit_push",
    }

    ticks = iter([0.0] + [idx * 0.01 for idx in range(1, 62)])
    ok, failures = run_smoke_suite(
        "http://staging",
        opener=lambda _tx: True,
        monotonic=lambda: next(ticks),
    )
    assert ok
    assert failures == ()


def test_ac2_metric_comparator_pulls_baseline_and_current() -> None:
    class FakeProm:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, promql: str) -> float:
            self.queries.append(promql)
            values = {
                PROMQL_CURRENT["error_rate"]: 0.01,
                PROMQL_BASELINE["error_rate"]: 0.01,
                PROMQL_CURRENT["p95_latency"]: 0.20,
                PROMQL_BASELINE["p95_latency"]: 0.15,
                PROMQL_CURRENT["request_rate"]: 100.0,
                PROMQL_BASELINE["request_rate"]: 95.0,
            }
            return values[promql]

    prom = FakeProm()
    samples = collect_metric_samples(prom)
    assert [sample.name for sample in samples] == [
        "error_rate",
        "p95_latency",
        "request_rate",
    ]
    assert len(prom.queries) == 6
    assert all("[7d:15m]" in PROMQL_BASELINE[name] for name in PROMQL_BASELINE)


def test_ac2_comparator_computes_deltas_and_passes_within_thresholds() -> None:
    decision = compare_metrics((
        MetricSample("error_rate", current=0.014, baseline=0.010),
        MetricSample("p95_latency", current=0.249, baseline=0.150),
        MetricSample("request_rate", current=81.0, baseline=100.0),
    ))
    assert decision.passed
    assert decision.regressions == ()
    assert decision.samples[0].delta == pytest.approx(0.004)


def test_ac3_regression_auto_aborts_and_emits_event(conflict_db: Path) -> None:
    decision = compare_metrics((
        MetricSample("error_rate", current=0.015, baseline=0.010),
        MetricSample("p95_latency", current=0.251, baseline=0.150),
        MetricSample("request_rate", current=125.0, baseline=100.0),
    ))
    assert not decision.passed
    assert len(decision.regressions) == 3

    assert emit_staging_regression_event(decision)
    rows = fetch_recent_observations_sync(
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].cause_category == "staging_regression"
    assert rows[0].ticket == "OP-768"
    assert rows[0].pre_existing_open_count == 3
    assert rows[0].files_in_conflict[0].startswith("error_rate:")


def test_ac4_synthetic_50_percent_error_injection_is_caught() -> None:
    decision = compare_metrics((
        MetricSample("error_rate", current=0.015, baseline=0.010),
        MetricSample("p95_latency", current=0.100, baseline=0.100),
        MetricSample("request_rate", current=100.0, baseline=100.0),
    ))
    assert not decision.passed
    assert decision.regressions == (
        "error_rate current 0.015 >= baseline+50% 0.015",
    )
