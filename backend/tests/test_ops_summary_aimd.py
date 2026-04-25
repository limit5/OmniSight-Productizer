"""H4a row 2583 — ``GET /api/v1/ops/summary`` exposes the AIMD snapshot.

Verifies the wiring from :func:`backend.adaptive_budget.snapshot` into the
ops-summary response so the OpsSummaryPanel can render the current
budget + 5-min rise/fall history.

Module-global state audit (SOP Step 1): ``adaptive_budget._state`` is
module-level but per-uvicorn-worker (qualifying answer #1 — every
worker derives the same AIMD curve from the same psutil signal). The
test owns the state via the autouse ``_reset_state`` fixture and drives
the controller deterministically with synthetic ``now=`` arguments.
"""

from __future__ import annotations

import time

import pytest

from backend import adaptive_budget as ab


@pytest.fixture(autouse=True)
def _reset_state():
    ab._reset_for_tests()
    yield
    ab._reset_for_tests()


@pytest.mark.asyncio
async def test_ops_summary_includes_aimd_block(client):
    """Default cold-start AIMD snapshot rides on /ops/summary."""
    r = await client.get("/api/v1/ops/summary")
    assert r.status_code == 200
    body = r.json()
    assert "aimd" in body, body.keys()
    aimd = body["aimd"]
    assert aimd is not None
    # Cold-start defaults match adaptive_budget.reset() — INIT seed +
    # one INIT trace entry.
    assert aimd["budget"] == ab.INIT_BUDGET
    assert aimd["floor"] == ab.FLOOR_BUDGET
    assert aimd["init_budget"] == ab.INIT_BUDGET
    assert aimd["last_reason"] == "init"
    assert aimd["capacity_max"] >= ab.INIT_BUDGET
    # Thresholds block populated so the UI can show "AI<70%, MD>85%"
    # tooltips without hardcoding the constants.
    th = aimd["thresholds"]
    assert th["cpu_ai_pct"] == ab.CPU_AI_THRESHOLD_PCT
    assert th["mem_ai_pct"] == ab.MEM_AI_THRESHOLD_PCT
    assert th["cpu_md_pct"] == ab.CPU_MD_THRESHOLD_PCT
    assert th["mem_md_pct"] == ab.MEM_MD_THRESHOLD_PCT
    # Trace contains at least the cold-start INIT entry.
    assert len(aimd["trace"]) >= 1
    init_entry = aimd["trace"][0]
    assert init_entry["reason"] == "init"
    assert init_entry["budget"] == ab.INIT_BUDGET


@pytest.mark.asyncio
async def test_ops_summary_aimd_trace_records_ai_then_md(client):
    """End-to-end: drive AI then MD, verify trace landed in the response.

    Uses near-wall-clock timestamps because :func:`adaptive_budget.snapshot`
    trims the trace deque to ``[now - TRACE_WINDOW_S, now]`` before serialising
    — synthetic 0-based timestamps would all evict to zero entries.
    """
    base = time.time() - 90.0  # well within the 5-min window
    # Drive an AI cycle (budget 6 → 7) then sustained MD (budget 7 → 3).
    ab.reset(initial_budget=6, now=base)
    ab.tick(cpu_percent=10.0, mem_percent=10.0, deferred_count=0, now=base + 30.0)
    assert ab.current_budget() == 7
    ab.tick(cpu_percent=99.0, mem_percent=10.0, deferred_count=0, now=base + 60.0)
    ab.tick(cpu_percent=99.0, mem_percent=10.0, deferred_count=0, now=base + 80.0)
    assert ab.current_budget() == 3

    r = await client.get("/api/v1/ops/summary")
    assert r.status_code == 200
    aimd = r.json()["aimd"]
    assert aimd["budget"] == 3
    assert aimd["last_reason"] == "multiplicative_decrease"
    reasons = [e["reason"] for e in aimd["trace"]]
    # Trace order: init → AI → MD (HOLD on the first MD-clock-arming
    # tick is intentionally not appended to keep the trace from being
    # spammed by every cycle).
    assert "additive_increase" in reasons
    assert "multiplicative_decrease" in reasons
    # Each trace row is JSON-serialisable with the shape the UI consumes.
    for entry in aimd["trace"]:
        assert set(entry.keys()) >= {
            "timestamp", "budget", "reason", "cpu_percent", "mem_percent",
        }
        assert isinstance(entry["budget"], int)
        assert isinstance(entry["timestamp"], (int, float))


@pytest.mark.asyncio
async def test_ops_summary_aimd_block_survives_snapshot_failure(
    client, monkeypatch,
):
    """If ``adaptive_budget.snapshot`` blows up, ops_summary still returns
    200 with ``aimd: null`` — older clients ignore unknown fields, but a
    500 here would knock out the whole panel for an optional sub-section.
    """
    import backend.adaptive_budget as _ab_mod

    def boom(*args, **kwargs):
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(_ab_mod, "snapshot", boom)
    r = await client.get("/api/v1/ops/summary")
    assert r.status_code == 200
    body = r.json()
    # Response shape: aimd is present but null (graceful degrade), the
    # rest of the panel data still renders.
    assert "aimd" in body
    assert body["aimd"] is None
    # Other ops-summary fields untouched.
    assert "checked_at" in body
    assert "coordinator" in body
