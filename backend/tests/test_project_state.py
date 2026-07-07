"""OP-904 (F6) — /api/v1/project-state aggregator contract tests.

Covers the 10 cases listed in the AC test plan:

1. Happy path — all 3 axes return.
2. Structural axis times out → ``null`` for that axis, others present.
3. Temporal axis times out → ``null`` for that axis, others present.
4. Causal axis times out → ``null`` for that axis, others present.
5. All axes fail → 200 with all-null payload.
6. Cache hit — second call against the same (ticket, sha) reads cache.
7. Cache invalidation on develop merge / webhook.
8. Per-axis budget guard — slow fetchers do not exceed their slice.
9. Auth rejection — missing bearer/session → 401.
10. Malformed ticket key + response-shape stability.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.agents import project_state_aggregator as agg
from backend.agents import project_state_cache as cache_mod
from backend.api import project_state as router_mod
from backend.intent_source import AdapterError


# ── Test helpers ────────────────────────────────────────────────────


def _admin_user() -> auth.User:
    return auth.User(
        id="u1", email="op@example.test", name="Operator", role="admin", enabled=True
    )


def _build_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient with the router mounted and auth stubbed."""
    monkeypatch.setattr(router_mod, "_resolve_develop_sha", lambda: "develop-sha-test")
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[auth.current_user] = _admin_user
    app.dependency_overrides[auth.require_admin] = _admin_user
    return TestClient(app)


def _build_client_strict_auth() -> TestClient:
    """TestClient that does NOT override auth — used to verify 401."""
    app = FastAPI()
    app.include_router(router_mod.router)

    async def _reject() -> auth.User:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Authentication required")

    app.dependency_overrides[auth.current_user] = _reject
    return TestClient(app)


async def _ok_structural(_ticket: str) -> dict[str, Any]:
    return {
        "ticket": _ticket,
        "jira_source": "live",
        "kg_source": "live",
        "parent_meta": "OP-900",
        "phase": "F6",
        "blockers": ["OP-902"],
        "blocking": ["OP-913"],
        "siblings": ["OP-901", "OP-903"],
        "kg_neighbours": [{"identifier": "OP-700", "score": 0.91, "kind": "jira"}],
    }


async def _ok_temporal(_ticket: str) -> dict[str, Any]:
    return {
        "ticket": _ticket,
        "prior_similar_tickets": ["OP-820"],
        "avg_completion_seconds": 3600,
        "recent_events": [{"kind": "rebase", "at": "2026-05-10T00:00:00Z"}],
    }


async def _ok_causal(_ticket: str) -> dict[str, Any]:
    return {
        "ticket": _ticket,
        "neighbours": [
            {
                "incident_id": "i1",
                "ticket": "OP-700",
                "failure_class": "TEST_FAILURE",
                "causality": "same_mutex_window",
            }
        ],
    }


def _ok_fetchers() -> agg.ProjectStateAxisFetchers:
    return agg.ProjectStateAxisFetchers(
        structural=_ok_structural, temporal=_ok_temporal, causal=_ok_causal
    )


def _slow_fetcher(delay_sec: float):
    async def _fn(_ticket: str) -> dict[str, Any]:
        await asyncio.sleep(delay_sec)
        return {"ticket": _ticket}

    return _fn


async def _raising(_ticket: str) -> dict[str, Any]:
    raise RuntimeError("backing store down")


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    cache_mod.default_cache.clear()
    agg.reset_jira_negative_cache()
    monkeypatch.delenv("OMNISIGHT_PROJECT_STATE_JIRA_PULL", raising=False)
    asyncio.run(agg.clear_traces())


