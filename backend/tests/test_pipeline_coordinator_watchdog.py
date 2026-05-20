"""OP-1548 coordinator-watchdog tests.

AC mapping:

  Code AC        → test_watchdog_entrypoint_seams_exist
  Integration AC → stale/fresh/fail/re-die tests below
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.agents.pipeline_coordinator_watchdog import (
    PipelineCoordinatorWatchdog,
    RestartResult,
    WatchdogConfig,
    heartbeat_age_seconds,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeSystemctl:
    def __init__(self, *results: RestartResult) -> None:
        self.results = list(results) or [RestartResult(ok=True)]
        self.calls: list[str] = []

    def restart(self, service_name: str) -> RestartResult:
        self.calls.append(service_name)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeEscalator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def escalate(self, reason: str, context: dict) -> None:
        self.calls.append((reason, context))


def _config(tmp_path: Path) -> WatchdogConfig:
    return WatchdogConfig(
        heartbeat_path=tmp_path / "coordinator" / "heartbeat",
        decision_log_dir=tmp_path / "coordinator" / "decision-log",
        stale_after_seconds=90.0,
        redie_window_seconds=300.0,
    )


def _write_heartbeat(path: Path, ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts.isoformat(), "pid": 123}) + "\n",
                    encoding="utf-8")


def _records(log_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(line))
    return out


def test_watchdog_entrypoint_seams_exist(tmp_path: Path) -> None:
    """Code AC: entrypoint accepts heartbeat/restart seams."""
    cfg = _config(tmp_path)
    clock = FakeClock()
    systemctl = FakeSystemctl()
    escalator = FakeEscalator()
    watchdog = PipelineCoordinatorWatchdog(
        cfg, clock=clock, systemctl=systemctl, escalator=escalator
    )
    assert callable(watchdog.run_once)
    assert callable(watchdog.run_forever)
    assert heartbeat_age_seconds(cfg.heartbeat_path, now=clock()) == float("inf")


def test_stale_heartbeat_triggers_exactly_one_restart(tmp_path: Path) -> None:
    """Integration AC: stale heartbeat restarts once and emits watchdog_restart."""
    cfg = _config(tmp_path)
    clock = FakeClock()
    _write_heartbeat(cfg.heartbeat_path, clock() - timedelta(seconds=91))
    systemctl = FakeSystemctl(RestartResult(ok=True))
    escalator = FakeEscalator()
    watchdog = PipelineCoordinatorWatchdog(
        cfg, clock=clock, systemctl=systemctl, escalator=escalator
    )

    out = watchdog.run_once()

    assert out.action == "restart"
    assert systemctl.calls == ["pipeline-coordinator.service"]
    assert escalator.calls == []
    events = [r["event"] for r in _records(cfg.decision_log_dir)]
    assert events == ["watchdog_restart"]


def test_fresh_heartbeat_noops(tmp_path: Path) -> None:
    """Integration AC: fresh heartbeat performs no restart and emits nothing."""
    cfg = _config(tmp_path)
    clock = FakeClock()
    _write_heartbeat(cfg.heartbeat_path, clock() - timedelta(seconds=5))
    systemctl = FakeSystemctl()
    escalator = FakeEscalator()
    watchdog = PipelineCoordinatorWatchdog(
        cfg, clock=clock, systemctl=systemctl, escalator=escalator
    )

    out = watchdog.run_once()

    assert out.action == "noop"
    assert systemctl.calls == []
    assert escalator.calls == []
    assert _records(cfg.decision_log_dir) == []


def test_restart_failure_escalates_operator(tmp_path: Path) -> None:
    """Integration AC: failed restart escalates without restart event."""
    cfg = _config(tmp_path)
    clock = FakeClock()
    _write_heartbeat(cfg.heartbeat_path, clock() - timedelta(seconds=120))
    systemctl = FakeSystemctl(RestartResult(ok=False, returncode=1,
                                            stderr="unit failed"))
    escalator = FakeEscalator()
    watchdog = PipelineCoordinatorWatchdog(
        cfg, clock=clock, systemctl=systemctl, escalator=escalator
    )

    out = watchdog.run_once()

    assert out.action == "escalate"
    assert out.escalated is True
    assert systemctl.calls == ["pipeline-coordinator.service"]
    assert escalator.calls[0][0] == "pipeline-coordinator restart failed"
    records = _records(cfg.decision_log_dir)
    assert [r["event"] for r in records] == ["watchdog_escalation"]
    assert records[0]["reason"] == "restart_failed"


def test_re_dies_within_five_minutes_escalates_without_second_restart(
    tmp_path: Path,
) -> None:
    """Integration AC: re-die window escalates instead of restart-looping."""
    cfg = _config(tmp_path)
    clock = FakeClock()
    _write_heartbeat(cfg.heartbeat_path, clock() - timedelta(seconds=120))
    systemctl = FakeSystemctl(RestartResult(ok=True))
    escalator = FakeEscalator()
    watchdog = PipelineCoordinatorWatchdog(
        cfg, clock=clock, systemctl=systemctl, escalator=escalator
    )

    assert watchdog.run_once().action == "restart"
    clock.advance(60)
    out = watchdog.run_once()

    assert out.action == "escalate"
    assert systemctl.calls == ["pipeline-coordinator.service"]
    assert escalator.calls[0][0] == "pipeline-coordinator re-died"
    assert [r["event"] for r in _records(cfg.decision_log_dir)] == [
        "watchdog_restart",
        "watchdog_escalation",
    ]
