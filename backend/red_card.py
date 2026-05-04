"""BP.H.2 — Red-card circuit breaker for repeated ``Verified -1``.

This row is the third tier in Blueprint Phase H: after CI hard
rejection and cognitive-penalty prompt feedback, three consecutive
``Verified -1`` labels for the same agent on the same JIRA ticket
trigger a red card.  The red card cuts that agent's API key and marks
the JIRA ticket blocked for human review.

The module mirrors the BP.H.1 ``backend.cognitive_penalty`` shape for
deterministic parsing/projection, plus the ``backend.merge_arbiter``
dependency-injection style for external effects.  Tests inject stubs;
production callers can use the default API-key revoker and JIRA status
updater.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable constants and stateless default collaborator instances
live at module scope.  No strike counter or subcall counter is stored
in-process: every worker derives the same decision from the same
caller-provided Gerrit / JIRA review history or root-task subcall
history (qualified answer #1).

Read-after-write timing audit
-----------------------------
The decision path is pure.  The execution path performs two independent
best-effort writes (API key revoke, JIRA blocked status/comment) through
injected collaborators; it does not add a new read-after-write contract
or parallelise a formerly serialised caller workflow.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


__all__ = [
    "JIRA_BLOCKED_PREFIX",
    "RED_CARD_REASON",
    "RED_CARD_THRESHOLD",
    "RECURSIVE_SUBCALL_BUDGET_REASON",
    "SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS",
    "SUBCALL_BUDGET_RED_THRESHOLD",
    "SUBCALL_BUDGET_YELLOW_THRESHOLD",
    "VERIFIED_MINUS_ONE",
    "RecursiveSubcallBudgetActionResult",
    "RecursiveSubcallBudgetDecision",
    "RecursiveSubcallBudgetDeps",
    "RecursiveSubcallBudgetEvent",
    "RecursiveSubcallBudgetHumanEscalationResult",
    "RecursiveSubcallBudgetOverride",
    "RecursiveSubcallBudgetSlowDownResult",
    "RedCardActionResult",
    "RedCardApiCutResult",
    "RedCardDecision",
    "RedCardDeps",
    "RedCardJiraBlockResult",
    "RedCardReviewEvent",
    "evaluate_red_card",
    "evaluate_recursive_subcall_budget",
    "execute_red_card",
    "execute_recursive_subcall_budget",
    "parse_recursive_subcall_history",
    "parse_review_history",
]


VERIFIED_MINUS_ONE: str = "Verified -1"
RED_CARD_THRESHOLD: int = 3
JIRA_BLOCKED_PREFIX: str = "[BLOCKED]"
RED_CARD_REASON: str = "red_card_verified_minus_one_streak"
SUBCALL_BUDGET_YELLOW_THRESHOLD: int = 3
SUBCALL_BUDGET_RED_THRESHOLD: int = 5
SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS: int = 30
RECURSIVE_SUBCALL_BUDGET_REASON: str = "recursive_subcall_budget_exceeded"


class RedCardReviewEvent(BaseModel):
    """One Gerrit verification event enriched with task ownership."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(..., min_length=1)
    jira_ticket: str = Field(..., min_length=1)
    verified_label: str = Field(..., min_length=1)
    change_id: str = ""
    patchset: str = ""
    api_key_id: str = ""
    created_at: str = ""

    @property
    def is_verified_minus_one(self) -> bool:
        return _normalise_label(self.verified_label) == VERIFIED_MINUS_ONE