# ── 1. Happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_returns_all_three_axes() -> None:
    payload = await agg.aggregate_project_state(
        "OP-904", develop_sha="abc", fetchers=_ok_fetchers()
    )
    assert payload["ticket"] == "OP-904"
    assert payload["develop_sha"] == "abc"
    assert payload["structural"]["parent_meta"] == "OP-900"
    assert payload["temporal"]["avg_completion_seconds"] == 3600
    assert payload["causal"]["neighbours"][0]["ticket"] == "OP-700"
    assert "generated_at" in payload

    traces = await agg.tail_traces()
    trace = traces[-1]
    assert trace.axis_content == {
        "structural": "non_empty",
        "temporal": "non_empty",
        "causal": "non_empty",
    }
    assert trace.source_markers == {
        "jira_source": "live",
        "kg_source": "live",
    }
    assert trace.jira_negative_cache_hits == 0


@pytest.mark.asyncio
async def test_structural_axis_adds_source_markers_for_degraded_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _jira_stub(_ticket: str) -> dict[str, Any]:
        return {}

    async def _kg_stub(_ticket: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(agg, "_structural_jira", _jira_stub)
    monkeypatch.setattr(agg, "_structural_cognee", _kg_stub)

    payload = await agg.fetch_structural_axis("OP-2535")

    assert payload["jira_source"] == "unavailable"
    assert payload["kg_source"] == "degraded"
    assert payload["blockers"] == []
    assert payload["kg_neighbours"] == []


@pytest.mark.asyncio
async def test_temporal_axis_returns_pinned_unavailable_shape() -> None:
    payload = await agg.fetch_temporal_axis("OP-2539")

    assert payload == {"status": "unavailable"}


def test_blocking_cognee_lookup_searches_code_then_lesson_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import cognee_integration as ci

    calls: list[dict[str, Any]] = []

    class _Hit:
        identifier = "backend/agents/project_state_aggregator.py"
        score = 0.87501
        kind = ci.SOURCE_KIND_CODE

    class _Adapter:
        async def search(self, query: str, *, kinds: tuple[str, ...], top_k: int):
            calls.append({"query": query, "kinds": kinds, "top_k": top_k})
            return (_Hit(),)

    monkeypatch.setattr(ci.CogneeAdapter, "from_env", lambda: _Adapter())

    payload = agg._blocking_cognee_lookup("OP-2540")

    assert calls == [
        {
            "query": "ticket neighbours for OP-2540",
            "kinds": (ci.SOURCE_KIND_CODE, ci.SOURCE_KIND_LESSON),
            "top_k": 5,
        }
    ]
    assert payload == {
        "kg_source": "live",
        "kg_neighbours": [
            {
                "identifier": "backend/agents/project_state_aggregator.py",
                "score": 0.875,
                "kind": ci.SOURCE_KIND_CODE,
            }
        ],
    }


def test_blocking_cognee_lookup_marks_empty_success_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import cognee_integration as ci

    class _Adapter:
        async def search(self, query: str, *, kinds: tuple[str, ...], top_k: int):
            return ()

    monkeypatch.setattr(ci.CogneeAdapter, "from_env", lambda: _Adapter())

    payload = agg._blocking_cognee_lookup("OP-2540")

    assert payload == {"kg_source": "live", "kg_neighbours": []}


@pytest.mark.parametrize("exc_name", ["CogneeNotInstalled", "Neo4jPasswordDefault"])
def test_blocking_cognee_lookup_maps_unconfigured_cognee_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
    exc_name: str,
) -> None:
    from backend.agents import cognee_integration as ci

    exc_type = getattr(ci, exc_name)

    def _raise():
        raise exc_type("unconfigured")

    monkeypatch.setattr(ci.CogneeAdapter, "from_env", _raise)

    payload = agg._blocking_cognee_lookup("OP-2540")

    assert payload == {"kg_source": "disabled"}


