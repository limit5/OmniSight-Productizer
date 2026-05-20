"""Pipeline-coordinator watchdog (ADR-0021 §9 L2 / OP-1548).

Small, separate watchdog for the release-pipeline coordinator. It only polls
the coordinator heartbeat file; it does not inspect the coordinator process
table or systemd unit state. When the heartbeat is stale it asks
``systemctl --user`` to restart ``pipeline-coordinator.service`` and emits a
``watchdog_restart`` record. A failed restart, or another stale heartbeat
within the re-die window after a restart, escalates to the operator.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.agents.pipeline_coordinator import (
    DECISION_LOG_DIR_ENV,
    DEFAULT_CONFIG_DIR,
    DecisionLog,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
SleepFn = Callable[[float], bool]

DEFAULT_SERVICE_NAME = "pipeline-coordinator.service"
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_STALE_AFTER_SECONDS = 90.0
DEFAULT_REDIE_WINDOW_SECONDS = 300.0

CONFIG_DIR_ENV = "OMNISIGHT_COORDINATOR_CONFIG_DIR"
HEARTBEAT_PATH_ENV = "OMNISIGHT_COORDINATOR_HEARTBEAT_PATH"
SERVICE_NAME_ENV = "OMNISIGHT_COORDINATOR_WATCHDOG_SERVICE"
POLL_INTERVAL_ENV = "OMNISIGHT_COORDINATOR_WATCHDOG_POLL_SECONDS"
STALE_AFTER_ENV = "OMNISIGHT_COORDINATOR_WATCHDOG_STALE_SECONDS"
REDIE_WINDOW_ENV = "OMNISIGHT_COORDINATOR_WATCHDOG_REDIE_SECONDS"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RestartResult:
    """Result from the systemctl restart seam."""

    ok: bool
    returncode: int = 0
    stderr: str = ""


class Systemctl(Protocol):
    """Fake-able restart seam; production implementation shells out."""

    def restart(self, service_name: str) -> RestartResult:
        ...


class OperatorEscalator(Protocol):
    """Fake-able operator escalation seam."""

    def escalate(self, reason: str, context: dict[str, Any]) -> None:
        ...


class SubprocessSystemctl:
    """Production ``systemctl --user restart`` adapter."""

    def restart(self, service_name: str) -> RestartResult:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", service_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return RestartResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stderr=(proc.stderr or "").strip(),
        )


class LoggingOperatorEscalator:
    """Default escalation path until a richer operator channel is wired."""

    def escalate(self, reason: str, context: dict[str, Any]) -> None:
        logger.error("[pipeline_coordinator_watchdog] operator escalation: %s %s",
                     reason, context)


@dataclass(frozen=True)
class WatchdogConfig:
    """Filesystem paths + thresholds for the coordinator watchdog."""

    heartbeat_path: Path
    decision_log_dir: Path
    service_name: str = DEFAULT_SERVICE_NAME
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    redie_window_seconds: float = DEFAULT_REDIE_WINDOW_SECONDS

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        config_dir: Path | None = None,
    ) -> "WatchdogConfig":
        env = env if env is not None else dict(os.environ)
        base = (
            Path(config_dir)
            if config_dir is not None
            else Path(env.get(CONFIG_DIR_ENV, str(DEFAULT_CONFIG_DIR))).expanduser()
        )
        heartbeat_path = Path(env.get(HEARTBEAT_PATH_ENV, str(base / "heartbeat")))
        decision_log_dir = Path(
            env.get(DECISION_LOG_DIR_ENV, str(base / "decision-log"))
        ).expanduser()
        return cls(
            heartbeat_path=heartbeat_path.expanduser(),
            decision_log_dir=decision_log_dir,
            service_name=env.get(SERVICE_NAME_ENV, DEFAULT_SERVICE_NAME),
            poll_interval_seconds=_float_env(
                env, POLL_INTERVAL_ENV, DEFAULT_POLL_INTERVAL_SECONDS
            ),
            stale_after_seconds=_float_env(
                env, STALE_AFTER_ENV, DEFAULT_STALE_AFTER_SECONDS
            ),
            redie_window_seconds=_float_env(
                env, REDIE_WINDOW_ENV, DEFAULT_REDIE_WINDOW_SECONDS
            ),
        )


@dataclass(frozen=True)
class WatchdogTick:
    """Outcome of one watchdog poll."""

    heartbeat_age_seconds: float
    action: str
    escalated: bool = False


def _float_env(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[pipeline_coordinator_watchdog] invalid %s=%r; using %.0fs",
            name, raw, default,
        )
        return default


def heartbeat_age_seconds(path: Path, *, now: datetime) -> float:
    """Return heartbeat age in seconds, or ``inf`` when absent/unreadable."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return float("inf")

    raw = payload.get("ts")
    if not isinstance(raw, str):
        return float("inf")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts.astimezone(timezone.utc)).total_seconds()
    return max(0.0, age)


