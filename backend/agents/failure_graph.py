"""OP-858 (C8) — Failure-Graph from cross-incident causality.

Builds a directed graph over runner failure incidents (the rows
produced by C2's ``runner_incidents`` table) so the runner can answer
"what cascaded into this ticket's prior failure?" at pickup time and so
operators can render a 7-day failure landscape for incident review.

Causality model (three edge kinds, all directed from earlier → later):

* ``same_mutex_window`` — incidents X and Y share a ``mutex_label`` and
  occurred within a configurable window (default 1h). Captures the
  pattern where the same file lock chains incidents across tickets.
* ``same_ticket`` — incidents X and Y carry the same ``ticket_key`` and
  are chained in occurrence order. Captures "ticket failed N times in
  a row".
* ``same_failure_class`` — incidents X and Y share a ``failure_class``
  cluster. Captures fleet-wide bursts of the same failure mode (e.g.
  every runner hitting "Missing tree" in the same 2-hour window).

Cognee integration is optional. The graph is derived data — the
authoritative store is ``runner_incidents`` (C2). When the C3 Cognee KG
layer is unavailable the module raises
:class:`FailureGraphCogneeUnavailable` so the caller can degrade to the
direct Postgres / in-memory query path; no data is lost because a full
rebuild always reconstitutes the graph from C2.

This module is intentionally pure-Python with no required external
dependencies: the in-memory incident source and edge inference run as a
deterministic function on a list of dataclasses, which makes unit tests
exercise the same code path that production uses against the Postgres
projection.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────


DEFAULT_QUERY_TIMEOUT_SEC: float = 30.0
"""Hard cap on a single ``get_failure_graph_neighbors`` query. Per AC
error catalog, callers receive a partial result on overrun rather than
an exception bubbling all the way out."""


DEFAULT_MUTEX_WINDOW_SEC: int = 3600
"""1-hour window for the ``same_mutex_window`` causality kind, per spec."""


DEFAULT_DEPTH: int = 2
"""BFS depth for the public neighbor query, per spec."""


CAUSALITY_TYPES: tuple[str, ...] = (
    "same_mutex_window",
    "same_ticket",
    "same_failure_class",
)


# ── Typed errors (AC §error catalog) ────────────────────────────────


class FailureGraphCogneeUnavailable(RuntimeError):
    """Raised when the C3 Cognee KG layer is not reachable.

    Per spec, callers MUST catch this and fall back to the direct query
    path against the in-memory / Postgres projection. The failure graph
    is derived from ``runner_incidents``; a full rebuild can always be
    issued from the cron entrypoint to repopulate Cognee once it is
    healthy again.
    """


class FailureGraphInferenceTimeout(RuntimeError):
    """Raised when a neighbor query exceeds the wall-clock budget.

    The exception carries the partial edge list that was visited before
    the deadline so the caller can render a best-effort answer instead
    of dropping the result entirely.
    """

    def __init__(
        self, *, deadline_sec: float, partial_neighbors: list["GraphEdge"]
    ) -> None:
        self.deadline_sec = deadline_sec
        self.partial_neighbors = partial_neighbors
        super().__init__(
            f"failure-graph neighbor query exceeded {deadline_sec:.1f}s; "
            f"returning {len(partial_neighbors)} partial edges"
        )


class FailureGraphCircularEdge(ValueError):
    """Raised when an inferred edge has identical endpoints.

    The inference helpers prune self-loops silently and log a debug
    record; this exception exists for callers that build the graph
    manually (operator tooling, test fixtures) and want a hard failure
    on programming errors rather than silent dedup.
    """

    def __init__(self, node_id: str, causality_type: str) -> None:
        self.node_id = node_id
        self.causality_type = causality_type
        super().__init__(
            f"circular failure-graph edge for node {node_id!r} "
            f"(causality_type={causality_type!r}) — pruned at insert"
        )


# ── Data shapes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunnerIncident:
    """One row of the C2 ``runner_incidents`` table, projected as a
    Python dataclass so this module is testable without the migration.

    The Postgres source-of-truth (filed under C2) will project rows into
    this shape via ``PostgresIncidentSource``; tests use the
    in-memory equivalent.
    """

    incident_id: str
    ticket_key: str
    failure_class: str
    mutex_label: str | None
    occurred_at: datetime
    summary: str = ""

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            # Force UTC so the same_mutex_window arithmetic doesn't break
            # silently on a naive datetime. Frozen dataclass needs the
            # object.__setattr__ escape hatch.
            object.__setattr__(
                self, "occurred_at", self.occurred_at.replace(tzinfo=timezone.utc)
            )


@dataclass(frozen=True)
class GraphEdge:
    """Directed causality edge between two incidents."""

    src_id: str
    dst_id: str
    causality_type: str
    weight: float = 1.0


class IncidentSource(Protocol):
    """Where the graph builder pulls incidents from.

    Production wires a Postgres-backed source against C2's
    ``runner_incidents`` table; tests inject the in-memory equivalent.
    The Protocol is intentionally tiny — adding methods here forces every
    downstream source to grow them, so keep the surface area minimal.
    """

    def list_since(self, since: datetime) -> Iterable[RunnerIncident]: ...
    def get(self, incident_id: str) -> RunnerIncident | None: ...


@dataclass
class InMemoryIncidentSource:
    """Test-friendly :class:`IncidentSource`. Also serves as the degrade
    target when Postgres + Cognee are both offline and the operator is
    running ``scripts/dump_failure_graph.py`` against a fixture file.
    """

    incidents: list[RunnerIncident] = field(default_factory=list)

    def list_since(self, since: datetime) -> Iterable[RunnerIncident]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return [i for i in self.incidents if i.occurred_at >= since]

    def get(self, incident_id: str) -> RunnerIncident | None:
        for inc in self.incidents:
            if inc.incident_id == incident_id:
                return inc
        return None


class _Clock(Protocol):
    """Monotonic wall-clock injected by tests to drive the timeout path
    without ``time.sleep``."""

    def now(self) -> float: ...


class _RealClock:
    def now(self) -> float:  # pragma: no cover — trivial wrapper
        return time.monotonic()


# ── Edge inference ──────────────────────────────────────────────────


def infer_edges(
    incidents: list[RunnerIncident],
    *,
    mutex_window_sec: int = DEFAULT_MUTEX_WINDOW_SEC,
) -> list[GraphEdge]:
    """Deterministic edge inference over a list of incidents.

    Edges are directed from the earlier incident to the later one
    (causal arrow). Self-loops are pruned at insert and logged at
    DEBUG. Duplicates with the same ``(src, dst, causality_type)`` tuple
    are dedup'd — two incidents that share both a ticket and a mutex
    label produce two edges of different kinds, not one combined.
    """

    by_time = sorted(incidents, key=lambda i: (i.occurred_at, i.incident_id))
    seen: set[tuple[str, str, str]] = set()
    edges: list[GraphEdge] = []

    def _emit(
        src: RunnerIncident, dst: RunnerIncident, kind: str
    ) -> None:
        if src.incident_id == dst.incident_id:
            log.debug(
                "failure_graph.edge.self_loop_pruned id=%s kind=%s",
                src.incident_id,
                kind,
            )
            return
        key = (src.incident_id, dst.incident_id, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append(GraphEdge(src.incident_id, dst.incident_id, kind))

    # Causality 1: same mutex_label within mutex_window_sec
    for i, src in enumerate(by_time):
        if not src.mutex_label:
            continue
        for dst in by_time[i + 1 :]:
            delta = (dst.occurred_at - src.occurred_at).total_seconds()
            if delta > mutex_window_sec:
                # by_time is sorted — once we exceed the window the rest
                # of the slice is also outside it.
                break
            if dst.mutex_label == src.mutex_label:
                _emit(src, dst, "same_mutex_window")

    # Causality 2: same ticket_key, chained by time
    by_ticket: dict[str, list[RunnerIncident]] = defaultdict(list)
    for inc in by_time:
        by_ticket[inc.ticket_key].append(inc)
    for chain in by_ticket.values():
        for prev, nxt in zip(chain, chain[1:]):
            _emit(prev, nxt, "same_ticket")

    # Causality 3: same failure_class, fleet-wide, chained by time
    by_class: dict[str, list[RunnerIncident]] = defaultdict(list)
    for inc in by_time:
        by_class[inc.failure_class].append(inc)
    for chain in by_class.values():
        for prev, nxt in zip(chain, chain[1:]):
            _emit(prev, nxt, "same_failure_class")

    return edges


# ── Graph object ────────────────────────────────────────────────────


@dataclass
class FailureGraph:
    nodes: dict[str, RunnerIncident]
    edges: list[GraphEdge]

    @classmethod
    def build(
        cls,
        incidents: Iterable[RunnerIncident],
        *,
        mutex_window_sec: int = DEFAULT_MUTEX_WINDOW_SEC,
    ) -> "FailureGraph":
        node_list = list(incidents)
        nodes = {i.incident_id: i for i in node_list}
        if len(nodes) != len(node_list):
            log.warning(
                "failure_graph.build duplicate_incident_ids count=%d unique=%d",
                len(node_list),
                len(nodes),
            )
        edges = infer_edges(node_list, mutex_window_sec=mutex_window_sec)
        return cls(nodes=nodes, edges=edges)

    def adjacency(self) -> dict[str, list[GraphEdge]]:
        adj: dict[str, list[GraphEdge]] = defaultdict(list)
        for e in self.edges:
            adj[e.src_id].append(e)
            adj[e.dst_id].append(e)
        return dict(adj)


# ── Public neighbor query (the contract from AC #2) ─────────────────


def get_failure_graph_neighbors(
    graph: FailureGraph,
    incident_id: str,
    *,
    depth: int = DEFAULT_DEPTH,
    timeout_sec: float = DEFAULT_QUERY_TIMEOUT_SEC,
    clock: _Clock | None = None,
) -> list[GraphEdge]:
    """BFS up to ``depth`` from ``incident_id``; returns the edges
    traversed in visitation order.

    The graph is treated as undirected for traversal (a same-ticket edge
    A→B lets the caller reach A from B as well), since incident review
    wants the full neighborhood regardless of arrow direction.

    On wall-clock budget overrun, raises
    :class:`FailureGraphInferenceTimeout` with the partial result. This
    matches the AC error catalog: 30s cap, partial result is the
    caller's responsibility to render.
    """

    if incident_id not in graph.nodes:
        return []

    clk: _Clock = clock or _RealClock()
    deadline = clk.now() + timeout_sec
    adj = graph.adjacency()

    visited_edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    seen_nodes: set[str] = {incident_id}
    frontier: list[str] = [incident_id]

    for _ in range(max(0, depth)):
        next_frontier: list[str] = []
        for node in frontier:
            if clk.now() >= deadline:
                raise FailureGraphInferenceTimeout(
                    deadline_sec=timeout_sec,
                    partial_neighbors=list(visited_edges),
                )
            for edge in adj.get(node, ()):
                key = (edge.src_id, edge.dst_id, edge.causality_type)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                visited_edges.append(edge)
                other = edge.dst_id if edge.src_id == node else edge.src_id
                if other not in seen_nodes:
                    seen_nodes.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier:
            break

    return visited_edges


# ── Cognee adapter (degrades to direct query when unavailable) ──────


def push_to_cognee(graph: FailureGraph) -> None:
    """Ingest the failure graph into the Cognee KG (C3).

    Raises :class:`FailureGraphCogneeUnavailable` when the ``cognee``
    package isn't installed or the upstream service rejects the write —
    callers MUST catch and fall back to the direct query path. The
    contract here is: "best-effort write to Cognee; the in-memory
    projection is always the source of truth."

    Cognee's API surface stabilises with C3 (filed under that ticket),
    so this function is intentionally narrow: it only declares the
    integration point and the failure-translation behavior.
    """

    try:
        import cognee  # type: ignore[import-not-found]
    except ImportError as e:
        raise FailureGraphCogneeUnavailable(
            "cognee package not installed; degrading to direct query"
        ) from e

    try:
        cognee.add_graph(  # type: ignore[attr-defined]
            nodes=[
                {
                    "id": n.incident_id,
                    "ticket_key": n.ticket_key,
                    "failure_class": n.failure_class,
                    "mutex_label": n.mutex_label,
                    "occurred_at": n.occurred_at.isoformat(),
                }
                for n in graph.nodes.values()
            ],
            edges=[
                {
                    "src": e.src_id,
                    "dst": e.dst_id,
                    "type": e.causality_type,
                }
                for e in graph.edges
            ],
        )
    except Exception as e:  # noqa: BLE001 — any cognee error degrades
        raise FailureGraphCogneeUnavailable(
            f"cognee.add_graph failed: {e}"
        ) from e


# ── Pickup-time context injection (AC #4) ───────────────────────────


def render_pickup_context(
    graph: FailureGraph,
    ticket_key: str,
    *,
    max_neighbors: int = 5,
    depth: int = DEFAULT_DEPTH,
    timeout_sec: float = DEFAULT_QUERY_TIMEOUT_SEC,
) -> str:
    """Return a system-message-shaped block describing prior failed
    attempts on ``ticket_key`` plus their failure-graph neighbors.

    Returns an empty string when ``ticket_key`` has no prior incidents.
    The runner caller checks this and skips injection in that case so a
    fresh ticket doesn't get a "0 prior attempts" preamble that would
    just waste tokens.
    """

    own = [i for i in graph.nodes.values() if i.ticket_key == ticket_key]
    if not own:
        return ""
    own.sort(key=lambda i: i.occurred_at)

    lines = [
        "# Failure-Graph context for prior attempts",
        "",
        f"This ticket ({ticket_key}) has {len(own)} prior runner "
        f"incident(s); the following information is injected from the "
        f"failure-graph so you can avoid repeating the same failure mode.",
        "",
    ]
    for inc in own:
        line = (
            f"- {inc.incident_id} @ {inc.occurred_at.isoformat()} "
            f"failure_class={inc.failure_class}"
        )
        if inc.mutex_label:
            line += f" mutex={inc.mutex_label}"
        lines.append(line)
        if inc.summary:
            lines.append(f"  summary: {inc.summary}")
    lines.append("")

    related: list[GraphEdge] = []
    for inc in own:
        try:
            related.extend(
                get_failure_graph_neighbors(
                    graph,
                    inc.incident_id,
                    depth=depth,
                    timeout_sec=timeout_sec,
                )
            )
        except FailureGraphInferenceTimeout as e:
            log.warning(
                "render_pickup_context.timeout ticket=%s incident=%s "
                "partial_edges=%d",
                ticket_key,
                inc.incident_id,
                len(e.partial_neighbors),
            )
            related.extend(e.partial_neighbors)

    related_ids: list[str] = []
    for edge in related:
        for nid in (edge.src_id, edge.dst_id):
            inc_other = graph.nodes.get(nid)
            if (
                inc_other is not None
                and inc_other.ticket_key != ticket_key
                and nid not in related_ids
            ):
                related_ids.append(nid)
    related_ids = related_ids[:max_neighbors]

    if related_ids:
        lines.append(
            "Related incidents on other tickets (depth=%d graph neighbors):"
            % depth
        )
        for nid in related_ids:
            inc_other = graph.nodes[nid]
            lines.append(
                f"- {nid} ticket={inc_other.ticket_key} "
                f"failure_class={inc_other.failure_class}"
            )
        lines.append("")

    lines.append(
        "Treat the above as failure-mode prior. If you observe a similar "
        "symptom, halt and post a JIRA comment instead of retrying the "
        "exact same approach (per CLAUDE.md L1 'after 2 identical errors, "
        "escalate to human')."
    )
    return "\n".join(lines)
