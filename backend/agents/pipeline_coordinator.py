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
from datetime import datetime, timedelta, timezone
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
    Action,
    DecisionContext,
    DecisionEngine,
    DecisionResult,
    NoopAction,
    Tier1RuleEngine,
    WorkGraph,
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
BRIDGE_EVENTS_FILE_ENV = "OMNISIGHT_COORDINATOR_BRIDGE_EVENTS_FILE"
JIRA_AGENT_CLASS_ENV = "OMNISIGHT_COORDINATOR_JIRA_AGENT_CLASS"

# ADR-0021 §9 L1: heartbeat every 60s.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
# Tick cadence of the steady-state loop. Bridge tap latency is <1s per
# ADR-0021 §4, while JIRA polling remains independently gated at 60s.
DEFAULT_TICK_INTERVAL_SECONDS = 1.0
DEFAULT_JIRA_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0 * 60.0
DEFAULT_EVENT_DEDUPE_SECONDS = 5.0 * 60.0

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
    bridge_events_path: Path | None = None
    jira_agent_class: str = "subscription-claude"
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS
    jira_poll_interval_seconds: float = DEFAULT_JIRA_POLL_INTERVAL_SECONDS
    sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS
    event_dedupe_seconds: float = DEFAULT_EVENT_DEDUPE_SECONDS

    def __post_init__(self) -> None:
        if self.capacity_path is None:
            object.__setattr__(
                self,
                "capacity_path",
                capacity_path_from_env(self.config_dir),
            )
        if self.bridge_events_path is None:
            object.__setattr__(
                self,
                "bridge_events_path",
                self.config_dir / "bridge-events.jsonl",
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
            bridge_events_path=Path(
                env.get(BRIDGE_EVENTS_FILE_ENV, str(base / "bridge-events.jsonl"))
            ).expanduser(),
            jira_agent_class=env.get(JIRA_AGENT_CLASS_ENV, "subscription-claude"),
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


# ── Action layer (ADR-0021 §6) — shadow stub for 29f-3 ────────────────


class ShadowActionExecutor:
    """Records actions instead of executing them (ADR §6 / shadow mode).

    The real §6 action layer (JIRA mutate / @-operator) lands in a later
    phase; until then — and during the 29f-14 7-day shadow canary — the
    coordinator must *observe* what a rule would do without touching JIRA.
    This executor returns a per-action result record (``executed=False``)
    that the daemon folds into the decision-log line, so the integration AC
    ("rule returns Action → action layer executes it (mock if not yet built)
    → action recorded in decision log") is satisfied without any mutation.

    A real executor is a drop-in: same ``execute`` signature, returning
    ``executed=True`` plus the mutation outcome.
    """

    #: Stamped into each result so a decision-log reader can tell shadow
    #: (observed-only) executions from real ones.
    executed: bool = False

    def execute(self, action: Action, ctx: DecisionContext) -> dict[str, Any]:
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": self.executed,
            "shadow": True,
        }


# Action executor seam: anything with ``execute(action, ctx) -> dict``.
ActionExecutor = Callable[[Action, DecisionContext], dict]


# ── Event sources (ADR-0021 §4) ──────────────────────────────────────


class EventDeduper:
    """Five-minute coalescing window for identical source events."""

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_EVENT_DEDUPE_SECONDS,
        clock: Clock = _utc_now,
    ) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._clock = clock
        self._seen: dict[str, datetime] = {}

    def fresh(self, event: dict[str, Any]) -> bool:
        key = self._key(event)
        now = self._clock()
        last = self._seen.get(key)
        if last is not None and now - last < self._window:
            return False
        self._seen[key] = now
        return True

    def _key(self, event: dict[str, Any]) -> str:
        payload = event.get("payload")
        if isinstance(payload, dict):
            change = payload.get("change")
            change = change if isinstance(change, dict) else {}
            parts = [
                str(event.get("source") or ""),
                str(payload.get("type") or payload.get("webhookEvent") or ""),
                str(payload.get("id") or ""),
                str(change.get("id") or ""),
                str(change.get("number") or ""),
                str(payload.get("key") or ""),
            ]
            if any(parts[1:]):
                return "\x1f".join(parts)
        return json.dumps(event, sort_keys=True, default=str)


