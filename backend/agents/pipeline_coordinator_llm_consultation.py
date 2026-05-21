"""Pipeline-coordinator Tier-2 LLM consultation (AUDIT-29f-6 / OP-1004).

ADR-0021 §2.1 specifies a *hybrid* decision engine: deterministic Python
Tier-1 rules (``pipeline_coordinator_rules.py``) handle ≥90 % of decisions;
the remaining novel / multi-plausible / cross-cutting situations escalate to
an **LLM consultation** (Tier 2). This module owns that Tier-2 path — the
piece ADR §13 calls "Tier 2 LLM consultation":

    LLMConsultationConfig — env-driven model / timeout / daily-budget knobs
    BudgetGuard           — daily USD cap (§5.3); degrade-to-Tier-1 on hit
    build_context_bundle  — the 7-section consultation prompt (§5.2)
    CliLLMRunner          — claude CLI invocation via subprocess + timeout
    parse_cli_envelope /  — strict JSON parse of the CLI + decision envelopes;
      parse_decision_json   invalid → escalate-operator (Code AC)
    validate_actions      — only ADR §6.1 allowed kinds survive; rest dropped
    Tier2Consultant       — assemble bundle → invoke CLI → parse → validate →
                            DecisionResult(tier=2), with budget guard + cost
                            telemetry threaded into the decision log
    HybridDecisionEngine  — Tier-1 first; Tier-2 when rules abstain / conflict /
                            the mode demands it (Integration AC)

Stateless-across-restarts (ADR §3.2): the consultant holds no durable state;
:class:`BudgetGuard` reconstructs the day's spend from the append-only
decision log (:meth:`BudgetGuard.from_decision_log`) rather than a private
counter file, so a restart mid-day does not reset the cap.

Module-global state audit (per project SOP)
-------------------------------------------
Immutable constants, frozen dataclasses, a Protocol, and classes that hold
only injected collaborators. No module globals are mutated at runtime and no
I/O happens at import time. The only stateful object is :class:`BudgetGuard`,
whose accumulator is instance-local and keyed by UTC date (so a new day
resets the cap without any process-global mutation). Default construction
wires the production claude CLI runner + Cognee lesson recall; tests inject
stubs for both so the unit suite never spawns a subprocess or touches Neo4j.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from backend.agents.pipeline_coordinator_modes import (
    ModeSelector,
    PersonalityMode,
    mode_behavior,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_ESCALATE,
    ACTION_FILE_TICKET,
    ACTION_MARK_FOR_FOLLOWUP,
    ACTION_MENTION_OPERATOR,
    ACTION_NOOP,
    ACTION_RELABEL,
    ACTION_TRANSITION,
    ALLOWED_ACTION_KINDS,
    Action,
    DecisionContext,
    DecisionResult,
    NoopAction,
    RegisteredRule,
    Ticket,
    Tier1RuleEngine,
)

logger = logging.getLogger(__name__)

# Bumped when Tier-2 decision semantics change. 2.x = LLM-consult era.
TIER2_ENGINE_VERSION = "2.0.0-tier2"
# The hybrid engine that fronts both tiers (its own version string so the
# decision log records "which engine produced this tick").
HYBRID_ENGINE_VERSION = "2.0.0-hybrid"

# ── Reason strings (grep-able in the decision log, ADR §9 L4) ──────────
# Tier-2 produced an LLM-consulted decision.
TIER2_CONSULTED_REASON = "tier2_llm_consulted"
# The LLM response could not be strict-parsed → escalate to operator.
TIER2_PARSE_FAILED_REASON = "tier2_llm_parse_failed"
# The CLI invocation itself failed (non-zero exit / timeout) → escalate.
TIER2_INVOCATION_FAILED_REASON = "tier2_llm_invocation_failed"
# Daily budget cap hit → degrade to Tier-1-only + operator alert (§5.3).
TIER2_DEGRADED_BUDGET_REASON = "tier2_degraded_budget_cap"
# OP-1556: an idle / empty-work-graph tick (no focal ticket AND no work-graph
# slice) has nothing to decide → Tier-1 NoopAction, never a Tier-2 consult.
# Distinguishes "nothing to decide" from "something to decide, no rule matched"
# (which still consults). Grep-able so the audit can confirm idle ticks no
# longer show up as tier=2 / tier1_no_match in the decision log.
TIER1_IDLE_NOOP_REASON = "tier1_idle_noop_short_circuit"

# ── Budget / CLI config (ADR §5.3) ─────────────────────────────────────
DAILY_BUDGET_ENV = "OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD"
MODEL_ENV = "OMNISIGHT_COORDINATOR_LLM_MODEL"
CLI_TIMEOUT_ENV = "OMNISIGHT_COORDINATOR_LLM_TIMEOUT_S"
CLI_PATH_ENV = "OMNISIGHT_COORDINATOR_CLAUDE_CLI"

# Conservative default cap so an un-configured deploy can't run away (§13.2
# "LLM budget runaway" risk). Operator raises it via the env var.
DEFAULT_DAILY_BUDGET_USD = 5.0
# claude (subscription) is the Phase-6 provider (ADR §12 open-q 5).
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_CLI_TIMEOUT_S = 90.0
DEFAULT_CLI_PATH = "claude"
# Pre-consult per-decision estimate (ADR §11: "~$0.20-1.00 per decision").
# Used by the budget pre-check (refuse a consult that *would* breach the cap)
# and by the Exercised-AC "$/decision matches estimate within 50%" telemetry.
DEFAULT_PER_DECISION_ESTIMATE_USD = 0.20

# The closed set of action *names* the LLM may emit (ADR §5.2 output block).
# Maps each name to the §6.1 allowed-kind constant; anything outside this map
# is dropped by :func:`validate_actions` (Code AC: only §6.1 actions execute).
_LLM_ACTION_NAME_TO_KIND: Mapping[str, str] = {
    "file_ticket": ACTION_FILE_TICKET,
    "relabel": ACTION_RELABEL,
    "transition": ACTION_TRANSITION,
    "mention_operator": ACTION_MENTION_OPERATOR,
    "mark_for_followup": ACTION_MARK_FOR_FOLLOWUP,
    "escalate": ACTION_ESCALATE,
    "noop": ACTION_NOOP,
}

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


Clock = Callable[[], datetime]


# ── Config ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMConsultationConfig:
    """Model / timeout / budget knobs for Tier-2 consultation (ADR §5.3)."""

    model: str = DEFAULT_MODEL
    cli_path: str = DEFAULT_CLI_PATH
    cli_timeout_s: float = DEFAULT_CLI_TIMEOUT_S
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD
    per_decision_estimate_usd: float = DEFAULT_PER_DECISION_ESTIMATE_USD

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConsultationConfig":
        env = env if env is not None else os.environ
        return cls(
            model=env.get(MODEL_ENV, DEFAULT_MODEL),
            cli_path=env.get(CLI_PATH_ENV, DEFAULT_CLI_PATH),
            cli_timeout_s=_float_env(env, CLI_TIMEOUT_ENV, DEFAULT_CLI_TIMEOUT_S),
            daily_budget_usd=_float_env(env, DAILY_BUDGET_ENV, DEFAULT_DAILY_BUDGET_USD),
        )


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[coord-llm] ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


# ── Budget guard (ADR §5.3) ────────────────────────────────────────────


class BudgetGuard:
    """Tracks Tier-2 LLM spend against a per-UTC-day USD cap (ADR §5.3).

    ``daily_budget_usd <= 0`` disables Tier-2 entirely (the guard reports
    exhausted from the first check) — an operator kill-switch. Spend is keyed
    by UTC date so the cap resets at the day boundary without any external
    scheduler. The accumulator is instance-local (module-state audit): no
    process-global counter, no counter file. To survive a mid-day restart,
    construct via :meth:`from_decision_log`, which re-sums the day's recorded
    consultation costs from the append-only log (ADR §3.2 stateless rebuild).
    """

    def __init__(
        self,
        daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
        *,
        clock: Clock = _utc_now,
        spent_today: float = 0.0,
    ) -> None:
        self._cap = float(daily_budget_usd)
        self._clock = clock
        self._spent: dict[date, float] = {}
        if spent_today:
            self._spent[self._today()] = float(spent_today)

    @classmethod
    def from_env(cls, *, clock: Clock = _utc_now) -> "BudgetGuard":
        return cls(LLMConsultationConfig.from_env().daily_budget_usd, clock=clock)

    @classmethod
    def from_decision_log(
        cls,
        decision_log_dir: Path,
        *,
        daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
        clock: Clock = _utc_now,
    ) -> "BudgetGuard":
        """Rebuild today's spend from the append-only decision log.

        Sums ``llm_consultation.cost_usd`` over every ``decision_tick`` whose
        ``ts`` falls on the current UTC day, so a coordinator restarted at
        noon resumes with the morning's spend already counted against the cap.
        """
        now = clock()
        spent = _sum_consultation_cost_for_day(decision_log_dir, now.date())
        return cls(daily_budget_usd, clock=clock, spent_today=spent)

    def _today(self) -> date:
        return self._clock().date()

    @property
    def cap_usd(self) -> float:
        return self._cap

    def spent_usd(self) -> float:
        return self._spent.get(self._today(), 0.0)

    def remaining_usd(self) -> float:
        return self._cap - self.spent_usd()

    @property
    def exhausted(self) -> bool:
        """True when no further consult can run today (cap reached / disabled)."""
        return self.remaining_usd() <= 0.0

    def can_afford(self, estimate_usd: float) -> bool:
        """Whether a consult estimated at ``estimate_usd`` fits under the cap."""
        if self._cap <= 0.0:
            return False
        return self.spent_usd() + max(0.0, estimate_usd) <= self._cap

    def record(self, cost_usd: float) -> None:
        """Charge ``cost_usd`` against today's budget."""
        today = self._today()
        self._spent[today] = self._spent.get(today, 0.0) + max(0.0, float(cost_usd))