class RedCardDecision(BaseModel):
    """Pure decision output for the red-card gate."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    jira_ticket: str
    status: Literal["clear", "warn", "red_card"]
    consecutive_failures: int = Field(..., ge=0)
    threshold: int = Field(..., ge=1)
    reason: str = ""
    api_key_id: str = ""
    change_id: str = ""
    patchset: str = ""

    @property
    def should_cut_api(self) -> bool:
        return self.status == "red_card"

    @property
    def should_block_jira(self) -> bool:
        return self.status == "red_card"


class RecursiveSubcallBudgetEvent(BaseModel):
    """One sub-LM call attributed to a root task."""

    model_config = ConfigDict(frozen=True)

    root_task_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    guild_id: str = ""
    jira_ticket: str = ""
    api_key_id: str = ""
    subcall_id: str = ""
    model: str = ""
    depth: int = Field(1, ge=1)
    created_at: str = ""


class RecursiveSubcallBudgetOverride(BaseModel):
    """Per-Guild recursive subcall budget override."""

    model_config = ConfigDict(frozen=True)

    yellow_threshold: int = Field(SUBCALL_BUDGET_YELLOW_THRESHOLD, ge=0)
    red_threshold: int = Field(SUBCALL_BUDGET_RED_THRESHOLD, ge=1)
    slow_down_seconds: int = Field(
        SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS,
        ge=1,
    )


class RecursiveSubcallBudgetDecision(BaseModel):
    """Pure decision output for a root task's recursive subcall budget."""

    model_config = ConfigDict(frozen=True)

    root_task_id: str
    agent_id: str
    guild_id: str = ""
    jira_ticket: str = ""
    status: Literal["clear", "yellow_card", "red_card"]
    subcall_count: int = Field(..., ge=0)
    yellow_threshold: int = Field(..., ge=0)
    red_threshold: int = Field(..., ge=1)
    slow_down_seconds: int = Field(..., ge=0)
    reason: str = ""
    api_key_id: str = ""

    @property
    def should_warn(self) -> bool:
        return self.status in {"yellow_card", "red_card"}

    @property
    def should_slow_down(self) -> bool:
        return self.status == "yellow_card"

    @property
    def should_cut_api(self) -> bool:
        return self.status == "red_card"

    @property
    def should_escalate_human(self) -> bool:
        return self.status == "red_card"


class RedCardApiCutResult(BaseModel):
    """Result of cutting the agent's API access."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    agent_id: str
    api_key_id: str = ""
    reason: str = ""


class RedCardJiraBlockResult(BaseModel):
    """Result of marking the JIRA ticket blocked."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    ticket: str
    reason: str = ""
    blocked_prefix: str = JIRA_BLOCKED_PREFIX


class RecursiveSubcallBudgetSlowDownResult(BaseModel):
    """Result of slowing a yellow-carded root task."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    root_task_id: str
    agent_id: str
    slow_down_seconds: int
    reason: str = ""


class RecursiveSubcallBudgetHumanEscalationResult(BaseModel):
    """Result of escalating a red-carded recursive subcall budget."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    root_task_id: str
    agent_id: str
    ticket: str = ""
    reason: str = ""


class RedCardActionResult(BaseModel):
    """Decision plus side-effect results."""

    model_config = ConfigDict(frozen=True)

    decision: RedCardDecision
    api_cut: RedCardApiCutResult | None = None
    jira_block: RedCardJiraBlockResult | None = None
    performed: bool = False


class RecursiveSubcallBudgetActionResult(BaseModel):
    """Decision plus subcall-budget side-effect results."""

    model_config = ConfigDict(frozen=True)

    decision: RecursiveSubcallBudgetDecision
    slow_down: RecursiveSubcallBudgetSlowDownResult | None = None
    api_cut: RedCardApiCutResult | None = None
    human_escalation: RecursiveSubcallBudgetHumanEscalationResult | None = None
    performed: bool = False


class AgentApiDisabler(Protocol):
    async def disable_agent_api(
        self,
        *,
        agent_id: str,
        api_key_id: str,
        reason: str,
    ) -> RedCardApiCutResult: ...


class JiraBlocker(Protocol):
    async def mark_blocked(
        self,
        *,
        ticket: str,
        agent_id: str,
        consecutive_failures: int,
        reason: str,
    ) -> RedCardJiraBlockResult: ...


