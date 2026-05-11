"""OP-734 file-touch graph coordinator.

Builds same-file ticket chains from open JIRA tickets and serializes each
chain with JIRA ``Blocks`` issue links. The runner still checks the links
at pickup time; this module creates the planning-layer ordering.
"""
from __future__ import annotations

import logging
import os
import urllib.error
from dataclasses import dataclass
from typing import Mapping, Sequence

from backend.agents import jira_dispatch
from backend.agents.scheduler import TicketSnapshot

log = logging.getLogger(__name__)

SKIP_FILE_COORDINATOR_LABEL = "skip-file-coordinator"
PUBLISHED_STATES = {"公開済み", "Published"}
FILE_GRAPH_JQL = 'project = OP AND status in ("To Do", "進行中", "Under Review")'

# OP-874: when this env var is "1"/"true"/"yes" (case-insensitive), the
# blockedBy check in ``has_unresolved_blockedby`` flips fail-open → fail-closed:
# an exception talking to JIRA returns ``(True, "skipped")`` so the runner
# refuses to pick up the ticket until JIRA recovers, rather than allowing
# pickup and risking a stale-blocker merge-conflict cascade.
BLOCKEDBY_FAIL_CLOSED_ENV = "OMNISIGHT_BLOCKEDBY_FAIL_CLOSED"

# OP-874: marker emitted on the blocked ticket whenever ``add_blocked_by``
# wires a blockedBy link. The audit script (``scripts/audit_blockedby_directions.py``)
# uses this marker — along with the legacy ``[file-coordinator] Linked blockedBy``
# marker emitted by ``serialize_file_chains`` — to recover the operator-intended
# direction of each link and flag any that are reversed.
ADD_BLOCKED_BY_COMMENT_PREFIX = "[blocked-by-link]"


class BlockedByLinkAlreadyExists(RuntimeError):
    """``add_blocked_by`` no-op: the requested link already exists.

    Idempotent callers should swallow this; loud callers (audit ``--fix``)
    use it to detect a redundant restore attempt.
    """


class BlockedByLinkSelfReference(ValueError):
    """``add_blocked_by(X, X)`` — defensive guard against self-links."""


class BlockedByAuditDirectionMismatch(RuntimeError):
    """Auditor found a Blocks link whose direction contradicts operator intent."""


def _fail_closed_enabled() -> bool:
    """Return True iff ``OMNISIGHT_BLOCKEDBY_FAIL_CLOSED`` is set truthy.

    Default OFF preserves the historical fail-open behaviour. Operators
    flip this on in prod once they trust the audit + helper to keep the
    blockedBy graph healthy.
    """
    raw = os.environ.get(BLOCKEDBY_FAIL_CLOSED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FileCoordinatorTicket:
    """Minimal ticket view needed by the file coordinator."""

    key: str
    labels: tuple[str, ...]
    description: str
    created_at: str


def extract_files_from_ticket(snapshot: TicketSnapshot | FileCoordinatorTicket) -> set[str]:
    """Return expected target files using the R1 file-mutex prediction helper."""
    if SKIP_FILE_COORDINATOR_LABEL in set(getattr(snapshot, "labels", ())):
        return set()
    return jira_dispatch.predict_target_files(
        snapshot, description=getattr(snapshot, "description", "")
    )


def jira_search_jql(
    client: jira_dispatch.DispatchClient,
    jql: str,
) -> list[FileCoordinatorTicket]:
    """Fetch open tickets for graph building."""
    resp = jira_dispatch._request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["labels", "description", "created", "status"],
        "maxResults": 100,
    })
    tickets: list[FileCoordinatorTicket] = []
    for issue in resp.get("issues", []):
        fields = issue.get("fields") or {}
        tickets.append(
            FileCoordinatorTicket(
                key=issue.get("key", "?"),
                labels=tuple(fields.get("labels") or ()),
                description=_adf_to_text(fields.get("description")),
                created_at=str(fields.get("created") or ""),
            )
        )
    return tickets


