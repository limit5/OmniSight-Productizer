"""Track camviewpro contribution PR state back to JIRA (OP-1847).

This is the consumer half of the B2 PR workflow: contribution tickets carry a
``camviewpro-pr:<number>`` label, and this module maps the GitHub PR state to
the narrow JIRA action needed to keep the ticket lifecycle aligned.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Iterable, Literal

import httpx

from backend.agents import jira_dispatch
from backend.agents.github_pr_target import GithubPrError, _github_headers, _parse_repo_url
from backend.agents.scheduler import TicketSnapshot


PR_LABEL_RE = re.compile(r"^camviewpro-pr:(\d+)$")
REGULATED_LANE_LABEL = "regulated-lane"
REGULATORY_CLEARED_REQUIRED_LABEL = "regulatory-cleared-required"
TRIAGE_LABEL = "camviewpro-pr-closed"

PrSyncAction = Literal["none", "to_under_review", "to_done", "flag_closed", "hold_regulated"]


class ContributionPrTrackerError(RuntimeError):
    """The contribution PR tracker could not resolve or sync state."""


@dataclass(frozen=True)
class PrSyncOutcome:
    ticket_key: str
    pr_number: int | None
    pr_state: str | None
    action: PrSyncAction


def _extract_pr_number(labels: Iterable[str]) -> int | None:
    for label in labels:
        match = PR_LABEL_RE.match(str(label))
        if match:
            return int(match.group(1))
    return None


def _pr_label_names(pr: dict) -> set[str]:
    names: set[str] = set()
    for label in pr.get("labels") or []:
        if isinstance(label, dict):
            name = str(label.get("name") or "").strip()
            if name:
                names.add(name)
    return names


async def _resolve_git_account(
    git_account_ref: str,
    *,
    tenant_id: str | None,
) -> dict:
    if not git_account_ref:
        raise ContributionPrTrackerError("git_account_ref is required")

    from backend import git_credentials

    row = await git_credentials.pick_by_id(git_account_ref, tenant_id=tenant_id)
    if row is None:
        raise ContributionPrTrackerError(
            f"git_accounts row {git_account_ref!r} not found"
        )
    return row


def _repo_url_from_row(row: dict) -> str:
    repo_url = str(
        row.get("repo_url")
        or row.get("url")
        or row.get("instance_url")
        or ""
    ).strip()
    if not repo_url:
        raise ContributionPrTrackerError("git_accounts row has no repo URL")
    return repo_url


def _token_from_row(row: dict, account_id: str) -> str:
    token = str(row.get("token") or "").strip()
    if not token:
        raise ContributionPrTrackerError(
            f"git_accounts row {account_id!r} has no token for GitHub PR tracking"
        )
    return token


async def _fetch_pr(repo_url: str, token: str, pr_number: int) -> dict:
    try:
        repo = _parse_repo_url(repo_url)
    except GithubPrError as exc:
        raise ContributionPrTrackerError(str(exc)) from exc

    url = f"{repo.api_base}/repos/{repo.slug}/pulls/{pr_number}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=_github_headers(token))
    if response.status_code >= 400:
        detail = response.text.strip()
        raise ContributionPrTrackerError(
            f"GitHub get pull request failed: HTTP {response.status_code} {detail}"
        )
    return response.json()


def _jira_client() -> jira_dispatch.DispatchClient:
    return jira_dispatch.make_client("subscription-codex")


def _comment_idem(key: str, suffix: str) -> str:
    return f"contribution-pr-{key}-{suffix}-{uuid.uuid4().hex[:12]}"


def _transition_to_done(client: jira_dispatch.DispatchClient, key: str) -> None:
    """Move a contribution ticket to the terminal Done/Published status.

    The OP workflow has historically named the terminal status "Published";
    this dynamic lookup accepts either the explicit "Done" transition or the
    existing deploy/publish transition without adding a new global workflow
    assumption to ``jira_dispatch``.
    """
    transitions = jira_dispatch._request(  # noqa: SLF001 - no public generic transition helper
        client, "GET", f"/issue/{key}/transitions"
    )
    candidates = transitions.get("transitions") or []
    wanted = {"done", "publish", "published", "deploy", "公開済み", "完了"}
    transition_id = None
    for transition in candidates:
        name = str(transition.get("name") or "").strip().lower()
        target = str(
            ((transition.get("to") or {}).get("name") or "")
        ).strip().lower()
        if name in wanted or target in wanted:
            transition_id = str(transition.get("id") or "")
            break
    if not transition_id:
        raise ContributionPrTrackerError(
            f"{key} has no visible Done/Published transition"
        )

    jira_dispatch._request_idempotent(  # noqa: SLF001 - see helper note above
        client,
        "POST",
        f"/issue/{key}/transitions",
        {"transition": {"id": transition_id}},
        f"contribution-pr-{key}-to-done",
    )


def _ensure_under_review(client: jira_dispatch.DispatchClient, key: str) -> None:
    jira_dispatch.transition_to_under_review_if_needed(
        client,
        key,
        idem_key=f"contribution-pr-{key}-to-under-review",
    )


def _mark_done(
    client: jira_dispatch.DispatchClient,
    key: str,
    *,
    merge_sha: str,
) -> None:
    _transition_to_done(client, key)
    jira_dispatch.add_comment(
        client,
        key,
        f"camviewpro PR merged with merge commit {merge_sha}.",
        idem_key=_comment_idem(key, "merged"),
    )


def _hold_regulated(
    client: jira_dispatch.DispatchClient,
    key: str,
    *,
    merge_sha: str,
) -> None:
    jira_dispatch.add_comment(
        client,
        key,
        f"camviewpro PR merged with merge commit {merge_sha}.",
        idem_key=_comment_idem(key, "merged"),
    )
    jira_dispatch.add_label(
        client,
        key,
        REGULATORY_CLEARED_REQUIRED_LABEL,
        idem_key=f"contribution-pr-{key}-regulatory-cleared-required-label",
    )


def _flag_closed(client: jira_dispatch.DispatchClient, key: str, pr_number: int) -> None:
    jira_dispatch.add_comment(
        client,
        key,
        (
            f"camviewpro PR #{pr_number} is closed without merge; "
            "human triage required."
        ),
        idem_key=_comment_idem(key, "closed"),
    )
    jira_dispatch.add_label(
        client,
        key,
        TRIAGE_LABEL,
        idem_key=f"contribution-pr-{key}-closed-label",
    )


async def sync_contribution_pr_state(
    ticket: TicketSnapshot,
    *,
    git_account_ref: str,
    tenant_id: str | None = None,
) -> PrSyncOutcome:
    pr_number = _extract_pr_number(ticket.labels)
    if pr_number is None:
        return PrSyncOutcome(
            ticket_key=ticket.key,
            pr_number=None,
            pr_state=None,
            action="none",
        )

    account = await _resolve_git_account(git_account_ref, tenant_id=tenant_id)
    token = _token_from_row(account, git_account_ref)
    pr = await _fetch_pr(_repo_url_from_row(account), token, pr_number)

    state = str(pr.get("state") or "").lower()
    merged = bool(pr.get("merged"))
    client = _jira_client()

    if state == "open" and not merged:
        _ensure_under_review(client, ticket.key)
        return PrSyncOutcome(ticket.key, pr_number, "open", "to_under_review")

    if merged:
        merge_sha = str(pr.get("merge_commit_sha") or "").strip()
        if REGULATED_LANE_LABEL in _pr_label_names(pr):
            _hold_regulated(client, ticket.key, merge_sha=merge_sha or "<unknown>")
            return PrSyncOutcome(ticket.key, pr_number, "merged", "hold_regulated")
        _mark_done(client, ticket.key, merge_sha=merge_sha or "<unknown>")
        return PrSyncOutcome(ticket.key, pr_number, "merged", "to_done")

    if state == "closed":
        _flag_closed(client, ticket.key, pr_number)
        return PrSyncOutcome(ticket.key, pr_number, "closed", "flag_closed")

    return PrSyncOutcome(ticket.key, pr_number, state or None, "none")


async def sync_all_open_contributions(
    tickets: Iterable[TicketSnapshot],
    *,
    git_account_ref: str,
    tenant_id: str | None = None,
) -> list[PrSyncOutcome]:
    outcomes: list[PrSyncOutcome] = []
    for ticket in tickets:
        outcomes.append(
            await sync_contribution_pr_state(
                ticket,
                git_account_ref=git_account_ref,
                tenant_id=tenant_id,
            )
        )
    return outcomes
