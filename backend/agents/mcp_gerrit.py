"""META OP-814 / A6 — MCP-Gerrit integration (read-only).

Same pattern as A5 (META OP-808 JIRA MCP) but for Gerrit. Exposes a
minimal MCP-shaped server, named ``mcp_gerrit``, that lets the agent:

  * call ``mcp_gerrit__queryChanges(filter)`` — issue arbitrary
    ``gerrit query`` filters and parse the JSON response,
  * call ``mcp_gerrit__getReview(change_id)`` — fetch one change's
    current-patch-set + review comments + approval votes,
  * call ``mcp_gerrit__hasOpenPsForTicket(ticket_key)`` — the
    idempotency-check helper used by the synthetic acceptance test
    ("model checks if its ticket has open PS before starting work").

All three tools are *read-only*. PS push stays in
:func:`backend.agents.jira_dispatch.push_to_gerrit_for_review` so the
runner's ``[runner-pushed-to-gerrit]`` JIRA-comment audit trail is
preserved (AC #3).

Implementation reuses the SSH key + bot-username resolution from
``jira_dispatch`` so we never duplicate the per-instance bot identity
logic (OP-783). Each subprocess call is gated by the existing
``BREAKERS["gerrit_ssh"]`` circuit breaker so an unreachable Gerrit
falls back to the same recovery path as the rest of the runner.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from backend.agents.circuit_breaker import BREAKERS
from backend.agents.jira_dispatch import (
    GERRIT_PROJECT_PATH,
    GERRIT_SSH_HOST,
    GERRIT_SSH_PORT,
    _gerrit_auth_for_instance,
)

logger = logging.getLogger(__name__)


SERVER_NAME = "mcp_gerrit"
"""Tool-name prefix exposed to the agent (``mcp_gerrit__<method>``)."""

# ─── Tunable constants ───────────────────────────────────────────
# Per-subprocess SSH-CLI ceiling. Sized to comfortably absorb a slow
# Gerrit query without indefinitely blocking the circuit breaker.
DEFAULT_TIMEOUT_SECS = 30

# Maximum stderr bytes embedded in a ``RuntimeError`` when the
# gerrit-ssh-cli call fails. Keeps a single broken call from flooding
# the agent's tool-result transcript with multi-KB SSH banners.
STDERR_TRUNCATE_LIMIT = 500

# HTTPS port that fronts Gerrit's web UI / REST surface. Distinct from
# ``GERRIT_SSH_PORT`` (the gerrit-ssh-cli port) — used only to synthesize
# fallback change URLs when ``gerrit query`` does not return one.
GERRIT_WEB_PORT = 29420

# Default cap on rows returned by ``query_changes``. Prevents a runaway
# model query from pulling a multi-MB JSON payload through the audit
# log; the caller can override per-call up to ``MAX_QUERY_CHANGES_LIMIT``.
DEFAULT_QUERY_CHANGES_LIMIT = 25

# Validation bounds advertised in the MCP tool schema for the
# ``queryChanges.limit`` argument. Upper bound matches Gerrit's own
# server-side default page size, lower bound rejects ``0`` / negative
# limits that would silently return an empty list.
MIN_QUERY_CHANGES_LIMIT = 1
MAX_QUERY_CHANGES_LIMIT = 100

# Row cap for the idempotency check in ``has_open_ps_for_ticket``. The
# subject-match filter is already narrow, so a small window is enough
# to spot a sibling open PS without scanning the full open-PS backlog.
OPEN_PS_LOOKUP_LIMIT = 5


@dataclass(frozen=True)
class GerritChangeSummary:
    """One row from a ``gerrit query`` result."""

    change_number: int
    change_id: str
    subject: str
    status: str
    owner: str
    project: str
    branch: str
    url: str

    def to_json(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "change_id": self.change_id,
            "subject": self.subject,
            "status": self.status,
            "owner": self.owner,
            "project": self.project,
            "branch": self.branch,
            "url": self.url,
        }


@dataclass(frozen=True)
class GerritReviewDetails:
    """Detailed view of one change including comments + approvals."""

    change_number: int
    change_id: str
    subject: str
    status: str
    owner: str
    current_revision: str | None
    approvals: tuple[dict[str, Any], ...]
    comments: tuple[dict[str, Any], ...]
    url: str

    def to_json(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "change_id": self.change_id,
            "subject": self.subject,
            "status": self.status,
            "owner": self.owner,
            "current_revision": self.current_revision,
            "approvals": list(self.approvals),
            "comments": list(self.comments),
            "url": self.url,
        }


def _ssh_argv(
    extra_args: list[str],
    *,
    agent_class: str,
    instance_id: str | None,
) -> list[str]:
    """Build the gerrit-ssh-cli argv used by ``jira_dispatch`` callers."""
    bot_username, ssh_key = _gerrit_auth_for_instance(agent_class, instance_id)
    return [
        "ssh",
        "-i", str(ssh_key),
        "-p", str(GERRIT_SSH_PORT),
        f"{bot_username}@{GERRIT_SSH_HOST}",
        *extra_args,
    ]


def _run_gerrit_query(
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> str:
    """Run a gerrit-ssh-cli subprocess through the shared circuit breaker."""
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gerrit-ssh-cli failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:STDERR_TRUNCATE_LIMIT]}"
        )
    return result.stdout


def _iter_gerrit_json_lines(stdout: str):
    """Yield decoded JSON dicts from a ``--format=JSON`` stdout, skipping
    the trailing ``{"type": "stats", ...}`` row."""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "stats":
            continue
        yield obj


def _build_change_url(change: dict[str, Any]) -> str:
    url = str(change.get("url") or "")
    if url:
        return url
    number = change.get("number") or change.get("_number") or "?"
    return f"https://{GERRIT_SSH_HOST}:{GERRIT_WEB_PORT}/c/{GERRIT_PROJECT_PATH}/+/{number}"


def _parse_change_summary(change: dict[str, Any]) -> GerritChangeSummary | None:
    number = change.get("number") or change.get("_number")
    if number is None:
        return None
    try:
        change_number = int(number)
    except (TypeError, ValueError):
        return None
    owner_blob = change.get("owner") or {}
    return GerritChangeSummary(
        change_number=change_number,
        change_id=str(change.get("id") or ""),
        subject=str(change.get("subject") or ""),
        status=str(change.get("status") or ""),
        owner=str(owner_blob.get("username") or owner_blob.get("email") or ""),
        project=str(change.get("project") or ""),
        branch=str(change.get("branch") or ""),
        url=_build_change_url(change),
    )


# ─── Read-only tools exposed via mcp_gerrit__* ────────────────────


def query_changes(
    filter: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
    limit: int = DEFAULT_QUERY_CHANGES_LIMIT,
) -> list[GerritChangeSummary]:
    """Run ``gerrit query --format=JSON <filter>`` and return parsed rows.

    ``filter`` is a free-form Gerrit search expression, e.g.
    ``"is:open owner:claude-bot project:omnisight/OmniSight-Productizer"``.
    Caller is responsible for whitespace-quoting; the SSH layer does not
    re-shell-expand.

    ``limit`` caps the returned rows so a runaway model query can't pull
    a multi-MB JSON payload through the audit log.
    """
    if not filter or not filter.strip():
        raise ValueError("query_changes: filter must be a non-empty string")
    argv = _ssh_argv(
        ["gerrit", "query", "--format=JSON", filter],
        agent_class=agent_class,
        instance_id=instance_id,
    )
    stdout = _run_gerrit_query(argv)
    rows: list[GerritChangeSummary] = []
    for obj in _iter_gerrit_json_lines(stdout):
        summary = _parse_change_summary(obj)
        if summary is None:
            continue
        rows.append(summary)
        if len(rows) >= limit:
            break
    return rows


def get_review(
    change_id: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
) -> GerritReviewDetails | None:
    """Fetch one change's review state + current patch-set metadata.

    ``change_id`` may be either a Gerrit Change-Id (``I…``) or a numeric
    change number — both are accepted by ``gerrit query change:<id>``.
    Returns None if Gerrit returns no rows.
    """
    if not change_id or not str(change_id).strip():
        raise ValueError("get_review: change_id must be a non-empty string")

    argv = _ssh_argv(
        [
            "gerrit", "query", "--format=JSON",
            "--current-patch-set",
            "--all-approvals",
            "--comments",
            f"change:{change_id}",
        ],
        agent_class=agent_class,
        instance_id=instance_id,
    )
    stdout = _run_gerrit_query(argv)
    for obj in _iter_gerrit_json_lines(stdout):
        number = obj.get("number") or obj.get("_number")
        if number is None:
            continue
        try:
            change_number = int(number)
        except (TypeError, ValueError):
            continue
        owner_blob = obj.get("owner") or {}
        current_ps = obj.get("currentPatchSet") or {}
        approvals = tuple(current_ps.get("approvals") or ())
        comments_raw = obj.get("comments") or []
        comments = tuple(
            {
                "timestamp": c.get("timestamp"),
                "reviewer": (c.get("reviewer") or {}).get("username")
                or (c.get("reviewer") or {}).get("email"),
                "message": c.get("message"),
            }
            for c in comments_raw
        )
        return GerritReviewDetails(
            change_number=change_number,
            change_id=str(obj.get("id") or ""),
            subject=str(obj.get("subject") or ""),
            status=str(obj.get("status") or ""),
            owner=str(owner_blob.get("username") or owner_blob.get("email") or ""),
            current_revision=str(current_ps.get("revision") or "") or None,
            approvals=approvals,
            comments=comments,
            url=_build_change_url(obj),
        )
    return None


def has_open_ps_for_ticket(
    ticket_key: str,
    *,
    agent_class: str = "subscription-claude",
    instance_id: str | None = None,
) -> bool:
    """Return True if Gerrit has an *open* PS whose subject mentions
    ``ticket_key``.

    Used by the META OP-814 synthetic acceptance: the agent calls this
    before starting work so the launcher's idempotency check is also
    visible inside the model's reasoning context (rather than only at
    the runner-level pre-flight gate).
    """
    if not ticket_key or not ticket_key.strip():
        raise ValueError("has_open_ps_for_ticket: ticket_key required")
    filter = (
        f"is:open project:{GERRIT_PROJECT_PATH} "
        f"message:{ticket_key.strip()}"
    )
    rows = query_changes(
        filter,
        agent_class=agent_class,
        instance_id=instance_id,
        limit=OPEN_PS_LOOKUP_LIMIT,
    )
    return any(ticket_key in r.subject for r in rows)


# ─── MCP-shaped dispatch surface (mcp_gerrit__<method>) ───────────


def _tool_query_changes(input: dict[str, Any]) -> list[dict[str, Any]]:
    rows = query_changes(
        filter=input["filter"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
        limit=int(input.get("limit", DEFAULT_QUERY_CHANGES_LIMIT)),
    )
    return [r.to_json() for r in rows]


def _tool_get_review(input: dict[str, Any]) -> dict[str, Any] | None:
    review = get_review(
        change_id=input["change_id"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
    )
    return review.to_json() if review is not None else None


def _tool_has_open_ps_for_ticket(input: dict[str, Any]) -> dict[str, Any]:
    has_open = has_open_ps_for_ticket(
        ticket_key=input["ticket_key"],
        agent_class=input.get("agent_class", "subscription-claude"),
        instance_id=input.get("instance_id"),
    )
    return {"ticket_key": input["ticket_key"], "has_open_ps": has_open}


# Read-only by design — every entry below is a query, never a mutation.
# Keep this dict in lock-step with ``MCP_GERRIT_TOOL_SCHEMAS`` so the
# registration in :mod:`mcp_integration` cannot drift.
TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    f"{SERVER_NAME}__queryChanges": _tool_query_changes,
    f"{SERVER_NAME}__getReview": _tool_get_review,
    f"{SERVER_NAME}__hasOpenPsForTicket": _tool_has_open_ps_for_ticket,
}


MCP_GERRIT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": f"{SERVER_NAME}__queryChanges",
        "description": (
            "Run a Gerrit search (``gerrit query --format=JSON <filter>``) "
            "and return parsed change rows. Read-only. Use to inspect "
            "open patchsets the bot owns, recent merged work for similar "
            "tickets, or sibling Change-Ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Gerrit search expression, e.g. "
                        "'is:open owner:claude-bot project:omnisight/OmniSight-Productizer'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": MIN_QUERY_CHANGES_LIMIT,
                    "maximum": MAX_QUERY_CHANGES_LIMIT,
                    "default": DEFAULT_QUERY_CHANGES_LIMIT,
                },
            },
            "required": ["filter"],
        },
    },
    {
        "name": f"{SERVER_NAME}__getReview",
        "description": (
            "Fetch one change's current patch-set + review approvals + "
            "comments. Read-only. Accepts either a Change-Id ('I...') or "
            "a numeric change number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string"},
            },
            "required": ["change_id"],
        },
    },
    {
        "name": f"{SERVER_NAME}__hasOpenPsForTicket",
        "description": (
            "Return ``{has_open_ps: bool}`` for a JIRA ticket key. "
            "Read-only idempotency check the agent calls before starting "
            "work to catch a sibling open PS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_key": {
                    "type": "string",
                    "description": "JIRA ticket key, e.g. 'OP-814'.",
                },
            },
            "required": ["ticket_key"],
        },
    },
]


def is_mcp_gerrit_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` is one of the registered Gerrit MCP tools."""
    return tool_name in TOOL_HANDLERS


def dispatch_tool(tool_name: str, input: dict[str, Any]) -> Any:
    """Execute a registered ``mcp_gerrit__*`` tool call."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise KeyError(
            f"Unknown mcp_gerrit tool {tool_name!r}; "
            f"known: {sorted(TOOL_HANDLERS)}"
        )
    return handler(input)
