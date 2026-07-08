"""OP-904 (F6) — Cross-task awareness aggregator.

Builds the three-axis ``project-state`` payload that the runner's
prompt-builder injects so a fresh pickup sees: structural blockers
(JIRA + BM25 lesson neighbours), temporal priors (Graphiti MCP), and
causal failure neighbours (failure_class + failure_graph).

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
Production wires the real JIRA / BM25-lessons / Graphiti / failure-graph
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
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents import agent_feature_flags, lesson_retrieval

log = logging.getLogger(__name__)

# AC #2 — the budgets are the public contract. Tests pin these values
# (see test_project_state.py::test_axis_budgets_match_spec) so the
# runbook and the code never drift.
TOTAL_BUDGET_SEC: float = 2.0
STRUCTURAL_BUDGET_SEC: float = 0.8
TEMPORAL_BUDGET_SEC: float = 0.6
CAUSAL_BUDGET_SEC: float = 0.6
STRUCTURAL_HALF_BUDGET_SEC: float = max(0.1, STRUCTURAL_BUDGET_SEC - 0.2)

# OP-2556 (B1) — the ``kg_source`` / ``kg_neighbours`` half is backed by
# the in-process BM25 lesson retrieval (OP-848), NOT Cognee. Cognee is
# retired from this hot path (measured LanceDB search > 0.6 s half
# budget, single-writer lock contention, LLM_API_KEY least-privilege
# gap) and PARKED for Phase U option A; its adapter module keeps
# independent runner/pipeline-fallback callers. The BM25 search is
# pure-Python, in-memory, ~3 ms warm — it runs synchronously after the
# JIRA fetch, no thread, no network, no store.
LESSON_NEIGHBOUR_TOP_K: int = 5
LESSON_SUMMARY_MAX_CHARS: int = 200

# OP-1454 — Cap the per-incident BFS loop in ``_causal_failure_neighbours``
# to the N most-recent own-incidents. OP-1450/1452 fixed the SQL + driver
# wiring so the input set stays small for typical tickets, but outlier
# tickets (e.g. OP-214 with 208 own incidents in the 7-day window) still
# blew the 0.6 s causal budget because the python loop ran
# ``len(own)`` × depth-2 BFS serially and ``asyncio.to_thread`` can't
# cancel the in-flight work after the axis timeout fires. The recent
# slice preserves the "recent failure neighbourhood" signal the
# prompt-builder actually consumes; the long tail of older incidents was
# noise in the prompt budget anyway.
CAUSAL_OWN_INCIDENT_CAP: int = 20

AXIS_STRUCTURAL = "structural"
AXIS_TEMPORAL = "temporal"
AXIS_CAUSAL = "causal"
ALL_AXES: tuple[str, ...] = (AXIS_STRUCTURAL, AXIS_TEMPORAL, AXIS_CAUSAL)
TEMPORAL_UNAVAILABLE_PAYLOAD: dict[str, str] = {"status": "unavailable"}

log.info("project_state.temporal.graphiti_unavailable status=unavailable")


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
    axis_content: dict[str, str] = field(default_factory=dict)
    source_markers: dict[str, str] = field(default_factory=dict)
    jira_negative_cache_hits: int = 0
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
    """Compose JIRA issuelinks + BM25 lesson neighbours (OP-2556 / B1).

    The JIRA half answers "what META, what blockers, what siblings."
    The lessons half answers "which prior lessons share the semantic
    neighbourhood" via the in-process BM25 index (OP-848) — it runs
    SYNCHRONOUSLY right after the JIRA fetch (~3 ms warm; no gather, no
    ``to_thread`` — the thread coupling is exactly what B1 removes).
    Both halves degrade to empty lists under never-raising wrappers
    (OP-2541); the JIRA inner fence stays at 0.6 s.
    """
    try:
        jira_view = await _structural_jira(ticket_key)
    except Exception as exc:  # noqa: BLE001 — a half must never raise
        log.info(
            "project_state.structural.half_degrade half=jira_source "
            "ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        jira_view = {"jira_source": "degraded"}

    # INTERNAL-only field: the ticket's own sanitized summary seeds the
    # BM25 query and is explicitly popped so it never reaches the public
    # structural payload (never injected into prompts).
    query_text = str(jira_view.pop("summary_internal", "") or "").strip()

    try:
        lessons_view = _structural_lessons(ticket_key, query_text)
    except Exception as exc:  # noqa: BLE001 — marker-consistency wrapper
        log.info(
            "project_state.structural.lessons_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        lessons_view = {"kg_source": "degraded"}

    return {
        "ticket": ticket_key,
        "jira_source": jira_view.get("jira_source") or "unavailable",
        "kg_source": lessons_view.get("kg_source") or "degraded",
        "parent_meta": jira_view.get("parent_meta"),
        "phase": jira_view.get("phase"),
        "blockers": jira_view.get("blockers") or [],
        "blocking": jira_view.get("blocking") or [],
        "siblings": jira_view.get("siblings") or [],
        "kg_neighbours": lessons_view.get("kg_neighbours") or [],
    }


async def fetch_temporal_axis(ticket_key: str) -> dict[str, Any]:
    """Return the pinned temporal-unavailable payload."""

    return await _temporal_graphiti(ticket_key)


async def fetch_causal_axis(ticket_key: str) -> dict[str, Any]:
    """Compose failure_class recall + failure_graph BFS neighbours."""

    return await _causal_failure_neighbours(ticket_key)


# ── JIRA half (OP-2541 / R2b part 2) ────────────────────────────────
# Pinned contracts (see the sibling OP-2536 transport seam):
# * ``fields=`` comma-string, forwarded verbatim to ``fetch_story``.
#   ``labels`` is deliberately NOT requested — nothing consumes it, so
#   ``phase`` stays null. ``description`` is never requested and never
#   read (prompt-injection surface).
# * inner fence 0.6 s = axis budget 0.8 − 0.2 margin;
#   ``timeout_s=1.5`` on the transport is the backstop that also kills
#   the curl subprocess.
JIRA_PULL_FIELDS = "issuelinks,parent,status,summary"
JIRA_TRANSPORT_TIMEOUT_SEC: float = 1.5
JIRA_INNER_BUDGET_SEC: float = STRUCTURAL_HALF_BUDGET_SEC
JIRA_SUMMARY_MAX_CHARS = 200

JIRA_NEGATIVE_CACHE_MAX_ENTRIES = 512
JIRA_NEGATIVE_CACHE_TTL_SEC: float = 600.0
_JIRA_NEGATIVE_CACHE_KEY_MAX_CHARS = 64

# 404-only negative cache: ticket-key → monotonic expiry. 429/5xx are
# transient and must NOT poison the cache. The counter is CUMULATIVE
# for the process lifetime; each trace snapshots the running total.
_jira_negative_cache: OrderedDict[str, float] = OrderedDict()
_jira_negative_cache_hits: int = 0

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _build_jira_adapter() -> Any:
    """Injectable module seam — tests monkeypatch this symbol."""
    from backend.jira_adapter import build_default_jira_adapter

    return build_default_jira_adapter()


def reset_jira_negative_cache() -> None:
    """Test helper — clear the 404 negative cache and its counter."""
    global _jira_negative_cache_hits
    _jira_negative_cache.clear()
    _jira_negative_cache_hits = 0


def _jira_negative_cache_key(ticket_key: str) -> str:
    return ticket_key.strip().upper()[:_JIRA_NEGATIVE_CACHE_KEY_MAX_CHARS]


def _jira_negative_cache_check(ticket_key: str) -> bool:
    """True when ``ticket_key`` has a live 404 entry (increments counter)."""
    global _jira_negative_cache_hits
    key = _jira_negative_cache_key(ticket_key)
    expiry = _jira_negative_cache.get(key)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _jira_negative_cache.pop(key, None)
        return False
    _jira_negative_cache_hits += 1
    return True


def _jira_negative_cache_put(ticket_key: str) -> None:
    global _jira_negative_cache_hits
    key = _jira_negative_cache_key(ticket_key)
    _jira_negative_cache[key] = time.monotonic() + JIRA_NEGATIVE_CACHE_TTL_SEC
    _jira_negative_cache.move_to_end(key)
    while len(_jira_negative_cache) > JIRA_NEGATIVE_CACHE_MAX_ENTRIES:
        _jira_negative_cache.popitem(last=False)
    # A fresh 404 counts alongside cached ones — the counter tracks
    # "JIRA said this ticket does not exist" events, not dict lookups.
    _jira_negative_cache_hits += 1


def _sanitize_jira_text(value: Any, max_chars: int = JIRA_SUMMARY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS_RE.sub("", value)[:max_chars]


def _jira_link_entry(issue: Any) -> dict[str, str] | None:
    """Allowlisted ``{key, status, summary}`` projection of a linked issue."""
    if not isinstance(issue, dict):
        return None
    key = _sanitize_jira_text(issue.get("key"), _JIRA_NEGATIVE_CACHE_KEY_MAX_CHARS)
    if not key:
        return None
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    status = fields.get("status")
    status_name = status.get("name") if isinstance(status, dict) else ""
    return {
        "key": key,
        "status": _sanitize_jira_text(status_name),
        "summary": _sanitize_jira_text(fields.get("summary")),
    }


def _map_jira_story(story: Any) -> dict[str, Any]:
    """Map the raw issue body onto the pinned structural half.

    Only ``Blocks``-type issuelinks are considered: ``inwardIssue`` is
    what blocks us (→ ``blockers``), ``outwardIssue`` is what we block
    (→ ``blocking``). ``parent`` → ``parent_meta``. Every entry carries
    only the allowlisted key / status-name / summary triple.

    ``summary_internal`` (OP-2556) is the ticket's OWN sanitized summary
    — INTERNAL-only, consumed by :func:`fetch_structural_axis` to build
    the BM25 lessons query and popped before the public payload is
    assembled. It must never ship on the wire.
    """
    raw = getattr(story, "raw", None)
    fields = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(fields, dict):
        fields = {}

    blockers: list[dict[str, str]] = []
    blocking: list[dict[str, str]] = []
    links = fields.get("issuelinks")
    for link in links if isinstance(links, list) else ():
        if not isinstance(link, dict):
            continue
        link_type = link.get("type")
        type_name = link_type.get("name") if isinstance(link_type, dict) else ""
        if type_name != "Blocks":
            continue
        inward = _jira_link_entry(link.get("inwardIssue"))
        if inward is not None:
            blockers.append(inward)
        outward = _jira_link_entry(link.get("outwardIssue"))
        if outward is not None:
            blocking.append(outward)

    return {
        "jira_source": "live",
        "parent_meta": _jira_link_entry(fields.get("parent")),
        "blockers": blockers,
        "blocking": blocking,
        "summary_internal": _sanitize_jira_text(fields.get("summary")),
    }


async def _structural_jira(ticket_key: str) -> dict[str, Any]:
    """Real JIRA pull for the structural half (OP-2541).

    ONE ``fetch_story`` call per cache-miss, budget-fenced so a slow
    JIRA yields ``jira_source: "degraded"`` with a present-but-marked
    payload — the axis never nulls out from a slow half. Kill-switch
    OFF → ``"disabled"`` with zero adapter calls; adapter import /
    construction failure → ``"unavailable"``; 404 (fresh or cached via
    the negative cache) → ``"degraded"``.
    """
    if not agent_feature_flags.project_state_jira_pull.enabled():
        return {"jira_source": "disabled"}

    if _jira_negative_cache_check(ticket_key):
        log.info(
            "project_state.structural.jira_negative_cache_hit ticket=%s",
            ticket_key,
        )
        return {"jira_source": "degraded"}

    try:
        adapter = _build_jira_adapter()
    except Exception as exc:  # noqa: BLE001 — import/construction miswire
        log.info(
            "project_state.structural.jira_unavailable ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {"jira_source": "unavailable"}

    try:
        story = await asyncio.wait_for(
            adapter.fetch_story(
                ticket_key,
                fields=JIRA_PULL_FIELDS,
                timeout_s=JIRA_TRANSPORT_TIMEOUT_SEC,
            ),
            timeout=JIRA_INNER_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        log.warning(
            "project_state.structural.jira_inner_timeout ticket=%s "
            "inner_budget_sec=%.3f outer_budget_sec=%.3f",
            ticket_key,
            JIRA_INNER_BUDGET_SEC,
            STRUCTURAL_BUDGET_SEC,
        )
        return {"jira_source": "degraded"}
    except Exception as exc:  # noqa: BLE001 — degrade-on-anything
        if getattr(exc, "status_code", None) == 404:
            _jira_negative_cache_put(ticket_key)
        log.info(
            "project_state.structural.jira_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {"jira_source": "degraded"}

    return _map_jira_story(story)


# OP-2556 (B1) — BM25 lessons half. Keeps the LEGACY wire names
# ``kg_source`` / ``kg_neighbours`` (consumers pinned them; the docs
# define them as lesson-backed now).


def _lessons_dir() -> Path:
    """Repo-root-anchored lessons dir (NOT CWD-relative).

    Env ``OMNISIGHT_LESSONS_DIR`` overrides; the serving image mounts
    ``/app/docs/sop/lessons``.
    """
    override = os.environ.get("OMNISIGHT_LESSONS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "docs" / "sop" / "lessons"


def warm_lessons_index() -> None:
    """Build the BM25 lessons index once per worker (never raises).

    Called from the FastAPI startup hook so the ~15-20 ms cold build
    never lands on a user request; warm searches are ~3 ms.
    """
    lessons_dir = _lessons_dir()
    try:
        index = lesson_retrieval.build_index(lessons_dir)
    except Exception as exc:  # noqa: BLE001 — warm-up is best-effort
        log.warning(
            "project_state.structural.lessons_warm_failed dir=%s err=%s: %s",
            lessons_dir,
            type(exc).__name__,
            exc,
        )
        return
    log.info(
        "project_state.structural.lessons_warmed dir=%s documents=%d",
        lessons_dir,
        len(index.documents) if index is not None else 0,
    )


def _lesson_excerpt(text: str) -> str:
    """Whitespace-collapsed, control-char-stripped ≤200-char excerpt.

    The full lesson body is never injected into the payload — only this
    sanitized excerpt ships (same sanitizer as the JIRA half).
    """
    collapsed = " ".join(str(text or "").split())
    return _sanitize_jira_text(collapsed, LESSON_SUMMARY_MAX_CHARS)


def _structural_lessons(ticket_key: str, query_text: str) -> dict[str, Any]:
    """BM25 lesson neighbours for the structural half. Never raises.

    Query = the ticket's own JIRA summary; falls back to the bare
    ``ticket_key`` when the JIRA half degraded (BM25 still returns).
    Markers: ``live`` = the search ran (even with 0 hits), ``degraded`` =
    index build/search raised, ``disabled`` = lessons dir missing or
    unreadable. Purely local + synchronous — no network, no DB, no
    ``to_thread`` (that coupling is what B1 removes).
    """
    lessons_dir = _lessons_dir()
    try:
        if not lessons_dir.is_dir():
            log.info(
                "project_state.structural.lessons_disabled ticket=%s dir=%s",
                ticket_key,
                lessons_dir,
            )
            return {"kg_source": "disabled"}
    except OSError as exc:
        log.info(
            "project_state.structural.lessons_disabled ticket=%s dir=%s "
            "err=%s: %s",
            ticket_key,
            lessons_dir,
            type(exc).__name__,
            exc,
        )
        return {"kg_source": "disabled"}

    query = query_text.strip() or ticket_key
    try:
        results = lesson_retrieval.retrieve_lessons(
            lessons_dir,
            ticket_title=query,
            acceptance_criteria="",
            top_k=LESSON_NEIGHBOUR_TOP_K,
        )
        neighbours = [
            {
                "identifier": r.path.stem,
                "score": round(float(r.score), 4),
                "kind": "lesson",
                "summary": _lesson_excerpt(r.text),
            }
            for r in results
        ]
    except Exception as exc:  # noqa: BLE001 — degrade per AC #3
        log.info(
            "project_state.structural.lessons_degrade ticket=%s err=%s: %s",
            ticket_key,
            type(exc).__name__,
            exc,
        )
        return {"kg_source": "degraded"}
    return {"kg_source": "live", "kg_neighbours": neighbours}


async def _temporal_graphiti(ticket_key: str) -> dict[str, Any]:
    """Graphiti MCP timeline is intentionally unavailable for R5."""
    del ticket_key
    return dict(TEMPORAL_UNAVAILABLE_PAYLOAD)


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
        # OP-1450: ``query_by_ticket`` is the index-served ticket-scoped
        # path. The prior ``list_since`` pulled every row in the 7-day
        # window (1935 in prod) and built a full ``FailureGraph`` on each
        # request — the in-memory build dominated wall-clock at ~6.7 s,
        # 11x the 0.6 s causal budget. Scoping the fetch to the ticket's
        # own incidents plus shared-failure_class / shared-mutex rows
        # keeps both the SQL set and the in-memory graph small. The
        # driver call may block, so it stays in ``asyncio.to_thread``.
        incidents = list(
            await asyncio.to_thread(
                incident_source.query_by_ticket, ticket_key, since
            )
        )
        graph = failure_graph.FailureGraph.build(incidents)
        own_all = [n for n in graph.nodes.values() if n.ticket_key == ticket_key]
        # OP-1454: keep only the most-recent N to bound the BFS loop below.
        own_all.sort(key=lambda n: n.occurred_at, reverse=True)
        own = own_all[:CAUSAL_OWN_INCIDENT_CAP]
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
        axis_content=_classify_axis_content(results),
        source_markers=_structural_source_markers(payload.get(AXIS_STRUCTURAL)),
        jira_negative_cache_hits=_jira_negative_cache_hits,
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


def _classify_axis_content(results: dict[str, AxisResult]) -> dict[str, str]:
    return {axis: _classify_payload(_payload(results, axis)) for axis in ALL_AXES}


def _classify_payload(payload: Any | None) -> str:
    if payload is None:
        return "degraded"
    if isinstance(payload, dict):
        if payload.get("status") == "unavailable":
            return "unavailable"
        if _dict_has_content(payload):
            return "non_empty"
        return "empty"
    return "non_empty" if payload else "empty"


def _dict_has_content(payload: dict[str, Any]) -> bool:
    for key, value in payload.items():
        if key in {"ticket", "jira_source", "kg_source"}:
            continue
        if value not in (None, "", [], {}):
            return True
    return False


def _structural_source_markers(payload: Any | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return {
        "jira_source": str(payload.get("jira_source") or "unavailable"),
        "kg_source": str(payload.get("kg_source") or "unavailable"),
    }


__all__ = [
    "ALL_AXES",
    "AXIS_CAUSAL",
    "AXIS_STRUCTURAL",
    "AXIS_TEMPORAL",
    "AxisResult",
    "CAUSAL_BUDGET_SEC",
    "CAUSAL_OWN_INCIDENT_CAP",
    "JIRA_INNER_BUDGET_SEC",
    "JIRA_NEGATIVE_CACHE_MAX_ENTRIES",
    "JIRA_NEGATIVE_CACHE_TTL_SEC",
    "JIRA_PULL_FIELDS",
    "JIRA_SUMMARY_MAX_CHARS",
    "JIRA_TRANSPORT_TIMEOUT_SEC",
    "LESSON_NEIGHBOUR_TOP_K",
    "LESSON_SUMMARY_MAX_CHARS",
    "ProjectStateAllAxesFailed",
    "ProjectStateAxisFetchers",
    "ProjectStateAxisTimeout",
    "ProjectStateBudgetExceeded",
    "ProjectStateBudgets",
    "ProjectStateTrace",
    "STRUCTURAL_BUDGET_SEC",
    "STRUCTURAL_HALF_BUDGET_SEC",
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
    "reset_jira_negative_cache",
    "tail_traces",
    "warm_lessons_index",
]
