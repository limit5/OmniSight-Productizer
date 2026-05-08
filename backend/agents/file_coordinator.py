"""OP-734 file-touch graph coordinator.

Builds same-file ticket chains from open JIRA tickets and serializes each
chain with JIRA ``Blocks`` issue links. The runner still checks the links
at pickup time; this module creates the planning-layer ordering.
"""
from __future__ import annotations

import logging
import urllib.error
from dataclasses import dataclass
from typing import Mapping, Sequence

from backend.agents import jira_dispatch
from backend.agents.scheduler import TicketSnapshot

log = logging.getLogger(__name__)

SKIP_FILE_COORDINATOR_LABEL = "skip-file-coordinator"
PUBLISHED_STATES = {"公開済み", "Published"}
FILE_GRAPH_JQL = 'project = OP AND status in ("To Do", "進行中", "Under Review")'


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
    """Create a JIRA issue link where ``inward`` blocks ``outward``."""
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
    """Return whether ``snapshot`` has a blocker that is not published."""
    try:
        blockers = jira_get_blocked_by(client, snapshot.key)
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        return False, f"blockedBy check skipped: {type(exc).__name__}: {exc}"
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
        return False, f"blockedBy state check skipped: {type(exc).__name__}: {exc}"
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