class SubcallBudgetSlower(Protocol):
    async def slow_down(
        self,
        *,
        root_task_id: str,
        agent_id: str,
        seconds: int,
        reason: str,
    ) -> RecursiveSubcallBudgetSlowDownResult: ...


class SubcallBudgetHumanEscalator(Protocol):
    async def escalate_human(
        self,
        *,
        root_task_id: str,
        agent_id: str,
        guild_id: str,
        ticket: str,
        subcall_count: int,
        reason: str,
    ) -> RecursiveSubcallBudgetHumanEscalationResult: ...


class _DefaultAgentApiDisabler:
    async def disable_agent_api(
        self,
        *,
        agent_id: str,
        api_key_id: str,
        reason: str,
    ) -> RedCardApiCutResult:
        if not api_key_id:
            return RedCardApiCutResult(
                ok=False,
                agent_id=agent_id,
                api_key_id="",
                reason="missing api_key_id; API revoke deferred to caller",
            )
        try:
            from backend import api_keys

            ok = await api_keys.revoke_key(api_key_id)
        except Exception as exc:  # pragma: no cover - defensive hot path
            logger.debug("red-card API revoke failed: %s", exc)
            return RedCardApiCutResult(
                ok=False,
                agent_id=agent_id,
                api_key_id=api_key_id,
                reason=f"api_keys.revoke_key failed: {exc}",
            )
        return RedCardApiCutResult(
            ok=ok,
            agent_id=agent_id,
            api_key_id=api_key_id,
            reason=reason if ok else "api key not found",
        )


class _DefaultJiraBlocker:
    async def mark_blocked(
        self,
        *,
        ticket: str,
        agent_id: str,
        consecutive_failures: int,
        reason: str,
    ) -> RedCardJiraBlockResult:
        comment = (
            f"{JIRA_BLOCKED_PREFIX} Red card: agent {agent_id} received "
            f"{consecutive_failures} consecutive {VERIFIED_MINUS_ONE} labels. "
            "API access has been cut; human review required."
        )
        try:
            from backend.intent_source import IntentStatus
            from backend.jira_adapter import build_default_jira_adapter

            adapter = build_default_jira_adapter()
            await adapter.update_status(ticket, IntentStatus.blocked, comment=comment)
        except Exception as exc:  # pragma: no cover - depends on live JIRA
            logger.debug("red-card JIRA block failed: %s", exc)
            return RedCardJiraBlockResult(
                ok=False,
                ticket=ticket,
                reason=f"jira blocked update failed: {exc}",
            )
        return RedCardJiraBlockResult(ok=True, ticket=ticket, reason=reason)


class _DefaultSubcallBudgetSlower:
    async def slow_down(
        self,
        *,
        root_task_id: str,
        agent_id: str,
        seconds: int,
        reason: str,
    ) -> RecursiveSubcallBudgetSlowDownResult:
        await asyncio.sleep(seconds)
        return RecursiveSubcallBudgetSlowDownResult(
            ok=True,
            root_task_id=root_task_id,
            agent_id=agent_id,
            slow_down_seconds=seconds,
            reason=reason,
        )


