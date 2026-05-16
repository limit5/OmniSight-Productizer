"""OP-883 D11 -- continuous SLO monitor + auto-rollback contract.

Six DoD cases mapped to the test plan:

* **T1** ``test_breach_triggers_rollback`` -- error-rate breaches for
  >2min and the monitor fires SSE + the canary rollback trigger.
* **T2** ``test_recovery_within_cooldown`` -- after a rollback fires
  the metric stream recovers; ticks during the AC #4 cooldown skip,
  and once cooldown elapses the monitor returns to ``ok``.
* **T3** ``test_metric_source_unavailable_fail_open`` -- source raises
  :class:`MetricSourceUnavailable`; the monitor returns
  ``metric_unavailable`` and **does not** invoke the rollback trigger.
* **T4** ``test_override_suppress_blocks_breach`` -- operator flag
  ``slo:monitor:suppress`` is set; even sustained breach samples
  return ``suppressed`` with the rollback trigger never called.
* **T5** ``test_p95_and_error_rate_breaches_are_independent`` -- a
  p95-only breach with healthy error rate still triggers rollback.
* **T6** ``test_cooldown_prevents_flap`` -- after the first rollback,
  a second sustained-breach sample inside the cooldown window does
  NOT trigger a second rollback (the AC #4 flap guard).
* **OP-912** cross-task awareness latency SLO cases -- each memory
  query axis breaches independently and uses the same rollback chain.

The tests do not touch Prometheus, the file system (beyond a temp
flag file in the override-source test), or Docker. Sources are stubbed:
``_FakeMetricSource`` returns scripted samples, ``_FakeOverride``
toggles via an attribute, ``_FakeRollback`` records invocations.

The file lives at ``test_slo_monitor_op883.py`` (not the bare
``test_slo_monitor.py`` mentioned in the OP-883 spec) because the
OP-772 deploy-window worker already owns ``test_slo_monitor.py``; the
``_<ticket>`` suffix matches the existing ``test_deploy_audit_op779``
/ ``test_canary_rollout_op771`` convention.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

import pytest

from backend import events
from backend.orchestrator import slo_monitor as sm


# ── helpers ──────────────────────────────────────────────────────────


class _Clock:
    """Manual clock so each test can drive AC #1/#3/#4 windows exactly."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _FakeMetricSource:
    """Scripted metric source. The first ``len(samples)-1`` samples are
    consumed in order; the last is repeated indefinitely so tests do
    not have to seed every tick by hand."""

    samples: list[sm.SloSample] = field(default_factory=list)
    raise_next: Exception | None = None
    fetched: int = 0

    def fetch(
        self,
        *,
        error_rate_window_seconds: int,
        p95_window_seconds: int,
    ) -> sm.SloSample:
        self.fetched += 1
        if self.raise_next is not None:
            exc = self.raise_next
            self.raise_next = None
            raise exc
        if not self.samples:
            raise AssertionError("test forgot to seed an SloSample")
        if len(self.samples) == 1:
            return self.samples[0]
        return self.samples.pop(0)


@dataclass
class _FakeOverride:
    suppressed: bool = False

    def is_suppressed(self) -> bool:
        return self.suppressed


@dataclass
class _FakeRollback:
    mode_value: str = "canary"
    invocations: list[str] = field(default_factory=list)
    outcome: sm.RollbackOutcome = field(
        default_factory=lambda: sm.RollbackOutcome(
            mode="canary", status="aborted", detail="fake",
        )
    )
    raise_on_trigger: Exception | None = None

    @property
    def mode(self) -> str:
        return self.mode_value

    def trigger(self, reason: str) -> sm.RollbackOutcome:
        self.invocations.append(reason)
        if self.raise_on_trigger is not None:
            raise self.raise_on_trigger
        return self.outcome


class _SSESpy:
    """Capture ``slo.breach`` SSE frames from the global bus."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._orig = events.bus.publish

        def _patched(event_name, data, *args, **kwargs):
            if event_name == "slo.breach":
                with self._lock:
                    self.events.append((event_name, dict(data)))
            return self._orig(event_name, data, *args, **kwargs)

        events.bus.publish = _patched  # type: ignore[assignment]

    def restore(self) -> None:
        events.bus.publish = self._orig  # type: ignore[assignment]


@pytest.fixture()
def sse_spy():
    spy = _SSESpy()
    try:
        yield spy
    finally:
        spy.restore()


class _StatusSpy:
    """Capture ``slo.status`` frames consumed by the F15 dashboard tile."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._orig = events.bus.publish

        def _patched(event_name, data, *args, **kwargs):
            if event_name == "slo.status":
                with self._lock:
                    self.events.append((event_name, dict(data)))
            return self._orig(event_name, data, *args, **kwargs)

        events.bus.publish = _patched  # type: ignore[assignment]

    def restore(self) -> None:
        events.bus.publish = self._orig  # type: ignore[assignment]