def build_file_graph(
    client: jira_dispatch.DispatchClient,
    tenant: str = "t-default",
) -> dict[str, list[str]]:
    """Group open tickets by predicted target file, ordered by JIRA creation time."""
    del tenant  # Reserved for future multi-tenant JIRA projects.
    tickets = jira_search_jql(client, FILE_GRAPH_JQL)
    created_by_key = {ticket.key: ticket.created_at for ticket in tickets}
    graph: dict[str, list[str]] = {}
    for ticket in tickets:
        for path in extract_files_from_ticket(ticket):
            graph.setdefault(path, []).append(ticket.key)

    for path, keys in graph.items():
        keys.sort(key=lambda key: (created_by_key.get(key, ""), key))
    return {path: keys for path, keys in graph.items() if len(keys) >= 2}


def serialize_file_chains(
    client: jira_dispatch.DispatchClient,
    graph: Mapping[str, Sequence[str]],
) -> int:
    """Create idempotent ``blockedBy`` chains for each same-file ticket list."""
    created = 0
    cyclic_pairs = _cyclic_adjacent_pairs(graph)
    for path, ticket_chain in graph.items():
        for index in range(1, len(ticket_chain)):
            blocker = ticket_chain[index - 1]
            blocked = ticket_chain[index]
            if (blocker, blocked) in cyclic_pairs:
                log.warning(
                    "Skipping file-coordinator cycle for %s: %s <-> %s",
                    path, blocker, blocked,
                )
                continue
            if jira_link_exists(client, blocked=blocked, blocker=blocker, link_type="Blocks"):
                continue
            jira_create_issue_link(
                client, inward=blocker, outward=blocked, link_type="Blocks",
            )
            jira_dispatch.add_comment(
                client,
                blocked,
                (
                    f"[file-coordinator] Linked blockedBy {blocker} "
                    f"(both touch `{path}`). Serialized to prevent "
                    f"merge-conflict cascade. Pickup deferred until "
                    f"{blocker} is 公開済み."
                ),
                idem_key=f"file-coordinator-{blocked}-{blocker}",
            )
            created += 1
        log.info("Serialized %d tickets on %s", len(ticket_chain), path)
    return created


def jira_link_exists(
    client: jira_dispatch.DispatchClient,
    *,
    blocked: str,
    blocker: str,
    link_type: str = "Blocks",
) -> bool:
    """Return True when ``blocked`` already has a ``blockedBy`` link to ``blocker``."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{blocked}?fields=issuelinks")
    for link in ((issue.get("fields") or {}).get("issuelinks") or []):
        if ((link.get("type") or {}).get("name")) != link_type:
            continue
        inward = link.get("inwardIssue") or {}
        outward = link.get("outwardIssue") or {}
        if inward.get("key") == blocker or outward.get("key") == blocker:
            return True
    return False


def jira_create_issue_link(
    client: jira_dispatch.DispatchClient,
    *,
    inward: str,
    outward: str,
    link_type: str = "Blocks",
) -> None:
    """Create a JIRA issue link where ``inward`` blocks ``outward``.

    .. deprecated:: OP-874
       The ``inward``/``outward`` parameter naming mirrors the raw
       Atlassian REST schema and is easy to invert (see Gerrit #387 /
       OP-858). New operator code should call :func:`add_blocked_by`,
       which takes intent-named ``blocked_key``/``blocker_key`` arguments
       and is impossible to flip silently. This low-level helper stays
       for ``serialize_file_chains`` and for the audit script's ``--fix``
       restore path, both of which have to speak the raw schema.
    """
    jira_dispatch._request_idempotent(
        client,
        "POST",
        "/issueLink",
        {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward},
            "outwardIssue": {"key": outward},
        },
        f"file-coordinator-link-{inward}-{outward}",
    )


def add_blocked_by(
    client: jira_dispatch.DispatchClient,
    blocked_key: str,
    blocker_key: str,
    *,
    reason: str | None = None,
    link_type: str = "Blocks",
) -> bool:
    """Wire ``blocked_key`` as blockedBy ``blocker_key`` with intent-safe args.

    Wraps :func:`jira_create_issue_link` with explicit ``blocked`` /
    ``blocker`` parameter names so an operator cannot silently invert
    the link direction. Idempotent: if the link already exists the
    helper returns ``False`` without re-posting. A ``reason`` string,
    when supplied, lands as a ``[blocked-by-link]`` comment on the
    blocked ticket so the audit script can verify operator intent
    against the link's actual direction.

    Returns ``True`` when a new link was created, ``False`` when the
    link already existed (idempotent no-op).

    Raises :class:`BlockedByLinkSelfReference` when ``blocked_key ==
    blocker_key``.
    """
    if blocked_key == blocker_key:
        raise BlockedByLinkSelfReference(
            f"add_blocked_by refused self-link: {blocked_key} cannot block itself"
        )
    if jira_link_exists(
        client, blocked=blocked_key, blocker=blocker_key, link_type=link_type
    ):
        log.info(
            "add_blocked_by idempotent no-op: %s already blockedBy %s",
            blocked_key,
            blocker_key,
        )
        return False
    jira_create_issue_link(
        client, inward=blocker_key, outward=blocked_key, link_type=link_type,
    )
    if reason:
        jira_dispatch.add_comment(
            client,
            blocked_key,
            f"{ADD_BLOCKED_BY_COMMENT_PREFIX} blockedBy {blocker_key}: {reason}",
            idem_key=f"add-blocked-by-{blocked_key}-{blocker_key}",
        )
    return True


def jira_get_blocked_by(
    client: jira_dispatch.DispatchClient,
    key: str,
) -> list[str]:
    """Return issue keys linked as blockers of ``key``."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=issuelinks")
    blockers: list[str] = []
    for link in ((issue.get("fields") or {}).get("issuelinks") or []):
        if ((link.get("type") or {}).get("name")) != "Blocks":
            continue
        inward = link.get("inwardIssue") or {}
        if inward.get("key"):
            blockers.append(inward["key"])
    return blockers


