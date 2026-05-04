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
live at module scope.  No strike counter is stored in-process: every
worker derives the same decision from the same caller-provided Gerrit /
JIRA review history (qualified answer #1).

Read-after-write timing audit
-----------------------------
The decision path is pure.  The execution path performs two independent
best-effort writes (API key revoke, JIRA blocked status/comment) through
injected collaborators; it does not add a new read-after-write contract
or parallelise a formerly serialised caller workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


__all__ = [
    "JIRA_BLOCKED_PREFIX",
    "RED_CARD_REASON",
    "RED_CARD_THRESHOLD",
    "VERIFIED_MINUS_ONE",
    "RedCardActionResult",
    "RedCardApiCutResult",
    "RedCardDecision",
    "RedCardDeps",
    "RedCardJiraBlockResult",
    "RedCardReviewEvent",
    "evaluate_red_card",
    "execute_red_card",
    "parse_review_history",
]


VERIFIED_MINUS_ONE: str = "Verified -1"
RED_CARD_THRESHOLD: int = 3
JIRA_BLOCKED_PREFIX: str = "[BLOCKED]"
RED_CARD_REASON: str = "red_card_verified_minus_one_streak"


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


class RedCardActionResult(BaseModel):
    """Decision plus side-effect results."""

    model_config = ConfigDict(frozen=True)

    decision: RedCardDecision
    api_cut: RedCardApiCutResult | None = None
    jira_block: RedCardJiraBlockResult | None = None
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


@dataclass(frozen=True)
class RedCardDeps:
    api_disabler: AgentApiDisabler = _DefaultAgentApiDisabler()
    jira_blocker: JiraBlocker = _DefaultJiraBlocker()


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
