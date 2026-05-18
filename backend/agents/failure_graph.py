"""OP-858 (C8) — Failure-Graph from cross-incident causality.

Builds a directed graph over runner failure incidents (the rows
produced by C2's ``runner_incidents`` table) so the runner can answer
"what cascaded into this ticket's prior failure?" at pickup time and so
operators can render a 7-day failure landscape for incident review.

OP-1449 (3d-memory activation): the F6 ``/api/v1/project-state`` causal
axis used to consult ``getattr(failure_graph, "_DEFAULT_INCIDENT_SOURCE",
None)`` — an attribute that was never assigned, so the axis always
returned an empty ``neighbours`` list even when ``runner_incidents`` had
thousands of rows. :func:`default_incident_source` is the wired entry
point: it returns a :class:`PostgresIncidentSource` when the env DSN
points at Postgres (the production path against the C2 table) and
falls back to an :class:`InMemoryIncidentSource` populated from the
:mod:`backend.agents.incident_recorder` buffer otherwise (covers the
dev SQLite path and unit tests).

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

import functools
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

    ``query_by_ticket`` is the OP-1450 hot-path entry: callers that only
    need a given ticket's failure-graph neighbourhood get just that
    ticket's incidents plus their direct edge-candidates, instead of
    paying the full ``list_since`` window scan.
    """

    def list_since(self, since: datetime) -> Iterable[RunnerIncident]: ...
    def get(self, incident_id: str) -> RunnerIncident | None: ...
    def query_by_ticket(
        self, ticket_key: str, since: datetime
    ) -> Iterable[RunnerIncident]: ...


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

    def query_by_ticket(
        self, ticket_key: str, since: datetime
    ) -> Iterable[RunnerIncident]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        in_window = [i for i in self.incidents if i.occurred_at >= since]
        own = [i for i in in_window if i.ticket_key == ticket_key]
        if not own:
            return []
        own_classes = {i.failure_class for i in own}
        own_mutexes = {i.mutex_label for i in own if i.mutex_label}
        return [
            i for i in in_window
            if (
                i.ticket_key == ticket_key
                or i.failure_class in own_classes
                or (i.mutex_label is not None and i.mutex_label in own_mutexes)
            )
        ]


# OP-1449 — Postgres-backed incident source. Keeps ``failure_graph``
# importable without sqlalchemy by lazy-importing inside the methods;
# the rest of this module remains pure-Python with no required external
# dependencies (per the module docstring).


@functools.lru_cache(maxsize=8)
def _cached_engine(url: str) -> "object":
    # OP-1455: cache the SQLAlchemy engine per DSN at module scope.
    # Without this, ``default_incident_source`` recreates the engine on
    # every causal-axis HTTP request — ``create_engine`` + the first
    # ``pool_pre_ping`` round-trip add ~130 ms per call and the new
    # connection pool starts empty, defeating connection reuse. Caching
    # by DSN string lets concurrent axes share one pool and pays the
    # ping cost once per process.
    import sqlalchemy as sa  # noqa: PLC0415 — lazy

    return sa.create_engine(url, future=True, pool_pre_ping=True)


