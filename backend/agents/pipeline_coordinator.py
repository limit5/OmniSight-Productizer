"""Release-pipeline coordinator daemon — skeleton (29f-coord / OP-1546).

The coordinator is the *domain-specific supervisor agent* for the autonomous
release pipeline (ADR-0021). This module is the **foundation skeleton** only:
a long-running daemon that ticks, asks an (empty) decision engine what to do,
logs exactly one no-op decision per tick, and stays alive with a heartbeat.
The real decision logic, cold-start recovery and event subscribers land in
sibling phases (29f-3 rules, 29f-7 cold-start, 29f-8 events) and plug into the
seams defined here without changing this daemon's contract:

    CoordinatorConfig    — paths + cadence (env-overridable)
    Clock                — injectable wall-clock (deterministic tests)
    HeartbeatWriter      — L1 liveness file, touched every tick (ADR §9 L1)
    DecisionLog          — L4 append-only JSONL audit (ADR §9 L4)
    PipelineCoordinator  — run_once() / run_forever() + SIGTERM drain (§9 L5)

Stateless across restarts (ADR-0021 §3.2): every tick re-builds the world.
The skeleton's "world" is an empty work-graph cache + an empty capacity
snapshot; 29f-3/7/8 fill those in.

Persistence paths live under ``~/.config/omnisight/coordinator/`` (NOT tmpfs)
so they survive a WSL ``/run`` wipe — per ADR-0021 §3.1. The decision-log
directory env var (``OMNISIGHT_COORDINATOR_DECISION_LOG_DIR``) is shared with
``gerrit_jira_bridge`` so both agents append to the same audit trail.

Module-global state audit (per project SOP)
-------------------------------------------
Immutable constants + frozen ``CoordinatorConfig`` + classes that hold only
injected collaborators. No database/socket opened at import time. The only
process-wide mutation is the SIGTERM handler the daemon installs in
``run_forever`` — guarded by ``install_signals`` so test threads opt out.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.agents.pipeline_coordinator_capacity import (
    CapacitySnapshot,
    capacity_path_from_env,
    load_capacity_snapshot_from_jsonl,
    write_capacity_snapshot,
)
from backend.agents.pipeline_coordinator_modes import (
    ModeSelector,
    SKELETON_MODE,
    SituationProfile,
)
from backend.agents.pipeline_coordinator_rules import (
    DecisionContext,
    DecisionEngine,
    DecisionResult,
    NoopAction,
)

logger = logging.getLogger(__name__)

# ── Injectable seams ──────────────────────────────────────────────────

# Wall clock. Returns a tz-aware UTC datetime. Injected so tests can pin
# time and assert heartbeat freshness deterministically.
Clock = Callable[[], datetime]

# Interruptible sleep. Returns True if a stop was requested while waiting
# (so the loop can break promptly), mirroring ``threading.Event.wait``.
SleepFn = Callable[[float], bool]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Config (ADR-0021 §3.1) ────────────────────────────────────────────

DEFAULT_CONFIG_DIR = Path("~/.config/omnisight/coordinator")
DECISION_LOG_DIR_ENV = "OMNISIGHT_COORDINATOR_DECISION_LOG_DIR"
CONFIG_DIR_ENV = "OMNISIGHT_COORDINATOR_CONFIG_DIR"

# ADR-0021 §9 L1: heartbeat every 60s.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
# Tick cadence of the steady-state loop (JIRA poll is 60s per §4; the
# skeleton just no-ops, so the tick cadence only governs heartbeat + log
# volume here).
DEFAULT_TICK_INTERVAL_SECONDS = 60.0

# Directory / file permission bits (ADR-0021 §3.1: dir 0700, files 0600).
_DIR_MODE = 0o700
_FILE_MODE = 0o600


@dataclass(frozen=True)
class CoordinatorConfig:
    """Filesystem paths + loop cadence for the coordinator daemon.

    Paths default under ``~/.config/omnisight/coordinator`` (survives a
    WSL ``/run`` wipe). ``from_env`` honours the same decision-log env var
    the bridge uses so both agents share one audit trail.
    """

    config_dir: Path
    heartbeat_path: Path
    decision_log_dir: Path
    capacity_path: Path | None = None
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.capacity_path is None:
            object.__setattr__(
                self,
                "capacity_path",
                capacity_path_from_env(self.config_dir),
            )

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        config_dir: Path | None = None,
    ) -> "CoordinatorConfig":
        env = env if env is not None else dict(os.environ)
        base = (
            Path(config_dir)
            if config_dir is not None
            else Path(env.get(CONFIG_DIR_ENV, str(DEFAULT_CONFIG_DIR))).expanduser()
        )
        decision_log_dir = Path(
            env.get(DECISION_LOG_DIR_ENV, str(base / "decision-log"))
        ).expanduser()
        return cls(
            config_dir=base,
            heartbeat_path=base / "heartbeat",
            decision_log_dir=decision_log_dir,
            capacity_path=capacity_path_from_env(base, env),
        )


# ── L1 heartbeat (ADR-0021 §9 L1) ─────────────────────────────────────


class HeartbeatWriter:
    """Writes the liveness file every tick (ADR-0021 §9 L1).

    The watchdog (§9 L2, sibling deploy) polls this file's mtime/content;
    here we only own the *write* side. The write is atomic (temp + rename)
    so a crashed mid-write never leaves a watcher reading a torn file.
    """

    def __init__(self, path: Path, *, clock: Clock = _utc_now) -> None:
        self._path = Path(path)
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def touch(self) -> datetime:
        """Stamp the heartbeat with the current time + pid. Returns the ts."""
        now = self._clock()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        payload = {
            "ts": now.isoformat(),
            "pid": os.getpid(),
            "event": "coordinator-alive",
        }
        tmp = self._path.with_name(self._path.name + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, self._path)
        return now


# ── L4 decision log (ADR-0021 §9 L4) ──────────────────────────────────


class DecisionLog:
    """Append-only JSONL audit, one file per UTC day (ADR-0021 §9 L4).

    ``append`` only ever opens the day's file in ``"a"`` mode — it never
    truncates or rewrites — so the log survives crashes and is safe to
    grep / replay for cold-start recovery (§9 L6). Directory is created
    0700 and each day file is chmod'd 0600 on first write.
    """

    def __init__(self, directory: Path, *, clock: Clock = _utc_now) -> None:
        self._dir = Path(directory)
        self._clock = clock

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, when: datetime | None = None) -> Path:
        when = when or self._clock()
        return self._dir / f"{when.date().isoformat()}.jsonl"

    def append(self, record: dict[str, Any]) -> Path:
        """Append one record as a JSON line. Returns the file written to."""
        self._dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        path = self.path_for(record_ts(record) or self._clock())
        existed = path.exists()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        if not existed:
            os.chmod(path, _FILE_MODE)
        return path


def record_ts(record: dict[str, Any]) -> datetime | None:
    """Parse the ``ts`` field of a record back to a datetime, if present.

    Used so a record's own timestamp picks the day-file (rather than the
    write moment), keeping the log partition consistent under clock skew.
    """
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ── Daemon ────────────────────────────────────────────────────────────


class PipelineCoordinator:
    """Skeleton coordinator daemon: tick → evaluate → log no-op → heartbeat.

    Collaborators are injected for deterministic tests; ``from_env`` /
    default construction wires the production defaults. The daemon holds
    no durable state of its own beyond an in-memory work-graph cache STUB
    that is rebuilt every tick (ADR-0021 §3.2 stateless-across-restarts).
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        *,
        engine: DecisionEngine | None = None,
        mode_selector: ModeSelector | None = None,
        clock: Clock = _utc_now,
        heartbeat: HeartbeatWriter | None = None,
        decision_log: DecisionLog | None = None,
        capacity_provider: Callable[[], CapacitySnapshot] | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._config = config
        self._engine = engine or DecisionEngine()
        self._mode_selector = mode_selector or ModeSelector()
        self._clock = clock
        self._heartbeat = heartbeat or HeartbeatWriter(config.heartbeat_path, clock=clock)
        self._decision_log = decision_log or DecisionLog(config.decision_log_dir, clock=clock)
        self._capacity_provider = capacity_provider or self._default_capacity
        self._stop = threading.Event()
        # Interruptible sleep: default waits on the stop event so SIGTERM
        # breaks the loop within one poll instead of one tick.
        self._sleep: SleepFn = sleep or self._stop.wait
        # Work-graph cache STUB — 29f-8 events / 29f-3 rules fill this.
        self._work_graph_cache: dict[str, Any] = {}
        self._signals_installed = False

    @property
    def config(self) -> CoordinatorConfig:
        return self._config

    # ── world-building (stateless per tick) ──

    def _default_capacity(self) -> CapacitySnapshot:
        return load_capacity_snapshot_from_jsonl(captured_at=self._clock())

    def _refresh_work_graph(self) -> dict[str, Any]:
        """STUB: rebuild the in-memory work-graph cache each tick.

        29f-8 wires JIRA poll + bridge-log tap here. The skeleton keeps the
        cache empty but exercises the refresh seam so the daemon contract
        (refresh → build context → evaluate) is fixed now.
        """
        self._work_graph_cache = {}
        return self._work_graph_cache

    def _current_situation(self) -> tuple[SituationProfile | None, str | None]:
        """STUB: the (profile, operator-override) the current tick decides on.

        29f-8 derives a :class:`SituationProfile` from the ticket/event under
        decision (urgency / risk / novelty / reversibility signals — ADR
        §7.1) and reads any ``coord-mode:*`` operator override label off the
        ticket. The skeleton has no situation, so it returns ``(None, None)``
        and the tick stays in the idle :data:`SKELETON_MODE`.
        """
        return None, None

    def build_context(self) -> DecisionContext:
        """Assemble the per-tick :class:`DecisionContext`.

        ``mode`` is the *idle* baseline (skeleton when there is no situation);
        the engine re-selects a real personality mode from ``situation`` +
        ``mode_override`` before rule evaluation (ADR-0021 §7).
        """
        situation, mode_override = self._current_situation()
        return DecisionContext(
            now=self._clock(),
            capacity=self._capacity_provider(),
            mode=self._mode_selector.select(),
            situation=situation,
            mode_override=mode_override,
            work_graph=dict(self._work_graph_cache),
        )

    # ── decision-log record (ADR-0021 §3.2 example) ──

    def _build_tick_record(self, result: DecisionResult) -> dict[str, Any]:
        # A no-op tick records ``actions:[]``; only real (non-noop) actions
        # are serialised into the array (skeleton emits none).
        actions = [a.to_record() for a in result.actions if not isinstance(a, NoopAction)]
        dry_run = all(a.dry_run for a in result.actions) if result.actions else True
        record: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "event": "decision_tick",
            "engine_version": result.engine_version,
            "mode": result.mode,
            "decision_id": result.decision_id,
            "actions": actions,
            "reason": result.reason,
            "dry_run": dry_run,
            "pid": os.getpid(),
        }
        # When a personality mode fired (non-idle tick), record the behavior
        # knobs that steered Tier-1 / Tier-2 so the selected mode is auditable
        # in the shadow/decision log (ADR-0021 §7.2 + Integration AC).
        if result.mode_behavior is not None:
            record["mode_behavior"] = {
                "tier1_dominant": result.mode_behavior.tier1_dominant,
                "tier2_policy": result.mode_behavior.tier2_policy,
                "llm_context_hops": result.mode_behavior.llm_context_hops,
                "llm_lessons_window": result.mode_behavior.llm_lessons_window,
            }
        return record

    # ── entrypoints ──

    def run_once(self) -> DecisionResult:
        """One tick: heartbeat → refresh world → evaluate → log decision.

        Returns the engine's :class:`DecisionResult` (a no-op in the
        skeleton) so callers/tests can assert on it directly.
        """
        self._heartbeat.touch()
        self._refresh_work_graph()
        ctx = self.build_context()
        if self._config.capacity_path is not None:
            write_capacity_snapshot(ctx.capacity, self._config.capacity_path)
        result = self._engine.evaluate(ctx)
        self._decision_log.append(self._build_tick_record(result))
        return result

    def request_stop(self) -> None:
        """Ask the steady-state loop to stop after the current tick."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run_forever(
        self,
        *,
        install_signals: bool = True,
        max_ticks: int | None = None,
    ) -> int:
        """Steady-state loop: tick, then sleep one interval, until stopped.

        On stop (SIGTERM via the installed handler, ``request_stop()``, or
        ``max_ticks`` reached) the loop drains cleanly: writes
        ``shutdown_began`` + ``shutdown_complete`` to the decision log,
        then returns the number of ticks executed (ADR-0021 §9 L5).

        ``install_signals`` is False-able so tests can drive the loop from
        a non-main thread (where ``signal.signal`` would raise).
        ``max_ticks`` bounds the loop for tests; production passes None.
        """
        if install_signals:
            self._install_signal_handlers()
        logger.info(
            "[pipeline_coordinator] entering steady state "
            "(engine=%s mode=%s tick=%.0fs heartbeat=%.0fs capacity_tracking=active capacity_path=%s)",
            self._engine.engine_version,
            SKELETON_MODE,
            self._config.tick_interval_seconds,
            self._config.heartbeat_interval_seconds,
            self._config.capacity_path,
        )
        ticks = 0
        try:
            while not self._stop.is_set():
                self.run_once()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if self._stop.is_set():
                    break
                # Interruptible: returns True if stop fired while waiting.
                if self._sleep(self._config.tick_interval_seconds):
                    break
        finally:
            self._drain()
        return ticks

    # ── L5 drain on shutdown (ADR-0021 §9 L5) ──

    def _drain(self) -> None:
        """Write the shutdown markers and exit cleanly.

        The skeleton has no in-flight LLM consultation to finish (§9 L5
        step 1 is a no-op here); it writes the ``shutdown_began`` /
        ``shutdown_complete`` pair (step 2) so L6 crash-recovery can tell a
        clean shutdown from a crash.
        """
        drain_id = uuid.uuid4().hex
        self._decision_log.append(
            {
                "ts": self._clock().isoformat(),
                "event": "shutdown_began",
                "engine_version": self._engine.engine_version,
                "decision_id": drain_id,
                "pid": os.getpid(),
            }
        )
        # (29f: finish in-flight LLM consultation here, max-wait 60s.)
        self._decision_log.append(
            {
                "ts": self._clock().isoformat(),
                "event": "shutdown_complete",
                "engine_version": self._engine.engine_version,
                "decision_id": drain_id,
                "pid": os.getpid(),
            }
        )
        logger.info("[pipeline_coordinator] drain complete — exiting cleanly")

    def _install_signal_handlers(self) -> None:
        if self._signals_installed:
            return

        def _handler(signum: int, _frame: Any) -> None:
            logger.warning("[pipeline_coordinator] caught signal %d — draining", signum)
            self.request_stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError) as exc:
                # Non-main thread / unsupported platform — caller drives
                # stop via request_stop() instead.
                logger.debug(
                    "[pipeline_coordinator] could not install handler for %s: %s",
                    sig, exc,
                )
        self._signals_installed = True


def build_default_coordinator() -> PipelineCoordinator:
    """Production wiring: config from env, skeleton engine + mode selector."""
    return PipelineCoordinator(CoordinatorConfig.from_env())


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. ``--once`` runs a single tick; default runs forever."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick (heartbeat + one no-op decision) and exit",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override the coordinator config dir (default ~/.config/omnisight/coordinator)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    config = CoordinatorConfig.from_env(config_dir=args.config_dir)
    coordinator = PipelineCoordinator(config)
    if args.once:
        coordinator.run_once()
        return 0
    coordinator.run_forever()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
