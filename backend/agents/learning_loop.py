"""Pipeline-coordinator learning loop (AUDIT-29f-11 / OP-1009) — ADR-0021 §10.

The coordinator closes the loop on its own decisions: every Tier-2
LLM-consulted decision is *written back* to the Cognee 3D-memory KG as a node
with edges to the tickets it touched, the lessons it cited, and the mode it
operated in (§10.1). A day later — once the action's effect is observable — a
``decision_outcome_check`` asks "did the action produce the expected result?"
and records the verdict back onto the decision (§10.1). Weekly, a
self-retrospective groups decisions by ``(situation_profile_class, action_type)``;
a class that consistently succeeded graduates to a *draft Tier-1 rule* written
to ``coord_rule_proposals/<slug>.py`` and the operator is @-mentioned to review
it (§10.2). Operator review is the hard gate — proposals are never auto-merged
(§13 risk table: "all graduated rules go through operator review").

What this module owns:

    DecisionRecord        — one parsed ``decision_tick`` line (the world a
                            write-back / outcome-check / retro reasons over)
    DecisionLogReader     — read the append-only log over a UTC-day window
    DecisionNode          — a write-back node (edges = tickets / lessons / mode)
    WriteBackGateway      — Cognee ingest seam (default → CogneeAdapter; the KG
                            falls back / no-ops when Neo4j is down)
    OutcomeObserver       — JIRA-state-change seam the 24h check reads (default
                            → jira_dispatch; injected stub in tests)
    RuleProposal          — a graduated draft rule + its rendered Python source
    LearningLoop          — daily + weekly handlers; the daemon-facing seam

Stateless-across-restarts (ADR §3.2)
------------------------------------
The loop holds no durable schedule state. ``LearningLoop.from_decision_log``
reconstructs the last daily/weekly run instant by scanning the log for the
loop's own ``learning_daily_ran`` / ``learning_weekly_ran`` markers, so a
restart does not double-fire (or skip) a scheduled run. Outcome verdicts and
write-back receipts are appended to the same append-only decision log, so the
outcome-check is itself idempotent: a decision whose outcome is already
recorded is skipped on the next daily pass.

Module-global state audit (per project SOP)
-------------------------------------------
Immutable constants, frozen dataclasses, two Protocols, and classes that hold
only injected collaborators + instance-local scheduling instants. No module
globals are mutated at runtime and no I/O happens at import time. Default
construction lazily wires the Cognee write-back + jira_dispatch outcome
observer; tests inject stubs for both so the unit suite never touches Neo4j or
a live JIRA.
"""

from __future__ import annotations

import json
import keyword
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

# Bumped when learning-loop semantics change. 1.x = first write-back era.
LEARNING_LOOP_VERSION = "1.0.0-learn"

# ── Decision-log event names this module reads + emits (grep-able, §9 L4) ──
DECISION_TICK_EVENT = "decision_tick"
# A Tier-2 decision was written back to Cognee as a node.
WRITEBACK_EVENT = "learning_writeback"
# The 24h outcome-check recorded a verdict for an earlier decision.
OUTCOME_CHECK_EVENT = "decision_outcome_check"
# The daily / weekly handlers ran (used to rebuild the schedule on restart).
DAILY_RAN_EVENT = "learning_daily_ran"
WEEKLY_RAN_EVENT = "learning_weekly_ran"
# A pattern graduated to a draft Tier-1 rule proposal.
GRADUATION_EVENT = "learning_rule_graduation"

# ── Cognee node kind for coordinator decisions (own dataset namespace) ──
# A literal kind string; the CogneeAdapter formats ``tenant:kind`` for any
# kind, so this never has to be registered in ``ALL_SOURCE_KINDS`` (which would
# change what a generic recall query sweeps).
SOURCE_KIND_COORD_DECISION = "coord_decision"

# ── Outcome verdicts (§10.1) ────────────────────────────────────────────
OUTCOME_SUCCESS = "success"          # action produced the expected result
OUTCOME_FAILURE = "failure"          # it did not (or made things worse)
OUTCOME_OPERATOR_OVERRIDE = "operator_override"  # operator corrected it
OUTCOME_UNKNOWN = "unknown"          # not yet observable / no signal
VALID_OUTCOMES = frozenset(
    {OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_OPERATOR_OVERRIDE, OUTCOME_UNKNOWN}
)

# ── Tunables (overridable via env / constructor) ────────────────────────
# An action's effect is "observable" after this delay (§10.1: "24h typical").
DEFAULT_OUTCOME_DELAY_HOURS = 24.0
# A decision older than this is past the outcome-check horizon — checking it
# is pointless (the world has moved on). Bounds the daily scan.
DEFAULT_OUTCOME_HORIZON_HOURS = 24.0 * 7.0
# Weekly retro looks back this far when grouping decisions.
DEFAULT_RETRO_WINDOW_DAYS = 7
# A (profile_class, action_type) class graduates only with at least this many
# observed decisions, all consistent (§10.2 "consistent outcomes").
DEFAULT_GRADUATION_MIN_SAMPLES = 3
# …and a success rate at or above this fraction (the rest must not be failures).
DEFAULT_GRADUATION_MIN_SUCCESS_RATE = 1.0
# Schedule cadence (the daemon ticks faster; these gate the handlers).
DEFAULT_DAILY_INTERVAL_HOURS = 24.0
DEFAULT_WEEKLY_INTERVAL_HOURS = 24.0 * 7.0