class _DefaultSubcallBudgetHumanEscalator:
    async def escalate_human(
        self,
        *,
        root_task_id: str,
        agent_id: str,
        guild_id: str,
        ticket: str,
        subcall_count: int,
        reason: str,
    ) -> RecursiveSubcallBudgetHumanEscalationResult:
        if not ticket:
            return RecursiveSubcallBudgetHumanEscalationResult(
                ok=False,
                root_task_id=root_task_id,
                agent_id=agent_id,
                reason="missing jira_ticket; human escalation deferred to caller",
            )
        comment = (
            f"{JIRA_BLOCKED_PREFIX} Recursive subcall budget red card: "
            f"agent {agent_id} in Guild {guild_id or 'unknown'} accumulated "
            f"{subcall_count} sub-LM calls under root task {root_task_id}. "
            "Agent API access has been cut; human review required."
        )
        try:
            from backend.intent_source import IntentStatus
            from backend.jira_adapter import build_default_jira_adapter

            adapter = build_default_jira_adapter()
            await adapter.update_status(ticket, IntentStatus.blocked, comment=comment)
        except Exception as exc:  # pragma: no cover - depends on live JIRA
            logger.debug("subcall-budget human escalation failed: %s", exc)
            return RecursiveSubcallBudgetHumanEscalationResult(
                ok=False,
                root_task_id=root_task_id,
                agent_id=agent_id,
                ticket=ticket,
                reason=f"jira blocked update failed: {exc}",
            )
        return RecursiveSubcallBudgetHumanEscalationResult(
            ok=True,
            root_task_id=root_task_id,
            agent_id=agent_id,
            ticket=ticket,
            reason=reason,
        )


@dataclass(frozen=True)
class RedCardDeps:
    api_disabler: AgentApiDisabler = _DefaultAgentApiDisabler()
    jira_blocker: JiraBlocker = _DefaultJiraBlocker()


@dataclass(frozen=True)
class RecursiveSubcallBudgetDeps:
    api_disabler: AgentApiDisabler = _DefaultAgentApiDisabler()
    slower: SubcallBudgetSlower = _DefaultSubcallBudgetSlower()
    human_escalator: SubcallBudgetHumanEscalator = (
        _DefaultSubcallBudgetHumanEscalator()
    )


def _normalise_label(value: Any) -> str:
    text = " ".join(str(value or "").split())
    lower = text.lower()
    if lower in {"verified -1", "verified-1", "-1", "verified:-1"}:
        return VERIFIED_MINUS_ONE
    if lower in {"verified +1", "verified+1", "+1", "verified:+1"}:
        return "Verified +1"
    if lower in {"verified 0", "verified+0", "verified 0", "0", "verified:0"}:
        return "Verified 0"
    return text


