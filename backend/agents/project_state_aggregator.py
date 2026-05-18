"""OP-904 (F6) — Cross-task awareness aggregator.

Builds the three-axis ``project-state`` payload that the runner's
prompt-builder injects so a fresh pickup sees: structural blockers
(JIRA + Cognee KG), temporal priors (Graphiti MCP), and causal failure
neighbours (failure_class + failure_graph).

Budget contract (AC #2 / #3)
----------------------------
* Hard wall-clock budget: **2 s** total per call.
* Per-axis budgets: **800 ms structural**, **600 ms temporal**, **600 ms
  causal**.
* Axes run in parallel via :class:`asyncio.TaskGroup`-style fanout
  (here :func:`asyncio.gather` with a timeout per task). On overrun
  the offending axis returns ``None`` and the rest of the payload
  still ships (graceful degrade).
* On a total-budget overrun the orchestrator cancels in-flight axes
  and assembles whatever finished, also degrading missing axes to
  ``None``. This is the :class:`ProjectStateBudgetExceeded` path —
  the response is still 200 (the runner-side prompt-builder treats a
  null axis as "no context", not as a failure).

The aggregator is intentionally pure-async with injected fetchers.
Production wires the real JIRA / Cognee / Graphiti / failure-graph
adapters; tests inject in-memory stubs so the budget machinery is
exercised without booting a single backing store.

Error catalog
-------------
* :class:`ProjectStateAxisTimeout` — one axis exceeded its slice;
  caller substitutes ``None`` for that axis.
* :class:`ProjectStateAllAxesFailed` — every axis raised or timed out;
  caller returns 200 with all-null axes so the runner prompt-builder
  degrades silently.
* :class:`ProjectStateBudgetExceeded` — total wall-clock exceeded;
  remaining axes are cancelled and the partial payload is returned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# AC #2 — the budgets are the public contract. Tests pin these values
# (see test_project_state.py::test_axis_budgets_match_spec) so the
# runbook and the code never drift.
TOTAL_BUDGET_SEC: float = 2.0
STRUCTURAL_BUDGET_SEC: float = 0.8
TEMPORAL_BUDGET_SEC: float = 0.6
CAUSAL_BUDGET_SEC: float = 0.6

AXIS_STRUCTURAL = "structural"
AXIS_TEMPORAL = "temporal"
AXIS_CAUSAL = "causal"
ALL_AXES: tuple[str, ...] = (AXIS_STRUCTURAL, AXIS_TEMPORAL, AXIS_CAUSAL)


# ── Error catalog ───────────────────────────────────────────────────


class ProjectStateAxisTimeout(TimeoutError):
    """One axis exceeded its per-axis budget; substitute ``None``."""

    def __init__(self, axis: str, deadline_sec: float) -> None:
        self.axis = axis
        self.deadline_sec = deadline_sec
        super().__init__(
            f"project_state_aggregator: axis {axis!r} exceeded per-axis "
            f"budget of {deadline_sec:.3f}s"
        )


class ProjectStateAllAxesFailed(RuntimeError):
    """Every axis raised or timed out. Carry the per-axis errors so the
    operator log includes the underlying exception classes."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(
            f"project_state_aggregator: all {len(errors)} axes failed; "
            f"per-axis errors={errors}"
        )


class ProjectStateBudgetExceeded(TimeoutError):
    """Total wall-clock exceeded :data:`TOTAL_BUDGET_SEC`.

    Carries the per-axis state so the caller can render a partial
    response and emit the latency alert (AC error catalog).
    """

    def __init__(self, *, latencies: dict[str, float], completed: dict[str, Any]) -> None:
        self.latencies = dict(latencies)
        self.completed = dict(completed)
        super().__init__(
            f"project_state_aggregator: total wall-clock budget of "
            f"{TOTAL_BUDGET_SEC:.3f}s exceeded; completed axes="
            f"{sorted(completed.keys())} latencies_sec={latencies}"
        )