OUTCOME_DELAY_ENV = "OMNISIGHT_COORDINATOR_OUTCOME_DELAY_HOURS"
RETRO_WINDOW_ENV = "OMNISIGHT_COORDINATOR_RETRO_WINDOW_DAYS"
GRADUATION_MIN_SAMPLES_ENV = "OMNISIGHT_COORDINATOR_GRADUATION_MIN_SAMPLES"
PROPOSAL_DIR_ENV = "OMNISIGHT_COORDINATOR_PROPOSAL_DIR"

# Default home for graduated draft rules (operator-reviewed artifacts).
DEFAULT_PROPOSAL_DIR = (
    Path(__file__).resolve().parent / "coord_rule_proposals"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


Clock = Callable[[], datetime]


# ── Config ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LearningLoopConfig:
    """Cadence / threshold knobs for the learning loop (ADR §10)."""

    outcome_delay_hours: float = DEFAULT_OUTCOME_DELAY_HOURS
    outcome_horizon_hours: float = DEFAULT_OUTCOME_HORIZON_HOURS
    retro_window_days: int = DEFAULT_RETRO_WINDOW_DAYS
    graduation_min_samples: int = DEFAULT_GRADUATION_MIN_SAMPLES
    graduation_min_success_rate: float = DEFAULT_GRADUATION_MIN_SUCCESS_RATE
    daily_interval_hours: float = DEFAULT_DAILY_INTERVAL_HOURS
    weekly_interval_hours: float = DEFAULT_WEEKLY_INTERVAL_HOURS
    proposal_dir: Path = DEFAULT_PROPOSAL_DIR

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LearningLoopConfig":
        env = env if env is not None else os.environ
        return cls(
            outcome_delay_hours=_float_env(
                env, OUTCOME_DELAY_ENV, DEFAULT_OUTCOME_DELAY_HOURS
            ),
            retro_window_days=int(
                _float_env(env, RETRO_WINDOW_ENV, float(DEFAULT_RETRO_WINDOW_DAYS))
            ),
            graduation_min_samples=int(
                _float_env(
                    env,
                    GRADUATION_MIN_SAMPLES_ENV,
                    float(DEFAULT_GRADUATION_MIN_SAMPLES),
                )
            ),
            proposal_dir=Path(
                env.get(PROPOSAL_DIR_ENV, str(DEFAULT_PROPOSAL_DIR))
            ).expanduser(),
        )


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[coord-learn] ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


# ── Decision record (one parsed decision_tick line) ─────────────────────


@dataclass(frozen=True)
class DecisionRecord:
    """A ``decision_tick`` line reduced to what the learning loop reads.

    ``tickets`` are the JIRA keys the decision touched (each action's target,
    union the optional Appendix-C ``tickets`` field). ``action_types`` is the
    set of §6.1 action kinds the decision emitted. ``lessons_cited`` /
    ``learning`` / ``confidence`` come from the Tier-2 ``llm_consultation``
    block. ``outcome`` is ``None`` until the 24h check fills it.
    """

    decision_id: str
    ts: datetime
    tier: int | None
    mode: str
    reason: str
    tickets: tuple[str, ...]
    action_types: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    situation_profile: Mapping[str, Any] | None
    lessons_cited: tuple[str, ...]
    learning: str
    confidence: str
    outcome: str | None = None

    @property
    def is_tier2(self) -> bool:
        return self.tier == 2

    @property
    def profile_class(self) -> str:
        """A stable label for the situation class (§10.2 grouping key).

        Prefers the 4-axis situation profile signature when present (the ADR's
        ``situation_profile_class``); falls back to the personality ``mode``
        when a tick carried no profile (e.g. idle / pre-29f-8 ticks).
        """
        return profile_class_of(self.situation_profile, self.mode)


def profile_class_of(profile: Mapping[str, Any] | None, mode: str) -> str:
    """Derive the stable ``situation_profile_class`` label (§10.2)."""
    if profile:
        axes = (
            f"u={profile.get('urgency', '?')}",
            f"r={profile.get('risk', '?')}",
            f"n={profile.get('novelty', '?')}",
            f"rev={profile.get('reversibility', '?')}",
        )
        return "|".join(axes)
    return f"mode={mode or 'skeleton'}"


def parse_decision_record(raw: Mapping[str, Any]) -> DecisionRecord | None:
    """Parse one log line into a :class:`DecisionRecord`, or ``None`` if not a tick."""
    if not isinstance(raw, Mapping) or raw.get("event") != DECISION_TICK_EVENT:
        return None
    ts = _parse_ts(raw.get("ts"))
    if ts is None:
        return None
    actions = tuple(a for a in raw.get("actions", []) if isinstance(a, Mapping))
    action_types = tuple(
        dict.fromkeys(str(a.get("kind", "")) for a in actions if a.get("kind"))
    )
    tickets = _collect_tickets(raw, actions)
    consult = raw.get("llm_consultation")
    consult = consult if isinstance(consult, Mapping) else {}
    lessons = consult.get("lessons_cited", [])
    lessons = tuple(str(x) for x in lessons) if isinstance(lessons, list) else ()
    profile = raw.get("situation_profile")
    profile = profile if isinstance(profile, Mapping) else None
    return DecisionRecord(
        decision_id=str(raw.get("decision_id", "")),
        ts=ts,
        tier=raw.get("tier") if isinstance(raw.get("tier"), int) else None,
        mode=str(raw.get("mode", "")),
        reason=str(raw.get("reason", "")),
        tickets=tickets,
        action_types=action_types,
        actions=actions,
        situation_profile=profile,
        lessons_cited=lessons,
        learning=str(consult.get("learning", "") or raw.get("learning", "") or ""),
        confidence=str(consult.get("confidence", "") or ""),
        outcome=str(raw["outcome"]) if raw.get("outcome") else None,
    )


def _collect_tickets(
    raw: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    keys: list[str] = []
    declared = raw.get("tickets")
    if isinstance(declared, list):
        keys.extend(str(k) for k in declared if k)
    for action in actions:
        target = action.get("target")
        if target:
            keys.append(str(target))
    return tuple(dict.fromkeys(k for k in keys if k))


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


# ── Decision-log reader (UTC-day-partitioned JSONL window) ──────────────


class DecisionLogReader:
    """Read the append-only decision log over a UTC-day window (§9 L4).

    Mirrors the daemon's own reader: day-partitioned ``YYYY-MM-DD.jsonl``
    files, malformed lines skipped so a torn record never aborts a pass.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        return self._dir

    def read_records(self, *, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Return raw log dicts whose ``ts`` falls in ``[since, until]``."""
        out: list[dict[str, Any]] = []
        day = since.date()
        while day <= until.date():
            path = self._dir / f"{day.isoformat()}.jsonl"
            day = day + timedelta(days=1)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is not None and since <= ts <= until:
                    out.append(rec)
        return out

    def decisions(self, *, since: datetime, until: datetime) -> list[DecisionRecord]:
        records = [parse_decision_record(r) for r in self.read_records(since=since, until=until)]
        return [r for r in records if r is not None]

    def last_event_ts(self, event: str, *, lookback: datetime, until: datetime) -> datetime | None:
        """The latest ``ts`` of ``event`` in the window, or ``None``."""
        latest: datetime | None = None
        for rec in self.read_records(since=lookback, until=until):
            if rec.get("event") != event:
                continue
            ts = _parse_ts(rec.get("ts"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        return latest

    def recorded_outcome_ids(self, *, since: datetime, until: datetime) -> set[str]:
        """Decision ids that already have an outcome-check record (idempotency)."""
        ids: set[str] = set()
        for rec in self.read_records(since=since, until=until):
            if rec.get("event") == OUTCOME_CHECK_EVENT and rec.get("decision_id"):
                ids.add(str(rec["decision_id"]))
        return ids


# ── Write-back node (§10.1) ─────────────────────────────────────────────


@dataclass(frozen=True)
class DecisionNode:
    """A Cognee write-back node for one Tier-2 decision (§10.1).

    The node's edges (to the tickets it touched, the lessons it cited, and the
    mode it operated in) are carried both in the rendered ``content`` — so
    Cognee's cognify step materialises them as graph edges — and in
    ``metadata`` for structured filtering.
    """

    decision_id: str
    ts: datetime
    mode: str
    rationale: str
    learning: str
    confidence: str
    tickets: tuple[str, ...]
    lessons_cited: tuple[str, ...]
    action_types: tuple[str, ...]
    outcome: str | None = None

    def render(self) -> str:
        """Markdown body fed to Cognee ECL (edges stated as explicit relations)."""
        lines = [
            f"# Coordinator decision {self.decision_id}",
            "",
            f"ts: {self.ts.isoformat()}",
            f"mode: {self.mode}",
            f"confidence: {self.confidence or 'unknown'}",
            f"action_types: {', '.join(self.action_types) or '-'}",
            f"outcome: {self.outcome or 'pending'}",
            "",
            "## Rationale",
            self.rationale or "(none recorded)",
        ]
        if self.learning:
            lines += ["", "## Learning", self.learning]
        lines += ["", "## Edges"]
        for key in self.tickets:
            lines.append(f"- touched_ticket -> {key}")
        for lesson in self.lessons_cited:
            lines.append(f"- cited_lesson -> {lesson}")
        lines.append(f"- operated_in_mode -> {self.mode}")
        return "\n".join(lines) + "\n"

    def metadata(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "ts": self.ts.isoformat(),
            "mode": self.mode,
            "confidence": self.confidence,
            "tickets": list(self.tickets),
            "lessons_cited": list(self.lessons_cited),
            "action_types": list(self.action_types),
            "outcome": self.outcome,
        }

    @classmethod
    def from_record(cls, record: DecisionRecord) -> "DecisionNode":
        consult_rationale = ""
        for action in record.actions:
            params = action.get("params") if isinstance(action, Mapping) else None
            if isinstance(params, Mapping) and params.get("reason"):
                consult_rationale = str(params["reason"])
                break
        return cls(
            decision_id=record.decision_id,
            ts=record.ts,
            mode=record.mode,
            rationale=consult_rationale or record.reason,
            learning=record.learning,
            confidence=record.confidence,
            tickets=record.tickets,
            lessons_cited=record.lessons_cited,
            action_types=record.action_types,
            outcome=record.outcome,
        )


@runtime_checkable
class WriteBackGateway(Protocol):
    """The Cognee write-back seam (injected as a stub in tests)."""

    def write_node(self, node: DecisionNode) -> bool: ...


class CogneeWriteBackGateway:
    """Default write-back: ingest the node into the ``coord_decision`` dataset.

    Lazy-imports ``cognee_integration`` so the heavy KG dependency stays off
    the import path and the unit suite (which injects a stub) never touches it.
    The Cognee adapter already degrades — it falls back / raises typed errors
    when Neo4j is down — so ``write_node`` swallows every failure into ``False``
    (fail-open): a learning write-back must never wedge a coordinator tick.
    """

    def write_node(self, node: DecisionNode) -> bool:
        try:
            from backend.agents.cognee_integration import (
                CogneeAdapter,
                IngestSource,
                _run_async,
            )

            adapter = CogneeAdapter.from_env()
            source = IngestSource(
                kind=SOURCE_KIND_COORD_DECISION,
                identifier=node.decision_id,
                content=node.render(),
                metadata=node.metadata(),
            )
            # Reuse the cognee adapter's sync→async bridge: it drives the
            # coroutine on a private loop when one is already running (the
            # coordinator tick is sync, but this keeps the no-running-loop
            # contract identical to the rest of the KG call sites).
            report = _run_async(adapter.ingest([source]))
            return report.sources_ingested >= 1
        except Exception as exc:  # noqa: BLE001 — write-back is best-effort
            logger.info("[coord-learn] write-back unavailable (%s): %s", type(exc).__name__, exc)
            return False


# ── Outcome observation (§10.1) ─────────────────────────────────────────


@dataclass(frozen=True)
class OutcomeVerdict:
    """The 24h outcome-check result for one decision (§10.1)."""

    outcome: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in VALID_OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")


@runtime_checkable
class OutcomeObserver(Protocol):
    """Reads the world to decide whether a decision produced its expected result."""

    def observe(self, record: DecisionRecord) -> OutcomeVerdict: ...


class JiraStateChangeObserver:
    """Default observer: correlate decision → outcome via JIRA state change.

    Integration AC: "outcome-check correlates decision → outcome via JIRA
    state-change observation." For each touched ticket it reads the current
    status / labels via ``jira_dispatch`` and checks the change the action
    intended actually landed (a ``transition`` reached its ``to_status``; a
    ``relabel`` added/removed its labels; an ``escalate`` left the
    ``needs-operator-action`` label). A ``coord-skip`` appearing after the
    decision is read as an operator override. Any read failure → ``unknown``
    (fail-open) so a flaky JIRA never poisons the learning signal.
    """

    def __init__(self, *, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory

    def observe(self, record: DecisionRecord) -> OutcomeVerdict:
        try:
            client = self._client()
            if client is None:
                return OutcomeVerdict(OUTCOME_UNKNOWN, "no jira client")
            return self._observe_with_client(client, record)
        except Exception as exc:  # noqa: BLE001 — observation is best-effort
            logger.info("[coord-learn] outcome observation failed: %s", exc)
            return OutcomeVerdict(OUTCOME_UNKNOWN, str(exc))

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from backend.agents import jira_dispatch

        getter = getattr(jira_dispatch, "get_client", None)
        return getter() if callable(getter) else None

    def _observe_with_client(self, client: Any, record: DecisionRecord) -> OutcomeVerdict:
        # Without a focal ticket there is nothing to observe.
        if not record.tickets:
            return OutcomeVerdict(OUTCOME_UNKNOWN, "no touched tickets")
        for action in record.actions:
            kind = str(action.get("kind", ""))
            target = str(action.get("target", ""))
            if not target:
                continue
            state = _read_ticket_state(client, target)
            if state is None:
                return OutcomeVerdict(OUTCOME_UNKNOWN, f"could not read {target}")
            labels = {str(x) for x in state.get("labels", [])}
            if "coord-skip" in labels:
                return OutcomeVerdict(OUTCOME_OPERATOR_OVERRIDE, f"{target} coord-skip set")
            params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
            if not _action_landed(kind, params, state, labels):
                return OutcomeVerdict(OUTCOME_FAILURE, f"{kind} on {target} did not land")
        return OutcomeVerdict(OUTCOME_SUCCESS, "all actions landed")


def _read_ticket_state(client: Any, key: str) -> dict[str, Any] | None:
    """Read ``{status, labels}`` for ``key`` via whatever jira_dispatch exposes."""
    getter = getattr(client, "get_ticket_state", None)
    if callable(getter):
        state = getter(key)
        return dict(state) if isinstance(state, Mapping) else None
    status_getter = getattr(client, "get_status", None)
    labels_getter = getattr(client, "get_labels", None)
    if callable(status_getter) or callable(labels_getter):
        return {
            "status": status_getter(key) if callable(status_getter) else "",
            "labels": labels_getter(key) if callable(labels_getter) else [],
        }
    return None


def _action_landed(
    kind: str,
    params: Mapping[str, Any],
    state: Mapping[str, Any],
    labels: set[str],
) -> bool:
    status = str(state.get("status", ""))
    if kind == "transition":
        want = str(params.get("to_status", ""))
        return not want or status == want
    if kind == "relabel":
        add = {str(x) for x in params.get("add", [])}
        remove = {str(x) for x in params.get("remove", [])}
        return add <= labels and not (remove & labels)
    if kind == "escalate":
        return "needs-operator-action" in labels
    # file_ticket / mention_operator / mark_for_followup have no directly
    # observable ticket-state delta on the focal ticket — treat as landed.
    return True


# ── Rule proposal (§10.2 graduation) ────────────────────────────────────


@dataclass(frozen=True)
class RetroGroup:
    """One ``(profile_class, action_type)`` bucket from the weekly retro."""

    profile_class: str
    action_type: str
    decisions: tuple[DecisionRecord, ...]

    @property
    def total(self) -> int:
        return len(self.decisions)

    @property
    def successes(self) -> int:
        return sum(1 for d in self.decisions if d.outcome == OUTCOME_SUCCESS)

    @property
    def failures(self) -> int:
        return sum(1 for d in self.decisions if d.outcome == OUTCOME_FAILURE)

    @property
    def success_rate(self) -> float:
        observed = self.successes + self.failures
        return (self.successes / observed) if observed else 0.0


@dataclass(frozen=True)
class RuleProposal:
    """A graduated draft Tier-1 rule + its rendered, syntax-valid Python source."""

    slug: str
    profile_class: str
    action_type: str
    sample_count: int
    success_rate: float
    source: str
    example_tickets: tuple[str, ...]

    @property
    def filename(self) -> str:
        return f"{self.slug}.py"


def _slugify(profile_class: str, action_type: str) -> str:
    raw = f"{action_type}-{profile_class}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "coord-rule-proposal"


def _safe_rule_name(slug: str) -> str:
    """A Python-identifier-safe function name derived from a slug."""
    ident = re.sub(r"[^0-9a-zA-Z]+", "_", slug).strip("_") or "proposed_rule"
    if ident[0].isdigit():
        ident = f"r_{ident}"
    if keyword.iskeyword(ident):
        ident = f"{ident}_rule"
    return f"proposed_{ident}"


def render_rule_proposal(group: RetroGroup, *, now: datetime) -> RuleProposal:
    """Render a draft ``@rule`` Python module for a graduated class (§10.2).

    The draft is intentionally conservative: it abstains (``return None``) by
    default and carries the observed evidence + a TODO in its body, so a
    proposal that an operator merges *as-is* is a safe no-op until the operator
    fills in the predicate. The point of the AC is that the generator emits
    *valid Python a human reviews* — not an auto-active rule.
    """
    slug = _slugify(group.profile_class, group.action_type)
    fn_name = _safe_rule_name(slug)
    example = tuple(
        dict.fromkeys(t for d in group.decisions for t in d.tickets)
    )[:5]
    rationales = [d.learning for d in group.decisions if d.learning][:3]
    priority = 75  # below the safety/routing band; operator re-prioritises on review.
    doc_evidence = "; ".join(rationales) or "(no one-line learnings recorded)"
    source = _RULE_TEMPLATE.format(
        generated_at=now.isoformat(),
        profile_class=group.profile_class,
        action_type=group.action_type,
        total=group.total,
        successes=group.successes,
        success_rate=f"{group.success_rate:.2f}",
        examples=", ".join(example) or "(none)",
        evidence=doc_evidence.replace('"""', "'''"),
        fn_name=fn_name,
        rule_name=slug,
        priority=priority,
    )
    # Code AC: the graduation pipeline produces *valid Python*. Compiling here
    # makes that a hard guarantee — a malformed render raises before we write.
    compile(source, f"<proposal:{slug}>", "exec")
    return RuleProposal(
        slug=slug,
        profile_class=group.profile_class,
        action_type=group.action_type,
        sample_count=group.total,
        success_rate=group.success_rate,
        source=source,
        example_tickets=example,
    )


_RULE_TEMPLATE = '''"""DRAFT Tier-1 rule proposal — coordinator self-retrospective graduation.

AUTO-GENERATED by backend.agents.learning_loop on {generated_at}.
NOT ACTIVE: this file is an operator-review artifact (ADR-0021 §10.2). A human
reviews, fills in the predicate, and moves it into
``pipeline_coordinator_rules.py`` to graduate the pattern to Tier 1. It is
never auto-loaded or auto-merged (§13 risk table).

Observed pattern
----------------
- situation_profile_class : {profile_class}
- action_type             : {action_type}
- observed decisions      : {total} (successes={successes}, success_rate={success_rate})
- example tickets         : {examples}
- recorded learnings      : {evidence}
"""

from __future__ import annotations

from backend.agents.pipeline_coordinator_rules import (
    Action,
    DecisionContext,
    rule,
)


@rule(name="{rule_name}", priority={priority})
def {fn_name}(ctx: "DecisionContext") -> "Action | None":
    """DRAFT — abstains until an operator fills in the graduated predicate.

    Graduated from {total} consistent Tier-2 decisions on
    ``{action_type}`` for situation class ``{profile_class}``. The operator
    encodes the matching condition here, then returns the {action_type}
    Action (see the §6.1 factory constructors on :class:`Action`).
    """
    # TODO(operator): encode the predicate that fired this pattern in Tier 2,
    # then emit the graduated action. Until then this rule safely abstains.
    return None
'''


# ── Learning loop (daemon-facing seam) ──────────────────────────────────


@dataclass(frozen=True)
class WriteBackResult:
    decision_id: str
    written: bool


@dataclass
class DailyReport:
    """What one :meth:`LearningLoop.run_daily` produced."""

    written_back: list[WriteBackResult] = field(default_factory=list)
    outcomes: list[tuple[str, str]] = field(default_factory=list)  # (decision_id, outcome)

    @property
    def writeback_count(self) -> int:
        return sum(1 for w in self.written_back if w.written)

    @property
    def outcome_count(self) -> int:
        return len(self.outcomes)


@dataclass
class WeeklyReport:
    """What one :meth:`LearningLoop.run_weekly` produced."""

    groups_considered: int = 0
    proposals: list[RuleProposal] = field(default_factory=list)
    proposal_paths: list[Path] = field(default_factory=list)

    @property
    def proposal_count(self) -> int:
        return len(self.proposals)


# Seam the loop @-mentions through; default no-ops (the daemon's action layer
# owns real @-mentions). Signature mirrors Action.mention_operator inputs.
OperatorMention = Callable[[str, str, str], None]


class LearningLoop:
    """Daily write-back + outcome-check and weekly graduation (ADR §10).

    Collaborators are injected for deterministic tests; default construction
    lazily wires the Cognee write-back + jira_dispatch outcome observer. The
    loop only ever *reads* the decision log and *appends* its own
    write-back / outcome / graduation records — it never rewrites a line, so
    it is safe to run repeatedly and is idempotent (a decision whose outcome is
    already recorded is skipped).
    """

    version: str = LEARNING_LOOP_VERSION

    def __init__(
        self,
        *,
        decision_log: Any,
        reader: DecisionLogReader | None = None,
        writeback: WriteBackGateway | None = None,
        observer: OutcomeObserver | None = None,
        operator_mention: OperatorMention | None = None,
        config: LearningLoopConfig | None = None,
        clock: Clock = _utc_now,
        last_daily_at: datetime | None = None,
        last_weekly_at: datetime | None = None,
    ) -> None:
        self._log = decision_log
        self._config = config or LearningLoopConfig.from_env()
        self._reader = reader or DecisionLogReader(self._log_directory())
        self._writeback = writeback if writeback is not None else CogneeWriteBackGateway()
        self._observer = observer if observer is not None else JiraStateChangeObserver()
        self._operator_mention = operator_mention or (lambda _t, _m, _u: None)
        self._clock = clock
        self._last_daily_at = last_daily_at
        self._last_weekly_at = last_weekly_at

    def _log_directory(self) -> Path:
        directory = getattr(self._log, "directory", None)
        return Path(directory) if directory is not None else Path(".")

    @classmethod
    def from_decision_log(
        cls,
        decision_log: Any,
        *,
        config: LearningLoopConfig | None = None,
        clock: Clock = _utc_now,
        **kwargs: Any,
    ) -> "LearningLoop":
        """Rebuild the schedule from the log (§3.2 stateless-across-restarts)."""
        cfg = config or LearningLoopConfig.from_env()
        directory = getattr(decision_log, "directory", None)
        reader = DecisionLogReader(Path(directory) if directory is not None else Path("."))
        now = clock()
        lookback = now - timedelta(hours=max(cfg.weekly_interval_hours * 2, 24.0))
        last_daily = reader.last_event_ts(DAILY_RAN_EVENT, lookback=lookback, until=now)
        last_weekly = reader.last_event_ts(WEEKLY_RAN_EVENT, lookback=lookback, until=now)
        return cls(
            decision_log=decision_log,
            reader=reader,
            config=cfg,
            clock=clock,
            last_daily_at=last_daily,
            last_weekly_at=last_weekly,
            **kwargs,
        )

    # ── write-back (§10.1) ──

    def write_back_decision(self, record: DecisionRecord | Mapping[str, Any]) -> WriteBackResult:
        """Write one Tier-2 decision back to Cognee as a node (Integration AC).

        Called by the daemon on every Tier-2 tick (so write-back happens within
        the same tick → "within 60s"). A non-Tier-2 / malformed record is a
        no-op. Appends a ``learning_writeback`` receipt to the decision log so a
        later daily pass can tell which decisions are already in the KG.
        """
        rec = record if isinstance(record, DecisionRecord) else parse_decision_record(record)
        if rec is None or not rec.is_tier2:
            return WriteBackResult(decision_id=getattr(rec, "decision_id", ""), written=False)
        node = DecisionNode.from_record(rec)
        written = self._safe_write(node)
        self._append(
            {
                "event": WRITEBACK_EVENT,
                "decision_id": rec.decision_id,
                "written": written,
                "tickets": list(rec.tickets),
                "lessons_cited": list(rec.lessons_cited),
                "mode": rec.mode,
            }
        )
        return WriteBackResult(decision_id=rec.decision_id, written=written)

    def _safe_write(self, node: DecisionNode) -> bool:
        try:
            return bool(self._writeback.write_node(node))
        except Exception as exc:  # noqa: BLE001 — write-back is best-effort
            logger.info("[coord-learn] write-back gateway raised: %s", exc)
            return False

    # ── daily handler: catch-up write-back + 24h outcome-check (§10.1) ──

    def run_daily(self, now: datetime | None = None) -> DailyReport:
        """Write back any un-written Tier-2 decisions + run the 24h outcome-check."""
        now = now or self._clock()
        report = DailyReport()
        horizon_start = now - timedelta(hours=self._config.outcome_horizon_hours)

        # Catch-up write-back: any Tier-2 decision in the horizon with no
        # write-back receipt yet (e.g. the daemon was down when it was made).
        written_ids = {
            str(r.get("decision_id"))
            for r in self._reader.read_records(since=horizon_start, until=now)
            if r.get("event") == WRITEBACK_EVENT and r.get("written")
        }
        for rec in self._reader.decisions(since=horizon_start, until=now):
            if rec.is_tier2 and rec.decision_id and rec.decision_id not in written_ids:
                report.written_back.append(self.write_back_decision(rec))

        # Outcome-check: decisions old enough to be observable and not yet
        # checked. Idempotent via the recorded outcome-check ids.
        delay = timedelta(hours=self._config.outcome_delay_hours)
        already = self._reader.recorded_outcome_ids(since=horizon_start, until=now)
        for rec in self._reader.decisions(since=horizon_start, until=now):
            if not rec.is_tier2 or not rec.decision_id:
                continue
            if rec.decision_id in already:
                continue
            if now - rec.ts < delay:
                continue  # not yet observable
            verdict = self._safe_observe(rec)
            self._record_outcome(rec, verdict)
            report.outcomes.append((rec.decision_id, verdict.outcome))
        self._append({"event": DAILY_RAN_EVENT, "version": self.version})
        self._last_daily_at = now
        return report

    def _safe_observe(self, rec: DecisionRecord) -> OutcomeVerdict:
        try:
            return self._observer.observe(rec)
        except Exception as exc:  # noqa: BLE001 — observation must never break the pass
            logger.info("[coord-learn] observer raised for %s: %s", rec.decision_id, exc)
            return OutcomeVerdict(OUTCOME_UNKNOWN, str(exc))

    def _record_outcome(self, rec: DecisionRecord, verdict: OutcomeVerdict) -> None:
        # Append the verdict (§10.1) and write the now-resolved node back to the
        # KG so the decision's Cognee node carries its outcome edge.
        self._append(
            {
                "event": OUTCOME_CHECK_EVENT,
                "decision_id": rec.decision_id,
                "outcome": verdict.outcome,
                "detail": verdict.detail,
                "tickets": list(rec.tickets),
            }
        )
        resolved = DecisionNode.from_record(
            DecisionRecord(**{**rec.__dict__, "outcome": verdict.outcome})
        )
        self._safe_write(resolved)

    # ── weekly handler: self-retro → graduation (§10.2) ──

    def run_weekly(self, now: datetime | None = None) -> WeeklyReport:
        """Group by (profile_class, action_type); graduate consistent classes."""
        now = now or self._clock()
        window_start = now - timedelta(days=self._config.retro_window_days)
        outcomes = self._outcomes_in_window(since=window_start, until=now)
        decisions = [
            self._with_outcome(d, outcomes)
            for d in self._reader.decisions(since=window_start, until=now)
            if d.is_tier2
        ]
        groups = self._group(decisions)
        report = WeeklyReport(groups_considered=len(groups))
        for group in groups:
            if not self._graduates(group):
                continue
            try:
                proposal = render_rule_proposal(group, now=now)
            except Exception as exc:  # noqa: BLE001 — a bad render must not abort the retro
                logger.warning("[coord-learn] proposal render failed for %s: %s", group.profile_class, exc)
                continue
            path = self._write_proposal(proposal)
            report.proposals.append(proposal)
            report.proposal_paths.append(path)
            self._announce_graduation(proposal, path)
        self._append({"event": WEEKLY_RAN_EVENT, "version": self.version,
                      "groups": len(groups), "proposals": report.proposal_count})
        self._last_weekly_at = now
        return report

    def _outcomes_in_window(self, *, since: datetime, until: datetime) -> dict[str, str]:
        """Map decision_id → recorded outcome (latest wins) over the window.

        Outcome verdicts are appended as their own ``decision_outcome_check``
        records (the daily handler never rewrites the original tick line), so
        the weekly retro joins them back onto the decisions here.
        """
        out: dict[str, str] = {}
        for rec in self._reader.read_records(since=since, until=until):
            if rec.get("event") == OUTCOME_CHECK_EVENT and rec.get("decision_id"):
                out[str(rec["decision_id"])] = str(rec.get("outcome", OUTCOME_UNKNOWN))
        return out

    @staticmethod
    def _with_outcome(record: DecisionRecord, outcomes: Mapping[str, str]) -> DecisionRecord:
        outcome = outcomes.get(record.decision_id)
        if outcome is None or record.outcome is not None:
            return record
        return DecisionRecord(**{**record.__dict__, "outcome": outcome})

    @staticmethod
    def _group(decisions: Sequence[DecisionRecord]) -> list[RetroGroup]:
        buckets: dict[tuple[str, str], list[DecisionRecord]] = {}
        for d in decisions:
            for action_type in d.action_types or ("noop",):
                buckets.setdefault((d.profile_class, action_type), []).append(d)
        groups = [
            RetroGroup(profile_class=pc, action_type=at, decisions=tuple(ds))
            for (pc, at), ds in buckets.items()
        ]
        groups.sort(key=lambda g: (g.profile_class, g.action_type))
        return groups

    def _graduates(self, group: RetroGroup) -> bool:
        if group.total < self._config.graduation_min_samples:
            return False
        if group.failures > 0:
            return False
        observed = group.successes + group.failures
        if observed < self._config.graduation_min_samples:
            return False
        return group.success_rate >= self._config.graduation_min_success_rate

    def _write_proposal(self, proposal: RuleProposal) -> Path:
        directory = self._config.proposal_dir
        directory.mkdir(parents=True, exist_ok=True)
        init_path = directory / "__init__.py"
        if not init_path.exists():
            init_path.write_text(
                '"""Operator-reviewed draft Tier-1 rule proposals (ADR-0021 §10.2)."""\n',
                encoding="utf-8",
            )
        path = directory / proposal.filename
        path.write_text(proposal.source, encoding="utf-8")
        return path

    def _announce_graduation(self, proposal: RuleProposal, path: Path) -> None:
        ticket = proposal.example_tickets[0] if proposal.example_tickets else "OP"
        message = (
            f"[learning-loop] I'd like to graduate a pattern to Tier 1: "
            f"{proposal.sample_count} consistent decisions on "
            f"`{proposal.action_type}` for situation class "
            f"`{proposal.profile_class}` (success_rate={proposal.success_rate:.2f}). "
            f"Draft rule written to `coord_rule_proposals/{proposal.filename}` — "
            f"please review + merge or reject (ADR-0021 §10.2)."
        )
        self._append(
            {
                "event": GRADUATION_EVENT,
                "slug": proposal.slug,
                "profile_class": proposal.profile_class,
                "action_type": proposal.action_type,
                "sample_count": proposal.sample_count,
                "success_rate": round(proposal.success_rate, 4),
                "proposal_path": str(path),
            }
        )
        try:
            self._operator_mention(ticket, message, "medium")
        except Exception as exc:  # noqa: BLE001 — @-mention is best-effort
            logger.info("[coord-learn] operator mention failed: %s", exc)

    # ── scheduling (daemon ticks faster; these gate the handlers) ──

    def maybe_run(self, now: datetime | None = None) -> tuple[DailyReport | None, WeeklyReport | None]:
        """Run whichever handlers are due (daily/weekly), else no-op.

        The daemon calls this each tick; the cadence gates keep the handlers
        firing at most once per configured interval (mirrors the hourly-sweep
        timer). The first call seeds the baseline instants without firing, so a
        freshly-booted daemon does not immediately run a retro.
        """
        now = now or self._clock()
        daily = weekly = None
        if self._due(self._last_daily_at, self._config.daily_interval_hours, now):
            daily = self.run_daily(now)
        if self._due(self._last_weekly_at, self._config.weekly_interval_hours, now):
            weekly = self.run_weekly(now)
        return daily, weekly

    @staticmethod
    def _due(last_at: datetime | None, interval_hours: float, now: datetime) -> bool:
        if last_at is None:
            return False  # seed baseline on first observation; do not fire cold
        return (now - last_at).total_seconds() >= interval_hours * 3600.0

    def seed_schedule(self, now: datetime | None = None) -> None:
        """Seed the baseline run instants (called once at daemon startup)."""
        now = now or self._clock()
        if self._last_daily_at is None:
            self._last_daily_at = now
        if self._last_weekly_at is None:
            self._last_weekly_at = now

    # ── log append ──

    def _append(self, record: Mapping[str, Any]) -> None:
        base: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "engine_version": self.version,
            "pid": os.getpid(),
            "dry_run": True,
        }
        base.update(record)
        try:
            self._log.append(base)
        except Exception as exc:  # noqa: BLE001 — never let a log write break the loop
            logger.warning("[coord-learn] decision-log append failed: %s", exc)


def build_default_learning_loop(decision_log: Any) -> LearningLoop:
    """Production wiring: Cognee write-back + jira_dispatch observer + log schedule."""
    return LearningLoop.from_decision_log(decision_log, config=LearningLoopConfig.from_env())
