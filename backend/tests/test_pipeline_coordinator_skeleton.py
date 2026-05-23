"""Skeleton tests for the pipeline-coordinator daemon (OP-1546 / 29f-coord).

Each Acceptance Criterion from OP-1546 maps to at least one named test so
the AC ↔ test mapping in the JIRA verification comment is mechanical:

  Code AC      → test_daemon_starts_dry_noop, test_four_modules_and_seams_exist,
                 test_heartbeat_written_every_tick, test_sigterm_drain_markers,
                 test_decision_log_one_noop_per_tick
  Integration  → test_decision_log_dir_auto_created_and_writeable,
                 test_decision_log_is_append_only
  Exercised    → test_run_once_daemon_up, test_heartbeat_fresh_under_two_minutes,
                 test_drain_on_sigterm_real_signal, test_decision_log_append_validity

All filesystem state goes under ``tmp_path``; a ``FakeClock`` makes
heartbeat-freshness + day-partitioning deterministic without real time.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.agents import pipeline_coordinator as pc
from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    DecisionLog,
    HeartbeatWriter,
    PipelineCoordinator,
)
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot, RunnerCapacity
from backend.agents.pipeline_coordinator_modes import SKELETON_MODE, ModeSelector, ModeProfile
from backend.agents.pipeline_coordinator_rules import (
    SKELETON_NOOP_REASON,
    DecisionContext,
    DecisionEngine,
    NoopAction,
)


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        heartbeat_interval_seconds=60.0,
        tick_interval_seconds=60.0,
    )


def _coordinator(tmp_path: Path, clock: FakeClock | None = None, **kw) -> PipelineCoordinator:
    clock = clock or FakeClock()
    return PipelineCoordinator(_config(tmp_path), clock=clock, **kw)


def _read_log_records(directory: Path) -> list[dict]:
    records: list[dict] = []
    for f in sorted(directory.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ── Code AC: daemon starts dry no-op ──────────────────────────────────


def test_daemon_starts_dry_noop(tmp_path: Path) -> None:
    """Code AC: daemon starts and produces a dry-run no-op decision."""
    coord = _coordinator(tmp_path)
    result = coord.run_once()
    assert result.is_noop
    assert result.reason == SKELETON_NOOP_REASON
    assert tuple(type(a) for a in result.actions) == (NoopAction,)


def test_run_once_daemon_up(tmp_path: Path) -> None:
    """Exercised AC: daemon-up via run_once writes both heartbeat + log."""
    coord = _coordinator(tmp_path)
    coord.run_once()
    assert coord.config.heartbeat_path.exists()
    assert _read_log_records(coord.config.decision_log_dir)


# ── Code AC: 4 modules + seams exist ──────────────────────────────────


def test_four_modules_and_seams_exist() -> None:
    """Code AC: the 4 modules + every named seam are importable."""
    # 4 modules
    import backend.agents.pipeline_coordinator  # noqa: F401
    import backend.agents.pipeline_coordinator_modes  # noqa: F401
    import backend.agents.pipeline_coordinator_rules  # noqa: F401
    import backend.agents.pipeline_coordinator_capacity  # noqa: F401

    # Seams enumerated in the ticket scope.
    clock = FakeClock()
    cfg = CoordinatorConfig.from_env(config_dir=Path("/tmp/x"))
    assert isinstance(cfg, CoordinatorConfig)

    ctx = DecisionContext(now=clock(), capacity=CapacitySnapshot.empty(captured_at=clock()))
    result = DecisionEngine().evaluate(ctx)
    assert isinstance(result.actions[0], NoopAction)
    assert NoopAction().kind == "noop"

    # DecisionLog.append / HeartbeatWriter.touch / ModeSelector / CapacitySnapshot
    assert hasattr(DecisionLog, "append")
    assert hasattr(HeartbeatWriter, "touch")
    assert ModeSelector().select(ModeProfile()) == SKELETON_MODE
    assert CapacitySnapshot.empty(captured_at=clock()).total_free_slots == 0

    # Injectable Clock + run_once/run_forever entrypoints on the daemon.
    assert callable(PipelineCoordinator.run_once)
    assert callable(PipelineCoordinator.run_forever)


def test_capacity_snapshot_holds_runner_rows(tmp_path: Path) -> None:
    """CapacitySnapshot seam carries per-runner-class capacity."""
    clock = FakeClock()
    snap = CapacitySnapshot(
        captured_at=clock(),
        runners={"claude": RunnerCapacity("claude", free_slots=2)},
    )
    assert snap.total_free_slots == 2


# ── Code AC + Exercised AC: L1 heartbeat every 60s, fresh < 2min ──────


def test_heartbeat_written_every_tick(tmp_path: Path) -> None:
    """Code AC: L1 heartbeat file written on each tick."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    hb = coord.config.heartbeat_path
    assert hb.exists()
    payload = json.loads(hb.read_text(encoding="utf-8"))
    assert payload["event"] == "coordinator-alive"
    assert payload["pid"] == os.getpid()
    assert payload["ts"] == clock().isoformat()