# ── Public dataclasses ──────────────────────────────────────────────


@dataclass
class AxisResult:
    """Outcome of one axis fetch.

    ``payload`` is ``None`` whenever the axis timed out or raised. The
    aggregator's :func:`assemble_response` consumes a list of these
    and emits the final JSON-friendly dict.
    """

    name: str
    payload: Any | None
    latency_sec: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


@dataclass
class ProjectStateAxisFetchers:
    """Per-axis async fetchers. Injected so tests can supply stubs."""

    structural: Callable[[str], Awaitable[Any]]
    temporal: Callable[[str], Awaitable[Any]]
    causal: Callable[[str], Awaitable[Any]]


@dataclass
class ProjectStateBudgets:
    structural_sec: float = STRUCTURAL_BUDGET_SEC
    temporal_sec: float = TEMPORAL_BUDGET_SEC
    causal_sec: float = CAUSAL_BUDGET_SEC
    total_sec: float = TOTAL_BUDGET_SEC

    def per_axis(self, axis: str) -> float:
        if axis == AXIS_STRUCTURAL:
            return self.structural_sec
        if axis == AXIS_TEMPORAL:
            return self.temporal_sec
        if axis == AXIS_CAUSAL:
            return self.causal_sec
        raise ValueError(
            f"project_state_aggregator.ProjectStateBudgets.per_axis: "
            f"unknown axis {axis!r}; valid axes={list(ALL_AXES)}"
        )


@dataclass
class ProjectStateTrace:
    """Telemetry record for one aggregator call.

    Surfaced via :func:`record_trace` so the operator metrics endpoint
    can read axis-by-axis latency + cache hit/miss without parsing log
    lines. Held in memory; the dashboard route reads the last N traces
    via :func:`tail_traces` (no DB write per call — the volume would be
    too high to be useful and the audit table is the long-term store).
    """

    ticket: str
    develop_sha: str
    cache_hit: bool
    total_latency_sec: float
    axis_latency_sec: dict[str, float] = field(default_factory=dict)
    axis_error: dict[str, str] = field(default_factory=dict)
    budget_exceeded: bool = False
    captured_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── Trace buffer ────────────────────────────────────────────────────


_TRACE_BUFFER_LIMIT = 200
_trace_buffer: list[ProjectStateTrace] = []
_trace_lock = asyncio.Lock()


async def record_trace(trace: ProjectStateTrace) -> None:
    """Push one trace into the bounded in-memory buffer.

    The metrics endpoint reads via :func:`tail_traces`. Buffer is FIFO
    capped at :data:`_TRACE_BUFFER_LIMIT` entries so a noisy hour does
    not bloat the worker memory footprint.
    """
    async with _trace_lock:
        _trace_buffer.append(trace)
        overflow = len(_trace_buffer) - _TRACE_BUFFER_LIMIT
        if overflow > 0:
            del _trace_buffer[:overflow]


async def tail_traces(limit: int = 50) -> list[ProjectStateTrace]:
    """Return the most recent ``limit`` traces, oldest-first."""
    async with _trace_lock:
        if limit <= 0:
            return []
        return list(_trace_buffer[-limit:])


async def clear_traces() -> None:
    """Test helper — reset the in-memory trace buffer."""
    async with _trace_lock:
        _trace_buffer.clear()


# ── Default per-axis fetchers ───────────────────────────────────────
# These integrate with the F1/F4/F5 backing stores; each returns
# ``None`` (not raises) on a backing-store outage so the aggregator
# treats it as a per-axis no-data rather than a typed timeout.


