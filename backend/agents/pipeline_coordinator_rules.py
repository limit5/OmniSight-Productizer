"""Pipeline-coordinator decision-engine seam (29f-coord skeleton).

ADR-0021 §2.1 specifies a *hybrid* decision engine: deterministic Python
Tier-1 rules handle ≥90 % of decisions, with LLM consultation reserved for
novel / multi-plausible cases. None of that logic exists yet — this is the
foundation ticket (29f-coord). What ships here is the **contract** the real
rule set (29f-3), cold-start orchestrator (29f-7) and event subscribers
(29f-8) all plug into without changing the daemon:

    DecisionContext  — everything the engine is allowed to read for a tick
    DecisionEngine.evaluate(ctx) -> DecisionResult
    Action / NoopAction          — the action vocabulary
    DecisionResult               — actions + reason + decision_id

The skeleton engine is intentionally empty: it returns a single
``NoopAction`` with reason ``skeleton_noop`` and ``dry_run=True`` so the
daemon can run end-to-end (heartbeat + decision-log) before any real
decision logic exists.

Module-global state audit (per project SOP)
-------------------------------------------
Frozen dataclasses, a Protocol, and a stateless engine class. No module
globals, no import-time I/O. ``engine_version`` is a class constant so the
decision-log records which engine produced each tick.
"""

from __future__ import annotations

import inspect
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot
from backend.agents.pipeline_coordinator_modes import (
    ModeSelector,
    PersonalityMode,
    SituationProfile,
    mode_behavior,
)

logger = logging.getLogger(__name__)

# Bumped when the engine's decision semantics change. 0.x = skeleton era.
SKELETON_ENGINE_VERSION = "0.1.0-skeleton"

# Stable reason string the skeleton emits each tick (asserted by tests and
# grep-able in the decision log per ADR-0021 §9 L4).
SKELETON_NOOP_REASON = "skeleton_noop"

# ── Action vocabulary (ADR-0021 §6.1) ─────────────────────────────────
# The *closed* set of action kinds a rule (or the Tier-2 LLM) may emit.
# §6.1 deliberately keeps this list closed for safety; new kinds are added
# by reviewable operator patch (§12 open-question 6), never by an LLM
# proposal. Anything outside this set is dropped by the action layer.
ACTION_NOOP = "noop"
ACTION_FILE_TICKET = "file_ticket"
ACTION_RELABEL = "relabel"
ACTION_TRANSITION = "transition"
ACTION_MENTION_OPERATOR = "mention_operator"
ACTION_MARK_FOR_FOLLOWUP = "mark_for_followup"
ACTION_ESCALATE = "escalate"

ALLOWED_ACTION_KINDS: frozenset[str] = frozenset(
    {
        ACTION_NOOP,
        ACTION_FILE_TICKET,
        ACTION_RELABEL,
        ACTION_TRANSITION,
        ACTION_MENTION_OPERATOR,
        ACTION_MARK_FOR_FOLLOWUP,
        ACTION_ESCALATE,
    }
)