def _sum_consultation_cost_for_day(decision_log_dir: Path, day: date) -> float:
    """Sum recorded Tier-2 consultation costs for ``day`` from the log file."""
    path = Path(decision_log_dir) / f"{day.isoformat()}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    total = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        consult = record.get("llm_consultation") if isinstance(record, dict) else None
        if isinstance(consult, Mapping):
            try:
                total += float(consult.get("cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
    return total


# ── Recalled-lesson value object (29b / Cognee) ────────────────────────


@dataclass(frozen=True)
class RecalledLesson:
    """One lesson surfaced by Cognee recall for the consultation bundle."""

    identifier: str
    summary: str
    score: float = 0.0


# Provider seams (injected for deterministic tests). Each returns plain data
# the bundle renders; defaults wire the real collaborators in production.
LessonsProvider = Callable[[DecisionContext, int], Sequence[RecalledLesson]]
OperatorPrefsProvider = Callable[[DecisionContext], Mapping[str, Any]]
RecentDecisionsProvider = Callable[[DecisionContext], Sequence[Mapping[str, Any]]]


# ── 7-section context bundle (ADR §5.2) ────────────────────────────────


@dataclass(frozen=True)
class ContextBundle:
    """The assembled Tier-2 consultation prompt — 7 sections per ADR §5.2.

    ``render()`` produces the prompt text fed to the claude CLI; ``sections``
    names each section in order so a test can assert all 7 are present and the
    daemon can log which were populated. ``lessons`` is retained separately so
    the consultant can record *which* lessons were cited (Integration AC:
    "Cognee lesson recall observable in Tier-2 context bundle (logged)").
    """

    sections: tuple[str, ...]
    body: str
    lessons: tuple[RecalledLesson, ...]

    def render(self) -> str:
        return self.body


# The 7 canonical section titles (ADR §5.2). Order is load-bearing — the
# Code-AC test asserts exactly these seven, in this order.
SECTION_TITLES: tuple[str, ...] = (
    "## Situation",
    "## Work-graph slice",
    "## Runner state",
    "## Relevant lessons (Cognee top-N by similarity)",
    "## Operator preferences (from memory)",
    "## Available actions",
    "# Output format (strict JSON)",
)


def build_context_bundle(
    ctx: DecisionContext,
    *,
    mode_behavior: PersonalityMode | None,
    lessons: Sequence[RecalledLesson] = (),
    operator_prefs: Mapping[str, Any] | None = None,
    recent_decisions: Sequence[Mapping[str, Any]] = (),
) -> ContextBundle:
    """Assemble the 7-section LLM consultation context (ADR §5.2).

    Each section is rendered even when its data is empty (so the LLM always
    sees the full, predictable frame and the section count is stable). The
    ``## Work-graph slice`` walks the focal ticket's ``blocked_by`` DAG up to
    the mode's ``llm_context_hops`` radius; ``## Relevant lessons`` lists the
    Cognee-recalled lessons the caller passed in.
    """
    hops = mode_behavior.llm_context_hops if mode_behavior is not None else 2
    parts: list[str] = ["# Coordinator LLM consultation context", ""]

    parts.append(SECTION_TITLES[0])
    parts.append(_render_situation(ctx))
    parts.append("")

    parts.append(SECTION_TITLES[1])
    parts.append(_render_work_graph(ctx, hops=hops, recent_decisions=recent_decisions))
    parts.append("")

    parts.append(SECTION_TITLES[2])
    parts.append(_render_runner_state(ctx))
    parts.append("")

    parts.append(SECTION_TITLES[3])
    parts.append(_render_lessons(lessons))
    parts.append("")

    parts.append(SECTION_TITLES[4])
    parts.append(_render_operator_prefs(operator_prefs or {}))
    parts.append("")

    parts.append(SECTION_TITLES[5])
    parts.append(_AVAILABLE_ACTIONS_BLOCK)
    parts.append("")

    parts.append(SECTION_TITLES[6])
    parts.append(_OUTPUT_FORMAT_BLOCK)

    return ContextBundle(
        sections=SECTION_TITLES,
        body="\n".join(parts).strip() + "\n",
        lessons=tuple(lessons),
    )


def _render_situation(ctx: DecisionContext) -> str:
    profile = ctx.situation.to_record() if ctx.situation is not None else {}
    return (
        f"personality_mode: {ctx.mode}\n"
        f"situation_profile: {json.dumps(profile, sort_keys=True)}"
    )


def _render_work_graph(
    ctx: DecisionContext,
    *,
    hops: int,
    recent_decisions: Sequence[Mapping[str, Any]],
) -> str:
    focal = ctx.ticket
    if focal is None:
        return "focal_ticket: (none — sprint-level / idle consult)"
    lines = [f"focal_ticket: {_ticket_brief(focal)}"]
    chain = _blocked_by_chain(focal, ctx.tickets, hops=hops)
    if chain:
        lines.append(f"blockedBy_chain ({hops}-hop):")
        for key in chain:
            blocker = ctx.tickets.get(key)
            lines.append(f"  - {_ticket_brief(blocker) if blocker else key}")
    if recent_decisions:
        lines.append("recent_decisions (last 7d):")
        for entry in recent_decisions:
            lines.append(f"  - {json.dumps(dict(entry), sort_keys=True)}")
    return "\n".join(lines)


def _blocked_by_chain(
    focal: Ticket,
    tickets: Mapping[str, Ticket],
    *,
    hops: int,
) -> list[str]:
    """BFS over ``blocked_by`` edges out to ``hops`` levels (cycle-safe)."""
    out: list[str] = []
    seen: set[str] = {focal.key}
    frontier = list(focal.blocked_by)
    depth = 0
    while frontier and depth < max(0, hops):
        nxt: list[str] = []
        for key in frontier:
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            blocker = tickets.get(key)
            if blocker is not None:
                nxt.extend(blocker.blocked_by)
        frontier = nxt
        depth += 1
    return out


def _ticket_brief(ticket: Ticket) -> str:
    return (
        f"{ticket.key} [status={ticket.status or '?'} type={ticket.issuetype or '?'} "
        f"areas={','.join(ticket.areas) or '-'} labels={','.join(ticket.labels) or '-'}]"
    )


def _render_runner_state(ctx: DecisionContext) -> str:
    cap = ctx.capacity
    if not cap.runners:
        return "capacity: (no runner telemetry available)"
    lines = [f"captured_at: {cap.captured_at.isoformat()}", "by_class:"]
    for name, row in sorted(cap.runners.items()):
        lines.append(
            f"  - {name}: free_slots={row.free_slots} "
            f"tokens_week={row.tokens_in_current_week}/{row.weekly_cap}"
        )
    return "\n".join(lines)


def _render_lessons(lessons: Sequence[RecalledLesson]) -> str:
    if not lessons:
        return "(no lessons recalled — Cognee unavailable or no match)"
    return "\n".join(
        f"- {lesson.identifier} (score={lesson.score:.4f}): {lesson.summary}"
        for lesson in lessons
    )


def _render_operator_prefs(prefs: Mapping[str, Any]) -> str:
    if not prefs:
        return "(no operator preferences on record)"
    return json.dumps(dict(prefs), sort_keys=True)


_AVAILABLE_ACTIONS_BLOCK = (
    "You may instruct the coordinator to do ONLY these actions; any other\n"
    "action name is dropped:\n"
    "- file_ticket(blocking, target_area, description)\n"
    "- relabel(ticket, add=[...], remove=[...])\n"
    "- transition(ticket, to_status)\n"
    "- mention_operator(ticket, message, urgency)\n"
    "- mark_for_followup(ticket, when, why)\n"
    "- escalate(ticket, reason)\n"
    "- noop(reason)"
)

_OUTPUT_FORMAT_BLOCK = (
    "Respond with a SINGLE strict-JSON object and nothing else:\n"
    "{\n"
    '  "decision_rationale": "...",\n'
    '  "actions": [{"action": "relabel", "ticket": "OP-XXX", "params": {...}}, ...],\n'
    '  "confidence": "high|medium|low",\n'
    '  "escalate_if_wrong": true,\n'
    '  "learning": "<one-line lesson to write back if action succeeds>"\n'
    "}"
)


# ── CLI invocation (ADR §5.2: "claude CLI invocation via subprocess") ──


@dataclass(frozen=True)
class LLMInvocation:
    """Raw outcome of one claude CLI invocation (before any parsing)."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@runtime_checkable
class LLMRunner(Protocol):
    """The CLI seam the consultant depends on (injected as a stub in tests)."""

    def invoke(self, prompt: str) -> LLMInvocation: ...


class CliLLMRunner:
    """Invoke the ``claude`` CLI in print mode with strict JSON output.

    Mirrors ``provider_adapters.anthropic_subscription`` — prompt over stdin,
    ``--output-format json`` so the envelope carries ``total_cost_usd`` +
    ``usage``, killed at ``timeout_s`` so a hung consultation can never wedge
    the daemon (ADR §5.2 "subprocess with timeout"; §9 L5 drain max-wait).
    """

    def __init__(self, config: LLMConsultationConfig | None = None) -> None:
        self._config = config or LLMConsultationConfig.from_env()

    def invoke(self, prompt: str) -> LLMInvocation:
        argv = [
            self._config.cli_path,
            "-p",
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--model",
            self._config.model,
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return LLMInvocation(stdout="", stderr=str(exc), returncode=127)
        try:
            stdout, stderr = proc.communicate(prompt, timeout=self._config.cli_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return LLMInvocation(
                stdout=stdout or "", stderr=stderr or "", returncode=-1, timed_out=True
            )
        return LLMInvocation(stdout=stdout or "", stderr=stderr or "", returncode=proc.returncode)


# ── Response parsing (Code AC: strict-parse; invalid → escalate) ───────


class LLMResponseInvalid(ValueError):
    """The CLI / decision JSON could not be strict-parsed."""


@dataclass(frozen=True)
class CliEnvelope:
    """The claude CLI ``--output-format json`` envelope, reduced.

    ``result_text`` is the assistant's reply (which must itself be the strict
    decision JSON); ``cost_usd`` / token counts come from the CLI's own
    accounting so cost telemetry (Exercised AC) reflects real spend.
    """

    result_text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    is_error: bool = False


def parse_cli_envelope(stdout: str) -> CliEnvelope:
    """Parse the CLI JSON envelope; raise :class:`LLMResponseInvalid` if absent.

    A bare (non-JSON) stdout is treated as the raw result text with unknown
    cost — so a CLI configured for plain output still flows to the decision
    parser rather than hard-failing here.
    """
    text = (stdout or "").strip()
    if not text:
        raise LLMResponseInvalid("empty CLI stdout")
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        # Not an envelope — hand the raw text on as the candidate decision.
        return CliEnvelope(result_text=text, cost_usd=0.0, input_tokens=0, output_tokens=0)
    if not isinstance(envelope, Mapping):
        raise LLMResponseInvalid("CLI envelope is not a JSON object")
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), Mapping) else {}
    return CliEnvelope(
        result_text=str(envelope.get("result", "") or ""),
        cost_usd=float(envelope.get("total_cost_usd", 0.0) or 0.0),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        is_error=bool(envelope.get("is_error", False)),
    )


@dataclass(frozen=True)
class ParsedDecision:
    """A strict-parsed + action-validated Tier-2 decision (ADR §5.2 output)."""

    decision_rationale: str
    actions: tuple[Action, ...]
    dropped_actions: tuple[Mapping[str, Any], ...]
    confidence: str
    escalate_if_wrong: bool
    learning: str


def parse_decision_json(result_text: str) -> ParsedDecision:
    """Strict-parse the LLM decision JSON; invalid shape → ``LLMResponseInvalid``.

    Tolerates the model wrapping the JSON in a ```json fenced block but
    nothing looser — a response without a parseable top-level object raises,
    and the consultant turns that into an operator escalation (Code AC:
    "JSON response strict-parsed; invalid → escalate operator").
    """
    candidate = _strip_code_fence(result_text)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMResponseInvalid(f"decision is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise LLMResponseInvalid("decision JSON is not an object")
    raw_actions = payload.get("actions", [])
    if not isinstance(raw_actions, list):
        raise LLMResponseInvalid("decision 'actions' must be a list")
    actions, dropped = validate_actions(raw_actions)
    confidence = str(payload.get("confidence", "")).strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"  # unrecognised → treat conservatively, never crash
    return ParsedDecision(
        decision_rationale=str(payload.get("decision_rationale", "") or ""),
        actions=actions,
        dropped_actions=tuple(dropped),
        confidence=confidence,
        escalate_if_wrong=bool(payload.get("escalate_if_wrong", False)),
        learning=str(payload.get("learning", "") or ""),
    )


def _strip_code_fence(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        # Drop the opening fence (``` or ```json) and the closing fence.
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -3]
        return body.strip()
    return stripped


def validate_actions(
    raw_actions: Sequence[Any],
) -> tuple[tuple[Action, ...], list[Mapping[str, Any]]]:
    """Map LLM action dicts to §6.1 :class:`Action`s; drop everything else.

    Code AC ("only ADR §6.1 allowed actions executed"): an action whose name
    is outside :data:`_LLM_ACTION_NAME_TO_KIND`, or that fails the
    :class:`Action` envelope's own §6.1 kind check, is dropped (not executed)
    and returned in the rejects list so the decision log records the attempt.
    A bare ``noop`` is dropped from the executable set (it carries no effect).
    """
    accepted: list[Action] = []
    dropped: list[Mapping[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            dropped.append({"reason": "not_an_object", "raw": repr(raw)})
            continue
        name = str(raw.get("action", "")).strip().lower()
        kind = _LLM_ACTION_NAME_TO_KIND.get(name)
        if kind is None or kind not in ALLOWED_ACTION_KINDS:
            dropped.append({"reason": "unknown_action", "action": name})
            continue
        if kind == ACTION_NOOP:
            # A noop instruction is a no-action; nothing to execute.
            continue
        ticket = str(raw.get("ticket", "") or "")
        params = raw.get("params", {})
        if not isinstance(params, Mapping):
            params = {}
        try:
            accepted.append(Action(kind=kind, target=ticket, params=dict(params)))
        except ValueError as exc:
            dropped.append({"reason": f"invalid_action: {exc}", "action": name})
    return tuple(accepted), dropped


# ── Tier-2 consultant ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Tier2Outcome:
    """What a single :meth:`Tier2Consultant.consult` produced.

    ``actions`` are the validated §6.1 actions to execute; ``llm_consultation``
    is the Appendix-C metadata block embedded in the decision-log line; and
    ``reason`` is the grep-able reason string. ``degraded`` flags the
    budget-cap path (Tier-1-only + operator alert).
    """

    actions: tuple[Action, ...]
    reason: str
    llm_consultation: Mapping[str, Any]
    degraded: bool = False
    learning: str = ""


class Tier2Consultant:
    """Assemble context → invoke claude CLI → parse → validate → DecisionResult.

    Collaborators are injected for deterministic tests; production defaults
    wire the real CLI runner + Cognee lesson recall + budget guard. The
    consultant never mutates JIRA itself — it only *produces* §6.1 actions for
    the daemon's action layer (shadow in the 29f-14 canary, real later), so
    Tier-2 output flows through the exact same action path as Tier-1
    (Integration AC).
    """

    engine_version: str = TIER2_ENGINE_VERSION

    def __init__(
        self,
        *,
        config: LLMConsultationConfig | None = None,
        runner: LLMRunner | None = None,
        budget: BudgetGuard | None = None,
        lessons_provider: LessonsProvider | None = None,
        operator_prefs_provider: OperatorPrefsProvider | None = None,
        recent_decisions_provider: RecentDecisionsProvider | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._config = config or LLMConsultationConfig.from_env()
        self._runner = runner or CliLLMRunner(self._config)
        self._budget = budget if budget is not None else BudgetGuard(
            self._config.daily_budget_usd, clock=clock
        )
        self._lessons_provider = lessons_provider or _default_lessons_provider
        self._operator_prefs_provider = operator_prefs_provider or (lambda _ctx: {})
        self._recent_decisions_provider = recent_decisions_provider or (lambda _ctx: ())
        self._clock = clock

    @property
    def budget(self) -> BudgetGuard:
        return self._budget

    def consult(
        self,
        ctx: DecisionContext,
        *,
        behavior: PersonalityMode | None = None,
        trigger: str = "tier1_no_match",
    ) -> Tier2Outcome:
        """Run one Tier-2 consultation for ``ctx`` (or degrade on budget)."""
        # Budget pre-check (§5.3): refuse a consult that would breach the cap;
        # degrade to Tier-1-only + alert the operator instead of spending.
        if self._budget.exhausted or not self._budget.can_afford(
            self._config.per_decision_estimate_usd
        ):
            return self._degrade_for_budget(ctx)

        window = behavior.llm_lessons_window if behavior is not None else 5
        lessons = tuple(self._safe_recall(ctx, window))
        bundle = build_context_bundle(
            ctx,
            mode_behavior=behavior,
            lessons=lessons,
            operator_prefs=self._operator_prefs_provider(ctx),
            recent_decisions=self._recent_decisions_provider(ctx),
        )
        lesson_ids = [lesson.identifier for lesson in lessons]
        logger.info(
            "[coord-llm] Tier-2 consult trigger=%s mode=%s lessons=%d cited=[%s]",
            trigger,
            ctx.mode,
            len(lessons),
            ",".join(lesson_ids),
        )

        invocation = self._runner.invoke(bundle.render())
        if not invocation.ok:
            return self._escalate_invocation_failure(ctx, invocation, lesson_ids)

        try:
            envelope = parse_cli_envelope(invocation.stdout)
            parsed = parse_decision_json(envelope.result_text)
        except LLMResponseInvalid as exc:
            return self._escalate_parse_failure(ctx, invocation, lesson_ids, exc)

        cost = envelope.cost_usd or self._config.per_decision_estimate_usd
        self._budget.record(cost)
        return self._build_consulted_outcome(
            ctx, bundle, envelope, parsed, cost, lesson_ids, trigger
        )

    # ── outcome builders ──

    def _build_consulted_outcome(
        self,
        ctx: DecisionContext,
        bundle: ContextBundle,
        envelope: CliEnvelope,
        parsed: ParsedDecision,
        cost: float,
        lesson_ids: list[str],
        trigger: str,
    ) -> Tier2Outcome:
        actions = parsed.actions
        # A consult that returned zero executable actions but asked to escalate
        # (or simply produced nothing) still surfaces to the operator so a
        # Tier-2 tick is never silently a no-op when the model wanted action.
        if not actions and parsed.escalate_if_wrong:
            actions = (
                Action.escalate(
                    _focal_key(ctx),
                    reason=f"[tier2] low-confidence consult, no safe action: "
                    f"{parsed.decision_rationale[:200]}",
                ),
            )
        meta = {
            "trigger": trigger,
            "context_size_tokens": envelope.input_tokens or _estimate_tokens(bundle.render()),
            "response_tokens": envelope.output_tokens,
            "cost_usd": round(cost, 6),
            "estimate_usd": round(self._config.per_decision_estimate_usd, 6),
            "confidence": parsed.confidence,
            "decision_rationale": parsed.decision_rationale,
            # 29f-11: the one-line lesson the learning loop writes back to
            # Cognee when this decision's 24h outcome-check succeeds (§10.1).
            "learning": parsed.learning,
            "lessons_cited": lesson_ids,
            "model": self._config.model,
            "budget_remaining_usd": round(self._budget.remaining_usd(), 6),
        }
        if parsed.dropped_actions:
            meta["dropped_actions"] = list(parsed.dropped_actions)
        return Tier2Outcome(
            actions=actions,
            reason=TIER2_CONSULTED_REASON,
            llm_consultation=meta,
            learning=parsed.learning,
        )

    def _degrade_for_budget(self, ctx: DecisionContext) -> Tier2Outcome:
        msg = (
            f"[coord-budget] daily Tier-2 LLM budget "
            f"${self._budget.cap_usd:.2f} exhausted "
            f"(spent ${self._budget.spent_usd():.2f}); degrading to Tier-1-only "
            f"until UTC day rollover."
        )
        logger.warning("[coord-llm] %s", msg)
        return Tier2Outcome(
            actions=(Action.mention_operator(_focal_key(ctx), message=msg, urgency="high"),),
            reason=TIER2_DEGRADED_BUDGET_REASON,
            llm_consultation={
                "degraded": "budget_cap",
                "daily_budget_usd": round(self._budget.cap_usd, 6),
                "spent_usd": round(self._budget.spent_usd(), 6),
                "cost_usd": 0.0,
            },
            degraded=True,
        )

    def _escalate_invocation_failure(
        self,
        ctx: DecisionContext,
        invocation: LLMInvocation,
        lesson_ids: list[str],
    ) -> Tier2Outcome:
        detail = "timeout" if invocation.timed_out else f"exit={invocation.returncode}"
        msg = f"[tier2] claude CLI consultation failed ({detail}); operator triage needed."
        logger.warning("[coord-llm] %s stderr=%s", msg, (invocation.stderr or "")[:300])
        return Tier2Outcome(
            actions=(Action.escalate(_focal_key(ctx), reason=msg),),
            reason=TIER2_INVOCATION_FAILED_REASON,
            llm_consultation={
                "error": detail,
                "cost_usd": 0.0,
                "lessons_cited": lesson_ids,
            },
        )

    def _escalate_parse_failure(
        self,
        ctx: DecisionContext,
        invocation: LLMInvocation,
        lesson_ids: list[str],
        exc: Exception,
    ) -> Tier2Outcome:
        msg = f"[tier2] LLM response was not valid decision JSON ({exc}); operator triage needed."
        logger.warning("[coord-llm] %s", msg)
        return Tier2Outcome(
            actions=(Action.escalate(_focal_key(ctx), reason=msg),),
            reason=TIER2_PARSE_FAILED_REASON,
            llm_consultation={
                "error": "parse_failed",
                "detail": str(exc),
                "cost_usd": 0.0,
                "lessons_cited": lesson_ids,
            },
        )

    def _safe_recall(self, ctx: DecisionContext, window: int) -> Sequence[RecalledLesson]:
        try:
            return self._lessons_provider(ctx, window)
        except Exception as exc:  # noqa: BLE001 — recall must never break a tick
            logger.info("[coord-llm] lesson recall unavailable: %s", exc)
            return ()


def _focal_key(ctx: DecisionContext) -> str:
    return ctx.ticket.key if ctx.ticket is not None else ""


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _default_lessons_provider(ctx: DecisionContext, window: int) -> Sequence[RecalledLesson]:
    """Production lesson recall via Cognee (29b); empty on any failure.

    Lazy-imports ``cognee_integration`` so the heavy KG dependency stays off
    the cold-start path and the unit suite (which injects a stub provider)
    never touches it. The Cognee adapter already falls back to the BM25 B10
    index when Neo4j is down, and to ``()`` when nothing matches.
    """
    focal = ctx.ticket
    if focal is None:
        return ()
    from backend.agents.cognee_integration import retrieve_lessons_via_cognee

    lessons_dir = Path(__file__).resolve().parents[2] / "docs" / "sop" / "lessons"
    title = _ticket_brief(focal)
    criteria = "\n".join(c.text for c in focal.comments[-3:]) if focal.comments else ""
    hits = retrieve_lessons_via_cognee(
        lessons_dir,
        ticket_title=title,
        acceptance_criteria=criteria,
        top_k=max(1, window),
    )
    return tuple(
        RecalledLesson(
            identifier=Path(hit.path).name,
            summary=_first_line(hit.text),
            score=hit.score,
        )
        for hit in hits
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


# ── Hybrid engine (Integration AC) ─────────────────────────────────────


class HybridDecisionEngine:
    """Tier-1 deterministic rules fronting Tier-2 LLM consultation.

    Drop-in for :class:`Tier1RuleEngine` (same ``engine_version`` + ``evaluate``
    + ``rule_names`` seam the daemon depends on). Per ADR §5.2 the engine
    consults Tier-2 when:

    * **no Tier-1 rule matches** (the registry abstains), or
    * **Tier-1 rules conflict** (≥2 rules fire mutually-distinct actions), or
    * the selected **personality mode demands it** (``tier2_policy == "always"``
      — Investigation / Rescue modes per ADR §7.2).

    An ``operator-keep-out`` veto (priority-0 :class:`NoopAction`) short-circuits
    *both* tiers — an escape-hatched ticket is never sent to the LLM. When the
    daily budget is exhausted the consultant degrades to Tier-1-only and the
    engine returns the Tier-1 decision (or the operator budget alert) at
    ``tier=1`` (Integration + Exercised AC).
    """

    engine_version: str = HYBRID_ENGINE_VERSION

    def __init__(
        self,
        *,
        tier1: Tier1RuleEngine | None = None,
        consultant: Tier2Consultant | None = None,
        mode_selector: ModeSelector | None = None,
    ) -> None:
        self._tier1 = tier1 or Tier1RuleEngine()
        self._consultant = consultant or Tier2Consultant()
        self._mode_selector = mode_selector or ModeSelector()

    @property
    def consultant(self) -> Tier2Consultant:
        return self._consultant

    def rule_names(self) -> tuple[str, ...]:
        return self._tier1.rule_names()

    def evaluate(self, ctx: DecisionContext) -> DecisionResult:
        mode = ctx.mode
        if ctx.situation is not None:
            mode = self._mode_selector.select(ctx.situation, override=ctx.mode_override)
        behavior = mode_behavior(mode)
        always = behavior is not None and behavior.tier2_policy == "always"

        # OP-1556: idle / empty-work-graph tick short-circuit. With no focal
        # ticket AND no work-graph slice there is nothing to decide — every
        # Tier-1 rule abstains (each early-returns on ctx.ticket is None), but
        # that abstention means "nothing to decide", NOT "a decision the rules
        # couldn't make". Falling through to Tier-2 here invokes the LLM on
        # every idle tick (AUDIT-29f: 1254/1254 idle ticks were tier=2,
        # 0 rules fired) — in acting mode that burns the daily budget cap in
        # minutes. So return a Tier-1 NoopAction without consulting. The sole
        # exception: a mode whose tier2_policy == "always" *with* an actual
        # situation attached is a deliberate sprint-level/idle consult and is
        # allowed to proceed to Tier-2.
        if ctx.ticket is None and not ctx.tickets and not (always and ctx.situation is not None):
            return DecisionResult(
                decision_id=uuid.uuid4().hex,
                actions=(NoopAction(),),
                reason=TIER1_IDLE_NOOP_REASON,
                mode=mode,
                engine_version=self.engine_version,
                mode_behavior=behavior,
                rule_name=None,
                tier=1,
            )

        fired = self._fired_rules(ctx)

        # Absolute operator veto (priority-0 NoopAction) short-circuits.
        if fired and isinstance(fired[0][1], NoopAction):
            rule = fired[0][0]
            return DecisionResult(
                decision_id=uuid.uuid4().hex,
                actions=(NoopAction(),),
                reason=f"tier1:{rule.name}",
                mode=mode,
                engine_version=self.engine_version,
                mode_behavior=behavior,
                rule_name=rule.name,
                tier=1,
            )

        tier1_match = fired[0] if fired else None
        conflict = _is_conflict(fired)

        if tier1_match is not None and not conflict and not always:
            rule, action = tier1_match
            return DecisionResult(
                decision_id=uuid.uuid4().hex,
                actions=(action,),
                reason=f"tier1:{rule.name}",
                mode=mode,
                engine_version=self.engine_version,
                mode_behavior=behavior,
                rule_name=rule.name,
                tier=1,
            )

        trigger = (
            "tier1_conflict"
            if conflict
            else ("mode_requires_tier2" if always and tier1_match is not None else "tier1_no_match")
        )
        outcome = self._consultant.consult(ctx, behavior=behavior, trigger=trigger)
        return self._result_from_outcome(ctx, mode, behavior, outcome, tier1_match)

    def _fired_rules(self, ctx: DecisionContext) -> list[tuple[RegisteredRule, Action]]:
        """Evaluate every rule (priority order); collect the ones that fire."""
        out: list[tuple[RegisteredRule, Action]] = []
        for rule in self._tier1.rules:
            action = rule.evaluate(ctx)
            if action is not None:
                out.append((rule, action))
        return out

    def _result_from_outcome(
        self,
        ctx: DecisionContext,
        mode: str,
        behavior: PersonalityMode | None,
        outcome: Tier2Outcome,
        tier1_match: tuple[RegisteredRule, Action] | None,
    ) -> DecisionResult:
        if outcome.degraded:
            # Budget cap hit → Tier-1-only: prefer the deterministic Tier-1
            # action (if any) and ride the operator budget alert alongside it.
            degraded_actions: tuple[Action, ...]
            rule_name: str | None
            if tier1_match is not None:
                degraded_actions = (tier1_match[1],) + outcome.actions
                rule_name = tier1_match[0].name
            else:
                degraded_actions = outcome.actions or (NoopAction(),)
                rule_name = None
            return DecisionResult(
                decision_id=uuid.uuid4().hex,
                actions=degraded_actions,
                reason=outcome.reason,
                mode=mode,
                engine_version=self.engine_version,
                mode_behavior=behavior,
                rule_name=rule_name,
                tier=1,
                llm_consultation=outcome.llm_consultation,
            )
        actions = outcome.actions or (NoopAction(),)
        return DecisionResult(
            decision_id=uuid.uuid4().hex,
            actions=actions,
            reason=outcome.reason,
            mode=mode,
            engine_version=self.engine_version,
            mode_behavior=behavior,
            rule_name=None,
            tier=2,
            llm_consultation=outcome.llm_consultation,
        )


def _is_conflict(fired: Sequence[tuple[RegisteredRule, Action]]) -> bool:
    """True when ≥2 rules fire mutually-distinct (conflicting) actions.

    Identical actions (same kind/target/params) from two rules are not a
    conflict — they agree. A :class:`NoopAction` veto is handled before this
    is called, so any Noop here is incidental and ignored.
    """
    distinct: list[tuple[str, str, str]] = []
    for _rule, action in fired:
        if isinstance(action, NoopAction):
            continue
        key = (action.kind, action.target, json.dumps(action.params, sort_keys=True))
        if key not in distinct:
            distinct.append(key)
    return len(distinct) > 1


def build_hybrid_engine(
    *,
    decision_log_dir: Path | None = None,
    config: LLMConsultationConfig | None = None,
) -> HybridDecisionEngine:
    """Production wiring: Tier-1 registry + Tier-2 consultant + day-budget guard.

    When ``decision_log_dir`` is given the budget guard rebuilds the day's
    spend from the log (ADR §3.2 stateless-across-restarts) so a restart does
    not silently reset the cap.
    """
    config = config or LLMConsultationConfig.from_env()
    if decision_log_dir is not None:
        budget = BudgetGuard.from_decision_log(
            decision_log_dir, daily_budget_usd=config.daily_budget_usd
        )
    else:
        budget = BudgetGuard(config.daily_budget_usd)
    consultant = Tier2Consultant(config=config, budget=budget)
    return HybridDecisionEngine(tier1=Tier1RuleEngine(), consultant=consultant)