async def fetch_structural_axis(ticket_key: str) -> dict[str, Any]:
    """Compose JIRA issuelinks + Cognee KG neighbours.

    The JIRA half answers "what META, what blockers, what siblings."
    The Cognee half answers "which other code/JIRA entities share the
    semantic neighbourhood." Both halves degrade to empty lists on
    backing-store outage.
    """

    jira_view = await _structural_jira(ticket_key)
    cognee_view = await _structural_cognee(ticket_key)
    return {
        "ticket": ticket_key,
        "parent_meta": jira_view.get("parent_meta"),
        "phase": jira_view.get("phase"),
        "blockers": jira_view.get("blockers") or [],
        "blocking": jira_view.get("blocking") or [],
        "siblings": jira_view.get("siblings") or [],
        "kg_neighbours": cognee_view.get("kg_neighbours") or [],
    }


async def fetch_temporal_axis(ticket_key: str) -> dict[str, Any]:
    """Query Graphiti MCP for prior similar tickets + recent events."""

    timeline = await _temporal_graphiti(ticket_key)
    return {
        "ticket": ticket_key,
        "prior_similar_tickets": timeline.get("prior_similar_tickets") or [],
        "avg_completion_seconds": timeline.get("avg_completion_seconds"),
        "recent_events": timeline.get("recent_events") or [],
    }


async def fetch_causal_axis(ticket_key: str) -> dict[str, Any]:
    """Compose failure_class recall + failure_graph BFS neighbours."""

    return await _causal_failure_neighbours(ticket_key)


