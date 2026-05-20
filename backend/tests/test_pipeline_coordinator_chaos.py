"""OP-1548 coordinator watchdog chaos test.

Spawns a real coordinator process, kills it with SIGKILL, then uses the
watchdog's fake-systemctl seam to restart a clean replacement based only on
heartbeat staleness.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.agents.pipeline_coordinator_watchdog import (
    PipelineCoordinatorWatchdog,
    RestartResult,
    WatchdogConfig,
)


_COORDINATOR_SNIPPET = """
from pathlib import Path
from backend.agents.pipeline_coordinator import CoordinatorConfig, PipelineCoordinator

base = Path(__import__("os").environ["OP1548_COORDINATOR_DIR"])
cfg = CoordinatorConfig(
    config_dir=base,
    heartbeat_path=base / "heartbeat",
    decision_log_dir=base / "decision-log",
    heartbeat_interval_seconds=0.2,
    tick_interval_seconds=0.2,
)
PipelineCoordinator(cfg).run_forever(install_signals=False)
"""


class RestartingSystemctl:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.calls: list[str] = []
        self.processes: list[subprocess.Popen] = []

    def restart(self, service_name: str) -> RestartResult:
        self.calls.append(service_name)
        self.processes.append(_spawn_coordinator(self.config_dir))
        return RestartResult(ok=True)


def _spawn_coordinator(config_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["OP1548_COORDINATOR_DIR"] = str(config_dir)
    return subprocess.Popen(
        [sys.executable, "-c", _COORDINATOR_SNIPPET],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_heartbeat(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_pid(path: Path, *, exclude: int | None = None,
                  timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pid = int(_read_heartbeat(path)["pid"])
            if exclude is None or pid != exclude:
                return pid
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"heartbeat pid did not refresh: {last_error!r}")


def _heartbeat_age(path: Path) -> float:
    payload = _read_heartbeat(path)
    ts = datetime.fromisoformat(payload["ts"]).astimezone(timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def test_watchdog_recovers_after_sigkill_without_orphan_state(tmp_path: Path) -> None:
    """Exercised AC: kill -9 → watchdog restart → fresh pid, no orphan."""
    config_dir = tmp_path / "coordinator"
    heartbeat = config_dir / "heartbeat"
    first = _spawn_coordinator(config_dir)
    fake_systemctl = RestartingSystemctl(config_dir)

    try:
        old_pid = _wait_for_pid(heartbeat)
        assert old_pid == first.pid

        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=2)
        assert first.returncode == -signal.SIGKILL

        time.sleep(0.35)
        watchdog = PipelineCoordinatorWatchdog(
            WatchdogConfig(
                heartbeat_path=heartbeat,
                decision_log_dir=config_dir / "decision-log",
                stale_after_seconds=0.2,
                redie_window_seconds=300.0,
            ),
            systemctl=fake_systemctl,
        )
        outcome = watchdog.run_once()

        assert outcome.action == "restart"
        assert fake_systemctl.calls == ["pipeline-coordinator.service"]
        new_pid = _wait_for_pid(heartbeat, exclude=old_pid)
        assert new_pid != old_pid
        assert fake_systemctl.processes[0].pid == new_pid
        assert fake_systemctl.processes[0].poll() is None
        assert _heartbeat_age(heartbeat) < 1.0
    finally:
        _terminate(first)
        for proc in fake_systemctl.processes:
            _terminate(proc)