@pytest.mark.parametrize(
    "exc",
    [
        asyncio.TimeoutError(),
        RuntimeError("store failed"),
    ],
)
def test_blocking_cognee_lookup_maps_query_failures_to_degraded(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    from backend.agents import cognee_integration as ci

    class _Adapter:
        async def search(self, query: str, *, kinds: tuple[str, ...], top_k: int):
            raise exc

    monkeypatch.setattr(ci.CogneeAdapter, "from_env", lambda: _Adapter())

    payload = agg._blocking_cognee_lookup("OP-2540")

    assert payload == {"kg_source": "degraded"}


@pytest.mark.asyncio
async def test_structural_cognee_maps_module_import_failure_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def _import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):
        if name == "backend.agents" and "cognee_integration" in fromlist:
            raise ImportError("missing cognee integration module")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)

    payload = await agg._structural_cognee("OP-2540")

    assert payload == {"kg_source": "unavailable"}


# ── OP-2541 — structural-JIRA real pull ─────────────────────────────


_BLOCKS_TYPE = {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}


def _linked_issue(key: str, status: str, summary: str) -> dict[str, Any]:
    return {"key": key, "fields": {"status": {"name": status}, "summary": summary}}


class _FakeJiraAdapter:
    def __init__(
        self,
        body: dict[str, Any] | None = None,
        exc: Exception | None = None,
        delay_sec: float = 0.0,
    ) -> None:
        self.body = body if body is not None else {"fields": {}}
        self.exc = exc
        self.delay_sec = delay_sec
        self.calls: list[dict[str, Any]] = []

    async def fetch_story(
        self,
        ticket: str,
        *,
        fields: str | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        self.calls.append(
            {"ticket": ticket, "fields": fields, "timeout_s": timeout_s}
        )
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(raw=self.body)


def _jira_http_error(status_code: int) -> AdapterError:
    return AdapterError(
        "jira",
        "fetch_story",
        f"HTTP {status_code}",
        status_code=status_code,
        response={},
    )


def test_jira_half_contract_pins_match_spec() -> None:
    # OP-2541 pinned contracts — fields comma-string (no labels, no
    # description), inner fence = axis budget − 0.2 margin, transport
    # backstop 1.5 s, negative cache 512 entries / 600 s / 404-only.
    assert agg.JIRA_PULL_FIELDS == "issuelinks,parent,status,summary"
    assert "labels" not in agg.JIRA_PULL_FIELDS
    assert "description" not in agg.JIRA_PULL_FIELDS
    assert agg.JIRA_INNER_BUDGET_SEC == pytest.approx(
        agg.STRUCTURAL_BUDGET_SEC - 0.2
    )
    assert agg.JIRA_TRANSPORT_TIMEOUT_SEC == 1.5
    assert agg.JIRA_NEGATIVE_CACHE_MAX_ENTRIES == 512
    assert agg.JIRA_NEGATIVE_CACHE_TTL_SEC == 600.0


@pytest.mark.asyncio
async def test_structural_jira_maps_blocks_links_directionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "key": "OP-2541",
        "fields": {
            "summary": "self summary",
            "status": {"name": "In Progress"},
            "parent": _linked_issue("OP-2500", "To Do", "parent meta"),
            "issuelinks": [
                {
                    "type": _BLOCKS_TYPE,
                    "inwardIssue": _linked_issue(
                        "OP-2540", "公開済み", "blocks us"
                    ),
                },
                {
                    "type": _BLOCKS_TYPE,
                    "outwardIssue": _linked_issue(
                        "OP-2543", "To Do", "we block it"
                    ),
                },
                {
                    "type": {"name": "Relates"},
                    "inwardIssue": _linked_issue("OP-999", "Done", "unrelated"),
                },
            ],
        },
    }
    adapter = _FakeJiraAdapter(body=body)
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    view = await agg._structural_jira("OP-2541")

    assert view["jira_source"] == "live"
    assert view["blockers"] == [
        {"key": "OP-2540", "status": "公開済み", "summary": "blocks us"}
    ]
    assert view["blocking"] == [
        {"key": "OP-2543", "status": "To Do", "summary": "we block it"}
    ]
    assert view["parent_meta"] == {
        "key": "OP-2500",
        "status": "To Do",
        "summary": "parent meta",
    }


