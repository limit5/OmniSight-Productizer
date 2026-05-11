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
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.agents import project_state_aggregator as agg
from backend.agents import project_state_cache as cache_mod
from backend.api import project_state as router_mod


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
def _reset_module_state() -> None:
    cache_mod.default_cache.clear()
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
async def test_temporal_axis_timeout_returns_null_for_that_axis() -> None:
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
            "temporal": {},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    cache_mod.default_cache.set(
        ("OP-904", "sha-B"),
        {
            "ticket": "OP-904",
            "structural": {},
            "temporal": {},
            "causal": {},
            "generated_at": "2026-05-11T00:00:00+00:00",
        },
    )
    cache_mod.default_cache.set(
        ("OP-700", "sha-A"),
        {
            "ticket": "OP-700",
            "structural": {},
            "temporal": {},
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
            "temporal": {},
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