def _string_from_any(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            text = " ".join(str(value).split())
            if text:
                return text
    return ""


def _event_from_mapping(data: Mapping[str, Any]) -> RedCardReviewEvent | None:
    agent_id = _string_from_any(data, "agent_id", "agent", "reviewer", "bot")
    jira_ticket = _string_from_any(data, "jira_ticket", "ticket", "issue_key", "issue")
    verified_label = _normalise_label(
        _string_from_any(data, "verified_label", "verified", "label", "score")
    )
    if not agent_id or not jira_ticket or not verified_label:
        return None
    return RedCardReviewEvent(
        agent_id=agent_id,
        jira_ticket=jira_ticket,
        verified_label=verified_label,
        change_id=_string_from_any(data, "change_id", "change"),
        patchset=_string_from_any(data, "patchset", "patch_set", "revision"),
        api_key_id=_string_from_any(data, "api_key_id", "key_id"),
        created_at=_string_from_any(data, "created_at", "timestamp", "time"),
    )


def _int_from_any(data: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _subcall_event_from_mapping(
    data: Mapping[str, Any],
) -> RecursiveSubcallBudgetEvent | None:
    root_task_id = _string_from_any(data, "root_task_id", "root_task", "task_id")
    agent_id = _string_from_any(data, "agent_id", "agent", "caller_agent")
    if not root_task_id or not agent_id:
        return None
    return RecursiveSubcallBudgetEvent(
        root_task_id=root_task_id,
        agent_id=agent_id,
        guild_id=_string_from_any(data, "guild_id", "guild"),
        jira_ticket=_string_from_any(data, "jira_ticket", "ticket", "issue_key"),
        api_key_id=_string_from_any(data, "api_key_id", "key_id"),
        subcall_id=_string_from_any(data, "subcall_id", "call_id", "request_id"),
        model=_string_from_any(data, "model", "provider_model"),
        depth=max(_int_from_any(data, "depth", "recursion_depth", default=1), 1),
        created_at=_string_from_any(data, "created_at", "timestamp", "time"),
    )


def parse_review_history(
    history: Sequence[RedCardReviewEvent | Mapping[str, Any]],
) -> tuple[RedCardReviewEvent, ...]:
    """Normalise Gerrit/JIRA review history into red-card events.

    Malformed rows are skipped so one degraded webhook payload does not
    crash the penalty path.
    """

    events: list[RedCardReviewEvent] = []
    for item in history:
        if isinstance(item, RedCardReviewEvent):
            events.append(item)
        elif isinstance(item, Mapping):
            event = _event_from_mapping(item)
            if event is not None:
                events.append(event)
    return tuple(events)


def parse_recursive_subcall_history(
    history: Sequence[RecursiveSubcallBudgetEvent | Mapping[str, Any]],
) -> tuple[RecursiveSubcallBudgetEvent, ...]:
    """Normalise root-task sub-LM call history into budget events."""

    events: list[RecursiveSubcallBudgetEvent] = []
    for item in history:
        if isinstance(item, RecursiveSubcallBudgetEvent):
            events.append(item)
        elif isinstance(item, Mapping):
            event = _subcall_event_from_mapping(item)
            if event is not None:
                events.append(event)
    return tuple(events)


def _coerce_subcall_budget_override(
    override: RecursiveSubcallBudgetOverride | Mapping[str, Any] | None,
) -> RecursiveSubcallBudgetOverride:
    if isinstance(override, RecursiveSubcallBudgetOverride):
        base = override
    elif isinstance(override, Mapping):
        base = RecursiveSubcallBudgetOverride(
            yellow_threshold=_int_from_any(
                override,
                "yellow_threshold",
                "yellow",
                default=SUBCALL_BUDGET_YELLOW_THRESHOLD,
            ),
            red_threshold=_int_from_any(
                override,
                "red_threshold",
                "red",
                default=SUBCALL_BUDGET_RED_THRESHOLD,
            ),
            slow_down_seconds=_int_from_any(
                override,
                "slow_down_seconds",
                "slow_down",
                default=SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS,
            ),
        )
    else:
        base = RecursiveSubcallBudgetOverride()

    red_threshold = max(base.red_threshold, base.yellow_threshold + 1)
    if red_threshold == base.red_threshold:
        return base
    return RecursiveSubcallBudgetOverride(
        yellow_threshold=base.yellow_threshold,
        red_threshold=red_threshold,
        slow_down_seconds=base.slow_down_seconds,
    )


def _resolve_subcall_budget_override(
    guild_id: str,
    overrides: Mapping[
        str,
        RecursiveSubcallBudgetOverride | Mapping[str, Any],
    ] | None,
) -> RecursiveSubcallBudgetOverride:
    if not overrides:
        return RecursiveSubcallBudgetOverride()
    return _coerce_subcall_budget_override(overrides.get(guild_id))


def evaluate_red_card(
    history: Sequence[RedCardReviewEvent | Mapping[str, Any]],
    *,
    agent_id: str = "",
    jira_ticket: str = "",
    threshold: int = RED_CARD_THRESHOLD,
) -> RedCardDecision:
    """Evaluate whether the latest same-agent/same-ticket streak is red.

    ``history`` must be chronological.  When ``agent_id`` or
    ``jira_ticket`` is omitted, the latest valid event supplies it and
    the streak is still scoped to that same pair.
    """

    threshold = max(int(threshold or RED_CARD_THRESHOLD), 1)
    events = parse_review_history(history)
    if not events:
        return RedCardDecision(
            agent_id=agent_id,
            jira_ticket=jira_ticket,
            status="clear",
            consecutive_failures=0,
            threshold=threshold,
        )

    latest = events[-1]
    scoped_agent = agent_id or latest.agent_id
    scoped_ticket = jira_ticket or latest.jira_ticket
    scoped = [
        event for event in events
        if event.agent_id == scoped_agent and event.jira_ticket == scoped_ticket
    ]
    if not scoped:
        return RedCardDecision(
            agent_id=scoped_agent,
            jira_ticket=scoped_ticket,
            status="clear",
            consecutive_failures=0,
            threshold=threshold,
        )

    consecutive = 0
    latest_scoped = scoped[-1]
    latest_failure = latest_scoped if latest_scoped.is_verified_minus_one else None
    for event in reversed(scoped):
        if not event.is_verified_minus_one:
            break
        consecutive += 1
        if latest_failure is None:
            latest_failure = event

    if consecutive >= threshold:
        status: Literal["clear", "warn", "red_card"] = "red_card"
        reason = RED_CARD_REASON
    elif consecutive > 0:
        status = "warn"
        reason = "verified_minus_one_streak"
    else:
        status = "clear"
        reason = ""

    source = latest_failure or latest_scoped
    return RedCardDecision(
        agent_id=scoped_agent,
        jira_ticket=scoped_ticket,
        status=status,
        consecutive_failures=consecutive,
        threshold=threshold,
        reason=reason,
        api_key_id=source.api_key_id,
        change_id=source.change_id,
        patchset=source.patchset,
    )


def evaluate_recursive_subcall_budget(
    history: Sequence[RecursiveSubcallBudgetEvent | Mapping[str, Any]],
    *,
    root_task_id: str = "",
    agent_id: str = "",
    guild_id: str = "",
    jira_ticket: str = "",
    api_key_id: str = "",
    guild_overrides: Mapping[
        str,
        RecursiveSubcallBudgetOverride | Mapping[str, Any],
    ] | None = None,
) -> RecursiveSubcallBudgetDecision:
    """Evaluate one root task's cumulative recursive sub-LM call count.

    Thresholds are strict greater-than gates from ADR R10 / Appendix C:
    count > 3 yields a yellow card, count > 5 yields a red card.  Per-Guild
    overrides are caller-provided so workers derive the same result from the
    same durable history/config instead of sharing an in-process counter.
    """

    events = parse_recursive_subcall_history(history)
    if not events:
        override = _resolve_subcall_budget_override(guild_id, guild_overrides)
        return RecursiveSubcallBudgetDecision(
            root_task_id=root_task_id,
            agent_id=agent_id,
            guild_id=guild_id,
            jira_ticket=jira_ticket,
            status="clear",
            subcall_count=0,
            yellow_threshold=override.yellow_threshold,
            red_threshold=override.red_threshold,
            slow_down_seconds=0,
            api_key_id=api_key_id,
        )

    latest = events[-1]
    scoped_root = root_task_id or latest.root_task_id
    scoped = [event for event in events if event.root_task_id == scoped_root]
    if not scoped:
        override = _resolve_subcall_budget_override(guild_id, guild_overrides)
        return RecursiveSubcallBudgetDecision(
            root_task_id=scoped_root,
            agent_id=agent_id,
            guild_id=guild_id,
            jira_ticket=jira_ticket,
            status="clear",
            subcall_count=0,
            yellow_threshold=override.yellow_threshold,
            red_threshold=override.red_threshold,
            slow_down_seconds=0,
            api_key_id=api_key_id,
        )

    source = scoped[-1]
    scoped_agent = agent_id or source.agent_id
    scoped_guild = guild_id or source.guild_id
    scoped_ticket = jira_ticket or source.jira_ticket
    scoped_api_key = api_key_id or source.api_key_id
    override = _resolve_subcall_budget_override(scoped_guild, guild_overrides)
    subcall_count = len(scoped)

    if subcall_count > override.red_threshold:
        status: Literal["clear", "yellow_card", "red_card"] = "red_card"
        reason = RECURSIVE_SUBCALL_BUDGET_REASON
        slow_down_seconds = 0
    elif subcall_count > override.yellow_threshold:
        status = "yellow_card"
        reason = RECURSIVE_SUBCALL_BUDGET_REASON
        slow_down_seconds = override.slow_down_seconds
    else:
        status = "clear"
        reason = ""
        slow_down_seconds = 0

    return RecursiveSubcallBudgetDecision(
        root_task_id=scoped_root,
        agent_id=scoped_agent,
        guild_id=scoped_guild,
        jira_ticket=scoped_ticket,
        status=status,
        subcall_count=subcall_count,
        yellow_threshold=override.yellow_threshold,
        red_threshold=override.red_threshold,
        slow_down_seconds=slow_down_seconds,
        reason=reason,
        api_key_id=scoped_api_key,
    )


async def execute_red_card(
    history: Sequence[RedCardReviewEvent | Mapping[str, Any]],
    *,
    agent_id: str = "",
    jira_ticket: str = "",
    threshold: int = RED_CARD_THRESHOLD,
    deps: RedCardDeps | None = None,
) -> RedCardActionResult:
    """Evaluate and, on red card, cut API access plus block JIRA."""

    decision = evaluate_red_card(
        history,
        agent_id=agent_id,
        jira_ticket=jira_ticket,
        threshold=threshold,
    )
    if not decision.should_cut_api:
        return RedCardActionResult(decision=decision, performed=False)

    deps = deps or RedCardDeps()
    api_cut = await deps.api_disabler.disable_agent_api(
        agent_id=decision.agent_id,
        api_key_id=decision.api_key_id,
        reason=decision.reason,
    )
    jira_block = await deps.jira_blocker.mark_blocked(
        ticket=decision.jira_ticket,
        agent_id=decision.agent_id,
        consecutive_failures=decision.consecutive_failures,
        reason=decision.reason,
    )
    return RedCardActionResult(
        decision=decision,
        api_cut=api_cut,
        jira_block=jira_block,
        performed=True,
    )


async def execute_recursive_subcall_budget(
    history: Sequence[RecursiveSubcallBudgetEvent | Mapping[str, Any]],
    *,
    root_task_id: str = "",
    agent_id: str = "",
    guild_id: str = "",
    jira_ticket: str = "",
    api_key_id: str = "",
    guild_overrides: Mapping[
        str,
        RecursiveSubcallBudgetOverride | Mapping[str, Any],
    ] | None = None,
    deps: RecursiveSubcallBudgetDeps | None = None,
) -> RecursiveSubcallBudgetActionResult:
    """Evaluate and apply the recursive subcall yellow/red-card action."""

    decision = evaluate_recursive_subcall_budget(
        history,
        root_task_id=root_task_id,
        agent_id=agent_id,
        guild_id=guild_id,
        jira_ticket=jira_ticket,
        api_key_id=api_key_id,
        guild_overrides=guild_overrides,
    )
    if decision.status == "clear":
        return RecursiveSubcallBudgetActionResult(
            decision=decision,
            performed=False,
        )

    deps = deps or RecursiveSubcallBudgetDeps()
    if decision.should_slow_down:
        slow_down = await deps.slower.slow_down(
            root_task_id=decision.root_task_id,
            agent_id=decision.agent_id,
            seconds=decision.slow_down_seconds,
            reason=decision.reason,
        )
        return RecursiveSubcallBudgetActionResult(
            decision=decision,
            slow_down=slow_down,
            performed=True,
        )

    api_cut = await deps.api_disabler.disable_agent_api(
        agent_id=decision.agent_id,
        api_key_id=decision.api_key_id,
        reason=decision.reason,
    )
    human_escalation = await deps.human_escalator.escalate_human(
        root_task_id=decision.root_task_id,
        agent_id=decision.agent_id,
        guild_id=decision.guild_id,
        ticket=decision.jira_ticket,
        subcall_count=decision.subcall_count,
        reason=decision.reason,
    )
    return RecursiveSubcallBudgetActionResult(
        decision=decision,
        api_cut=api_cut,
        human_escalation=human_escalation,
        performed=True,
    )