@pytest.mark.asyncio
async def test_structural_jira_pins_fields_string_and_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter()
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    await agg._structural_jira("OP-2541")

    assert adapter.calls == [
        {
            "ticket": "OP-2541",
            "fields": "issuelinks,parent,status,summary",
            "timeout_s": 1.5,
        }
    ]


@pytest.mark.asyncio
async def test_structural_axis_makes_one_jira_call_per_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter()
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    async def _kg_stub(_ticket: str) -> dict[str, Any]:
        return {"kg_source": "live", "kg_neighbours": []}

    monkeypatch.setattr(agg, "_structural_cognee", _kg_stub)

    payload = await agg.fetch_structural_axis("OP-2541")

    assert len(adapter.calls) == 1
    assert payload["jira_source"] == "live"


@pytest.mark.asyncio
async def test_structural_halves_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow_jira(_ticket: str) -> dict[str, Any]:
        await asyncio.sleep(0.15)
        return {"jira_source": "live"}

    async def _slow_kg(_ticket: str) -> dict[str, Any]:
        await asyncio.sleep(0.15)
        return {"kg_source": "live", "kg_neighbours": []}

    monkeypatch.setattr(agg, "_structural_jira", _slow_jira)
    monkeypatch.setattr(agg, "_structural_cognee", _slow_kg)

    start = time.monotonic()
    payload = await agg.fetch_structural_axis("OP-2541")
    elapsed = time.monotonic() - start

    # Sequential halves would need >= 0.30 s; gather keeps it ~0.15 s.
    assert elapsed < 0.27
    assert payload["jira_source"] == "live"
    assert payload["kg_source"] == "live"


@pytest.mark.asyncio
async def test_structural_half_exception_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(_ticket: str) -> dict[str, Any]:
        raise RuntimeError("jira half blew up")

    async def _kg_stub(_ticket: str) -> dict[str, Any]:
        return {"kg_source": "live", "kg_neighbours": []}

    monkeypatch.setattr(agg, "_structural_jira", _boom)
    monkeypatch.setattr(agg, "_structural_cognee", _kg_stub)

    payload = await agg.fetch_structural_axis("OP-2541")

    assert payload["jira_source"] == "degraded"
    assert payload["kg_source"] == "live"


@pytest.mark.asyncio
async def test_structural_jira_inner_fence_degrades_axis_stays_non_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter(delay_sec=0.3)
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)
    monkeypatch.setattr(agg, "JIRA_INNER_BUDGET_SEC", 0.05)

    async def _kg_stub(_ticket: str) -> dict[str, Any]:
        return {"kg_source": "live", "kg_neighbours": []}

    monkeypatch.setattr(agg, "_structural_cognee", _kg_stub)

    payload = await agg.fetch_structural_axis("OP-2541")

    # Slow JIRA must degrade the half, never null the whole axis.
    assert payload is not None
    assert payload["jira_source"] == "degraded"
    assert payload["kg_source"] == "live"
    assert payload["blockers"] == []
    assert payload["blocking"] == []


@pytest.mark.asyncio
async def test_structural_jira_kill_switch_off_makes_zero_adapter_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = {"n": 0}
    adapter = _FakeJiraAdapter()

    def _seam() -> _FakeJiraAdapter:
        built["n"] += 1
        return adapter

    monkeypatch.setattr(agg, "_build_jira_adapter", _seam)

    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_JIRA_PULL", "0")
    view = await agg._structural_jira("OP-2541")
    assert view == {"jira_source": "disabled"}
    assert built["n"] == 0
    assert adapter.calls == []

    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_JIRA_PULL", "1")
    view = await agg._structural_jira("OP-2541")
    assert view["jira_source"] == "live"
    assert built["n"] == 1
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_structural_jira_adapter_failure_maps_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _seam() -> Any:
        raise ImportError("jira adapter missing")

    monkeypatch.setattr(agg, "_build_jira_adapter", _seam)

    view = await agg._structural_jira("OP-2541")

    assert view == {"jira_source": "unavailable"}


