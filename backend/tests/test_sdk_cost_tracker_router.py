"""OP-820 SDK runner per-ticket cost tracker contracts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.agents.cost_guard import (
    BudgetAlert,
    CostGuard,
    ScopeKey,
    estimate_cost,
)
from backend.routers import sdk_cost_tracker as router_mod


def _admin_user() -> auth.User:
    return auth.User(id="u1", email="op@example.test", name="Operator", role="admin")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[auth.require_admin] = _admin_user
    return TestClient(app)


def _api_alias_client() -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api")
    app.dependency_overrides[auth.require_admin] = _admin_user
    return TestClient(app)


def _alert(ticket: str, observed_usd: float, idx: int) -> BudgetAlert:
    fired_at = (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=idx)
    )
    return BudgetAlert(
        alert_id=f"alert_{idx}",
        scope=ScopeKey(kind="workspace", key=ticket),
        period="daily",
        level="warn_80",
        threshold_usd=1.0,
        observed_usd=observed_usd,
        action="notify",
        fired_at=fired_at,
    )


@pytest.fixture(autouse=True)
def reset_tracker() -> None:
    asyncio.run(router_mod._reset_for_tests())


def test_get_sdk_cost_exposes_ticket_snapshot() -> None:
    asyncio.run(router_mod.sdk_cost_alert_sink(_alert("OP-820", 0.25, 1)))

    response = _client().get("/sdk/cost?ticket=OP-820")

    assert response.status_code == 200
    body = response.json()
    assert body["ticket"] == "OP-820"
    assert body["rows"][0]["ticket"] == "OP-820"
    assert body["rows"][0]["rolling_24h_usd"] == 0.25
    assert body["rows"][0]["cumulative_usd"] == 0.25
    assert body["rows"][0]["alert_count_24h"] == 1
    assert body["rows"][0]["last_alert_at"] is not None
    assert body["total_rolling_24h_usd"] == 0.25
    assert body["total_cumulative_usd"] == 0.25


def test_get_sdk_cost_exposes_api_alias_named_by_acceptance() -> None:
    asyncio.run(router_mod.sdk_cost_alert_sink(_alert("OP-820", 0.25, 1)))

    response = _api_alias_client().get("/api/sdk/cost?ticket=OP-820")

    assert response.status_code == 200
    assert response.json()["rows"][0]["ticket"] == "OP-820"


@pytest.mark.asyncio
async def test_cost_guard_alert_sink_feeds_tracker() -> None:
    guard = CostGuard(alert_sink=router_mod.sdk_cost_alert_sink)
    await guard.configure_budget(
        ScopeKey(kind="workspace", key="OP-820"),
        daily_limit_usd=1.0,
    )
    estimate = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=300_000,
        output_tokens=0,
        workspace="OP-820",
    )

    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    check = await guard.check(
        estimate,
        now=now,
    )
    snapshot = await router_mod.tracker.snapshot("OP-820", now=now)

    assert check.allowed is True
    assert len(check.triggered_alerts) == 1
    assert snapshot["rows"][0]["ticket"] == "OP-820"
    assert snapshot["rows"][0]["rolling_24h_usd"] == pytest.approx(0.9)
    assert snapshot["rows"][0]["cumulative_usd"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_synthetic_sdk_run_posts_five_alerts_to_live_subscriber() -> None:
    queue = await router_mod.tracker.subscribe("OP-820")
    try:
        for idx in range(1, 6):
            await router_mod.sdk_cost_alert_sink(_alert("OP-820", idx * 0.10, idx))

        updates = [queue.get_nowait() for _ in range(5)]
        snapshot = await router_mod.tracker.snapshot("OP-820")
    finally:
        await router_mod.tracker.unsubscribe("OP-820", queue)

    assert [u["delta_usd"] for u in updates] == pytest.approx([0.1] * 5)
    assert updates[-1]["cumulative_usd"] == pytest.approx(0.5)
    assert snapshot["rows"][0]["alert_count_24h"] == 5
    assert snapshot["rows"][0]["rolling_24h_usd"] == pytest.approx(0.5)
    assert snapshot["rows"][0]["cumulative_usd"] == pytest.approx(0.5)


def test_get_sdk_cost_rejects_bad_ticket() -> None:
    response = _client().get("/sdk/cost?ticket=BAD-820")

    assert response.status_code == 400
    assert response.json()["detail"] == "ticket must look like OP-XX"