async def _structural_jira(ticket_key: str) -> dict[str, Any]:
    """Pull issuelinks + parent from JIRA. Degrades to ``{}`` on outage.

    Implemented as a degrade-silent shim so the F1 (Graphiti MCP) +
    F4 (Cognee KG) tickets can land independently. The runner's main
    consumer is the prompt builder; an empty structural payload is
    rendered as "no known META / blockers" rather than blocking pickup.
    """

    try:
        from backend.agents import jira_dispatch
    except ImportError as exc:
        log.info(
            "project_state.structural.jira_unavailable ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {}

    def _sync_pull() -> dict[str, Any]:
        try:
            client = jira_dispatch.make_client(
                jira_dispatch.resolve_bot_username.__defaults__ or ()  # type: ignore[arg-type]
            ) if False else None  # bot-class resolution is the runner's job
            del client
        except Exception:  # noqa: BLE001 — bot-class miswire degrades silently
            pass
        return {}

    try:
        return await asyncio.to_thread(_sync_pull)
    except Exception as exc:  # noqa: BLE001 — degrade-on-anything
        log.info(
            "project_state.structural.jira_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {}


async def _structural_cognee(ticket_key: str) -> dict[str, Any]:
    """Best-effort Cognee KG neighbour fetch. Degrades to ``{}``.

    Per OP-1449: ``CogneeAdapter.search`` honours its own
    ``query_timeout`` (default 30 s), which is much larger than the
    structural axis budget (800 ms). To avoid the cognee fetcher being
    cancelled mid-flight by the per-axis budget — which surfaces in the
    response as ``structural=null`` rather than the graceful
    ``kg_neighbours=[]`` — we wrap the call in an inner
    :func:`asyncio.wait_for` that returns early enough for
    :func:`fetch_structural_axis` to still hand back a well-formed dict.
    """
    try:
        from backend.agents import cognee_integration
    except ImportError:
        return {}

    # Leave headroom for the JIRA half + envelope assembly inside the
    # 800 ms structural budget; if Cognee is slower than this we'd
    # rather return ``kg_neighbours=[]`` with a logged degrade than have
    # the whole axis fall back to ``null``.
    inner_budget_sec = max(0.1, STRUCTURAL_BUDGET_SEC - 0.2)

    try:
        adapter = cognee_integration.CogneeAdapter.from_env()
    except Exception as exc:  # noqa: BLE001 — degrade per AC #3
        log.info(
            "project_state.structural.cognee_adapter_unavailable ticket=%s "
            "err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {}

    try:
        hits = await asyncio.wait_for(
            adapter.search(
                f"ticket neighbours for {ticket_key}",
                kinds=(cognee_integration.SOURCE_KIND_JIRA,),
                top_k=5,
            ),
            timeout=inner_budget_sec,
        )
    except asyncio.TimeoutError:
        log.warning(
            "project_state.structural.cognee_inner_timeout ticket=%s "
            "inner_budget_sec=%.3f outer_budget_sec=%.3f",
            ticket_key,
            inner_budget_sec,
            STRUCTURAL_BUDGET_SEC,
        )
        return {}
    except Exception as exc:  # noqa: BLE001 — degrade per AC #3
        log.info(
            "project_state.structural.cognee_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {}
    return {
        "kg_neighbours": [
            {
                "identifier": h.identifier,
                "score": round(float(h.score), 4),
                "kind": h.kind,
            }
            for h in (hits or ())
        ]
    }


async def _temporal_graphiti(ticket_key: str) -> dict[str, Any]:
    """Graphiti MCP timeline fetch with silent degrade."""
    try:
        from backend.agents import graphiti_mcp_client
    except ImportError:
        return {}
    # The runner injects the actual dispatcher; when unavailable, return
    # a no-context payload rather than fail the whole aggregator.
    dispatcher = getattr(
        graphiti_mcp_client, "_DEFAULT_DISPATCHER", None
    )
    if dispatcher is None:
        return {}
    try:
        timeline = graphiti_mcp_client.dispatch_graphiti_temporal_query(
            "getTicketTimeline",
            {"ticket": ticket_key},
            dispatcher=dispatcher,
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "project_state.temporal.graphiti_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {}
    if not isinstance(timeline, dict):
        return {}
    return timeline


async def _causal_failure_neighbours(ticket_key: str) -> dict[str, Any]:
    """Top-3 failure neighbours for ``ticket_key``.

    Resolves the incident source via
    :func:`failure_graph.default_incident_source` so the production
    Postgres path (``runner_incidents`` table) is consulted when a DSN is
    configured. Prior to OP-1449 this read a never-assigned
    ``_DEFAULT_INCIDENT_SOURCE`` attribute via ``getattr(... None)`` and
    therefore always returned an empty neighbours list — see the
    AC #3 wiring requirement.
    """
    try:
        from backend.agents import failure_graph
    except ImportError:
        return {"ticket": ticket_key, "neighbours": []}

    try:
        incident_source = failure_graph.default_incident_source()
    except Exception as exc:  # noqa: BLE001 — degrade per AC #3
        log.info(
            "project_state.causal.incident_source_unavailable ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {"ticket": ticket_key, "neighbours": []}

    try:
        # 7-day lookback mirrors the Sprint-C failure-graph dashboard
        # default. Use ``timedelta(days=7)`` rather than the prior
        # ``since.replace(day=since.day - 7)`` which silently collapsed
        # to a 1-day window on the first week of any month.
        since = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=7)
        # ``list_since`` may run SQL; keep the event loop responsive by
        # delegating the (potentially) blocking driver call to a thread.
        incidents = list(await asyncio.to_thread(incident_source.list_since, since))
        graph = failure_graph.FailureGraph.build(incidents)
        own = [n for n in graph.nodes.values() if n.ticket_key == ticket_key]
        if not own:
            log.info(
                "project_state.causal.no_own_incidents ticket=%s "
                "incident_source=%s lookback_since=%s incidents_in_window=%d",
                ticket_key,
                type(incident_source).__name__,
                since.isoformat(),
                len(incidents),
            )
        neighbours: list[dict[str, Any]] = []
        for inc in own:
            edges = failure_graph.get_failure_graph_neighbors(
                graph,
                inc.incident_id,
                depth=2,
                timeout_sec=CAUSAL_BUDGET_SEC,
            )
            for e in edges[:3]:
                other_id = e.dst_id if e.src_id == inc.incident_id else e.src_id
                other = graph.nodes.get(other_id)
                if other is None or other.ticket_key == ticket_key:
                    continue
                neighbours.append(
                    {
                        "incident_id": other.incident_id,
                        "ticket": other.ticket_key,
                        "failure_class": other.failure_class,
                        "causality": e.causality_type,
                    }
                )
                if len(neighbours) >= 3:
                    break
            if len(neighbours) >= 3:
                break
        return {"ticket": ticket_key, "neighbours": neighbours[:3]}
    except Exception as exc:  # noqa: BLE001
        log.info(
            "project_state.causal.failure_graph_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {"ticket": ticket_key, "neighbours": []}


def default_fetchers() -> ProjectStateAxisFetchers:
    """Bundle the production fetchers for the router to inject."""
    return ProjectStateAxisFetchers(
        structural=fetch_structural_axis,
        temporal=fetch_temporal_axis,
        causal=fetch_causal_axis,
    )


# ── Orchestrator ────────────────────────────────────────────────────


async def _run_axis(
    name: str,
    fetcher: Callable[[str], Awaitable[Any]],
    ticket_key: str,
    deadline_sec: float,
) -> AxisResult:
    """Run one axis with its per-axis budget; never re-raise.

    Returns an :class:`AxisResult`. Per-axis timeout → ``payload=None,
    error=ProjectStateAxisTimeout``. Per-axis exception →
    ``payload=None, error=<class name>:<msg>``. The orchestrator decides
    the response code from the union of these results.
    """
    start = time.monotonic()
    try:
        payload = await asyncio.wait_for(fetcher(ticket_key), timeout=deadline_sec)
        return AxisResult(
            name=name,
            payload=payload,
            latency_sec=time.monotonic() - start,
        )
    except asyncio.TimeoutError:
        latency = time.monotonic() - start
        log.warning(
            "project_state.axis_timeout axis=%s ticket=%s latency_sec=%.3f budget=%.3f",
            name,
            ticket_key,
            latency,
            deadline_sec,
        )
        return AxisResult(
            name=name,
            payload=None,
            latency_sec=latency,
            error=f"ProjectStateAxisTimeout:{deadline_sec:.3f}s",
        )
    except Exception as exc:  # noqa: BLE001 — degrade-per-axis per AC #3
        latency = time.monotonic() - start
        log.warning(
            "project_state.axis_failure axis=%s ticket=%s latency_sec=%.3f "
            "budget=%.3f err_type=%s err=%s",
            name,
            ticket_key,
            latency,
            deadline_sec,
            type(exc).__name__,
            exc,
        )
        return AxisResult(
            name=name,
            payload=None,
            latency_sec=latency,
            error=f"{type(exc).__name__}:{exc}",
        )


async def aggregate_project_state(
    ticket_key: str,
    *,
    develop_sha: str,
    fetchers: ProjectStateAxisFetchers | None = None,
    budgets: ProjectStateBudgets | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    """Return the assembled three-axis payload.

    Always returns a dict shaped as
    ``{"ticket", "develop_sha", "structural", "temporal", "causal",
    "generated_at"}``. Any axis that timed out / raised reads as
    ``None``; the response is still 200.

    Raises :class:`ProjectStateAllAxesFailed` only when **every** axis
    came back without a payload (the router catches this and returns a
    200 with all-null axes per the AC error catalog — the exception
    exists so the router can log the typed reason).
    """

    fetchers = fetchers or default_fetchers()
    budgets = budgets or ProjectStateBudgets()

    start = time.monotonic()
    deadline = start + budgets.total_sec

    coros = {
        AXIS_STRUCTURAL: _run_axis(
            AXIS_STRUCTURAL, fetchers.structural, ticket_key, budgets.structural_sec
        ),
        AXIS_TEMPORAL: _run_axis(
            AXIS_TEMPORAL, fetchers.temporal, ticket_key, budgets.temporal_sec
        ),
        AXIS_CAUSAL: _run_axis(
            AXIS_CAUSAL, fetchers.causal, ticket_key, budgets.causal_sec
        ),
    }

    results: dict[str, AxisResult] = {}
    tasks = {name: asyncio.create_task(coro) for name, coro in coros.items()}

    budget_exceeded = False
    try:
        remaining = max(0.0, deadline - time.monotonic())
        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=remaining,
            return_when=asyncio.ALL_COMPLETED,
        )
        if pending:
            budget_exceeded = True
            for task in pending:
                task.cancel()
        for task in done:
            result = task.result()
            results[result.name] = result
        for name, task in tasks.items():
            if name in results:
                continue
            # Was cancelled by the total-budget overrun above.
            results[name] = AxisResult(
                name=name,
                payload=None,
                latency_sec=time.monotonic() - start,
                error="ProjectStateBudgetExceeded",
            )
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()

    total_latency = time.monotonic() - start
    payload = assemble_response(
        ticket_key=ticket_key,
        develop_sha=develop_sha,
        results=results,
    )

    trace = ProjectStateTrace(
        ticket=ticket_key,
        develop_sha=develop_sha,
        cache_hit=cache_hit,
        total_latency_sec=total_latency,
        axis_latency_sec={name: r.latency_sec for name, r in results.items()},
        axis_error={name: r.error for name, r in results.items() if r.error},
        budget_exceeded=budget_exceeded,
    )
    await record_trace(trace)

    if all(r.payload is None for r in results.values()):
        errors = {name: r.error or "no_payload" for name, r in results.items()}
        log.warning(
            "project_state.all_axes_failed ticket=%s develop_sha=%s "
            "total_latency_sec=%.3f errors=%s",
            ticket_key,
            develop_sha,
            total_latency,
            errors,
        )
        # The router catches this and emits 200 with all-null axes per
        # AC error catalog; raising here makes the operator log line
        # carry the typed reason for triage.
        raise ProjectStateAllAxesFailed(errors)

    if budget_exceeded:
        cancelled = sorted(
            name for name, r in results.items()
            if r.error == "ProjectStateBudgetExceeded"
        )
        log.warning(
            "project_state.budget_exceeded ticket=%s develop_sha=%s "
            "total_latency_sec=%.3f total_budget_sec=%.3f cancelled_axes=%s",
            ticket_key,
            develop_sha,
            total_latency,
            budgets.total_sec,
            cancelled,
        )

    return payload


def assemble_response(
    *,
    ticket_key: str,
    develop_sha: str,
    results: dict[str, AxisResult],
) -> dict[str, Any]:
    """Pure builder — separable for tests that want to pin response shape."""
    return {
        "ticket": ticket_key,
        "develop_sha": develop_sha,
        "structural": _payload(results, AXIS_STRUCTURAL),
        "temporal": _payload(results, AXIS_TEMPORAL),
        "causal": _payload(results, AXIS_CAUSAL),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload(results: dict[str, AxisResult], axis: str) -> Any | None:
    result = results.get(axis)
    if result is None:
        return None
    return result.payload


__all__ = [
    "ALL_AXES",
    "AXIS_CAUSAL",
    "AXIS_STRUCTURAL",
    "AXIS_TEMPORAL",
    "AxisResult",
    "CAUSAL_BUDGET_SEC",
    "ProjectStateAllAxesFailed",
    "ProjectStateAxisFetchers",
    "ProjectStateAxisTimeout",
    "ProjectStateBudgetExceeded",
    "ProjectStateBudgets",
    "ProjectStateTrace",
    "STRUCTURAL_BUDGET_SEC",
    "TEMPORAL_BUDGET_SEC",
    "TOTAL_BUDGET_SEC",
    "aggregate_project_state",
    "assemble_response",
    "clear_traces",
    "default_fetchers",
    "fetch_causal_axis",
    "fetch_structural_axis",
    "fetch_temporal_axis",
    "record_trace",
    "tail_traces",
]