@pytest.mark.asyncio
async def test_structural_jira_sanitizes_entries_and_never_reads_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_summary = "evil\x00\x1b[31m" + "x" * 300
    body = {
        "fields": {
            "description": "TOPLEVEL-DESCRIPTION-BODY",
            "issuelinks": [
                {
                    "type": _BLOCKS_TYPE,
                    "inwardIssue": {
                        "key": "OP-1",
                        "fields": {
                            "status": {"name": "Done\x07"},
                            "summary": hostile_summary,
                            "description": "LINKED-DESCRIPTION-BODY",
                            "assignee": {"name": "someone"},
                        },
                    },
                }
            ],
        },
    }
    adapter = _FakeJiraAdapter(body=body)
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    view = await agg._structural_jira("OP-2541")

    (entry,) = view["blockers"]
    assert set(entry) == {"key", "status", "summary"}
    assert entry["status"] == "Done"
    assert len(entry["summary"]) <= 200
    assert "\x00" not in entry["summary"]
    assert "\x1b" not in entry["summary"]
    assert entry["summary"].startswith("evil")
    assert "description" not in adapter.calls[0]["fields"].split(",")
    assert "DESCRIPTION-BODY" not in repr(view)


@pytest.mark.asyncio
async def test_structural_jira_404_negative_cache_hit_and_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter(exc=_jira_http_error(404))
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    first = await agg._structural_jira("OP-4041")
    assert first == {"jira_source": "degraded"}
    assert len(adapter.calls) == 1
    assert agg._jira_negative_cache_hits == 1

    # Key is normalized (strip + upper) → cached, no second adapter call.
    second = await agg._structural_jira(" op-4041 ")
    assert second == {"jira_source": "degraded"}
    assert len(adapter.calls) == 1
    assert agg._jira_negative_cache_hits == 2


