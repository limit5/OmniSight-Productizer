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
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from backend.agents.pipeline_coordinator_capacity import (
    CapacitySnapshot,
    capacity_path_from_env,
    load_capacity_snapshot_from_jsonl,
    write_capacity_snapshot,
)
from backend.agents.pipeline_coordinator_modes import (
    ModeSelector,
    SituationProfile,
)
from backend.agents.pipeline_coordinator_llm_consultation import build_hybrid_engine
from backend.agents.pipeline_coordinator_rules import (
    ACTION_FILE_TICKET,
    ACTION_RELABEL,
    ACTION_TRANSITION,
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



# ══════════════════════════════════════════════════════════════════════
# Cold-start 4-phase recovery (29f-7 / OP-1005) — ADR-0021 §3.3 + §9 L6
# ══════════════════════════════════════════════════════════════════════
#
# "The coordinator is *the* canonical startup orchestrator — if it is healthy,
# everything else is" (ADR §2.4). On boot it runs four phases before entering
# the steady-state loop:
#
#   Startup-1  infra verify   — deployment-audit.sh + systemctl --user start
#   (L6)       crash recovery — replay last 24h decision log (ADR §9 L6)
#   Startup-2  JIRA reconcile — interrupted-ticket decision tree (§3.3)
#   Startup-3  stale sweep    — orphaned claim:* / dead waiting-* markers
#   Startup-4  enter loop     — log steady-state entry, hand off to run_forever
#
# Everything that touches infra / JIRA / git is funnelled through a single
# injected :class:`ColdStartGateway` seam so the orchestration stays a pure,
# deterministically-testable decision tree; the production default
# (:class:`JiraDispatchColdStartGateway`) wires the real adapters and is
# fail-safe (a degraded collaborator never raises out of startup — it logs +
# yields an empty result so the daemon still reaches its loop).

# Decision-log event names for the cold-start phases (grep-able per §9 L4).
COLD_START_BEGAN_EVENT = "cold_start_began"
COLD_START_COMPLETE_EVENT = "cold_start_complete"
STARTUP_PHASE_EVENT = "startup_phase"
CRASH_RECOVERY_EVENT = "crash_recovery_applied"
STEADY_STATE_ENTERED_EVENT = "coordinator_entered_steady_state"

# Label the runner reads to continue from an existing feature branch (§3.3 (a)).
RESUME_FROM_FEATURE_LABEL = "runner:resume-from-feature-branch"

# L6 replays at most this far back (ADR §9 L6: "last 24h of decision log").
L6_REPLAY_WINDOW_HOURS = 24.0


@dataclass(frozen=True)
class InfraUnit:
    """One expected-live unit as reported by the deployment audit (Startup-1)."""

    name: str
    live: bool


@dataclass(frozen=True)
class InfraAuditResult:
    """Outcome of a Startup-1 deployment audit pass."""

    units: tuple[InfraUnit, ...] = ()

    @property
    def down(self) -> tuple[str, ...]:
        return tuple(u.name for u in self.units if not u.live)


@dataclass(frozen=True)
class InterruptedTicket:
    """A ticket left ``進行中`` by an interrupted/crashed runner (Startup-2).

    ``stale`` is the gateway's verdict on whether the ticket has had no
    activity for > 2× the CLI timeout (ADR §3.3 (c)) — it separates a truly
    abandoned ticket (revert) from one a runner may have only just started
    on (ambiguous → operator, case (d)).
    """

    key: str
    status: str = ""
    stale: bool = False


@dataclass(frozen=True)
class StaleSweepPlan:
    """The Startup-3 sweep work the gateway found, ready to apply.

    ``stale_claims`` maps a ticket to the orphaned ``claim:*`` labels to drop;
    ``orphan_assignees`` are To-Do tickets still bot-assigned with no claim;
    ``resolved_waiting`` maps a ticket to ``runner-blocked:waiting-X`` markers
    whose blocker X is already 公開済み (a dead wait — drop the marker).
    """

    stale_claims: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    orphan_assignees: tuple[str, ...] = ()
    resolved_waiting: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class ColdStartReport:
    """Mutable accumulator for what the 4-phase recovery did (one boot)."""

    started_units: list[str] = field(default_factory=list)
    still_down_units: list[str] = field(default_factory=list)
    phase1_halted: bool = False
    crash_recoveries: list[dict[str, Any]] = field(default_factory=list)
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    swept: list[dict[str, Any]] = field(default_factory=list)
    entered_loop: bool = False


@runtime_checkable
class ColdStartGateway(Protocol):
    """The infra / JIRA / git seam the 4-phase recovery drives (ADR §3.3).

    Every method is best-effort in production: an adapter that cannot reach
    its backend returns an empty / conservative answer rather than raising,
    so a degraded environment can never wedge the daemon before its loop.
    """

    # ── Startup-1 (infra) ──
    def audit_infra(self) -> InfraAuditResult: ...
    def start_unit(self, unit: str) -> bool: ...

    # ── Startup-2 (JIRA reconcile) ──
    def interrupted_tickets(self) -> list[InterruptedTicket]: ...
    def has_live_runner(self, key: str) -> bool: ...
    def gerrit_change_mergeable(self, key: str) -> bool: ...
    def branch_has_commits(self, key: str) -> bool: ...
    def mark_resumable(self, key: str) -> None: ...
    def transition_under_review(self, key: str) -> None: ...
    def reset_to_todo(self, key: str) -> None: ...

    # ── Startup-3 (stale sweep) ──
    def stale_sweep_plan(self) -> StaleSweepPlan: ...
    def remove_label(self, key: str, label: str) -> None: ...
    def clear_assignee(self, key: str) -> None: ...

    # ── shared (Startup-1 halt / ambiguous Startup-2 (d)) ──
    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None: ...


class JiraDispatchColdStartGateway:
    """Production :class:`ColdStartGateway` over ``jira_dispatch`` + git + shell.

    Heavy collaborators (``jira_dispatch.make_client``, the Gerrit SSH path)
    are imported + constructed lazily and wrapped so a failure degrades to a
    safe default — the daemon must reach its loop even on a half-up host.
    The agent class used for the JIRA/Gerrit identity defaults to the
    coordinator's own bot account (``claude``); it only ever *reads* + applies
    the §6.1-bounded actions (relabel / transition / comment), never +2.
    """

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        agent_class: str = "claude",
        cli_timeout_seconds: float = 60.0,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root is not None else _repo_root()
        self._agent_class = agent_class
        self._cli_timeout = cli_timeout_seconds
        self._client: Any = None
        self._client_tried = False

    # ── lazy JIRA client (fail-open) ──
    def _jira(self) -> Any:
        if self._client is None and not self._client_tried:
            self._client_tried = True
            try:
                from backend.agents import jira_dispatch

                self._client = jira_dispatch.make_client(self._agent_class)
            except Exception as exc:  # noqa: BLE001 — degrade, never wedge boot
                logger.warning(
                    "[pipeline_coordinator] cold-start JIRA client unavailable "
                    "(reconcile/sweep degraded): %s",
                    exc,
                )
                self._client = None
        return self._client

    # ── Startup-1 ──
    def audit_infra(self) -> InfraAuditResult:
        return run_deployment_audit(
            repo_root=self._repo_root, timeout_seconds=self._cli_timeout
        )

    def start_unit(self, unit: str) -> bool:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                text=True,
                timeout=self._cli_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("[pipeline_coordinator] systemctl start %s failed: %s", unit, exc)
            return False
        if proc.returncode != 0:
            logger.warning(
                "[pipeline_coordinator] systemctl start %s rc=%d: %s",
                unit, proc.returncode, proc.stderr.strip(),
            )
        return proc.returncode == 0

    # ── Startup-2 ──
    def interrupted_tickets(self) -> list[InterruptedTicket]:
        client = self._jira()
        if client is None:
            return []
        try:
            from backend.agents import jira_dispatch

            jql = (
                f'project = "{client.project_key}" '
                'AND status in ("In Progress", "進行中") '
                "ORDER BY updated ASC"
            )
            resp = jira_dispatch._request(
                client, "POST", "/search/jql",
                {"jql": jql, "fields": ["status", "updated"], "maxResults": 100},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] interrupted-ticket query failed: %s", exc)
            return []
        from backend.agents.pipeline_coordinator_rules import (
            DEFAULT_CLI_TIMEOUT_SECONDS,
            STALE_MULTIPLIER,
        )

        stale_after = DEFAULT_CLI_TIMEOUT_SECONDS * STALE_MULTIPLIER
        now = datetime.now(timezone.utc)
        out: list[InterruptedTicket] = []
        for issue in resp.get("issues", []):
            fields = issue.get("fields") or {}
            status = str((fields.get("status") or {}).get("name", ""))
            # ``updated`` is a coarse last-activity proxy for staleness (the
            # exact 進行中-entry time needs the changelog; updated is a safe,
            # cheaper lower bound — if it's old, the ticket is certainly stale).
            stale = False
            updated_raw = fields.get("updated")
            if isinstance(updated_raw, str):
                try:
                    updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    stale = (now - updated).total_seconds() > stale_after
                except ValueError:
                    pass
            out.append(
                InterruptedTicket(key=issue.get("key", "?"), status=status, stale=stale)
            )
        return out

    def has_live_runner(self, key: str) -> bool:
        return _ticket_has_live_runner(key, repo_root=self._repo_root)

    def gerrit_change_mergeable(self, key: str) -> bool:
        # Conservative: only a *positively confirmed* mergeable open change
        # routes a ticket to Under Review (§3.3 (b)). When the Gerrit query
        # is unavailable we return False so the ticket falls through to the
        # resume / revert / operator branches rather than a wrong transition.
        try:
            return _gerrit_open_change_mergeable(key, self._agent_class)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] gerrit mergeable check failed for %s: %s", key, exc)
            return False

    def branch_has_commits(self, key: str) -> bool:
        return _feature_branch_has_commits(key, repo_root=self._repo_root)

    def mark_resumable(self, key: str) -> None:
        self._add_label(key, RESUME_FROM_FEATURE_LABEL)

    def transition_under_review(self, key: str) -> None:
        client = self._jira()
        if client is None:
            return
        try:
            from backend.agents import jira_dispatch

            # Idempotent per OP-691: skips when already Under Review.
            jira_dispatch.transition_to_under_review_if_needed(client, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] transition_under_review(%s) failed: %s", key, exc)

    def reset_to_todo(self, key: str) -> None:
        client = self._jira()
        if client is None:
            return
        try:
            from backend.agents import jira_dispatch

            jira_dispatch.transition_back_to_todo(
                client, key,
                reason="[cold-start Startup-2(c)] interrupted with no commits/activity; "
                       "reverting to To Do for clean re-pickup.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] reset_to_todo(%s) failed: %s", key, exc)

    # ── Startup-3 ──
    def stale_sweep_plan(self) -> StaleSweepPlan:
        client = self._jira()
        if client is None:
            return StaleSweepPlan()
        try:
            return _build_stale_sweep_plan(client, repo_root=self._repo_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] stale-sweep plan failed: %s", exc)
            return StaleSweepPlan()

    def remove_label(self, key: str, label: str) -> None:
        client = self._jira()
        if client is None:
            return
        try:
            from backend.agents import jira_dispatch

            jira_dispatch.remove_label(client, key, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] remove_label(%s, %s) failed: %s", key, label, exc)

    def clear_assignee(self, key: str) -> None:
        client = self._jira()
        if client is None:
            return
        try:
            from backend.agents import jira_dispatch

            jira_dispatch.clear_assignee(client, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] clear_assignee(%s) failed: %s", key, exc)

    # ── shared ──
    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None:
        client = self._jira()
        if client is None:
            logger.error("[pipeline_coordinator] OPERATOR-ALERT (JIRA down) %s: %s", key, message)
            return
        try:
            from backend.agents import jira_dispatch

            jira_dispatch.add_comment(client, key, f"@nanakusa-sora {message}")
            if urgency == "high":
                jira_dispatch.add_label(client, key, "needs-operator-action")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] mention_operator(%s) failed: %s", key, exc)

    def _add_label(self, key: str, label: str) -> None:
        client = self._jira()
        if client is None:
            return
        try:
            from backend.agents import jira_dispatch

            jira_dispatch.add_label(client, key, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pipeline_coordinator] add_label(%s, %s) failed: %s", key, label, exc)