@pytest.fixture()
def status_spy():
    spy = _StatusSpy()
    try:
        yield spy
    finally:
        spy.restore()


# Smaller windows than prod so each test runs in a handful of ticks
# without breaking the AC #3/#4 ratio (sustain > sample, cooldown >
# sustain).
_TEST_THRESHOLDS = sm.SloThresholds(
    error_rate_max=0.01,
    p95_latency_ms_max=500.0,
    error_rate_window_seconds=60,
    p95_window_seconds=300,
    sample_interval_seconds=30,
    breach_sustain_seconds=60,
    cooldown_seconds=120,
)


def _make_monitor(
    *,
    source: _FakeMetricSource,
    override: _FakeOverride | None = None,
    rollback: _FakeRollback | None = None,
    clock: _Clock | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> tuple[sm.SloMonitor, _Clock, _FakeRollback, _FakeOverride]:
    clock = clock or _Clock()
    rollback = rollback or _FakeRollback()
    override = override or _FakeOverride()
    monitor = sm.SloMonitor(
        thresholds=_TEST_THRESHOLDS,
        source=source,
        override_source=override,
        rollback=rollback,
        clock=clock,
        sleeper=lambda _s: None,
        notify=notify,
    )
    return monitor, clock, rollback, override


def _healthy() -> sm.SloSample:
    return sm.SloSample(
        error_rate=0.0, p95_latency_ms=100.0, observed_at=0.0,
    )


def _err_breach() -> sm.SloSample:
    return sm.SloSample(
        error_rate=0.05, p95_latency_ms=100.0, observed_at=0.0,
    )


def _p95_breach() -> sm.SloSample:
    return sm.SloSample(
        error_rate=0.0, p95_latency_ms=900.0, observed_at=0.0,
    )


def _cross_task_breach(sample_attr: str, value_ms: float) -> sm.SloSample:
    return sm.SloSample(
        error_rate=0.0,
        p95_latency_ms=100.0,
        observed_at=0.0,
        **{sample_attr: value_ms},
    )


# ── T1: breach triggers rollback ─────────────────────────────────────


def test_breach_triggers_rollback(sse_spy):
    """AC #3 -- sustained breach >2min fires SSE + rollback trigger."""
    src = _FakeMetricSource(samples=[_err_breach()])
    monitor, clock, rb, _ = _make_monitor(source=src)

    # tick #1: breach observed at t=0, streak opens but no trigger.
    t1 = monitor.tick()
    assert t1.action == sm.TickAction.breach_pending
    assert rb.invocations == []
    assert sse_spy.events == []

    # tick #2: 60s later -- breach_sustain_seconds elapsed, trigger fires.
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    t2 = monitor.tick()
    assert t2.action == sm.TickAction.rollback_triggered
    assert t2.breached_metrics == ("error_rate",)
    assert t2.rollback is not None and t2.rollback.mode == "canary"
    # Trigger called exactly once with a reason mentioning the metric.
    assert len(rb.invocations) == 1
    assert "error_rate" in rb.invocations[0]
    # SSE: one ``slo.breach`` frame carrying the rollback mode.
    assert len(sse_spy.events) == 1
    _, payload = sse_spy.events[0]
    assert payload["rollback_mode"] == "canary"
    assert "error_rate" in payload["breached_metrics"]
    assert payload["error_rate"] == pytest.approx(0.05)


# ── T2: recovery within cooldown ─────────────────────────────────────


def test_recovery_within_cooldown(sse_spy):
    """After rollback fires, metrics recover; cooldown ticks skip
    re-evaluation, and once the AC #4 window closes the monitor
    returns to ``ok``."""
    src = _FakeMetricSource(samples=[_err_breach(), _err_breach(), _healthy()])
    monitor, clock, rb, _ = _make_monitor(source=src)

    # Reach the sustained-breach trigger.
    monitor.tick()
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    monitor.tick()
    assert len(rb.invocations) == 1
    # A tick INSIDE the cooldown window must short-circuit BEFORE
    # pulling a sample (so the recovery doesn't waste Prometheus
    # queries either).
    fetched_before = src.fetched
    inside = monitor.tick()
    assert inside.action == sm.TickAction.cooldown_skip
    assert src.fetched == fetched_before, "metric source must not be probed during cooldown"
    # Advance past the cooldown -- next tick reads the healthy sample.
    clock.advance(_TEST_THRESHOLDS.cooldown_seconds + 1)
    after = monitor.tick()
    assert after.action == sm.TickAction.ok
    # No second rollback fired during recovery.
    assert len(rb.invocations) == 1


# ── T3: metric source down -> fail-open ─────────────────────────────


def test_metric_source_unavailable_fail_open(sse_spy):
    """Error catalog: ``MetricSourceUnavailable`` => fail-open + log.
    Critically, the rollback trigger is NOT invoked when our own
    measurement plane is down (a Prometheus outage is not evidence
    of a prod incident)."""
    src = _FakeMetricSource(samples=[_healthy()])
    src.raise_next = sm.MetricSourceUnavailable("prom down")
    monitor, _clock, rb, _ = _make_monitor(source=src)

    result = monitor.tick()
    assert result.action == sm.TickAction.metric_unavailable
    assert "prom down" in result.detail
    assert rb.invocations == []
    assert sse_spy.events == []
    # And the breach streak must NOT have advanced -- a healthy tick
    # after recovery should return ``ok`` immediately.
    result2 = monitor.tick()
    assert result2.action == sm.TickAction.ok


# ── T4: override suppress ────────────────────────────────────────────


def test_override_suppress_blocks_breach(sse_spy):
    """AC #5 -- the operator flag ``slo:monitor:suppress`` keeps the
    monitor passive even on sustained breach samples."""
    src = _FakeMetricSource(samples=[_err_breach()])
    override = _FakeOverride(suppressed=True)
    monitor, clock, rb, _ = _make_monitor(source=src, override=override)

    monitor.tick()
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    second = monitor.tick()
    assert second.action == sm.TickAction.suppressed
    assert rb.invocations == []
    assert sse_spy.events == []

    # Removing the override mid-incident must NOT instantly trigger --
    # the streak was cleared while suppressed, so we restart the
    # 2-min window from scratch.
    override.suppressed = False
    clock.advance(_TEST_THRESHOLDS.sample_interval_seconds)
    third = monitor.tick()
    assert third.action == sm.TickAction.breach_pending
    assert rb.invocations == []


# ── T5: p95 vs error-rate independent breaches ──────────────────────


def test_p95_and_error_rate_breaches_are_independent(sse_spy):
    """AC #2 -- p95 latency breach with healthy error rate must still
    trigger rollback. Symmetric to the error_rate-only T1 path."""
    src = _FakeMetricSource(samples=[_p95_breach()])
    monitor, clock, rb, _ = _make_monitor(source=src)

    monitor.tick()
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    triggered = monitor.tick()
    assert triggered.action == sm.TickAction.rollback_triggered
    assert triggered.breached_metrics == ("p95_latency_ms",)
    assert len(rb.invocations) == 1
    assert "p95_latency_ms" in rb.invocations[0]
    _, payload = sse_spy.events[-1]
    assert payload["breached_metrics"] == ["p95_latency_ms"]
    assert payload["p95_latency_ms"] == pytest.approx(900.0)
    assert payload["error_rate"] == pytest.approx(0.0)


# ── OP-912: cross-task awareness latency SLOs ───────────────────────


@pytest.mark.parametrize(
    ("metric_name", "sample_attr", "threshold_attr", "value_ms"),
    [
        (
            "project_state_api_p95",
            "project_state_api_p95_ms",
            "project_state_api_p95_ms_max",
            2100.0,
        ),
        (
            "cognee_query_p95",
            "cognee_query_p95_ms",
            "cognee_query_p95_ms_max",
            900.0,
        ),
        (
            "graphiti_query_p95",
            "graphiti_query_p95_ms",
            "graphiti_query_p95_ms_max",
            700.0,
        ),
        (
            "failure_recall_p95",
            "failure_recall_p95_ms",
            "failure_recall_p95_ms_max",
            600.0,
        ),
    ],
)
def test_cross_task_awareness_breach_triggers_canary_rollback(
    sse_spy, metric_name, sample_attr, threshold_attr, value_ms,
):
    """OP-912 AC #1/#2 -- each F14 latency SLO independently rolls
    through the D11 sustained-breach path into the F13 canary trigger."""
    src = _FakeMetricSource(
        samples=[_cross_task_breach(sample_attr, value_ms)]
    )
    monitor, clock, rb, _ = _make_monitor(source=src)

    first = monitor.tick()
    assert first.action == sm.TickAction.breach_pending
    assert first.breached_metrics == (metric_name,)
    assert first.axis_status[metric_name]["ok"] is False

    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    triggered = monitor.tick()
    assert triggered.action == sm.TickAction.rollback_triggered
    assert triggered.breached_metrics == (metric_name,)
    assert triggered.rollback is not None and triggered.rollback.mode == "canary"
    assert len(rb.invocations) == 1
    assert metric_name in rb.invocations[0]

    _, payload = sse_spy.events[-1]
    assert payload["rollback_mode"] == "canary"
    assert payload["breached_metrics"] == [metric_name]
    assert payload["axes"][metric_name] == {
        "value_ms": value_ms,
        "threshold_ms": getattr(_TEST_THRESHOLDS, threshold_attr),
        "ok": False,
    }


def test_status_event_exposes_cross_task_axes_for_dashboard(status_spy):
    """OP-912 AC #4 -- evaluated ticks emit live per-axis SLO state
    without requiring the F15 frontend tile in this backend ticket."""
    src = _FakeMetricSource(samples=[_healthy()])
    monitor, _clock, _rb, _ = _make_monitor(source=src)

    result = monitor.tick()

    assert result.action == sm.TickAction.ok
    assert set(result.axis_status) == {
        "project_state_api_p95",
        "cognee_query_p95",
        "graphiti_query_p95",
        "failure_recall_p95",
    }
    assert len(status_spy.events) == 1
    _, payload = status_spy.events[0]
    assert payload["axes"] == result.axis_status
    assert payload["axes"]["project_state_api_p95"] == {
        "value_ms": 0.0,
        "threshold_ms": 2000.0,
        "ok": True,
    }


# ── T6: cooldown prevents flap ──────────────────────────────────────


def test_cooldown_prevents_flap(sse_spy):
    """AC #4 -- a second sustained-breach sample arriving inside the
    cooldown window must NOT fire a second rollback."""
    src = _FakeMetricSource(samples=[_err_breach()])
    monitor, clock, rb, _ = _make_monitor(source=src)

    # First sustained breach -> rollback.
    monitor.tick()
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    monitor.tick()
    assert len(rb.invocations) == 1
    sse_count = len(sse_spy.events)

    # Within the cooldown window, even repeated breach samples must
    # not fire a second rollback. The metric source must NOT be probed
    # either -- the cooldown gate short-circuits BEFORE the fetch.
    fetched_before = src.fetched
    for _ in range(3):
        clock.advance(_TEST_THRESHOLDS.sample_interval_seconds)
        result = monitor.tick()
        assert result.action == sm.TickAction.cooldown_skip
    assert len(rb.invocations) == 1
    assert len(sse_spy.events) == sse_count
    assert src.fetched == fetched_before


# ── Auxiliary contracts ─────────────────────────────────────────────


def test_load_thresholds_defaults_propagate(tmp_path):
    cfg = tmp_path / "slo_thresholds.yaml"
    cfg.write_text(
        "error_rate_max: 0.02\np95_latency_ms_max: 750\n",
        encoding="utf-8",
    )
    thresholds = sm.load_thresholds(cfg)
    assert thresholds.error_rate_max == 0.02
    assert thresholds.p95_latency_ms_max == 750
    # Defaults preserved for unspecified keys
    assert thresholds.sample_interval_seconds == 30
    assert thresholds.breach_sustain_seconds == 120
    assert thresholds.cooldown_seconds == 600


def test_load_thresholds_reads_op912_cross_task_slos(tmp_path):
    cfg = tmp_path / "slo_thresholds.yaml"
    cfg.write_text(
        "\n".join(
            [
                "project_state_api_p95_ms_max: 2000",
                "cognee_query_p95_ms_max: 800",
                "graphiti_query_p95_ms_max: 600",
                "failure_recall_p95_ms_max: 500",
            ]
        ),
        encoding="utf-8",
    )

    thresholds = sm.load_thresholds(cfg)

    assert thresholds.project_state_api_p95_ms_max == 2000
    assert thresholds.cognee_query_p95_ms_max == 800
    assert thresholds.graphiti_query_p95_ms_max == 600
    assert thresholds.failure_recall_p95_ms_max == 500


def test_maybe_rollback_raises_in_cooldown():
    src = _FakeMetricSource(samples=[_err_breach()])
    monitor, clock, rb, _ = _make_monitor(source=src)

    monitor.tick()
    clock.advance(_TEST_THRESHOLDS.breach_sustain_seconds)
    monitor.tick()
    # auto-rollback armed cooldown -- a manual call must refuse
    with pytest.raises(sm.CooldownInEffect):
        monitor.maybe_rollback("manual_drain")
    # Cooldown expires -> manual call succeeds.
    clock.advance(_TEST_THRESHOLDS.cooldown_seconds + 1)
    outcome = monitor.maybe_rollback("manual_drain")
    assert outcome.mode == "canary"
    assert rb.invocations[-1] == "manual_drain"


def test_file_or_env_override_source(tmp_path, monkeypatch):
    monkeypatch.delenv(sm.SUPPRESS_ENV_VAR, raising=False)
    flag = tmp_path / "slo_monitor_suppress.flag"
    src = sm.FileOrEnvOverrideSource(flag_path=flag)
    assert src.is_suppressed() is False
    flag.write_text("on", encoding="utf-8")
    assert src.is_suppressed() is True
    flag.unlink()
    assert src.is_suppressed() is False
    monkeypatch.setenv(sm.SUPPRESS_ENV_VAR, "1")
    assert src.is_suppressed() is True