@dataclass(frozen=True)
class Action:
    """A single thing the coordinator decides to do.

    The skeleton only ever emits :class:`NoopAction`; the Tier-1 rule set
    (29f-3) emits the §6.1 action kinds via the factory classmethods below.
    The envelope is unchanged: ``kind`` is one of :data:`ALLOWED_ACTION_KINDS`,
    ``target`` is the JIRA key the action applies to, ``params`` carries the
    kind-specific payload, and ``dry_run`` defaults to True so a half-wired
    (or shadow-mode, 29f-14) daemon can never mutate JIRA/Gerrit.

    "Union type" (Code AC): :class:`Action` + :class:`NoopAction` over the
    constrained :data:`ALLOWED_ACTION_KINDS` *is* the action union — a closed
    set discriminated by ``kind`` rather than a ``typing.Union`` of N classes,
    so the decision-log serialisation (:meth:`to_record`) stays uniform.
    """

    kind: str
    target: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Action.kind must be a non-empty string")
        if self.kind not in ALLOWED_ACTION_KINDS:
            raise ValueError(
                f"Action.kind {self.kind!r} is not in the §6.1 allowed set "
                f"{sorted(ALLOWED_ACTION_KINDS)}"
            )
        object.__setattr__(self, "params", dict(self.params))

    def to_record(self) -> dict[str, Any]:
        """Serialisable form embedded in a decision-log ``actions`` array."""
        return {
            "kind": self.kind,
            "target": self.target,
            "params": dict(self.params),
            "dry_run": self.dry_run,
        }

    # ── §6.1 factory constructors ─────────────────────────────────────
    # Each rule emits one of these so payloads stay well-shaped (the action
    # layer reads fixed param keys). ``dry_run`` defaults True everywhere;
    # the action layer / shadow-mode toggle flips it, never a rule.

    @classmethod
    def file_ticket(
        cls,
        *,
        blocking: str,
        target_area: str,
        description: str,
        dry_run: bool = True,
    ) -> "Action":
        """§6.1 file_ticket — file a dependency/work ticket in another area."""
        return cls(
            kind=ACTION_FILE_TICKET,
            target=blocking,
            params={
                "blocking": blocking,
                "target_area": target_area,
                "description": description,
            },
            dry_run=dry_run,
        )

    @classmethod
    def relabel(
        cls,
        ticket: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        dry_run: bool = True,
    ) -> "Action":
        """§6.1 relabel — add/remove labels (never operator-only labels)."""
        return cls(
            kind=ACTION_RELABEL,
            target=ticket,
            params={"add": list(add), "remove": list(remove)},
            dry_run=dry_run,
        )

    @classmethod
    def transition(
        cls, ticket: str, *, to_status: str, dry_run: bool = True
    ) -> "Action":
        """§6.1 transition — move a ticket through an allowed status edge."""
        return cls(
            kind=ACTION_TRANSITION,
            target=ticket,
            params={"to_status": to_status},
            dry_run=dry_run,
        )

    @classmethod
    def mention_operator(
        cls,
        ticket: str,
        *,
        message: str,
        urgency: str = "medium",
        dry_run: bool = True,
    ) -> "Action":
        """§6.1 mention_operator — @-mention operator; high urgency tags too."""
        return cls(
            kind=ACTION_MENTION_OPERATOR,
            target=ticket,
            params={"message": message, "urgency": urgency},
            dry_run=dry_run,
        )

    @classmethod
    def mark_for_followup(
        cls, ticket: str, *, when: str, why: str, dry_run: bool = True
    ) -> "Action":
        """§6.1 mark_for_followup — defer attention via coord-resume-after."""
        return cls(
            kind=ACTION_MARK_FOR_FOLLOWUP,
            target=ticket,
            params={"when": when, "why": why},
            dry_run=dry_run,
        )

    @classmethod
    def escalate(cls, ticket: str, *, reason: str, dry_run: bool = True) -> "Action":
        """§6.1 escalate — needs-operator-action label + reason comment."""
        return cls(
            kind=ACTION_ESCALATE,
            target=ticket,
            params={"reason": reason},
            dry_run=dry_run,
        )


@dataclass(frozen=True)
class NoopAction(Action):
    """The do-nothing action the skeleton engine returns every tick."""

    def __init__(self) -> None:
        super().__init__(kind="noop", target="", params={}, dry_run=True)