class PostgresIncidentSource:
    """Read ``runner_incidents`` rows from Postgres via SQLAlchemy.

    Used by :func:`default_incident_source` when ``OMNISIGHT_DATABASE_URL``
    points at Postgres. The query mirrors
    ``scripts/generate_failure_graph_fixture.fetch_runner_incidents``
    (which produces the same shape for fixture-driven incident review),
    plus an optional ``ticket_key`` index hint so per-ticket lookups can
    use ``idx_runner_incidents_ticket_key``.

    Rows where ``failure_class = 'MEMORY_RECALL_AUDIT'`` are skipped —
    those are the C6 audit slot and do not represent runner failures.
    """

    def __init__(self, engine: "object") -> None:
        self._engine = engine

    @property
    def engine(self) -> "object":
        return self._engine

    @classmethod
    def from_database_url(cls, url: str) -> "PostgresIncidentSource":
        # OP-1452: failure_graph uses SYNC SQLAlchemy (engine.begin()
        # called via asyncio.to_thread from the F6 axis). Mixing an
        # asyncpg driver into a sync engine raises MissingGreenlet on
        # first IO, which the axis catches and degrades to empty
        # neighbours — making OP-1450's index-served SQL invisible in
        # prod. Strip the +asyncpg variant so we end up on psycopg2.
        if url.startswith("postgresql+asyncpg://"):
            url = "postgresql+psycopg2://" + url[len("postgresql+asyncpg://"):]
        return cls(_cached_engine(url))

    def list_since(self, since: datetime) -> Iterable[RunnerIncident]:
        import sqlalchemy as sa  # noqa: PLC0415 — lazy

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stmt = sa.text(
            """
            SELECT incident_id, ticket_key, failure_class, mutex_label,
                   created_at, summary
            FROM runner_incidents
            WHERE created_at >= :since
              AND failure_class != 'MEMORY_RECALL_AUDIT'
            ORDER BY created_at ASC, incident_id ASC
            """
        )
        try:
            with self._engine.begin() as conn:
                rows = conn.execute(stmt, {"since": since}).mappings().all()
        except Exception as exc:  # noqa: BLE001 — degrade per AC #3
            log.warning(
                "PostgresIncidentSource.list_since degrade since=%s err=%s: %s",
                since.isoformat(),
                type(exc).__name__,
                exc,
            )
            return []
        return [_row_to_incident(dict(row)) for row in rows]

    def get(self, incident_id: str) -> RunnerIncident | None:
        import sqlalchemy as sa  # noqa: PLC0415 — lazy

        stmt = sa.text(
            """
            SELECT incident_id, ticket_key, failure_class, mutex_label,
                   created_at, summary
            FROM runner_incidents
            WHERE incident_id = :incident_id
              AND failure_class != 'MEMORY_RECALL_AUDIT'
            """
        )
        try:
            with self._engine.begin() as conn:
                row = conn.execute(stmt, {"incident_id": incident_id}).mappings().first()
        except Exception as exc:  # noqa: BLE001 — degrade per AC #3
            log.warning(
                "PostgresIncidentSource.get degrade incident_id=%s err=%s: %s",
                incident_id,
                type(exc).__name__,
                exc,
            )
            return None
        return _row_to_incident(dict(row)) if row else None

    def query_by_ticket(
        self, ticket_key: str, since: datetime
    ) -> Iterable[RunnerIncident]:
        """Return rows sufficient to build the failure-graph neighbourhood
        of ``ticket_key`` without scanning the whole window.

        The result is the union of:
          * the ticket's own incidents (via ``idx_runner_incidents_ticket_key``),
          * any incident sharing one of the ticket's ``failure_class`` values
            (``idx_runner_incidents_class_area``),
          * any incident sharing one of the ticket's non-null ``mutex_label``
            values (``idx_runner_incidents_mutex``).

        This is the OP-1450 hot path: the prior implementation pulled every
        row in the 7-day window (1935 rows in prod) and built a full
        ``FailureGraph`` on each request, which timed out the causal axis
        at ~6.7 s — 11x the 0.6 s budget. The ticket-scoped union here is
        index-served and returns the few hundred rows that actually
        participate in ``ticket_key``'s edges.
        """
        import sqlalchemy as sa  # noqa: PLC0415 — lazy

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stmt = sa.text(
            """
            WITH own AS (
                SELECT failure_class, mutex_label
                FROM runner_incidents
                WHERE ticket_key = :ticket_key
                  AND created_at >= :since
                  AND failure_class != 'MEMORY_RECALL_AUDIT'
            )
            SELECT incident_id, ticket_key, failure_class, mutex_label,
                   created_at, summary
            FROM runner_incidents
            WHERE created_at >= :since
              AND failure_class != 'MEMORY_RECALL_AUDIT'
              AND (
                ticket_key = :ticket_key
                OR failure_class IN (SELECT DISTINCT failure_class FROM own)
                OR (
                  mutex_label IS NOT NULL
                  AND mutex_label IN (
                    SELECT DISTINCT mutex_label
                    FROM own
                    WHERE mutex_label IS NOT NULL
                  )
                )
              )
            ORDER BY created_at ASC, incident_id ASC
            """
        )
        try:
            with self._engine.begin() as conn:
                rows = conn.execute(
                    stmt, {"ticket_key": ticket_key, "since": since}
                ).mappings().all()
        except Exception as exc:  # noqa: BLE001 — degrade per AC #3
            log.warning(
                "PostgresIncidentSource.query_by_ticket degrade "
                "ticket_key=%s since=%s err=%s: %s",
                ticket_key,
                since.isoformat(),
                type(exc).__name__,
                exc,
            )
            return []
        return [_row_to_incident(dict(row)) for row in rows]


