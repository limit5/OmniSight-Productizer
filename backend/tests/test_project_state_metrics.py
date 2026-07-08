"""OP-2557 — project-state axis-health Prometheus metrics.

Covers the 4-AC synthetic test plan:

  * Code — the three families (axis counter, structural-half counter,
    per-axis latency histogram) register on ``metrics.REGISTRY`` and
    survive ``reset_for_tests()`` (the three-place pattern);
  * Exercised (a) — a CACHED call still increments
    ``axis_total{axis="structural",content="non_empty"}`` (the
    cache-hit undercount fix: emission is at the router boundary, not
    in ``record_trace`` which is empty on cache hits);
  * Exercised (b) — an all-axes-failed payload emits
    ``axis_total{content="degraded"}`` (the exact hollow case), while
    the half counters are SKIPPED (structural is None);
  * Exercised (c) — a kg-live-but-empty payload emits
    ``structural_half_total{half="kg",useful="false"}``;
  * The latency histogram is observed at the AGGREGATOR measurement
    point (the router payload carries no per-axis latency), so a fresh
    call observes it and a cache hit does not;
  * Best-effort contract — a raising metric surface never breaks a
    served 200.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend import metrics as m
from backend.agents import lesson_retrieval
from backend.agents import project_state_aggregator as agg
from backend.agents import project_state_cache as cache_mod
from backend.api import project_state as router_mod


prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


# ── Test helpers (mirroring test_project_state.py) ──────────────────


def _admin_user() -> auth.User:
    return auth.User(
        id="u1", email="op@example.test", name="Operator", role="admin", enabled=True
    )


def _build_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(router_mod, "_resolve_develop_sha", lambda: "develop-sha-test")
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[auth.current_user] = _admin_user
    app.dependency_overrides[auth.require_admin] = _admin_user
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
        "siblings": ["OP-901"],
        "kg_neighbours": [{"identifier": "OP-700", "score": 0.91, "kind": "jira"}],
    }


async def _kg_empty_structural(_ticket: str) -> dict[str, Any]:
    """kg half is LIVE but returned zero neighbours (hollow-kg case)."""
    return {
        "ticket": _ticket,
        "jira_source": "live",
        "kg_source": "live",
        "parent_meta": None,
        "phase": None,
        "blockers": ["OP-902"],
        "blocking": [],
        "siblings": [],
        "kg_neighbours": [],
    }


async def _ok_temporal(_ticket: str) -> dict[str, Any]:
    return {"ticket": _ticket, "prior_similar_tickets": ["OP-820"]}


async def _ok_causal(_ticket: str) -> dict[str, Any]:
    return {"ticket": _ticket, "neighbours": [{"incident_id": "i1"}]}


async def _raising(_ticket: str) -> dict[str, Any]:
    raise RuntimeError("backing store down")


def _fetchers(structural=_ok_structural) -> agg.ProjectStateAxisFetchers:
    return agg.ProjectStateAxisFetchers(
        structural=structural, temporal=_ok_temporal, causal=_ok_causal
    )


def _axis_total(axis: str, content: str) -> float | None:
    return m.REGISTRY.get_sample_value(
        "omnisight_project_state_axis_total",
        {"axis": axis, "content": content},
    )


def _half_total(half: str, useful: str) -> float | None:
    return m.REGISTRY.get_sample_value(
        "omnisight_project_state_structural_half_total",
        {"half": half, "useful": useful},
    )


def _latency_count(axis: str) -> float | None:
    return m.REGISTRY.get_sample_value(
        "omnisight_project_state_axis_latency_seconds_count",
        {"axis": axis},
    )


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    m.reset_for_tests()
    cache_mod.default_cache.clear()
    agg.reset_jira_negative_cache()
    monkeypatch.setattr(lesson_retrieval, "_INDEX", None)
    monkeypatch.delenv("OMNISIGHT_BUILD_GIT_SHA", raising=False)
    asyncio.run(agg.clear_traces())


# ── Code AC: three-place registration pattern ────────────────────────


@prom_only
def test_families_registered_and_survive_reset() -> None:
    # reset_for_tests() already ran in the fixture — these must be the
    # REBOUND instances (a family missing from the reset rebind would
    # silently never appear in the exposition).
    m.project_state_axis_total.labels(axis="structural", content="non_empty").inc()
    m.project_state_structural_half_total.labels(half="kg", useful="false").inc()
    m.project_state_axis_latency_seconds.labels(axis="structural").observe(0.01)

    body = m.render_exposition()[0].decode()
    assert "omnisight_project_state_axis_total" in body
    assert "omnisight_project_state_structural_half_total" in body
    assert "omnisight_project_state_axis_latency_seconds_bucket" in body


# ── Exercised (a): cached call still increments axis_total ───────────


@prom_only
def test_cache_hit_still_increments_axis_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg, "default_fetchers", _fetchers)
    client = _build_client(monkeypatch)
    hits_before = cache_mod.default_cache.stats()["hits"]

    first = client.get("/project-state", params={"ticket": "OP-2557"})
    second = client.get("/project-state", params={"ticket": "OP-2557"})

    assert first.status_code == 200
    assert second.status_code == 200
    # the second call really was served from cache…
    assert cache_mod.default_cache.stats()["hits"] == hits_before + 1
    # …and STILL counted (router-boundary emit — the undercount fix).
    assert _axis_total("structural", "non_empty") == 2.0
    assert _axis_total("causal", "non_empty") == 2.0


# ── Exercised (b): all-axes-failed emits degraded, halves skipped ────


@prom_only
def test_all_axes_failed_emits_degraded_and_skips_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agg,
        "default_fetchers",
        lambda: agg.ProjectStateAxisFetchers(
            structural=_raising, temporal=_raising, causal=_raising
        ),
    )
    client = _build_client(monkeypatch)

    resp = client.get("/project-state", params={"ticket": "OP-2557"})

    assert resp.status_code == 200
    assert resp.json()["structural"] is None
    # the exact hollow case is NOT skipped:
    for axis in agg.ALL_AXES:
        assert _axis_total(axis, "degraded") == 1.0
    # structural is None → the half counters are skipped entirely
    for half in ("jira", "kg"):
        for useful in ("true", "false"):
            assert _half_total(half, useful) is None


# ── Exercised (c): kg live-but-empty → half=kg,useful=false ──────────


@prom_only
def test_kg_live_but_empty_emits_kg_half_not_useful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agg, "default_fetchers", lambda: _fetchers(structural=_kg_empty_structural)
    )
    client = _build_client(monkeypatch)

    resp = client.get("/project-state", params={"ticket": "OP-2557"})

    assert resp.status_code == 200
    assert _half_total("kg", "false") == 1.0
    # jira half had blockers → useful
    assert _half_total("jira", "true") == 1.0


# ── Histogram lives at the aggregator measurement point ──────────────


@prom_only
def test_latency_histogram_observed_by_aggregator_not_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg, "default_fetchers", _fetchers)
    client = _build_client(monkeypatch)
    hits_before = cache_mod.default_cache.stats()["hits"]

    client.get("/project-state", params={"ticket": "OP-2557"})
    for axis in agg.ALL_AXES:
        assert _latency_count(axis) == 1.0, f"axis {axis} never observed"

    # a cache hit never reaches the aggregator → no new observation
    client.get("/project-state", params={"ticket": "OP-2557"})
    assert cache_mod.default_cache.stats()["hits"] == hits_before + 1
    for axis in agg.ALL_AXES:
        assert _latency_count(axis) == 1.0

    # not a register-but-never-observe (dead) histogram in the direct
    # aggregator path either — failed axes observe their latency too
    with pytest.raises(agg.ProjectStateAllAxesFailed):
        asyncio.run(
            agg.aggregate_project_state(
                "OP-2557X",
                develop_sha="x",
                fetchers=agg.ProjectStateAxisFetchers(
                    structural=_raising, temporal=_raising, causal=_raising
                ),
            )
        )
    for axis in agg.ALL_AXES:
        assert _latency_count(axis) == 2.0


# ── Best-effort contract: emission never breaks serving ──────────────


@prom_only
def test_metric_fault_never_breaks_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Raising:
        def labels(self, *_a, **_kw):
            raise RuntimeError("metric surface down")

        def observe(self, *_a, **_kw):
            raise RuntimeError("metric surface down")

    monkeypatch.setattr(m, "project_state_axis_total", _Raising())
    monkeypatch.setattr(m, "project_state_axis_latency_seconds", _Raising())
    monkeypatch.setattr(agg, "default_fetchers", _fetchers)
    client = _build_client(monkeypatch)

    resp = client.get("/project-state", params={"ticket": "OP-2557"})

    assert resp.status_code == 200
    assert resp.json()["structural"]["kg_source"] == "live"