@dataclass(frozen=True)
class DecisionContext:
    """Everything the engine may read to make one decision (a "tick").

    Stateless-across-restarts (ADR-0021 §3.2): the daemon re-builds this
    each tick from the injected clock + work-graph cache + capacity
    snapshot. The skeleton populates only ``now`` and ``capacity``; later
    phases add the JIRA work graph, bridge-log tail and event payload.
    """

    now: datetime
    capacity: CapacitySnapshot
    mode: str = "skeleton"
    # Per-situation personality inputs (ADR-0021 §7). The daemon attaches a
    # ``situation`` profile + any operator ``coord-mode:*`` override when a
    # tick is actually deciding about a ticket/event; an idle tick leaves
    # both ``None`` so the engine keeps the skeleton ``mode``.
    situation: SituationProfile | None = None
    mode_override: str | None = None
    # Open-ended bag for later phases (event payload, work-graph cache,
    # bridge-log tail). Skeleton leaves it empty.
    work_graph: Mapping[str, Any] = field(default_factory=dict)
    # 29f-3: the ticket this tick is reasoning about (the "focal" ticket).
    # ``None`` when the daemon ticks with an empty work-graph (skeleton /
    # idle); the Tier-1 rules each early-return ``None`` in that case.
    ticket: "Ticket | None" = None
    # The wider work-graph slice the cross-ticket rules traverse
    # (runner-blocked-marker-stale resolves the blocker's status;
    # chain-deadlock walks the blockedBy DAG). Keyed by JIRA key.
    tickets: Mapping[str, "Ticket"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("DecisionContext.now must be timezone-aware")
        object.__setattr__(self, "work_graph", dict(self.work_graph))
        object.__setattr__(self, "tickets", dict(self.tickets))


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of one ``evaluate`` call.

    ``decision_id`` is a fresh UUID per tick so a decision-log line can be
    correlated with downstream action records (and L6 crash-recovery can
    pair ``shutdown_began`` / ``shutdown_complete`` with the right tick).
    """

    decision_id: str
    actions: tuple[Action, ...]
    reason: str
    mode: str
    engine_version: str
    # The selected mode's behavior overlay (ADR-0021 §7.2), or ``None`` for
    # an idle/skeleton tick. Tier-1 rules + Tier-2 LLM read this to know how
    # hard to lean on rules vs. LLM, how wide to make the LLM context, and
    # whether to file a follow-up — the Integration-AC "see + respect the
    # selected mode" seam.
    mode_behavior: PersonalityMode | None = None
    # 29f-3: which Tier-1 rule produced this result (ADR Appendix C
    # ``rule_name``) and which tier decided. Default ``None``/``None`` keeps
    # the skeleton engine's construction (which names neither) valid.
    rule_name: str | None = None
    tier: int | None = None

    @property
    def is_noop(self) -> bool:
        return all(isinstance(a, NoopAction) for a in self.actions) or not self.actions


@runtime_checkable
class DecisionEngineProtocol(Protocol):
    """The seam 29f-3 implements; the daemon depends on this, not the class."""

    engine_version: str

    def evaluate(self, ctx: DecisionContext) -> DecisionResult: ...


class DecisionEngine:
    """Skeleton decision engine — selects a mode, then logs one no-op per tick.

    Real Tier-1 rules + Tier-2 LLM consultation land in 29f-3 / 29f-6. What
    AUDIT-29f-5 wires here is the **mode-selection-before-rule-evaluation**
    contract (ADR-0021 §7, Integration AC): when a tick carries a situation
    profile, the engine asks its :class:`ModeSelector` for the personality
    mode *before* any rule body would run, and threads the selected mode +
    its behavior overlay through the result so the (future) Tier-1 rules and
    Tier-2 LLM consultation can read + respect it. An idle tick (no
    ``situation``) keeps the skeleton ``mode`` so a no-op still logs "skeleton".

    Keeping the skeleton a concrete class (rather than only a Protocol) lets
    the daemon default-construct an engine and run end-to-end today.
    """

    engine_version: str = SKELETON_ENGINE_VERSION

    def __init__(self, *, mode_selector: ModeSelector | None = None) -> None:
        self._mode_selector = mode_selector or ModeSelector()

    def evaluate(self, ctx: DecisionContext) -> DecisionResult:
        """Select the mode, then return a single :class:`NoopAction`.

        Mode selection runs first (before any rule body) and is the only
        thing this skeleton engine decides; the action set stays a no-op
        until 29f-3 lands the Tier-1 rules. No I/O.
        """
        mode = ctx.mode
        if ctx.situation is not None:
            mode = self._mode_selector.select(ctx.situation, override=ctx.mode_override)
        behavior = mode_behavior(mode)
        # (29f-3 Tier-1 rules + 29f-6 Tier-2 LLM consultation read `behavior`
        #  here to steer rule-vs-LLM weighting, LLM context width, follow-up
        #  filing — then populate `actions`. The skeleton stays no-op.)
        return DecisionResult(
            decision_id=uuid.uuid4().hex,
            actions=(NoopAction(),),
            reason=SKELETON_NOOP_REASON,
            mode=mode,
            engine_version=self.engine_version,
            mode_behavior=behavior,
        )


# ══════════════════════════════════════════════════════════════════════
# Tier-1 deterministic rules (29f-3 / OP-1001) — ADR-0021 §5.1
# ══════════════════════════════════════════════════════════════════════
#
# The 10 rules below handle the ≥90% of decisions that don't need an LLM.
# Each is a *pure* function ``(DecisionContext) -> Action | None``:
#   - return an :class:`Action`  → this rule fires (first match wins)
#   - return ``None``            → rule abstains; the next rule (or Tier-2)
#                                  takes the situation
# Purity (no I/O, reads only the injected context) keeps them trivially
# unit-testable and replayable against the decision log.

TIER1_ENGINE_VERSION = "1.0.0-tier1"

# Reason string when the full registry abstains (→ Tier-2 LLM consult, 29f-6).
TIER1_NO_MATCH_REASON = "tier1_no_rule_matched"

# ── Domain vocabulary (kept in sync with docs/sop/jira-label-schema.yaml
#    and backend/agents/jira_dispatch.py status/label constants) ────────

# The closed `area:` taxonomy (jira-label-schema.yaml → area.value.enum).
KNOWN_AREAS: frozenset[str] = frozenset(
    {
        "backend",
        "frontend",
        "devops",
        "tests",
        "db",
        "docs",
        "security",
        "embedded",
        "tooling",
    }
)

# Operator-authority / trigger labels (ADR-0021 §4, §6.2, Appendix B).
COORD_SKIP_LABEL = "coord-skip"
NEEDS_COORDINATOR_LABEL = "needs-coordinator"
NEEDS_OPERATOR_ACTION_LABEL = "needs-operator-action"
COORD_QUARANTINE_LABEL = "coord-quarantine"

# Label prefixes (jira_dispatch: claim:default:*, runner-blocked:waiting-*).
CLAIM_LABEL_PREFIX = "claim:"
DEPENDENCY_WAITING_LABEL_PREFIX = "runner-blocked:waiting-"
CLASS_LABEL_PREFIX = "class:"
AREA_LABEL_PREFIX = "area:"
CAPABILITY_ENABLE_LABEL = "capability:enable=*"

# Comment markers the runner emits (jira_dispatch / auto-runner-jira.py).
DISCOVERED_DEPENDENCY_MARKER = "[runner-discovered-dependency]"
CAPABILITY_BLOCKED_MARKER = "[runner-capability-blocked]"

# JP-locale status names mirrored from jira_dispatch.*_STATUS_NAMES.
IN_PROGRESS_STATUS_NAMES: frozenset[str] = frozenset({"In Progress", "進行中"})
PUBLISHED_STATUS_NAMES: frozenset[str] = frozenset({"Published", "公開済み"})
# JP-locale Story issuetype (jira_dispatch §353 gotcha: `ストーリー`).
STORY_ISSUETYPE_NAMES: frozenset[str] = frozenset({"Story", "ストーリー"})

# Thresholds. The runner's CLI task timeout is the unit ADR-0021 §3.3/§5.1
# measures staleness in ("2× CLI timeout"); mirror auto-runner-jira's
# OMNISIGHT_RUNNER_TIMEOUT_S default (1800s) so the two stay aligned.
DEFAULT_CLI_TIMEOUT_SECONDS = float(
    os.environ.get("OMNISIGHT_RUNNER_TIMEOUT_S", "1800")
)
STALE_MULTIPLIER = 2.0
# merger-repeat-fail: escalate after > N attempts on the same ticket (§5.1 #4).
MERGER_FAIL_ESCALATE_THRESHOLD = 3
# revert-loop-quarantine: quarantine after > N reverts in 24h (§5.1 #5).
REVERT_LOOP_QUARANTINE_THRESHOLD = 5


# ── Work-graph data model (the read-only world a rule may inspect) ─────


@dataclass(frozen=True)
class Comment:
    """One JIRA comment, reduced to what the rules read."""

    text: str
    created: datetime | None = None


@dataclass(frozen=True)
class Ticket:
    """Read-only projection of a JIRA ticket for Tier-1 rule evaluation.

    29f-8 builds these from the live JIRA poll + bridge tail; the rules and
    their tests construct them directly. Every field is optional past ``key``
    so a partially-known ticket still type-checks — a rule that needs a field
    it doesn't have simply abstains.
    """

    key: str
    status: str = ""
    issuetype: str = ""
    areas: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    comments: tuple[Comment, ...] = ()
    # Wall-clock the ticket entered its current 進行中 state / acquired its
    # claim. Rules compare these against ``ctx.now`` for staleness.
    in_progress_since: datetime | None = None
    claim_started_at: datetime | None = None
    # Liveness: is a runner process currently working this ticket? (29f-8
    # resolves via /proc cmdline + worktree sentinel — ADR §3.3 Startup-2.)
    has_live_runner: bool = False
    # blockedBy DAG edges (L-OP-870 direction: this ticket is blocked by …).
    blocked_by: tuple[str, ...] = ()
    # Failure counters maintained from the decision log / incident rows.
    merger_fail_count: int = 0
    revert_count_24h: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("Ticket.key must be a non-empty string")


@dataclass(frozen=True)
class WorkGraph:
    """The per-tick world: a focal ticket + the slice rules may traverse.

    The daemon rebuilds this each tick (ADR §3.2 stateless-across-restarts).
    ``focal`` is the ticket this tick reasons about; ``tickets`` is the keyed
    slice the cross-ticket rules (runner-blocked-marker-stale, chain-deadlock)
    walk. An empty graph (skeleton / idle tick) makes every rule abstain.
    """

    focal: Ticket | None = None
    tickets: Mapping[str, Ticket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tickets", dict(self.tickets))


# ── Read helpers (pure; shared by the rules) ──────────────────────────


def has_label(ticket: Ticket, label: str) -> bool:
    return label in ticket.labels


def labels_with_prefix(ticket: Ticket, prefix: str) -> tuple[str, ...]:
    return tuple(l for l in ticket.labels if l.startswith(prefix))


def latest_comment(ticket: Ticket) -> Comment | None:
    """The most recent comment, or ``None``. Order = chronological input."""
    return ticket.comments[-1] if ticket.comments else None


def ticket_class(ticket: Ticket) -> str | None:
    """The runner class label value (``class:subscription-claude`` → that)."""
    classes = labels_with_prefix(ticket, CLASS_LABEL_PREFIX)
    return classes[0] if classes else None


def _age_seconds(now: datetime, since: datetime | None) -> float | None:
    if since is None:
        return None
    return (now - since).total_seconds()


def _stale_threshold_seconds() -> float:
    return DEFAULT_CLI_TIMEOUT_SECONDS * STALE_MULTIPLIER


# ── @rule decorator + registry (load-once, immutable) ─────────────────


@dataclass(frozen=True)
class RuleSpec:
    """Metadata the ``@rule`` decorator stamps onto a rule function."""

    name: str
    priority: int


def rule(*, name: str, priority: int) -> Callable[[Callable], Callable]:
    """Mark a function as a Tier-1 rule with a name + priority.

    Priority convention: **lower number = evaluated first = wins ties**.
    The decorator only attaches metadata (no mutable module-global registry —
    see the module-state audit note); :func:`load_registry` discovers the
    decorated functions explicitly and freezes them into an ordered tuple.
    """

    def deco(fn: Callable) -> Callable:
        if priority < 0:
            raise ValueError("rule priority must be non-negative")
        setattr(fn, "_coord_rule_spec", RuleSpec(name=name, priority=priority))
        return fn

    return deco


@dataclass(frozen=True)
class RegisteredRule:
    """A discovered rule: its spec + the callable, ready to evaluate."""

    name: str
    priority: int
    fn: Callable[[DecisionContext], "Action | None"]

    def evaluate(self, ctx: DecisionContext) -> "Action | None":
        return self.fn(ctx)


def load_registry(module: Any = None) -> tuple[RegisteredRule, ...]:
    """Discover every ``@rule``-decorated function and order it by priority.

    Returns an immutable tuple sorted by ``(priority, name)`` so evaluation
    order is deterministic. Raises on duplicate rule names (a copy-paste
    foot-gun) so the registry can't silently shadow a rule.
    """
    import sys

    mod = module if module is not None else sys.modules[__name__]
    found: list[RegisteredRule] = []
    seen: set[str] = set()
    for _, obj in inspect.getmembers(mod, inspect.isfunction):
        spec: RuleSpec | None = getattr(obj, "_coord_rule_spec", None)
        if spec is None:
            continue
        if spec.name in seen:
            raise ValueError(f"duplicate Tier-1 rule name: {spec.name!r}")
        seen.add(spec.name)
        found.append(RegisteredRule(name=spec.name, priority=spec.priority, fn=obj))
    found.sort(key=lambda r: (r.priority, r.name))
    return tuple(found)


# ══════════════════════════════════════════════════════════════════════
# The 10 rules (ADR-0021 §5.1). Priority bands (lower = first):
#   0   operator-keep-out          — absolute veto; must short-circuit all
#   5   chain-deadlock             — halt condition (should never happen)
#   10  revert-loop-quarantine     — runaway protection
#   20  merger-repeat-fail         — escalate stuck merger
#   30  stuck-in-progress          — interrupted-runner reconciliation
#   40  stale-claim-cleanup        — orphaned claim hygiene
#   50  runner-blocked-marker-stale— blocker already published
#   60  dependency-out-of-area     — reroute discovered cross-area dep
#   70  capability-blocked-known-locale — known capability workaround
#   80  wrong-class-routing        — soft reroute suggestion (comment only)
# The veto/halt/safety rules sit above the routing rules so a coord-skip or a
# deadlock is never overridden by a lower-stakes routing action.
# ══════════════════════════════════════════════════════════════════════


@rule(name="operator-keep-out", priority=0)
def operator_keep_out(ctx: DecisionContext) -> "Action | None":
    """§5.1 #10 — ``coord-skip`` present: take no action, ever.

    Returns a :class:`NoopAction` (not ``None``) so, sitting at priority 0,
    it short-circuits the whole registry: an operator escape-hatched ticket
    can never be touched by a lower-priority rule (ADR §6.2 self-modify ban,
    §6.3 operator override). Abstains (``None``) when there's no focal ticket
    or the label is absent, so other rules still run.
    """
    t = ctx.ticket
    if t is None or not has_label(t, COORD_SKIP_LABEL):
        return None
    return NoopAction()


@rule(name="chain-deadlock", priority=5)
def chain_deadlock(ctx: DecisionContext) -> "Action | None":
    """§5.1 #8 — A blocks B blocks A (or longer cycle): diagnose + escalate.

    Walks the blockedBy DAG from the focal ticket over ``ctx.tickets``. If
    the focal key is reachable from itself, that's a cycle — file a
    diagnostic + @-mention operator (this should never happen; if it does,
    halt rather than guess). Abstains when the graph is acyclic / unknown.
    """
    t = ctx.ticket
    if t is None:
        return None
    # DFS over blockedBy edges looking for a path back to the focal key.
    start = t.key
    stack = list(t.blocked_by)
    visited: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur == start:
            return Action.mention_operator(
                start,
                message=(
                    f"[chain-deadlock] blockedBy cycle detected through {start}; "
                    f"halting automated routing — operator triage required."
                ),
                urgency="high",
            )
        if cur in visited:
            continue
        visited.add(cur)
        nxt = ctx.tickets.get(cur)
        if nxt is not None:
            stack.extend(nxt.blocked_by)
    return None


@rule(name="revert-loop-quarantine", priority=10)
def revert_loop_quarantine(ctx: DecisionContext) -> "Action | None":
    """§5.1 #5 — reverted > 5× in 24h: add ``coord-quarantine`` + @operator."""
    t = ctx.ticket
    if t is None:
        return None
    if t.revert_count_24h <= REVERT_LOOP_QUARANTINE_THRESHOLD:
        return None
    if has_label(t, COORD_QUARANTINE_LABEL):
        return None  # already quarantined — don't re-fire each tick
    return Action.relabel(
        t.key,
        add=(COORD_QUARANTINE_LABEL, NEEDS_OPERATOR_ACTION_LABEL),
    )


@rule(name="merger-repeat-fail", priority=20)
def merger_repeat_fail(ctx: DecisionContext) -> "Action | None":
    """§5.1 #4 — merger failed > 3× on one ticket: escalate with context."""
    t = ctx.ticket
    if t is None:
        return None
    if t.merger_fail_count <= MERGER_FAIL_ESCALATE_THRESHOLD:
        return None
    if has_label(t, NEEDS_OPERATOR_ACTION_LABEL):
        return None  # already escalated
    return Action.escalate(
        t.key,
        reason=(
            f"[merger-repeat-fail] merger failed {t.merger_fail_count} times on "
            f"{t.key}; conflict is not auto-resolvable — operator merge needed."
        ),
    )


@rule(name="stuck-in-progress", priority=30)
def stuck_in_progress(ctx: DecisionContext) -> "Action | None":
    """§5.1 #7 — 進行中 > 2× CLI timeout, no live runner: reconcile to To Do.

    The single-ticket equivalent of Phase Startup-2's "no commits, no recent
    activity → restart cleanly": revert to To Do + clear the claim so a fresh
    runner can re-pick it. Abstains when a runner is alive (it's handling) or
    the ticket isn't actually stale.
    """
    t = ctx.ticket
    if t is None or t.status not in IN_PROGRESS_STATUS_NAMES:
        return None
    if t.has_live_runner:
        return None
    age = _age_seconds(ctx.now, t.in_progress_since)
    if age is None or age <= _stale_threshold_seconds():
        return None
    return Action.transition(t.key, to_status="To Do")


@rule(name="stale-claim-cleanup", priority=40)
def stale_claim_cleanup(ctx: DecisionContext) -> "Action | None":
    """§5.1 #2 — ``claim:default:*`` > 2× CLI timeout, no live runner: clear.

    Removes the orphaned claim label(s) (ADR §3.3 Startup-3 stale-state sweep
    for a single ticket). Assignee clearing rides along in the action layer's
    relabel handler. Abstains while a runner is alive or the claim is fresh.
    """
    t = ctx.ticket
    if t is None or t.has_live_runner:
        return None
    claim_labels = labels_with_prefix(t, CLAIM_LABEL_PREFIX)
    if not claim_labels:
        return None
    age = _age_seconds(ctx.now, t.claim_started_at)
    if age is None or age <= _stale_threshold_seconds():
        return None
    return Action.relabel(t.key, remove=claim_labels)


@rule(name="runner-blocked-marker-stale", priority=50)
def runner_blocked_marker_stale(ctx: DecisionContext) -> "Action | None":
    """§5.1 #3 — ``runner-blocked:waiting-X`` where X is 公開済み: drop marker.

    Resolves each ``runner-blocked:waiting-<KEY>`` against ``ctx.tickets``;
    any blocker already Published is a dead wait — remove that marker so the
    blocked ticket is pickable again. Abstains if blockers are unknown or
    still open.
    """
    t = ctx.ticket
    if t is None:
        return None
    waiting = labels_with_prefix(t, DEPENDENCY_WAITING_LABEL_PREFIX)
    stale: list[str] = []
    for label in waiting:
        blocker_key = label[len(DEPENDENCY_WAITING_LABEL_PREFIX):]
        blocker = ctx.tickets.get(blocker_key)
        if blocker is not None and blocker.status in PUBLISHED_STATUS_NAMES:
            stale.append(label)
    if not stale:
        return None
    return Action.relabel(t.key, remove=tuple(stale))


@rule(name="dependency-out-of-area", priority=60)
def dependency_out_of_area(ctx: DecisionContext) -> "Action | None":
    """§5.1 #1 — runner discovered a dependency in another known area.

    Gated on ``needs-coordinator`` + a ``[runner-discovered-dependency]``
    marker in the latest comment. Parses the named area; if it's a known
    out-of-area domain, file a dependency ticket there. Unknown / unparseable
    → abstain (Tier-2 takes it).
    """
    t = ctx.ticket
    if t is None or not has_label(t, NEEDS_COORDINATOR_LABEL):
        return None
    comment = latest_comment(t)
    if comment is None or DISCOVERED_DEPENDENCY_MARKER not in comment.text:
        return None
    target_area = _parse_discovered_area(comment.text, ticket_areas=t.areas)
    if target_area is None:
        return None
    return Action.file_ticket(
        blocking=t.key,
        target_area=target_area,
        description=(
            f"Dependency discovered by runner on {t.key} requiring "
            f"area:{target_area} work. See {t.key} discovered-dependency note."
        ),
    )


def _parse_discovered_area(text: str, *, ticket_areas: tuple[str, ...]) -> str | None:
    """Extract a known out-of-area name from a discovered-dependency comment.

    Conservative: returns an area only when exactly one *known* area outside
    the ticket's own area(s) is named. Zero or multiple candidates → ``None``
    (defer to Tier-2) so we never reroute on an ambiguous mention.
    """
    own = {a.lower() for a in ticket_areas}
    lowered = text.lower()
    candidates = {
        area
        for area in KNOWN_AREAS
        if area not in own and _mentions_area(lowered, area)
    }
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _mentions_area(lowered_text: str, area: str) -> bool:
    """True if ``area`` is named as ``area:<area>`` or a whole word."""
    if f"{AREA_LABEL_PREFIX}{area}" in lowered_text:
        return True
    import re

    return re.search(rf"\b{re.escape(area)}\b", lowered_text) is not None


@rule(name="capability-blocked-known-locale", priority=70)
def capability_blocked_known_locale(ctx: DecisionContext) -> "Action | None":
    """§5.1 #6 — ``[runner-capability-blocked]`` on a ストーリー: apply workaround.

    Until AUDIT-27 wires capability resolution properly, a capability-blocked
    Story gets the ``capability:enable=*`` operator-escape-hatch label applied
    automatically. Restricted to the Story issuetype (the observed pattern);
    other issuetypes defer to Tier-2.
    """
    t = ctx.ticket
    if t is None or t.issuetype not in STORY_ISSUETYPE_NAMES:
        return None
    comment = latest_comment(t)
    if comment is None or CAPABILITY_BLOCKED_MARKER not in comment.text:
        return None
    if has_label(t, CAPABILITY_ENABLE_LABEL):
        return None  # workaround already applied
    return Action.relabel(t.key, add=(CAPABILITY_ENABLE_LABEL,))


@rule(name="wrong-class-routing", priority=80)
def wrong_class_routing(ctx: DecisionContext) -> "Action | None":
    """§5.1 #9 — backend ticket on subscription-claude while codex idle.

    Comment-only suggestion (ADR is explicit: "don't relabel without LLM
    consult"). Fires when the focal ticket is strongly area:backend, currently
    ``class:subscription-claude``, and the capacity snapshot shows a codex
    runner with free slots. Emits a low-urgency operator note, never a
    relabel.
    """
    t = ctx.ticket
    if t is None:
        return None
    if "backend" not in {a.lower() for a in t.areas}:
        return None
    if ticket_class(t) != f"{CLASS_LABEL_PREFIX}subscription-claude":
        return None
    if not _codex_has_capacity(ctx.capacity):
        return None
    return Action.mention_operator(
        t.key,
        message=(
            f"[wrong-class-routing] {t.key} is area:backend on "
            f"class:subscription-claude while a codex runner is idle; "
            f"consider rerouting to subscription-codex (suggestion only)."
        ),
        urgency="low",
    )


def _codex_has_capacity(capacity: CapacitySnapshot) -> bool:
    for runner_class, row in capacity.runners.items():
        if "codex" in runner_class.lower() and row.free_slots > 0:
            return True
    return False


# ── Tier-1 rule engine (implements DecisionEngineProtocol) ─────────────


class Tier1RuleEngine:
    """Deterministic Tier-1 engine: first matching rule (by priority) wins.

    Drop-in for the skeleton :class:`DecisionEngine` — same ``engine_version``
    + ``evaluate`` seam (:class:`DecisionEngineProtocol`). Loads the rule
    registry once at construction and logs a startup line naming every loaded
    rule (Deploy AC: "rule registry loaded at startup, visible in startup
    log line"). When no rule matches, returns a no-op with
    :data:`TIER1_NO_MATCH_REASON` — the situation that Tier-2 LLM consult
    (29f-6) will pick up.
    """

    engine_version: str = TIER1_ENGINE_VERSION

    def __init__(self, registry: tuple[RegisteredRule, ...] | None = None) -> None:
        self._rules = registry if registry is not None else load_registry()
        logger.info(
            "[pipeline_coordinator] Tier-1 rule registry loaded: %d rules "
            "(engine=%s) [%s]",
            len(self._rules),
            self.engine_version,
            ", ".join(f"{r.name}@{r.priority}" for r in self._rules),
        )

    @property
    def rules(self) -> tuple[RegisteredRule, ...]:
        return self._rules

    def rule_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self._rules)

    def evaluate(self, ctx: DecisionContext) -> DecisionResult:
        """Iterate the registry in priority order; first Action wins."""
        for r in self._rules:
            action = r.evaluate(ctx)
            if action is None:
                continue
            return DecisionResult(
                decision_id=uuid.uuid4().hex,
                actions=(action,),
                reason=f"tier1:{r.name}",
                mode=ctx.mode,
                engine_version=self.engine_version,
                rule_name=r.name,
                tier=1,
            )
        # No Tier-1 rule matched → Tier-2 LLM consult territory (29f-6).
        return DecisionResult(
            decision_id=uuid.uuid4().hex,
            actions=(NoopAction(),),
            reason=TIER1_NO_MATCH_REASON,
            mode=ctx.mode,
            engine_version=self.engine_version,
            rule_name=None,
            tier=1,
        )