def jira_get_state(client: jira_dispatch.DispatchClient, key: str) -> str:
    """Return the JIRA status name for one ticket."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=status")
    return str((((issue.get("fields") or {}).get("status") or {}).get("name")) or "")


def has_unresolved_blockedby(
    client: jira_dispatch.DispatchClient,
    snapshot: TicketSnapshot,
) -> tuple[bool, str]:
    """Return whether ``snapshot`` has a blocker that is not published.

    Exception handling honours :data:`BLOCKEDBY_FAIL_CLOSED_ENV` (OP-874):
    default is fail-open (return ``(False, "skipped")`` — allow pickup),
    flipping the env var truthy fails closed (``(True, "skipped")`` —
    block pickup) so a wedged JIRA cannot silently bypass the gate.
    """
    fail_closed = _fail_closed_enabled()
    try:
        blockers = jira_get_blocked_by(client, snapshot.key)
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        reason = (
            f"blockedBy check skipped (fail-closed): {type(exc).__name__}: {exc}"
            if fail_closed
            else f"blockedBy check skipped: {type(exc).__name__}: {exc}"
        )
        return fail_closed, reason
    try:
        for blocker_key in blockers:
            reverse_blockers = jira_get_blocked_by(client, blocker_key)
            if snapshot.key in reverse_blockers:
                log.warning(
                    "JIRA Blocks cycle detected; treating as no-block for operator review: %s <-> %s",
                    snapshot.key,
                    blocker_key,
                )
                continue
            state = jira_get_state(client, blocker_key)
            if state not in PUBLISHED_STATES:
                return True, f"blocked by {blocker_key} (state={state})"
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        reason = (
            f"blockedBy state check skipped (fail-closed): {type(exc).__name__}: {exc}"
            if fail_closed
            else f"blockedBy state check skipped: {type(exc).__name__}: {exc}"
        )
        return fail_closed, reason
    return False, "all blockers resolved"


def _cyclic_adjacent_pairs(graph: Mapping[str, Sequence[str]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for chain in graph.values():
        for index in range(1, len(chain)):
            pairs.add((chain[index - 1], chain[index]))
    return {pair for pair in pairs if (pair[1], pair[0]) in pairs}


def _adf_to_text(node: object) -> str:
    chunks: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            node_type = value.get("type")
            if node_type == "text":
                chunks.append(str(value.get("text", "")))
            elif node_type == "hardBreak":
                chunks.append("\n")
            else:
                for child in value.get("content", []) or []:
                    walk(child)
                if node_type in {"paragraph", "codeBlock"}:
                    chunks.append("\n")
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return "".join(chunks)