def _repo_root() -> Path:
    """Repo root holding ``scripts/`` + git worktree (this file is backend/agents/*)."""
    return Path(__file__).resolve().parents[2]


def run_deployment_audit(
    *, repo_root: Path | None = None, timeout_seconds: float = 60.0
) -> InfraAuditResult:
    """Run ``scripts/deployment-audit.sh`` (29a) and parse its unit verdicts.

    The script's ``DEPLOYMENT_AUDIT_JSONL_LOG`` hook is used to get a
    machine-readable row dump (rather than scraping the human table); each
    ``systemd-unit`` / ``systemd-timer`` row maps to an :class:`InfraUnit`
    (``live = status == "OK"``). The script exits non-zero when an
    expected-live row is red — that's expected input here, not an error, so
    the return code is ignored and only the JSONL is read.
    """
    repo_root = Path(repo_root) if repo_root is not None else _repo_root()
    script = repo_root / "scripts" / "deployment-audit.sh"
    if not script.exists():
        logger.warning("[pipeline_coordinator] deployment-audit.sh not found at %s", script)
        return InfraAuditResult()
    import tempfile

    with tempfile.NamedTemporaryFile(
        prefix="coord-audit-", suffix=".jsonl", delete=False
    ) as tmp:
        jsonl_path = Path(tmp.name)
    try:
        env = dict(os.environ)
        env["DEPLOYMENT_AUDIT_JSONL_LOG"] = str(jsonl_path)
        try:
            subprocess.run(
                ["bash", str(script)],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("[pipeline_coordinator] deployment-audit.sh run failed: %s", exc)
            return InfraAuditResult()
        record = _last_json_line(jsonl_path)
        if not record:
            return InfraAuditResult()
        units: list[InfraUnit] = []
        for row in record.get("rows", []):
            if row.get("kind") in ("systemd-unit", "systemd-timer"):
                units.append(
                    InfraUnit(name=row.get("name", "?"), live=row.get("status") == "OK")
                )
        return InfraAuditResult(units=tuple(units))
    finally:
        try:
            jsonl_path.unlink()
        except OSError:
            pass


def _last_json_line(path: Path) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return None
            return rec if isinstance(rec, dict) else None
    return None


def _ticket_has_live_runner(key: str, *, repo_root: Path | None = None) -> bool:
    """ADR §3.3 Startup-2: a runner is live for ``key`` if a process cmdline
    references it (auto-runner / its fresh feature branch) or an ephemeral
    worktree sentinel for the ticket exists. Best-effort; any probe error
    means "unknown" → treated as *not* live so reconciliation can proceed."""
    branch = f"feature/{key}-runner-fresh"
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                )
            except OSError:
                continue
            if key in cmdline and ("auto-runner" in cmdline or branch in cmdline):
                return True
    # Worktree sentinel: an ephemeral worktree dir named for the ticket.
    try:
        from backend.agents import jira_dispatch

        base = jira_dispatch.EPHEMERAL_WORKTREE_BASE
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir() and key in child.name:
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _feature_branch_has_commits(key: str, *, repo_root: Path | None = None) -> bool:
    """True if ``feature/<key>-runner-fresh`` exists with commits past develop."""
    repo_root = Path(repo_root) if repo_root is not None else _repo_root()
    branch = f"feature/{key}-runner-fresh"
    try:
        base = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", branch, "develop"],
            capture_output=True, text=True, timeout=15,
        )
        if base.returncode != 0:
            return False
        merge_base = base.stdout.strip()
        if not merge_base:
            return False
        log = subprocess.run(
            ["git", "-C", str(repo_root), "rev-list", "--count", f"{merge_base}..{branch}"],
            capture_output=True, text=True, timeout=15,
        )
        if log.returncode != 0:
            return False
        return int((log.stdout.strip() or "0")) > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _gerrit_open_change_mergeable(key: str, agent_class: str) -> bool:
    """True iff Gerrit has an *open, mergeable* change whose subject names ``key``."""
    from backend.agents import jira_dispatch
    from backend.agents.circuit_breaker import BREAKERS

    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS.get(agent_class)
    if auth is None:
        return False
    user, ssh_key = auth
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        [
            "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
            f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
            "gerrit", "query", "--format=JSON", f"is:open message:{key}",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        subject = str(change.get("subject", ""))
        if key in subject and change.get("mergeable") is True:
            return True
    return False


def _build_stale_sweep_plan(client: Any, *, repo_root: Path | None = None) -> StaleSweepPlan:
    """Assemble the Startup-3 sweep plan from a JIRA snapshot (ADR §3.3).

    Three hygiene classes, each gated on liveness / staleness so a healthy
    in-flight ticket is never swept:
      - ``claim:default:*`` labels with no live runner → drop the claim
      - To-Do tickets bot-assigned with no ``claim:*`` → clear the assignee
      - ``runner-blocked:waiting-X`` where blocker X is 公開済み → drop marker
    """
    from backend.agents import jira_dispatch
    from backend.agents.pipeline_coordinator_rules import (
        CLAIM_LABEL_PREFIX,
        DEPENDENCY_WAITING_LABEL_PREFIX,
        PUBLISHED_STATUS_NAMES,
    )

    repo_root = Path(repo_root) if repo_root is not None else _repo_root()
    jql = (
        f'project = "{client.project_key}" '
        'AND (labels ~ "claim:*" OR labels ~ "runner-blocked:waiting-*" '
        'OR (status = "To Do" AND assignee is not EMPTY)) '
        "ORDER BY updated ASC"
    )
    resp = jira_dispatch._request(
        client, "POST", "/search/jql",
        {"jql": jql, "fields": ["status", "labels", "assignee"], "maxResults": 200},
    )
    stale_claims: dict[str, tuple[str, ...]] = {}
    orphan_assignees: list[str] = []
    resolved_waiting: dict[str, tuple[str, ...]] = {}
    for issue in resp.get("issues", []):
        key = issue.get("key", "?")
        fields = issue.get("fields") or {}
        status = str((fields.get("status") or {}).get("name", ""))
        labels = list(fields.get("labels") or [])
        assignee = fields.get("assignee")
        claim_labels = tuple(l for l in labels if l.startswith(CLAIM_LABEL_PREFIX))
        if claim_labels and not _ticket_has_live_runner(key, repo_root=repo_root):
            stale_claims[key] = claim_labels
        elif assignee and status in jira_dispatch.TODO_STATUS_NAMES and not claim_labels:
            orphan_assignees.append(key)
        waiting = tuple(l for l in labels if l.startswith(DEPENDENCY_WAITING_LABEL_PREFIX))
        dead: list[str] = []
        for label in waiting:
            blocker_key = label[len(DEPENDENCY_WAITING_LABEL_PREFIX):]
            try:
                blocker_status = jira_dispatch.get_issue_status(client, blocker_key)
            except Exception:  # noqa: BLE001
                continue
            if blocker_status in PUBLISHED_STATUS_NAMES:
                dead.append(label)
        if dead:
            resolved_waiting[key] = tuple(dead)
    return StaleSweepPlan(
        stale_claims=stale_claims,
        orphan_assignees=tuple(orphan_assignees),
        resolved_waiting=resolved_waiting,
    )


def _default_cold_start_gateway(config: CoordinatorConfig) -> ColdStartGateway:
    """Production gateway wiring (no I/O at construction — lazy + fail-open)."""
    return JiraDispatchColdStartGateway()


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
        cold_start_gateway: ColdStartGateway | None = None,
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
        # 29f-7: cold-start recovery seam. Default wires the production
        # jira_dispatch / git / systemctl adapters (lazy + fail-open); tests
        # inject a fake so the 4-phase decision tree is verified in isolation.
        self._cold_start_gateway: ColdStartGateway = (
            cold_start_gateway or _default_cold_start_gateway(config)
        )
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
        # 29f-6: the Tier-2 LLM-consultation block (ADR Appendix C) — only on a
        # tick that actually consulted (or degraded) the LLM, so a Tier-1 / no-op
        # tick keeps its minimal record shape and ``budget_cap`` events stay
        # grep-able + replayable for the daily-spend rebuild.
        if result.llm_consultation is not None:
            record["llm_consultation"] = dict(result.llm_consultation)
        if action_results:
            record["action_results"] = action_results
        return record

    # ── cold-start 4-phase recovery (ADR §3.3 + §9 L6) ──

    def _log_cold_start(self, record: dict[str, Any]) -> None:
        """Append a cold-start decision-log record, stamping ts/pid/engine."""
        base: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "engine_version": self._engine.engine_version,
            "pid": os.getpid(),
        }
        base.update(record)
        self._decision_log.append(base)

    def startup(self) -> ColdStartReport:
        """Run the 4-phase cold-start recovery (ADR §3.3). Returns a report.

        Order: Startup-1 (infra) → L6 crash-recovery (ADR §9 L6: "after Phase
        Startup-1") → Startup-2 (JIRA reconcile) → Startup-3 (stale sweep) →
        Startup-4 (log steady-state entry). If Startup-1 cannot bring the
        expected infra up, the daemon halts startup (``phase1_halted=True``)
        and does NOT proceed — entering the loop with degraded infra is worse
        than not running (ADR §3.3 Startup-1 failure path). :meth:`run` reads
        the report to decide whether to enter the loop.
        """
        report = ColdStartReport()
        self._log_cold_start({"event": COLD_START_BEGAN_EVENT})
        self._startup_1_infra(report)
        if report.phase1_halted:
            self._log_cold_start(
                {"event": COLD_START_COMPLETE_EVENT, "halted_at": "startup-1",
                 "still_down": report.still_down_units}
            )
            return report
        self._l6_crash_recovery(report)
        self._startup_2_reconcile(report)
        self._startup_3_sweep(report)
        self._startup_4_enter_loop(report)
        self._log_cold_start(
            {
                "event": COLD_START_COMPLETE_EVENT,
                "started_units": report.started_units,
                "crash_recoveries": len(report.crash_recoveries),
                "reconciled": len(report.reconciled),
                "swept": len(report.swept),
            }
        )
        return report

    def _startup_1_infra(self, report: ColdStartReport) -> None:
        """Startup-1: verify infra; start any down unit; halt if still down."""
        gw = self._cold_start_gateway
        audit = gw.audit_infra()
        for unit in audit.down:
            ok = gw.start_unit(unit)
            (report.started_units if ok else report.still_down_units).append(unit)
            self._log_cold_start(
                {"event": STARTUP_PHASE_EVENT, "phase": 1, "unit": unit,
                 "action": "start_unit", "ok": ok}
            )
        # Re-verify after starts: a unit that refused to start (or died again)
        # leaves the infra degraded → @-operator + halt (do not enter loop).
        if report.still_down_units:
            recheck = gw.audit_infra()
            report.still_down_units = list(recheck.down)
        if report.still_down_units:
            report.phase1_halted = True
            gw.mention_operator(
                "OP",  # infra-level alert is not ticket-scoped
                "[cold-start Startup-1] coordinator could not bring up "
                f"{report.still_down_units}; halting startup rather than "
                "entering the loop with degraded infra (ADR-0021 §3.3).",
                urgency="high",
            )
            self._log_cold_start(
                {"event": STARTUP_PHASE_EVENT, "phase": 1, "action": "halt",
                 "still_down": report.still_down_units}
            )

    def _l6_crash_recovery(self, report: ColdStartReport) -> None:
        """L6 (ADR §9): replay last 24h decision log; resume/complete/rollback.

        A ``shutdown_began`` with no matching ``shutdown_complete`` (paired by
        ``decision_id``) means the daemon crashed mid-drain. For each such
        crash we look at the immediately-preceding tick: if it executed a real
        (non-dry-run) action that was never confirmed complete, we resolve it
        per action kind — idempotent relabel/transition are *completed*
        (re-issued safely), file_ticket is effectively irreversible so we
        *escalate*, everything else is a logged no-op. Every resolution writes
        a ``crash_recovery_applied`` record (ADR §9 L6).
        """
        records = self._read_recent_decision_log()
        began: dict[str, dict[str, Any]] = {}
        completed: set[str] = set()
        last_real_action_tick: dict[str, Any] | None = None
        for rec in records:
            event = rec.get("event")
            if event == "decision_tick" and not rec.get("dry_run", True) and rec.get("actions"):
                last_real_action_tick = rec
            elif event == "shutdown_began":
                began[str(rec.get("decision_id", ""))] = rec
                # Snapshot which real-action tick preceded this drain.
                rec["_preceding_real_tick"] = last_real_action_tick
            elif event == "shutdown_complete":
                completed.add(str(rec.get("decision_id", "")))
        for drain_id, drain_rec in began.items():
            if drain_id in completed:
                continue  # clean shutdown — nothing to recover
            tick = drain_rec.get("_preceding_real_tick")
            resolutions = self._resolve_interrupted_actions(tick)
            entry = {
                "event": CRASH_RECOVERY_EVENT,
                "crashed_drain_id": drain_id,
                "resolutions": resolutions,
            }
            report.crash_recoveries.append(entry)
            self._log_cold_start(entry)

    def _resolve_interrupted_actions(self, tick: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Map each interrupted action of a crashed tick to a recovery step."""
        if not tick:
            return [{"resolution": "noop", "reason": "no in-flight real action before crash"}]
        gw = self._cold_start_gateway
        out: list[dict[str, Any]] = []
        for action in tick.get("actions", []):
            kind = action.get("kind")
            target = action.get("target", "")
            if kind in (ACTION_RELABEL, ACTION_TRANSITION):
                # Idempotent → safe to complete (re-issue) on recovery.
                if kind == ACTION_TRANSITION and action.get("params", {}).get(
                    "to_status"
                ) == "Under Review":
                    gw.transition_under_review(target)
                resolution = "completed"
            elif kind == ACTION_FILE_TICKET:
                # Not safely reversible/repeatable — surface to operator.
                gw.mention_operator(
                    target,
                    f"[cold-start L6] crash interrupted a file_ticket action on {target}; "
                    "verify the dependency ticket exists and was not double-filed.",
                    urgency="medium",
                )
                resolution = "escalated"
            else:
                resolution = "noop"
            out.append({"kind": kind, "target": target, "resolution": resolution})
        return out

    def _read_recent_decision_log(self) -> list[dict[str, Any]]:
        """Return decision-log records from the last :data:`L6_REPLAY_WINDOW_HOURS`.

        Reads the day-partitioned JSONL files spanning the window (today + the
        days it reaches back into), in chronological order, skipping malformed
        lines so a single torn record never aborts recovery.
        """
        now = self._clock()
        window_start = now - timedelta(hours=L6_REPLAY_WINDOW_HOURS)
        records: list[dict[str, Any]] = []
        day = window_start.date()
        seen_paths: list[Path] = []
        while day <= now.date():
            path = self._decision_log.directory / f"{day.isoformat()}.jsonl"
            if path.exists():
                seen_paths.append(path)
            day = day + timedelta(days=1)
        for path in seen_paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    ts = record_ts(rec)
                    if ts is None or ts >= window_start:
                        records.append(rec)
        return records

    def _startup_2_reconcile(self, report: ColdStartReport) -> None:
        """Startup-2: reconcile each interrupted (進行中) ticket (ADR §3.3).

        Decision tree per ticket with no live runner:
          (b) Gerrit change exists + mergeable      → transition Under Review
          (a) feature branch has commits             → mark resumable, leave
          (c) no commits + stale (>2× CLI timeout)   → revert To Do (clear claim)
          (d) no commits + not yet stale (ambiguous) → @-operator, leave alone
        A ticket whose runner is still alive is left untouched (it's handling
        it). (b) is checked before (a): a mergeable change means the work is
        *finished* and only missed its transition, which beats re-running it.
        (c) vs (d) hinges on staleness so a runner that only just started (and
        hasn't committed yet) is never aggressively reverted out from under.
        """
        gw = self._cold_start_gateway
        for ticket in gw.interrupted_tickets():
            key = ticket.key
            if gw.has_live_runner(key):
                self._record_reconcile(report, key, "live-runner", "leave")
            elif gw.gerrit_change_mergeable(key):
                gw.transition_under_review(key)
                self._record_reconcile(report, key, "gerrit-mergeable", "under_review")
            elif gw.branch_has_commits(key):
                gw.mark_resumable(key)
                self._record_reconcile(report, key, "branch-has-commits", "resume")
            elif ticket.stale:
                # No commits, no live runner, long-stale → abandoned work.
                gw.reset_to_todo(key)
                self._record_reconcile(report, key, "no-commits-stale", "revert_todo")
            else:
                # No commits but recent — a runner may have only just started.
                gw.mention_operator(
                    key,
                    "[cold-start Startup-2(d)] interrupted with no commits but "
                    "recent activity — ambiguous; leaving in place for operator review.",
                    urgency="medium",
                )
                self._record_reconcile(report, key, "ambiguous", "operator")

    def _record_reconcile(
        self, report: ColdStartReport, key: str, classification: str, action: str
    ) -> None:
        entry = {"event": STARTUP_PHASE_EVENT, "phase": 2, "ticket": key,
                 "classification": classification, "action": action}
        report.reconciled.append({"ticket": key, "classification": classification, "action": action})
        self._log_cold_start(entry)

    def _startup_3_sweep(self, report: ColdStartReport) -> None:
        """Startup-3: drop orphaned claim:* / dead waiting-* + clear assignees."""
        gw = self._cold_start_gateway
        plan = gw.stale_sweep_plan()
        for key, labels in plan.stale_claims.items():
            for label in labels:
                gw.remove_label(key, label)
            gw.clear_assignee(key)
            self._record_sweep(report, key, "stale-claim", list(labels))
        for key in plan.orphan_assignees:
            gw.clear_assignee(key)
            self._record_sweep(report, key, "orphan-assignee", [])
        for key, labels in plan.resolved_waiting.items():
            for label in labels:
                gw.remove_label(key, label)
            self._record_sweep(report, key, "resolved-waiting", list(labels))

    def _record_sweep(
        self, report: ColdStartReport, key: str, kind: str, labels: list[str]
    ) -> None:
        entry = {"event": STARTUP_PHASE_EVENT, "phase": 3, "ticket": key,
                 "sweep": kind, "labels": labels}
        report.swept.append({"ticket": key, "sweep": kind, "labels": labels})
        self._log_cold_start(entry)

    def _startup_4_enter_loop(self, report: ColdStartReport) -> None:
        """Startup-4: log steady-state entry (the loop itself is run() → run_forever)."""
        report.entered_loop = True
        self._log_cold_start(
            {"event": STEADY_STATE_ENTERED_EVENT, "ts_entered": self._clock().isoformat()}
        )
        logger.info(
            "[pipeline_coordinator] cold-start complete — entered steady state at %s",
            self._clock().isoformat(),
        )

    def run(
        self,
        *,
        install_signals: bool = True,
        max_ticks: int | None = None,
    ) -> int:
        """Production boot: cold-start recovery, then the steady-state loop.

        Runs :meth:`startup` first (ADR §3.3 — the coordinator is the canonical
        startup orchestrator). If Startup-1 halted on degraded infra, the loop
        is NOT entered and 0 ticks are returned. Otherwise hands off to
        :meth:`run_forever` (Startup-4).
        """
        report = self.startup()
        if report.phase1_halted:
            logger.error(
                "[pipeline_coordinator] cold-start halted in Startup-1 "
                "(infra still down: %s) — not entering steady-state loop",
                report.still_down_units,
            )
            return 0
        return self.run_forever(install_signals=install_signals, max_ticks=max_ticks)

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
    """Production wiring: env config, hybrid Tier-1+Tier-2 engine, shadow layer.

    29f-3 shipped the Tier-1 rule engine; 29f-6 fronts it with the Tier-2 LLM
    consultant (:class:`HybridDecisionEngine`) so a tick that no Tier-1 rule
    matches (or that the personality mode escalates) consults the claude CLI
    under a daily budget cap. Constructing the engine logs the loaded rule
    registry (Deploy AC). The budget guard rebuilds the day's spend from the
    decision log (ADR §3.2 stateless-across-restarts). The action layer
    defaults to shadow (observe-only) so this stays safe for the 29f-14 7-day
    shadow canary; acting mode is enabled later by swapping the executor.
    """
    config = config or CoordinatorConfig.from_env()
    return PipelineCoordinator(
        config,
        engine=build_hybrid_engine(decision_log_dir=config.decision_log_dir),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. ``--once`` runs a single tick; default runs forever."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick (heartbeat + one no-op decision) and exit "
             "(skips cold-start recovery — a diagnostic tick, not a boot)",
    )
    parser.add_argument(
        "--no-cold-start",
        action="store_true",
        help="enter the steady-state loop directly, skipping the 4-phase "
             "cold-start recovery (ADR §3.3) — for local debugging only",
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
    if args.no_cold_start:
        coordinator.run_forever()
        return 0
    # Production boot: 4-phase cold-start recovery, then steady-state loop.
    coordinator.run()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