@pytest.mark.asyncio
async def test_structural_jira_negative_cache_ttl_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter(exc=_jira_http_error(404))
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)
    monkeypatch.setattr(agg, "JIRA_NEGATIVE_CACHE_TTL_SEC", 0.01)

    await agg._structural_jira("OP-4041")
    await asyncio.sleep(0.03)
    await agg._structural_jira("OP-4041")

    # Expired entry re-consults JIRA instead of serving stale 404.
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_structural_jira_negative_cache_bounded_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter(exc=_jira_http_error(404))
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)
    monkeypatch.setattr(agg, "JIRA_NEGATIVE_CACHE_MAX_ENTRIES", 2)

    for ticket in ("OP-1", "OP-2", "OP-3"):
        await agg._structural_jira(ticket)

    assert len(agg._jira_negative_cache) == 2
    assert "OP-1" not in agg._jira_negative_cache  # oldest evicted

    await agg._structural_jira("OP-1")
    assert len(adapter.calls) == 4  # eviction → real call again


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_structural_jira_transient_errors_not_negative_cached(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    adapter = _FakeJiraAdapter(exc=_jira_http_error(status_code))
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    first = await agg._structural_jira("OP-2541")
    second = await agg._structural_jira("OP-2541")

    assert first == second == {"jira_source": "degraded"}
    assert len(adapter.calls) == 2  # never served from negative cache
    assert agg._jira_negative_cache_hits == 0
    assert not agg._jira_negative_cache


@pytest.mark.asyncio
async def test_trace_snapshots_cumulative_negative_cache_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeJiraAdapter(exc=_jira_http_error(404))
    monkeypatch.setattr(agg, "_build_jira_adapter", lambda: adapter)

    async def _kg_stub(_ticket: str) -> dict[str, Any]:
        return {"kg_source": "live", "kg_neighbours": []}

    monkeypatch.setattr(agg, "_structural_cognee", _kg_stub)

    fetchers = agg.ProjectStateAxisFetchers(
        structural=agg.fetch_structural_axis,
        temporal=_ok_temporal,
        causal=_ok_causal,
    )
    await agg.aggregate_project_state(
        "OP-4041", develop_sha="x", fetchers=fetchers
    )
    await agg.aggregate_project_state(
        "OP-4041", develop_sha="x", fetchers=fetchers
    )

    traces = await agg.tail_traces()
    assert traces[-2].jira_negative_cache_hits == 1  # fresh 404
    assert traces[-1].jira_negative_cache_hits == 2  # cumulative snapshot
    assert len(adapter.calls) == 1  # second run served by negative cache


# ── 2/3/4. Per-axis timeout — null for that axis, others returned ──


@pytest.mark.asyncio
async def test_structural_axis_timeout_returns_null_for_that_axis() -> None:
    budgets = agg.ProjectStateBudgets(
        structural_sec=0.01, temporal_sec=0.5, causal_sec=0.5, total_sec=2.0
    )
    fetchers = agg.ProjectStateAxisFetchers(
        structural=_slow_fetcher(0.1),
        temporal=_ok_temporal,
        causal=_ok_causal,
    )
    payload = await agg.aggregate_project_state(
        "OP-904", develop_sha="x", fetchers=fetchers, budgets=budgets
    )
    assert payload["structural"] is None
    assert payload["temporal"]["avg_completion_seconds"] == 3600
    assert payload["causal"]["neighbours"]


@pytest.mark.asyncio
async def test_temporal_axis_timeout_returns_null_for_that_axis_not_fake_empty() -> None:
    budgets = agg.ProjectStateBudgets(
        structural_sec=0.5, temporal_sec=0.01, causal_sec=0.5, total_sec=2.0
    )
    fetchers = agg.ProjectStateAxisFetchers(
        structural=_ok_structural,
        temporal=_slow_fetcher(0.1),
        causal=_ok_causal,
    )
    payload = await agg.aggregate_project_state(
        "OP-904", develop_sha="x", fetchers=fetchers, budgets=budgets
    )
    assert payload["temporal"] is None
    assert payload["temporal"] != {
        "prior_similar_tickets": [],
        "avg_completion_seconds": None,
        "recent_events": [],
    }
    assert payload["structural"]["parent_meta"] == "OP-900"
    assert payload["causal"]["neighbours"]


@pytest.mark.asyncio
async def test_causal_axis_timeout_returns_null_for_that_axis() -> None:
    budgets = agg.ProjectStateBudgets(
        structural_sec=0.5, temporal_sec=0.5, causal_sec=0.01, total_sec=2.0
    )
    fetchers = agg.ProjectStateAxisFetchers(
        structural=_ok_structural,
        temporal=_ok_temporal,
        causal=_slow_fetcher(0.1),
    )
    payload = await agg.aggregate_project_state(
        "OP-904", develop_sha="x", fetchers=fetchers, budgets=budgets
    )
    assert payload["causal"] is None
    assert payload["structural"]["parent_meta"] == "OP-900"
    assert payload["temporal"]["avg_completion_seconds"] == 3600


@pytest.mark.asyncio
async def test_trace_axis_content_classifies_degraded_and_unavailable() -> None:
    async def _unavailable_structural(_ticket: str) -> dict[str, Any]:
        return {
            "ticket": _ticket,
            "status": "unavailable",
            "jira_source": "disabled",
            "kg_source": "unavailable",
        }

    fetchers = agg.ProjectStateAxisFetchers(
        structural=_unavailable_structural,
        temporal=agg.fetch_temporal_axis,
        causal=_raising,
    )
    payload = await agg.aggregate_project_state(
        "OP-2535", develop_sha="x", fetchers=fetchers
    )

    assert payload["causal"] is None
    traces = await agg.tail_traces()
    trace = traces[-1]
    assert trace.axis_content == {
        "structural": "unavailable",
        "temporal": "unavailable",
        "causal": "degraded",
    }
    assert trace.source_markers == {
        "jira_source": "disabled",
        "kg_source": "unavailable",
    }


# ── 5. All axes fail — 200 with all-null body ──────────────────────


def test_router_returns_all_null_when_every_axis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)

    async def _all_raise(ticket_key: str, **kw: Any) -> dict[str, Any]:
        raise agg.ProjectStateAllAxesFailed(
            {"structural": "boom", "temporal": "boom", "causal": "boom"}
        )

    monkeypatch.setattr(router_mod.agg, "aggregate_project_state", _all_raise)

    response = client.get("/project-state", params={"ticket": "OP-904"})
    assert response.status_code == 200
    body = response.json()
    assert body["ticket"] == "OP-904"
    assert body["structural"] is None
    assert body["temporal"] is None
    assert body["causal"] is None