class BridgeEventTailer:
    """Tail the bridge JSONL event tap without blocking the coordinator."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._offset = 0

    @property
    def path(self) -> Path:
        return self._path

    def poll(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        size = self._path.stat().st_size
        if size < self._offset:
            self._offset = 0
        events: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            fh.seek(self._offset)
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("event") if isinstance(record, dict) else record
                if isinstance(payload, dict):
                    events.append(
                        {
                            "source": "bridge",
                            "trigger": f"bridge-event:{payload.get('type', 'unknown')}",
                            "payload": payload,
                            "bridge_record_ts": record.get("ts")
                            if isinstance(record, dict)
                            else None,
                        }
                    )
            self._offset = fh.tell()
        return events


class JiraEventPoller:
    """60s JIRA poll for coordinator-triggering ticket states."""

    def __init__(
        self,
        *,
        agent_class: str,
        clock: Clock = _utc_now,
        interval_seconds: float = DEFAULT_JIRA_POLL_INTERVAL_SECONDS,
        client_factory: Callable[[str], Any] | None = None,
        search: Callable[[Any, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._agent_class = agent_class
        self._clock = clock
        self._interval = timedelta(seconds=interval_seconds)
        self._client_factory = client_factory
        self._search = search
        self._client: Any | None = None
        self._last_poll: datetime | None = None

    def due(self) -> bool:
        return (
            self._last_poll is None
            or self._clock() - self._last_poll >= self._interval
        )

    def poll(self) -> list[dict[str, Any]]:
        if not self.due():
            return []
        self._last_poll = self._clock()
        try:
            client = self._client or self._make_client()
            self._client = client
            issues = self._search_issues(client, coordinator_jql(client.project_key))
        except Exception as exc:  # noqa: BLE001 — source failure is non-fatal
            logger.warning("[pipeline_coordinator] jira poll failed: %s", exc)
            return []
        events: list[dict[str, Any]] = []
        for issue in issues:
            key = str(issue.get("key") or "")
            if not key:
                continue
            events.append(
                {
                    "source": "jira",
                    "trigger": "jira-poll",
                    "ticket_key": key,
                    "payload": issue,
                }
            )
        return events

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self._agent_class)
        from backend.agents import jira_dispatch

        return jira_dispatch.make_client(self._agent_class)

    def _search_issues(self, client: Any, jql: str) -> list[dict[str, Any]]:
        if self._search is not None:
            return self._search(client, jql)
        from backend.agents import jira_dispatch

        resp = jira_dispatch._request(
            client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": ["summary", "labels", "status", "assignee", "updated"],
                "maxResults": 100,
            },
        )
        return list(resp.get("issues", []))


def coordinator_jql(project_key: str) -> str:
    """ADR-0021 §4 JQL for the 60s coordinator poll."""

    return (
        f'project = "{project_key}" '
        'AND (labels is EMPTY OR labels not in ("coord-skip")) '
        "AND ("
        'labels = "needs-coordinator" '
        'OR (status in ("Done", "Closed", "Won\'t Do", "却下") '
        "AND statusCategoryChangedDate >= -1d) "
        'OR (status = "進行中" AND assignee is EMPTY)'
        ") "
        "ORDER BY updated ASC"
    )


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
        work_graph_provider: Callable[[], WorkGraph] | None = None,
        action_executor: ActionExecutor | None = None,
        sleep: SleepFn | None = None,
        bridge_tailer: BridgeEventTailer | None = None,
        jira_poller: JiraEventPoller | None = None,
        deduper: EventDeduper | None = None,
    ) -> None:
        self._config = config
        self._engine = engine or DecisionEngine()
        self._mode_selector = mode_selector or ModeSelector()
        self._clock = clock
        self._heartbeat = heartbeat or HeartbeatWriter(config.heartbeat_path, clock=clock)
        self._decision_log = decision_log or DecisionLog(config.decision_log_dir, clock=clock)
        self._capacity_provider = capacity_provider or self._default_capacity
        # 29f-3: how the daemon learns the per-tick world. Default yields an
        # empty WorkGraph (skeleton / idle); 29f-8 injects the JIRA-poll +
        # bridge-tap builder. Every rule abstains on an empty graph.
        self._work_graph_provider = work_graph_provider or self._default_work_graph
        # 29f-3: action layer. Default is shadow (observe-only) so a rule that
        # fires never mutates JIRA until the real §6 layer / acting mode lands.
        executor = action_executor or ShadowActionExecutor().execute
        self._action_executor: ActionExecutor = executor
        # 29f-6: event sources (ADR §4) — bridge JSONL tail + 60s JIRA poll +
        # hourly sweep, coalesced through a 5-min deduper. They feed the
        # per-tick raw events staged for the work-graph builder.
        self._bridge_tailer = bridge_tailer or BridgeEventTailer(
            config.bridge_events_path or (config.config_dir / "bridge-events.jsonl")
        )
        self._jira_poller = jira_poller or JiraEventPoller(
            agent_class=config.jira_agent_class,
            clock=clock,
            interval_seconds=config.jira_poll_interval_seconds,
        )
        self._deduper = deduper or EventDeduper(
            window_seconds=config.event_dedupe_seconds,
            clock=clock,
        )
        self._last_sweep_at: datetime | None = self._clock()
        self._stop = threading.Event()
        # Interruptible sleep: default waits on the stop event so SIGTERM
        # breaks the loop within one poll instead of one tick.
        self._sleep: SleepFn = sleep or self._stop.wait
        # Current-tick world (rebuilt every tick — ADR §3.2 stateless).
        self._work_graph: WorkGraph = WorkGraph()
        # 29f-6: raw source events collected this tick, staged for the
        # events→WorkGraph builder (29f-8). Empty until the first refresh.
        self._pending_events: list[dict[str, Any]] = []
        self._signals_installed = False

    @property
    def config(self) -> CoordinatorConfig:
        return self._config

    # ── world-building (stateless per tick) ──

    def _default_capacity(self) -> CapacitySnapshot:
        return load_capacity_snapshot_from_jsonl(captured_at=self._clock())

    def _default_work_graph(self) -> WorkGraph:
        """Empty world — the skeleton / idle default (29f-8 supplies the real one)."""
        return WorkGraph()

    def _refresh_work_graph(self) -> WorkGraph:
        """Rebuild the per-tick world.

        29f-6 collects raw source events (bridge tail + 60s JIRA poll +
        hourly sweep, coalesced) and stages them in ``_pending_events``;
        29f-8 turns those into the WorkGraph the rules reason over via the
        injected provider. Until that builder lands, the default provider
        yields an empty graph so the daemon ticks safely while events are
        already being collected.
        """
        self._pending_events = self._collect_source_events()
        self._work_graph = self._work_graph_provider()
        return self._work_graph

    def _current_situation(self) -> tuple[SituationProfile | None, str | None]:
        """STUB: the (profile, operator-override) the current tick decides on.

        29f-8 derives a :class:`SituationProfile` from the ticket/event under
        decision (urgency / risk / novelty / reversibility signals — ADR
        §7.1) and reads any ``coord-mode:*`` operator override label off the
        ticket. The skeleton has no situation, so it returns ``(None, None)``
        and the tick stays in the idle :data:`SKELETON_MODE`.
        """
        return None, None

    def _collect_source_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        events.extend(self._bridge_tailer.poll())
        events.extend(self._jira_poller.poll())
        sweep = self._hourly_sweep_event()
        if sweep is not None:
            events.append(sweep)
        fresh: list[dict[str, Any]] = []
        for event in events:
            if self._deduper.fresh(event):
                fresh.append(event)
        return fresh

    def _hourly_sweep_event(self) -> dict[str, Any] | None:
        now = self._clock()
        if self._last_sweep_at is None:
            self._last_sweep_at = now
            return None
        elapsed = (now - self._last_sweep_at).total_seconds()
        if elapsed < self._config.sweep_interval_seconds:
            return None
        self._last_sweep_at = now
        return {
            "source": "timer",
            "trigger": "hourly-sweep",
            "payload": {
                "scan": "full-work-graph-anomaly",
                "elapsed_seconds": elapsed,
            },
        }

    def build_context(self) -> DecisionContext:
        """Assemble the per-tick :class:`DecisionContext`.

        ``mode`` is the *idle* baseline (skeleton when there is no situation);
        the engine re-selects a real personality mode from ``situation`` +
        ``mode_override`` before rule evaluation (ADR-0021 §7). ``ticket`` /
        ``tickets`` expose the WorkGraph slice the Tier-1 rules reason over.
        """
        situation, mode_override = self._current_situation()
        wg = self._work_graph
        return DecisionContext(
            now=self._clock(),
            capacity=self._capacity_provider(),
            mode=self._mode_selector.select(),
            situation=situation,
            mode_override=mode_override,
            ticket=wg.focal,
            tickets=dict(wg.tickets),
        )

    # ── decision-log record (ADR-0021 §3.2 example) ──

    def _build_tick_record(
        self,
        result: DecisionResult,
        action_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
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
        # 29f-3: name the firing rule + tier (ADR Appendix C) and the action
        # layer's per-action outcomes — only when a rule actually fired, so a
        # plain no-op tick keeps the skeleton's minimal record shape.
        if result.rule_name is not None:
            record["rule_name"] = result.rule_name
        if result.tier is not None:
            record["tier"] = result.tier
        if action_results:
            record["action_results"] = action_results
        return record

    # ── entrypoints ──

    def run_once(self) -> DecisionResult:
        """One tick: heartbeat → refresh world → evaluate → log decision.

        Returns the engine's :class:`DecisionResult` (a no-op in the
        skeleton) so callers/tests can assert on it directly.
        """
        self._heartbeat.touch()
        self._refresh_work_graph()
        self._append_source_event_records()
        ctx = self.build_context()
        if self._config.capacity_path is not None:
            write_capacity_snapshot(ctx.capacity, self._config.capacity_path)
        result = self._engine.evaluate(ctx)
        # Hand each real (non-noop) action to the action layer. In shadow
        # mode it only records the would-be execution; the outcomes go into
        # the decision-log line so a fired rule is observable end-to-end.
        action_results = [
            self._action_executor(a, ctx)
            for a in result.actions
            if not isinstance(a, NoopAction)
        ]
        self._decision_log.append(self._build_tick_record(result, action_results))
        return result

    def _append_source_event_records(self) -> None:
        for event in self._pending_events:
            self._decision_log.append(
                {
                    "ts": self._clock().isoformat(),
                    "event": "coordinator_source_event",
                    "trigger": event.get("trigger"),
                    "source": event.get("source"),
                    "ticket_key": event.get("ticket_key"),
                    "payload": event.get("payload"),
                    "dry_run": True,
                    "pid": os.getpid(),
                }
            )

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
        # Startup line names the loaded rule registry when the Tier-1 engine
        # is wired (Deploy AC: registry visible at startup). The engine also
        # logs its full rule list at construction.
        rule_names = getattr(self._engine, "rule_names", None)
        rules_desc = ",".join(rule_names()) if callable(rule_names) else "n/a"
        logger.info(
            "[pipeline_coordinator] entering steady state "
            "(engine=%s mode=%s tick=%.0fs heartbeat=%.0fs "
            "capacity_tracking=active capacity_path=%s rules=[%s])",
            self._engine.engine_version,
            self._mode_selector.select(),
            self._config.tick_interval_seconds,
            self._config.heartbeat_interval_seconds,
            self._config.capacity_path,
            rules_desc,
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


def build_default_coordinator(
    config: CoordinatorConfig | None = None,
) -> PipelineCoordinator:
    """Production wiring: env config, Tier-1 rule engine + shadow action layer.

    29f-3: the production default engine is now :class:`Tier1RuleEngine`
    (constructing it logs the loaded rule registry — Deploy AC). The action
    layer defaults to shadow (observe-only) so this stays safe for the
    29f-14 7-day shadow canary; acting mode is enabled later by swapping the
    executor, not by changing rules.
    """
    return PipelineCoordinator(
        config or CoordinatorConfig.from_env(),
        engine=Tier1RuleEngine(),
    )


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
    coordinator = build_default_coordinator(config)
    if args.once:
        coordinator.run_once()
        return 0
    coordinator.run_forever()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