def _row_to_incident(row: dict) -> RunnerIncident:
    """Coerce a ``runner_incidents`` row dict to :class:`RunnerIncident`.

    Accepts both Postgres ``TIMESTAMPTZ`` (already ``datetime``) and SQLite
    ``TEXT`` (parsed via ``datetime.fromisoformat``) so the same projection
    works against either dialect. Defensive against legacy ``Z`` suffix on
    the timestamp string — the SQLite path stores the python repr which
    keeps the offset, but log archives backfilled via
    ``scripts/backfill_runner_incidents.py`` round-trip via ISO strings.
    """
    occurred_at = row["created_at"]
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    elif occurred_at is None:
        occurred_at = datetime.now(timezone.utc)
    return RunnerIncident(
        incident_id=str(row["incident_id"]),
        ticket_key=str(row["ticket_key"]),
        failure_class=str(row["failure_class"]),
        mutex_label=row.get("mutex_label"),
        occurred_at=occurred_at,
        summary=str(row.get("summary") or ""),
    )


def default_incident_source() -> IncidentSource:
    """Return the production incident source.

    Resolution order (per OP-1449 wiring):

    1. ``OMNISIGHT_DATABASE_URL`` / ``DATABASE_URL`` set to a Postgres
       DSN — return a :class:`PostgresIncidentSource` against the
       ``runner_incidents`` table.
    2. ``OMNISIGHT_DATABASE_URL`` / ``DATABASE_URL`` set to a non-PG
       URL (e.g. SQLite) — return a :class:`PostgresIncidentSource`
       against that engine anyway; the same SQL works against SQLite
       because :mod:`backend.alembic.versions.0206_runner_incidents`
       ships dialect-symmetric DDL.
    3. No DSN configured — fall back to an :class:`InMemoryIncidentSource`
       populated from the
       :func:`backend.agents.incident_recorder.get_runner_incidents`
       in-process buffer. This is the dev-mode + unit-test path; the
       buffer is empty on a fresh process so the causal axis still
       degrades to "no neighbours" but at least the wiring works.
    """
    import os  # noqa: PLC0415 — lazy

    dsn = (
        os.environ.get("OMNISIGHT_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if dsn:
        try:
            return PostgresIncidentSource.from_database_url(dsn)
        except Exception as exc:  # noqa: BLE001 — degrade per AC #3
            log.warning(
                "default_incident_source: DSN connect failed, "
                "falling back to in-memory buffer err=%s: %s",
                type(exc).__name__,
                exc,
            )

    try:
        from backend.agents.incident_recorder import (  # noqa: PLC0415 — lazy
            get_runner_incidents,
        )
    except ImportError:
        return InMemoryIncidentSource([])

    incidents: list[RunnerIncident] = []
    for rec in get_runner_incidents():
        occurred_at = datetime.fromtimestamp(rec.created_at, tz=timezone.utc)
        incidents.append(
            RunnerIncident(
                incident_id=rec.incident_id,
                ticket_key=rec.ticket_key,
                failure_class=rec.failure_class.value,
                mutex_label=rec.mutex_label,
                occurred_at=occurred_at,
                summary=rec.summary,
            )
        )
    return InMemoryIncidentSource(incidents)


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