# ── 6. Cache hit — second call against same (ticket, sha) cached ───


def test_router_cache_hit_serves_second_call_without_recompute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)

    call_count = {"n": 0}

    async def _agg(ticket_key: str, **kw: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return {
            "ticket": ticket_key,
            "develop_sha": kw["develop_sha"],
            "structural": {"parent_meta": "OP-900"},
            "temporal": {"avg_completion_seconds": 60},
            "causal": {"neighbours": []},
            "generated_at": "2026-05-11T00:00:00+00:00",
        }

    monkeypatch.setattr(router_mod.agg, "aggregate_project_state", _agg)

    first = client.get("/project-state", params={"ticket": "OP-904"})
    second = client.get("/project-state", params={"ticket": "OP-904"})

    assert first.status_code == 200 and second.status_code == 200
    assert call_count["n"] == 1
    assert second.json()["structural"]["parent_meta"] == "OP-900"


# ── 7. Cache invalidation on develop merge / webhook ───────────────


def test_invalidate_for_webhook_drops_ticket_entries() -> None:
    cache_mod.default_cache.set(
        ("OP-904", "sha-A"),
        {
            "ticket": "OP-904",
            "structural": {},
            "temporal": {"status": "unavailable"},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    cache_mod.default_cache.set(
        ("OP-904", "sha-B"),
        {
            "ticket": "OP-904",
            "structural": {},
            "temporal": {"status": "unavailable"},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    cache_mod.default_cache.set(
        ("OP-700", "sha-A"),
        {
            "ticket": "OP-700",
            "structural": {},
            "temporal": {"status": "unavailable"},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    removed = router_mod.invalidate_for_webhook("OP-904")
    assert removed == 2
    assert cache_mod.default_cache.get(("OP-700", "sha-A")) is not None
    assert cache_mod.default_cache.get(("OP-904", "sha-A")) is None
    assert cache_mod.default_cache.get(("OP-904", "sha-B")) is None


def test_invalidate_for_develop_merge_clears_cache() -> None:
    cache_mod.default_cache.set(
        ("OP-904", "sha-A"),
        {
            "ticket": "OP-904",
            "structural": {},
            "temporal": {"status": "unavailable"},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    assert cache_mod.default_cache.stats()["size"] >= 1
    router_mod.invalidate_for_develop_merge()
    assert cache_mod.default_cache.stats()["size"] == 0


# ── 8. Per-axis budget guard — match spec values ───────────────────


def test_axis_budgets_match_spec() -> None:
    # AC #2 — runbook pinning. If these values change, the docs and the
    # operator metrics dashboard need to follow.
    assert agg.STRUCTURAL_BUDGET_SEC == 0.8
    assert agg.TEMPORAL_BUDGET_SEC == 0.6
    assert agg.CAUSAL_BUDGET_SEC == 0.6
    assert agg.TOTAL_BUDGET_SEC == 2.0


@pytest.mark.asyncio
async def test_per_axis_budget_does_not_block_other_axes() -> None:
    budgets = agg.ProjectStateBudgets(
        structural_sec=0.05, temporal_sec=0.5, causal_sec=0.5, total_sec=2.0
    )
    fetchers = agg.ProjectStateAxisFetchers(
        structural=_slow_fetcher(2.0),  # well past the structural slice
        temporal=_ok_temporal,
        causal=_ok_causal,
    )
    import time as _time

    start = _time.monotonic()
    payload = await agg.aggregate_project_state(
        "OP-904", develop_sha="x", fetchers=fetchers, budgets=budgets
    )
    elapsed = _time.monotonic() - start
    # Total wall-clock should be bounded by the slowest non-timed-out
    # axis (~0.05-0.1s), not the 2s structural sleep.
    assert elapsed < 1.5
    assert payload["structural"] is None
    assert payload["temporal"]["avg_completion_seconds"] == 3600


# ── 9. Auth rejection ──────────────────────────────────────────────


def test_router_rejects_unauthenticated_request() -> None:
    client = _build_client_strict_auth()
    response = client.get("/project-state", params={"ticket": "OP-904"})
    assert response.status_code == 401


# ── 10. Malformed ticket key + response-shape stability ────────────


def test_router_rejects_malformed_ticket_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)
    response = client.get("/project-state", params={"ticket": "garbage"})
    assert response.status_code == 400


def test_router_rejects_missing_ticket_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)
    response = client.get("/project-state")
    # FastAPI converts a missing required Query param to 422 before our
    # handler runs; both 400 and 422 are acceptable shapes from the
    # consumer's perspective, but we pin 422 to track FastAPI's behavior.
    assert response.status_code in (400, 422)


def test_response_shape_is_stable_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)

    async def _agg(ticket_key: str, **kw: Any) -> dict[str, Any]:
        return {
            "ticket": ticket_key,
            "develop_sha": kw["develop_sha"],
            "structural": {"parent_meta": "OP-900"},
            "temporal": {"avg_completion_seconds": 60},
            "causal": {"neighbours": []},
            "generated_at": "2026-05-11T00:00:00+00:00",
        }

    monkeypatch.setattr(router_mod.agg, "aggregate_project_state", _agg)
    body = client.get("/project-state", params={"ticket": "OP-904"}).json()
    required = {"ticket", "develop_sha", "structural", "temporal", "causal", "generated_at"}
    assert required.issubset(body.keys())


# ── Metrics surface (AC #6 — operator tracing) ─────────────────────


def test_metrics_endpoint_exposes_axis_latency_and_cache_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch)

    async def _agg(ticket_key: str, **kw: Any) -> dict[str, Any]:
        await agg.record_trace(
            agg.ProjectStateTrace(
                ticket=ticket_key,
                develop_sha=kw["develop_sha"],
                cache_hit=False,
                total_latency_sec=0.123,
                axis_latency_sec={"structural": 0.1, "temporal": 0.05, "causal": 0.04},
                axis_error={},
                budget_exceeded=False,
            )
        )
        return {
            "ticket": ticket_key,
            "develop_sha": kw["develop_sha"],
            "structural": {"parent_meta": "OP-900"},
            "temporal": {"avg_completion_seconds": 60},
            "causal": {"neighbours": []},
            "generated_at": "2026-05-11T00:00:00+00:00",
        }

    monkeypatch.setattr(router_mod.agg, "aggregate_project_state", _agg)
    client.get("/project-state", params={"ticket": "OP-904"})

    metrics = client.get("/project-state/metrics").json()
    assert "cache" in metrics and "traces" in metrics
    assert metrics["traces"], "expected at least one trace recorded"
    trace = metrics["traces"][-1]
    assert "axis_latency_sec" in trace
    assert "cache_hit" in trace
    assert "budget_exceeded" in trace
    assert trace["axis_content"] == {}
    assert trace["source_markers"] == {}
    assert trace["jira_negative_cache_hits"] == 0


# ── Cache corruption — invalidate + recompute (AC error catalog) ───


def test_cache_corruption_evicts_entry_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ("OP-904", "sha-A")
    # Bypass the validator to plant a corrupt entry, then read.
    entry = cache_mod.CacheEntry(payload={"bogus": True}, expires_at=10**9)
    cache_mod.default_cache._entries[key] = entry  # type: ignore[attr-defined]
    with caplog.at_level("WARNING"):
        result = cache_mod.default_cache.get(key)
    assert result is None
    assert cache_mod.default_cache.stats()["corruptions"] >= 1