def test_heartbeat_fresh_under_two_minutes(tmp_path: Path) -> None:
    """Exercised AC: heartbeat-fresh (<2min) via injectable Clock."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    written = datetime.fromisoformat(
        json.loads(coord.config.heartbeat_path.read_text())["ts"]
    )
    # Advance the (injected) clock 90s — still fresh; 150s — stale.
    clock.advance(90)
    assert (clock() - written) < timedelta(minutes=2)
    clock.advance(60)
    assert (clock() - written) >= timedelta(minutes=2)


def test_heartbeat_dir_mode_0700_file_0600(tmp_path: Path) -> None:
    """ADR §3.1 permission contract on the heartbeat path."""
    coord = _coordinator(tmp_path)
    coord.run_once()
    hb = coord.config.heartbeat_path
    assert oct(hb.stat().st_mode & 0o777) == "0o600"
    assert oct(hb.parent.stat().st_mode & 0o777) == "0o700"


# ── Code AC: L5 SIGTERM drain markers ─────────────────────────────────


def test_sigterm_drain_markers(tmp_path: Path) -> None:
    """Code AC: drain writes shutdown_began + shutdown_complete, clean exit.

    Drives the loop with a sleep stub that requests stop on the first
    sleep, so run_forever ticks once then drains — without a real signal.
    """
    coord = _coordinator(tmp_path)

    def _sleep_then_stop(_seconds: float) -> bool:
        coord.request_stop()
        return True

    coord._sleep = _sleep_then_stop  # injected interruptible sleep
    ticks = coord.run_forever(install_signals=False)
    assert ticks == 1

    events = [r["event"] for r in _read_log_records(coord.config.decision_log_dir)]
    assert events.count("shutdown_began") == 1
    assert events.count("shutdown_complete") == 1
    # Began precedes complete; both follow the tick.
    assert events == ["decision_tick", "shutdown_began", "shutdown_complete"]


def test_drain_on_sigterm_real_signal(tmp_path: Path) -> None:
    """Exercised AC: a real SIGTERM breaks the loop and drains.

    The sleep stub delivers SIGTERM to our own process; the installed
    handler flips the stop event so the loop exits and drains. Runs on the
    main thread (pytest default) so ``signal.signal`` is permitted.
    """
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers require the main thread")

    coord = _coordinator(tmp_path)

    def _sleep_send_sigterm(_seconds: float) -> bool:
        os.kill(os.getpid(), signal.SIGTERM)
        return coord.stopping

    coord._sleep = _sleep_send_sigterm
    try:
        ticks = coord.run_forever(install_signals=True, max_ticks=5)
    finally:
        # Restore default disposition so a later test's process is clean.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)

    assert coord.stopping
    assert ticks >= 1
    events = [r["event"] for r in _read_log_records(coord.config.decision_log_dir)]
    assert "shutdown_began" in events
    assert "shutdown_complete" in events


# ── Code AC: L4 decision-log append-only, one no-op per tick ──────────


def test_decision_log_one_noop_per_tick(tmp_path: Path) -> None:
    """Code AC: exactly one decision_tick no-op record per run_once."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    coord.run_once()
    ticks = [r for r in _read_log_records(coord.config.decision_log_dir)
             if r["event"] == "decision_tick"]
    assert len(ticks) == 2


def test_decision_log_append_validity(tmp_path: Path) -> None:
    """Exercised AC: decision-log record matches the ADR §3.2 schema."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    [record] = _read_log_records(coord.config.decision_log_dir)
    assert record["event"] == "decision_tick"
    assert record["mode"] == "skeleton"
    assert record["actions"] == []
    assert record["reason"] == SKELETON_NOOP_REASON
    assert record["dry_run"] is True
    assert record["pid"] == os.getpid()
    assert record["ts"] == clock().isoformat()
    assert record["engine_version"] == DecisionEngine.engine_version
    assert record["decision_id"]  # non-empty uuid


def test_decision_log_dir_auto_created_and_writeable(tmp_path: Path) -> None:
    """Integration AC: decision-log dir auto-created + writeable, 0700."""
    coord = _coordinator(tmp_path)
    assert not coord.config.decision_log_dir.exists()
    coord.run_once()
    d = coord.config.decision_log_dir
    assert d.is_dir()
    assert os.access(d, os.W_OK)
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    log_file = next(d.glob("*.jsonl"))
    assert oct(log_file.stat().st_mode & 0o777) == "0o600"


def test_decision_log_is_append_only(tmp_path: Path) -> None:
    """Integration AC: append-only — earlier lines are never rewritten."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    log_file = next(coord.config.decision_log_dir.glob("*.jsonl"))
    first_line = log_file.read_text(encoding="utf-8").splitlines()[0]

    coord.run_once()
    coord.run_once()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # The original first line is byte-for-byte unchanged (no rewrite).
    assert lines[0] == first_line
    # Each decision_id is unique (fresh per tick).
    ids = {json.loads(l)["decision_id"] for l in lines}
    assert len(ids) == 3


