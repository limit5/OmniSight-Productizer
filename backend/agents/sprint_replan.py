"""Sprint-level periodic re-plan handler (AUDIT-29f-9 / OP-1007).

ADR-0021 §13 calls for an hourly coordinator pass that looks at the next
pickable JIRA tickets, reads runner capacity, asks Tier-2 in Investigation
Mode for an assignment plan, and applies the resulting safe actions.  This
module owns that pass while keeping the daemon wiring small:

    SprintReplanHandler  — one hourly handler entrypoint
    JiraSprintReplanGateway — production JIRA read/write adapter
    SprintReplanActionLayer — validates §6.1 actions before mutation

Module-global state audit (per project SOP)
-------------------------------------------
Constants, frozen dataclasses, and protocol definitions only.  No JIRA client
is constructed and no files are read at import time; production I/O happens
inside injected gateway / capacity provider calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from backend.agents.pipeline_coordinator_capacity import (
    CapacitySnapshot,
    capacity_path_from_env,
    load_capacity_snapshot_from_json,
)
from backend.agents.pipeline_coordinator_llm_consultation import (
    Tier2Consultant,
)
from backend.agents.pipeline_coordinator_modes import (
    INVESTIGATION_MODE,
    InvestigationMode,
    Level,
    SituationProfile,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_FILE_TICKET,
    ACTION_MENTION_OPERATOR,
    ACTION_RELABEL,
    Action,
    Comment,
    DecisionContext,
    Ticket,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_PICKABLE = 20
DEFAULT_OPERATOR_TICKET = "OP"
SPRINT_REPLAN_FOCAL_KEY = "SPRINT-REPLAN"
SPRINT_REPLAN_LABEL = "coord-sprint-replan"
SCOPE_REVIEW_LABEL = "scope-review"
CAPACITY_INSUFFICIENT_LABEL = "capacity-insufficient"

Clock = Callable[[], datetime]
CapacityProvider = Callable[[], CapacitySnapshot]


@dataclass(frozen=True)
class PickableTicket:
    """Reduced JIRA issue shape needed by the sprint re-plan pass."""

    key: str
    summary: str = ""
    priority: str = ""
    created: datetime | None = None
    labels: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("PickableTicket.key must be non-empty")


@dataclass(frozen=True)
class SprintReplanResult:
    """Observable outcome of one hourly re-plan handler run."""

    event: str
    pickable_count: int
    total_free_slots: int
    actions: tuple[Action, ...] = ()
    action_results: tuple[Mapping[str, Any], ...] = ()
    llm_consultation: Mapping[str, Any] | None = None
    scope_review_tickets: tuple[str, ...] = ()
    capacity_insufficient: bool = False
    error: str = ""

    def to_event(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pickable_count": self.pickable_count,
            "total_free_slots": self.total_free_slots,
            "actions": [action.to_record() for action in self.actions],
            "action_results": [dict(row) for row in self.action_results],
            "scope_review_tickets": list(self.scope_review_tickets),
            "capacity_insufficient": self.capacity_insufficient,
        }
        if self.llm_consultation is not None:
            payload["llm_consultation"] = dict(self.llm_consultation)
        if self.error:
            payload["error"] = self.error
        return {
            "source": "timer",
            "trigger": "hourly-sprint-replan",
            "payload": payload,
        }


class SprintReplanGateway(Protocol):
    """External effects used by the hourly re-plan pass."""

    def list_pickable(self, max_results: int) -> Sequence[PickableTicket]: ...

    def add_label(self, key: str, label: str) -> None: ...

    def remove_label(self, key: str, label: str) -> None: ...

    def file_scope_review_ticket(
        self,
        *,
        source_key: str,
        target_area: str,
        description: str,
    ) -> str: ...

    def mention_operator(self, key: str, message: str, *, urgency: str) -> None: ...


class SprintReplanActionLayer:
    """Execute only validated ADR §6.1 actions for sprint re-planning."""

    def __init__(self, gateway: SprintReplanGateway) -> None:
        self._gateway = gateway

    def execute(self, action: Action) -> dict[str, Any]:
        if action.kind == ACTION_RELABEL:
            return self._execute_relabel(action)
        if action.kind == ACTION_FILE_TICKET:
            return self._execute_file_ticket(action)
        if action.kind == ACTION_MENTION_OPERATOR:
            return self._execute_mention(action)
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": False,
            "reason": "unsupported_for_sprint_replan",
        }

    def _execute_relabel(self, action: Action) -> dict[str, Any]:
        add = tuple(str(x) for x in action.params.get("add", ()))
        remove = tuple(str(x) for x in action.params.get("remove", ()))
        for label in add:
            self._gateway.add_label(action.target, label)
        for label in remove:
            self._gateway.remove_label(action.target, label)
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": True,
            "add": list(add),
            "remove": list(remove),
        }

    def _execute_file_ticket(self, action: Action) -> dict[str, Any]:
        key = self._gateway.file_scope_review_ticket(
            source_key=str(action.params.get("blocking") or action.target),
            target_area=str(action.params.get("target_area") or "backend"),
            description=str(action.params.get("description") or ""),
        )
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": True,
            "created_key": key,
        }

    def _execute_mention(self, action: Action) -> dict[str, Any]:
        self._gateway.mention_operator(
            action.target or DEFAULT_OPERATOR_TICKET,
            str(action.params.get("message") or ""),
            urgency=str(action.params.get("urgency") or "medium"),
        )
        return {
            "kind": action.kind,
            "target": action.target or DEFAULT_OPERATOR_TICKET,
            "executed": True,
        }


@dataclass
class SprintReplanHandler:
    """Hourly sprint-level re-plan handler."""

    gateway: SprintReplanGateway
    consultant: Tier2Consultant
    capacity_provider: CapacityProvider
    clock: Clock = field(default=lambda: datetime.now(timezone.utc))
    max_pickable: int = DEFAULT_MAX_PICKABLE

    def run(self) -> SprintReplanResult:
        """Run one re-plan pass and return an observable result."""
        pickable = tuple(self.gateway.list_pickable(self.max_pickable))
        capacity = self.capacity_provider()
        ctx = _decision_context(self.clock(), capacity, pickable)
        outcome = self.consultant.consult(
            ctx,
            behavior=InvestigationMode,
            trigger="sprint_replan_hourly",
        )
        actions = _normalise_actions(
            pickable,
            capacity=capacity,
            llm_actions=outcome.actions,
        )
        action_layer = SprintReplanActionLayer(self.gateway)
        action_results = tuple(action_layer.execute(action) for action in actions)
        created = tuple(
            str(row["created_key"])
            for row in action_results
            if row.get("kind") == ACTION_FILE_TICKET and row.get("created_key")
        )
        return SprintReplanResult(
            event="sprint_replan",
            pickable_count=len(pickable),
            total_free_slots=capacity.total_free_slots,
            actions=actions,
            action_results=action_results,
            llm_consultation=outcome.llm_consultation,
            scope_review_tickets=created,
            capacity_insufficient=len(pickable) > capacity.total_free_slots,
        )


class JiraSprintReplanGateway:
    """Production JIRA adapter for the hourly sprint re-plan."""

    def __init__(self, *, agent_class: str = "subscription-claude") -> None:
        self._agent_class = agent_class
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from backend.agents import jira_dispatch

            self._client = jira_dispatch.make_client(self._agent_class)
        return self._client

    def list_pickable(self, max_results: int) -> Sequence[PickableTicket]:
        from backend.agents import jira_dispatch

        jql = (
            f'project = "{self.client.project_key}" '
            'AND issuetype = Story '
            'AND status = "To Do" '
            'AND assignee is EMPTY '
            'AND status != "Waiting for External" '
            'AND labels not in ("tier:X") '
            'ORDER BY priority DESC, created ASC'
        )
        resp = jira_dispatch._request(
            self.client,
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": [
                    "summary",
                    "labels",
                    "created",
                    "priority",
                    "components",
                ],
                "maxResults": max_results,
            },
        )
        return tuple(_issue_to_pickable(issue) for issue in resp.get("issues", ()))

    def add_label(self, key: str, label: str) -> None:
        from backend.agents import jira_dispatch

        jira_dispatch.add_label(self.client, key, label)

    def remove_label(self, key: str, label: str) -> None:
        from backend.agents import jira_dispatch

        jira_dispatch.remove_label(self.client, key, label)

    def file_scope_review_ticket(
        self,
        *,
        source_key: str,
        target_area: str,
        description: str,
    ) -> str:
        from backend.agents import jira_dispatch

        body = {
            "fields": {
                "project": {"key": self.client.project_key},
                "summary": f"scope-review: {source_key}",
                "description": jira_dispatch._adf_paragraph(
                    "\n".join(
                        [
                            "[sprint-replan scope-review]",
                            f"Source ticket: {source_key}",
                            f"Target area: {target_area}",
                            description,
                        ]
                    )
                ),
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "labels": [
                    SCOPE_REVIEW_LABEL,
                    SPRINT_REPLAN_LABEL,
                    f"area:{target_area}",
                ],
            }
        }
        resp = jira_dispatch._request(self.client, "POST", "/issue", body)
        key = str(resp.get("key") or "")
        if not key:
            raise RuntimeError(f"JIRA POST /issue returned no key: {resp!r}")
        return key

    def mention_operator(self, key: str, message: str, *, urgency: str) -> None:
        from backend.agents import jira_dispatch

        prefix = "@nanakusa-sora "
        jira_dispatch.add_comment(self.client, key, f"{prefix}[{urgency}] {message}")


def build_default_handler(
    *,
    config_dir: Path,
    decision_log_dir: Path,
    capacity_path: Path | None = None,
    agent_class: str = "subscription-claude",
    clock: Clock | None = None,
) -> SprintReplanHandler:
    """Production wiring for the coordinator daemon."""
    from backend.agents.pipeline_coordinator_llm_consultation import (
        BudgetGuard,
        LLMConsultationConfig,
    )

    clock = clock or (lambda: datetime.now(timezone.utc))
    config = LLMConsultationConfig.from_env()
    consultant = Tier2Consultant(
        config=config,
        budget=BudgetGuard.from_decision_log(
            decision_log_dir,
            daily_budget_usd=config.daily_budget_usd,
            clock=clock,
        ),
        clock=clock,
    )
    capacity_path = capacity_path or capacity_path_from_env(config_dir)
    return SprintReplanHandler(
        gateway=JiraSprintReplanGateway(agent_class=agent_class),
        consultant=consultant,
        capacity_provider=lambda: load_capacity_snapshot_from_json(
            capacity_path,
            captured_at=clock(),
        ),
        clock=clock,
    )


def _decision_context(
    now: datetime,
    capacity: CapacitySnapshot,
    pickable: Sequence[PickableTicket],
) -> DecisionContext:
    tickets = {ticket.key: _to_rule_ticket(ticket) for ticket in pickable}
    focal = Ticket(
        key=SPRINT_REPLAN_FOCAL_KEY,
        labels=(SPRINT_REPLAN_LABEL,),
        comments=(
            Comment(
                text=(
                    "Sprint-level re-plan: assign next pickable tickets by "
                    "runner capacity; relabel selected tickets; file "
                    "scope-review tickets for ambiguous scope."
                )
            ),
        ),
        blocked_by=tuple(ticket.key for ticket in pickable),
    )
    return DecisionContext(
        now=now,
        capacity=capacity,
        mode=INVESTIGATION_MODE,
        situation=SituationProfile(
            urgency=Level.LOW,
            risk=Level.MEDIUM,
            novelty=Level.HIGH,
            reversibility=Level.HIGH,
        ),
        ticket=focal,
        tickets=tickets,
    )


def _normalise_actions(
    pickable: Sequence[PickableTicket],
    *,
    capacity: CapacitySnapshot,
    llm_actions: Sequence[Action],
) -> tuple[Action, ...]:
    actions: list[Action] = []
    seen: set[tuple[str, str, str]] = set()
    pickable_keys = {ticket.key for ticket in pickable}
    for action in llm_actions:
        if action.kind not in {ACTION_RELABEL, ACTION_FILE_TICKET, ACTION_MENTION_OPERATOR}:
            continue
        if action.target and action.target not in pickable_keys and action.target != DEFAULT_OPERATOR_TICKET:
            continue
        marker = (action.kind, action.target, repr(sorted(dict(action.params).items())))
        if marker in seen:
            continue
        seen.add(marker)
        actions.append(action)

    if capacity.total_free_slots < len(pickable):
        actions.append(
            Action.mention_operator(
                DEFAULT_OPERATOR_TICKET,
                message=(
                    "[sprint-replan] capacity insufficient: "
                    f"{capacity.total_free_slots} free runner slots for "
                    f"{len(pickable)} pickable tickets."
                ),
                urgency="high",
            )
        )
    return tuple(actions)


def _to_rule_ticket(ticket: PickableTicket) -> Ticket:
    return Ticket(
        key=ticket.key,
        issuetype="Story",
        areas=ticket.areas,
        labels=ticket.labels,
        comments=(Comment(text=ticket.summary),) if ticket.summary else (),
    )


def _issue_to_pickable(issue: Mapping[str, Any]) -> PickableTicket:
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    labels = tuple(str(x) for x in fields.get("labels") or ())
    components = fields.get("components") or ()
    areas = tuple(
        str(component.get("name"))
        for component in components
        if isinstance(component, Mapping) and component.get("name")
    )
    priority = fields.get("priority") if isinstance(fields.get("priority"), Mapping) else {}
    return PickableTicket(
        key=str(issue.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        priority=str(priority.get("name") or ""),
        created=_parse_jira_datetime(fields.get("created")),
        labels=labels,
        areas=areas,
    )


def _parse_jira_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
