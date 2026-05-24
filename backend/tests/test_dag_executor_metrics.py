"""OP-1665 — DAG executor heartbeat / Prometheus metric surface.

Covers the acceptance criteria for the executor heartbeat metric surface:

  * Code — the heartbeat gauge + counter + last-beat timestamp register in
    the Prometheus registry and accept the ``instance_id`` label;
  * Integration — the metric is scrapeable: it shows up in
    ``metrics.render_exposition()``'s exposition body;
  * Exercised — driving the executor's heartbeat sets the liveness gauge to
    1, bumps the heartbeat counter, and stamps the last-heartbeat timestamp;
    a graceful stop flips the liveness gauge back to 0.

Local harness only — uses a recording heartbeat double, so no Redis / host
state is touched (the metric surface is independent of the store write).
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import dag_executor as dx
from backend import metrics as m


prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test double: a no-op heartbeat so the metric path is exercised without a
#  store (the metric surface is deliberately independent of the store write).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _RecordingHeartbeat:
    def __init__(self) -> None:
        self.beats: list[tuple[str, dict[str, Any], int]] = []
        self.cleared: list[str] = []

    def beat(self, instance_id: str, info: dict[str, Any], ttl_s: int) -> None:
        self.beats.append((instance_id, dict(info), ttl_s))

    def clear(self, instance_id: str) -> None:
        self.cleared.append(instance_id)


def _gauge_value(metric, **labels):
    for s in metric.collect()[0].samples:
        if all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def _counter_value(metric, **labels):
    for s in metric.collect()[0].samples:
        if s.name.endswith("_total") and all(
            s.labels.get(k) == v for k, v in labels.items()
        ):
            return s.value
    return None


# ─── Code AC: the heartbeat metrics register + accept the label ──────


@prom_only
def test_heartbeat_metrics_registered_and_accept_instance_label():
    m.reset_for_tests()
    for name in (
        "dag_executor_up",
        "dag_executor_heartbeat_total",
        "dag_executor_last_heartbeat_timestamp_seconds",
    ):
        metric = getattr(m, name)
        assert metric is not None, f"{name} unbound after reset"
        child = metric.labels(instance_id="dag-exec-x")
        # the declared type must accept its op without raising
        (child.inc if name.endswith("_total") else child.set)(1)

    collected = {mf.name for mf in m.REGISTRY.collect()}
    for prefix in (
        "omnisight_dag_executor_up",
        "omnisight_dag_executor_heartbeat",  # counter strips _total
        "omnisight_dag_executor_last_heartbeat_timestamp_seconds",
    ):
        assert any(n.startswith(prefix) for n in collected), f"{prefix} missing"


@prom_only
def test_heartbeat_metric_rejects_unknown_label():
    m.reset_for_tests()
    with pytest.raises(Exception):  # ValueError from prometheus_client
        m.dag_executor_up.labels(not_a_real_label="x")


# ─── Exercised AC: a beat sets the gauge / counter; clear resets up ──


@prom_only
def test_beat_sets_liveness_and_counter_then_clear_resets():
    m.reset_for_tests()
    iid = "dag-exec-metrics-direct"
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(instance_id=iid),
        heartbeat=_RecordingHeartbeat(), enabled=True,
    )

    ex._beat("alive")
    assert _gauge_value(m.dag_executor_up, instance_id=iid) == 1
    assert _counter_value(m.dag_executor_heartbeat_total, instance_id=iid) == 1
    assert _gauge_value(
        m.dag_executor_last_heartbeat_timestamp_seconds, instance_id=iid,
    ) > 0

    ex._beat("alive")
    assert _counter_value(m.dag_executor_heartbeat_total, instance_id=iid) == 2

    ex._clear()
    assert _gauge_value(m.dag_executor_up, instance_id=iid) == 0


# ─── Exercised AC (full loop): run() drives heartbeats then stops ────


@prom_only
async def test_run_exercises_heartbeat_metrics():
    m.reset_for_tests()
    iid = "dag-exec-metrics-run"
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(
            instance_id=iid, poll_interval_s=0.0,
            heartbeat_interval_s=0.0, max_ticks=3,
        ),
        heartbeat=_RecordingHeartbeat(), enabled=True,
    )
    result = await ex.run()

    assert result.heartbeats >= 1
    # the loop emitted at least one heartbeat tick to the counter
    assert _counter_value(
        m.dag_executor_heartbeat_total, instance_id=iid,
    ) >= 1
    # graceful stop flipped the liveness gauge back to 0
    assert _gauge_value(m.dag_executor_up, instance_id=iid) == 0


# ─── Integration AC: the gauge is scrapeable (local scrape) ──────────


@prom_only
def test_heartbeat_metrics_are_scrapeable():
    m.reset_for_tests()
    iid = "dag-exec-metrics-scrape"
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(instance_id=iid),
        heartbeat=_RecordingHeartbeat(), enabled=True,
    )
    ex._beat("alive")

    body, ctype = m.render_exposition()
    assert b"omnisight_dag_executor_up" in body
    assert b"omnisight_dag_executor_heartbeat_total" in body
    assert b"omnisight_dag_executor_last_heartbeat_timestamp_seconds" in body
    # the scraped gauge carries this instance's label + value 1.0
    assert iid.encode() in body
    assert "text/plain" in ctype or "openmetrics" in ctype


# ─── best-effort contract: metric faults never break the heartbeat ───


def test_beat_survives_metric_surface_error(monkeypatch):
    """A raising metric surface must not fault the heartbeat loop."""
    iid = "dag-exec-metrics-resilient"
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(instance_id=iid),
        heartbeat=_RecordingHeartbeat(), enabled=True,
    )

    class _Boom:
        def labels(self, *_a, **_kw):
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(m, "dag_executor_up", _Boom())
    # must not raise — heartbeat still recorded on the store double
    ex._beat("alive")
    ex._clear()
    assert ex._heartbeats == 1