def test_decision_log_partitions_by_utc_day(tmp_path: Path) -> None:
    """L4 day-partitioning: a tick after midnight lands in the next file."""
    clock = FakeClock(datetime(2026, 5, 20, 23, 59, 30, tzinfo=timezone.utc))
    coord = _coordinator(tmp_path, clock=clock)
    coord.run_once()
    clock.advance(60)  # cross midnight UTC
    coord.run_once()
    files = sorted(p.name for p in coord.config.decision_log_dir.glob("*.jsonl"))
    assert files == ["2026-05-20.jsonl", "2026-05-21.jsonl"]


# ── Config seam: env overrides + path defaults ────────────────────────


def test_config_from_env_honours_decision_log_env(monkeypatch, tmp_path: Path) -> None:
    """CoordinatorConfig.from_env honours the shared decision-log env var."""
    target = tmp_path / "shared-log"
    monkeypatch.setenv(pc.DECISION_LOG_DIR_ENV, str(target))
    cfg = CoordinatorConfig.from_env(config_dir=tmp_path / "cfg")
    assert cfg.decision_log_dir == target
    assert cfg.heartbeat_path == tmp_path / "cfg" / "heartbeat"


def test_config_from_env_reads_acting_and_cold_start_caps(monkeypatch, tmp_path: Path) -> None:
    """OP-1618: env gate + per-phase cold-start cap envs are parsed together."""
    monkeypatch.setenv(pc.ACTING_ENV, "true")
    monkeypatch.setenv(pc.COLD_START_MAX_INFRA_ENV, "2")
    monkeypatch.setenv(pc.COLD_START_MAX_RECONCILE_ENV, "3")
    monkeypatch.setenv(pc.COLD_START_MAX_SWEEP_ENV, "4")
    monkeypatch.setenv(pc.COLD_START_BOT_ACCOUNT_IDS_ENV, "bot-a, bot-b")

    cfg = CoordinatorConfig.from_env(config_dir=tmp_path / "cfg")

    assert cfg.acting is True
    assert cfg.cold_start_max_infra == 2
    assert cfg.cold_start_max_reconcile == 3
    assert cfg.cold_start_max_sweep == 4
    assert cfg.cold_start_bot_account_ids == ("bot-a", "bot-b")


def test_config_from_env_defaults_to_shadow_and_one_per_phase_cap(tmp_path: Path) -> None:
    """OP-1618: unset daemon env stays shadow-safe and caps each phase at one."""
    cfg = CoordinatorConfig.from_env(env={}, config_dir=tmp_path / "cfg")

    assert cfg.acting is False
    assert cfg.cold_start_max_infra == 1
    assert cfg.cold_start_max_reconcile == 1
    assert cfg.cold_start_max_sweep == 1


def test_main_env_gate_selects_shadow_or_live_executor(monkeypatch, tmp_path: Path) -> None:
    """OP-1618: main() uses the daemon env gate to wire shadow vs live action."""
    captured: list[dict[str, Any]] = []

    class _FakeCoordinator:
        def run_once(self) -> None:
            return None

        def run_forever(self) -> None:
            return None

        def run(self) -> None:
            return None

    class _FakeLiveActionExecutor:
        def __init__(self, config: CoordinatorConfig) -> None:
            captured.append({"live_agent_class": config.jira_agent_class})

        def execute(self, action, ctx):
            return {"executed": True}

    def _build(config, *, acting=False, action_executor=None):
        captured.append({"acting": acting, "has_executor": action_executor is not None})
        return _FakeCoordinator()

    monkeypatch.setattr(pc, "LiveActionExecutor", _FakeLiveActionExecutor)
    monkeypatch.setattr(pc, "build_default_coordinator", _build)

    monkeypatch.delenv(pc.ACTING_ENV, raising=False)
    assert pc.main(["--once", "--config-dir", str(tmp_path / "shadow")]) == 0
    monkeypatch.setenv(pc.ACTING_ENV, "1")
    assert pc.main(["--once", "--config-dir", str(tmp_path / "acting")]) == 0

    assert captured == [
        {"acting": False, "has_executor": False},
        {"live_agent_class": "subscription-claude"},
        {"acting": True, "has_executor": True},
    ]


def test_main_once_runs_single_tick(tmp_path: Path, monkeypatch) -> None:
    """CLI --once entrypoint runs exactly one tick and exits 0."""
    cfg_dir = tmp_path / "cli"
    monkeypatch.delenv(pc.DECISION_LOG_DIR_ENV, raising=False)
    rc = pc.main(["--once", "--config-dir", str(cfg_dir)])
    assert rc == 0
    ticks = [r for r in _read_log_records(cfg_dir / "decision-log")
             if r["event"] == "decision_tick"]
    assert len(ticks) == 1
