"""OP-834 TDD applicability resolution from JIRA issue metadata."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from backend.agents import jira_dispatch


TDD_VALUES = frozenset({"yes", "no", "conditional"})
DEFAULT_TDD_APPLICABILITY = "conditional"
LABEL_PREFIX = "tdd:"
ENV_FIELD_KEY = "OMNISIGHT_JIRA_TDD_FIELD"


@dataclass(frozen=True)
class TDDApplicability:
    value: str
    source: str

    @property
    def active_without_locator(self) -> bool:
        return self.value == "yes"


def _normalise(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "name"):
            normalised = _normalise(value.get(key))
            if normalised is not None:
                return normalised
        return None
    if isinstance(value, list):
        for item in value:
            normalised = _normalise(item)
            if normalised is not None:
                return normalised
        return None
    text = str(value).strip().lower()
    return text if text in TDD_VALUES else None


def resolve_from_issue(
    issue: dict[str, Any],
    *,
    field_key: str | None = None,
) -> TDDApplicability:
    """Resolve per-ticket TDD applicability.

    Lookup order mirrors the ticket AC: configured JIRA custom field first,
    then labels ``tdd:yes`` / ``tdd:no`` / ``tdd:conditional``, then the
    default ``conditional``.
    """
    fields = issue.get("fields") or {}
    configured_key = field_key or os.environ.get(ENV_FIELD_KEY, "").strip()
    if configured_key:
        value = _normalise(fields.get(configured_key))
        if value is not None:
            return TDDApplicability(value=value, source=f"field:{configured_key}")

    for label in fields.get("labels") or []:
        if not isinstance(label, str):
            continue
        lower = label.strip().lower()
        if not lower.startswith(LABEL_PREFIX):
            continue
        value = lower.split(":", 1)[1]
        if value in TDD_VALUES:
            return TDDApplicability(value=value, source=f"label:{lower}")

    return TDDApplicability(value=DEFAULT_TDD_APPLICABILITY, source="default")


def fetch_for_ticket(
    client: jira_dispatch.DispatchClient,
    ticket_key: str,
    *,
    field_key: str | None = None,
) -> TDDApplicability:
    """Fetch JIRA labels plus optional custom field and resolve applicability."""
    configured_key = field_key or os.environ.get(ENV_FIELD_KEY, "").strip()
    fields = ["labels"]
    if configured_key:
        fields.append(configured_key)
    issue = jira_dispatch._request(
        client,
        "GET",
        f"/issue/{ticket_key}?fields={','.join(fields)}",
    )
    return resolve_from_issue(issue, field_key=configured_key)