class PipelineCoordinatorWatchdog:
    """Poll heartbeat age and restart the coordinator when it goes stale."""

    def __init__(
        self,
        config: WatchdogConfig,
        *,
        clock: Clock = _utc_now,
        systemctl: Systemctl | None = None,
        escalator: OperatorEscalator | None = None,
        event_log: DecisionLog | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._systemctl = systemctl or SubprocessSystemctl()
        self._escalator = escalator or LoggingOperatorEscalator()
        self._event_log = event_log or DecisionLog(config.decision_log_dir, clock=clock)
        self._stop = threading.Event()
        self._sleep: SleepFn = sleep or self._stop.wait
        self._last_restart_at: datetime | None = None

    @property
    def config(self) -> WatchdogConfig:
        return self._config

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> WatchdogTick:
        """Run one heartbeat poll, restarting/escalating if needed."""
        now = self._clock()
        age = heartbeat_age_seconds(self._config.heartbeat_path, now=now)
        context = {
            "heartbeat_path": str(self._config.heartbeat_path),
            "heartbeat_age_seconds": age,
            "service_name": self._config.service_name,
        }
        if age <= self._config.stale_after_seconds:
            return WatchdogTick(heartbeat_age_seconds=age, action="noop")

        if self._re_died(now):
            self._emit("watchdog_escalation", {**context, "reason": "re_died"})
            self._escalator.escalate("pipeline-coordinator re-died", context)
            return WatchdogTick(heartbeat_age_seconds=age, action="escalate",
                                escalated=True)

        result = self._systemctl.restart(self._config.service_name)
        if not result.ok:
            fail_context = {
                **context,
                "returncode": result.returncode,
                "stderr": result.stderr,
            }
            self._emit("watchdog_escalation", {**fail_context, "reason": "restart_failed"})
            self._escalator.escalate("pipeline-coordinator restart failed", fail_context)
            return WatchdogTick(heartbeat_age_seconds=age, action="escalate",
                                escalated=True)

        self._last_restart_at = now
        self._emit("watchdog_restart", context)
        return WatchdogTick(heartbeat_age_seconds=age, action="restart")

    def run_forever(self, *, max_ticks: int | None = None) -> int:
        """Poll forever at the configured cadence until stopped."""
        ticks = 0
        while not self._stop.is_set():
            self.run_once()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if self._sleep(self._config.poll_interval_seconds):
                break
        return ticks

    def _re_died(self, now: datetime) -> bool:
        if self._last_restart_at is None:
            return False
        age = (now - self._last_restart_at).total_seconds()
        return age <= self._config.redie_window_seconds

    def _emit(self, event: str, context: dict[str, Any]) -> None:
        self._event_log.append({
            "ts": self._clock().isoformat(),
            "event": event,
            **context,
        })


def build_default_watchdog() -> PipelineCoordinatorWatchdog:
    """Production wiring for the standalone watchdog entrypoint."""
    return PipelineCoordinatorWatchdog(WatchdogConfig.from_env())


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. ``--once`` runs a single poll; default runs forever."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one watchdog poll")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override the coordinator config dir (default ~/.config/omnisight/coordinator)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    config = WatchdogConfig.from_env(config_dir=args.config_dir)
    watchdog = PipelineCoordinatorWatchdog(config)
    if args.once:
        watchdog.run_once()
        return 0
    watchdog.run_forever()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
