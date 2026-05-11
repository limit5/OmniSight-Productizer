"""OP-947 H2 — JIRA-side event handlers for the L3 conductor.

Per ADR-0018 §Auth-2 the JIRA webhook is bearer-token protected at the
HTTP edge — by the time we reach this module the secret has already
been validated. Our job here is the *dispatch action* spelled out in
the matrix:

* row 5 (transition to 公開済み / Done) — read the changelog, walk the
  blockedBy graph, surface "advance next child" hint for the runner.
* row 6 (transition to 進行中 / In Progress) — dashboard breadcrumb,
  no graph advance.
* row 7 (label set/unset on release:* / hotfix:*) — update the
  release META's child set.

We do NOT call the JIRA REST API here — the runner's pickup gate does
that already, and re-issuing the transition would create the duplicate
operator-visible side effect that ADR-0018 §Failure modes /
duplicate-delivery warns against. The handler returns a classified
hint and the result lands in ``handler_result_json``.
"""
from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# Status name → bucket. JIRA Cloud serves both English and Japanese
# names depending on tenant locale; the L3 receiver normalises both.
_DONE_NAMES = frozenset({"Done", "Closed", "Resolved", "公開済み"})
_PROGRESS_NAMES = frozenset({"In Progress", "進行中"})


def _status_change(event: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(field, from_value, to_value)`` for the first status
    item in the changelog, or ``("", "", "")`` if none."""
    changelog = event.get("changelog") or {}
    items = changelog.get("items") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("field") == "status":
            return (
                "status",
                str(item.get("fromString") or item.get("from") or ""),
                str(item.get("toString") or item.get("to") or ""),
            )
    return ("", "", "")


def _label_changes(event: dict[str, Any]) -> list[dict[str, str]]:
    changelog = event.get("changelog") or {}
    items = changelog.get("items") or []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("field") == "labels":
            out.append(
                {
                    "from": str(item.get("fromString") or ""),
                    "to": str(item.get("toString") or ""),
                }
            )
    return out


def on_issue_updated(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 rows 5 / 6 / 7 — dispatched by changelog content.

    Priority order matches the ADR:

    1. If the changelog includes a status transition into a "done"
       bucket name (公開済み / Done / Closed / Resolved), return a
       ``advance_next`` hint with the issue key.
    2. Otherwise, if the status went to 進行中 / In Progress, return
       a ``dashboard_breadcrumb`` hint.
    3. Otherwise, if the changelog includes label changes touching
       a ``release:*`` / ``hotfix:*`` namespace, return a
       ``meta_child_set_updated`` hint.
    4. Otherwise: ``outcome="ignored"`` — every JIRA issue update fires
       this webhook and most are routine field edits.
    """
    issue = event.get("issue") or {}
    issue_key = str(issue.get("key") or "")

    _, from_status, to_status = _status_change(event)
    if to_status in _DONE_NAMES:
        logger.info(
            "release_conductor.jira.advance_next issue=%s from=%s to=%s",
            issue_key,
            from_status,
            to_status,
        )
        return {
            "outcome": "advance_next",
            "issue_key": issue_key,
            "from_status": from_status,
            "to_status": to_status,
        }
    if to_status in _PROGRESS_NAMES:
        logger.info(
            "release_conductor.jira.dashboard_breadcrumb issue=%s to=%s",
            issue_key,
            to_status,
        )
        return {
            "outcome": "dashboard_breadcrumb",
            "issue_key": issue_key,
            "from_status": from_status,
            "to_status": to_status,
        }

    label_diffs = _label_changes(event)
    interesting = [
        d
        for d in label_diffs
        if (d["to"].startswith("release:") or d["to"].startswith("hotfix:"))
        or (d["from"].startswith("release:") or d["from"].startswith("hotfix:"))
    ]
    if interesting:
        logger.info(
            "release_conductor.jira.meta_child_set_updated issue=%s diffs=%s",
            issue_key,
            interesting,
        )
        return {
            "outcome": "meta_child_set_updated",
            "issue_key": issue_key,
            "label_diffs": interesting,
        }

    return {"outcome": "ignored", "issue_key": issue_key}
