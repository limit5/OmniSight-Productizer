"""W10 #284 — FastAPI router tests for /rum/* endpoints."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import intent_source
from backend.intent_source import IntentStory, SubtaskRef
from backend.observability import (
    reset_default_aggregator,
    reset_default_router,
)
from backend.routers.web_observability import router


# ── Fixtures ────────────────────────────────────────────────────


class FakeIntentSource:
    vendor = "jira"

    def __init__(self):
        self.created = []
        self.next_id = 1

    async def fetch_story(self, ticket): return IntentStory(vendor="jira", ticket=ticket, summary="")
    async def update_status(self, *a, **kw): return {"ok": True}
    async def comment(self, *a, **kw): return {"ok": True}
    async def verify_webhook(self, *a, **kw): return True
    def parse_webhook(self, *a, **kw): return ("", "")

    async def create_subtasks(self, parent, payloads):
        out = []
        for p in payloads:
            out.append(SubtaskRef(
                vendor="jira",
                ticket=f"OMNI-{self.next_id}",
                url=f"https://jira.example.com/browse/OMNI-{self.next_id}",
                parent=parent,
            ))
            self.next_id += 1
        self.created.append((parent, list(payloads)))
        return out


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def fresh_state():
    reset_default_aggregator()
    reset_default_router()
    intent_source.reset_registry_for_tests()
    yield
    reset_default_aggregator()
    reset_default_router()
    intent_source.reset_registry_for_tests()


# ── /rum/vitals ─────────────────────────────────────────────────


class TestVitalsIngest:

    def test_minimal_payload_accepted(self, client):
        resp = client.post("/rum/vitals", json={"name": "LCP", "value": 2200})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert body["name"] == "LCP"
        assert body["value"] == 2200
        assert body["rating"] == "good"

    def test_classification_overridden_by_payload(self, client):
        resp = client.post("/rum/vitals", json={
            "name": "LCP", "value": 5500, "rating": "good",
        })
        # Caller-provided rating wins.
        assert resp.json()["rating"] == "good"

    def test_default_classification_when_rating_omitted(self, client):
        resp = client.post("/rum/vitals", json={
            "name": "LCP", "value": 5500,
        })
        assert resp.json()["rating"] == "poor"

    def test_missing_name_400(self, client):
        resp = client.post("/rum/vitals", json={"value": 100})
        assert resp.status_code == 400

    def test_non_number_value_400(self, client):
        resp = client.post("/rum/vitals", json={"name": "LCP", "value": "fast"})
        assert resp.status_code == 400

    def test_payload_too_large_413(self, client):
        # 17 KiB body > 16 KiB limit.
        big = "x" * (17 * 1024)
        resp = client.post(
            "/rum/vitals",
            content=big,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413

    def test_invalid_json_400(self, client):
        resp = client.post(
            "/rum/vitals",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_non_object_json_400(self, client):
        resp = client.post(
            "/rum/vitals",
            content="[1,2,3]",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_user_agent_falls_back_to_header(self, client):
        from backend.observability import get_default_aggregator
        client.post("/rum/vitals",
                    json={"name": "LCP", "value": 2200, "page": "/x"},
                    headers={"User-Agent": "test/1.0"})
        # The recorded sample's user_agent should reflect the header.
        snap = get_default_aggregator().snapshot(page="/x")
        assert snap.metrics  # one bucket recorded


# ── /rum/errors ─────────────────────────────────────────────────


class TestErrorsIngest:

    def test_minimal_payload_creates_jira_ticket(self, client):
        fake = FakeIntentSource()
        intent_source.register_source(fake)

        resp = client.post("/rum/errors", json={
            "message": "TypeError: x is undefined",
            "page": "/blog",
            "release": "1.0.0",
            "stack": "at app.js:1:2",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert body["routed"] is True
        assert body["ticket"] == "OMNI-1"
        assert body["ticket_url"].endswith("OMNI-1")
        assert body["fingerprint"]
        assert len(fake.created) == 1

    def test_no_intent_source_still_200(self, client):
        # No registered IntentSource — the endpoint must still 200 so
        # the browser doesn't retry. routed=False.
        resp = client.post("/rum/errors", json={
            "message": "boom", "release": "1.0.0",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert body["routed"] is False

    def test_missing_message_400(self, client):
        resp = client.post("/rum/errors", json={"stack": "x"})
        assert resp.status_code == 400

    def test_payload_too_large_413(self, client):
        big = "x" * (65 * 1024)
        resp = client.post(
            "/rum/errors", data=big,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413

    def test_dedup_within_session(self, client):
        fake = FakeIntentSource()
        intent_source.register_source(fake)
        payload = {"message": "boom", "release": "1.0.0", "stack": "a.js:1:2"}
        r1 = client.post("/rum/errors", json=payload)
        r2 = client.post("/rum/errors", json=payload)
        assert r1.json()["ticket"] == r2.json()["ticket"]
        assert len(fake.created) == 1


# ── /rum/dashboard ──────────────────────────────────────────────


class TestDashboard:

    def test_dashboard_after_ingest_returns_metrics(self, client):
        for v in (1500, 2000, 2500, 3500, 4500):
            client.post("/rum/vitals", json={
                "name": "LCP", "value": v, "page": "/",
            })
        resp = client.get("/rum/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_samples"] == 5
        # Two buckets: page="/" + rollup="*"
        assert len(body["metrics"]) == 2

    def test_dashboard_filter_by_metric(self, client):
        client.post("/rum/vitals", json={"name": "LCP", "value": 2200, "page": "/"})
        client.post("/rum/vitals", json={"name": "INP", "value": 120, "page": "/"})
        resp = client.get("/rum/dashboard?metric=LCP")
        body = resp.json()
        assert all(m["name"] == "LCP" for m in body["metrics"])

    def test_dashboard_filter_by_page(self, client):
        client.post("/rum/vitals", json={"name": "LCP", "value": 2200, "page": "/blog"})
        client.post("/rum/vitals", json={"name": "LCP", "value": 1800, "page": "/about"})
        resp = client.get("/rum/dashboard?page=/blog")
        body = resp.json()
        assert all(m["page"] == "/blog" for m in body["metrics"])

    def test_dashboard_reset_wipes(self, client):
        client.post("/rum/vitals", json={"name": "LCP", "value": 2200})
        resp = client.get("/rum/dashboard?reset=true")
        assert resp.json()["reset"] is True
        # Subsequent dashboard call shows empty.
        resp2 = client.get("/rum/dashboard")
        assert resp2.json()["total_samples"] == 0


# ── /rum/errors/recent ──────────────────────────────────────────


class TestErrorsRecent:

    def test_lists_routed_fingerprints(self, client):
        fake = FakeIntentSource()
        intent_source.register_source(fake)
        for i in range(3):
            client.post("/rum/errors", json={
                "message": f"e{i}", "release": "1.0.0",
                "stack": f"a{i}.js:1:2",
            })
        resp = client.get("/rum/errors/recent")
        body = resp.json()
        assert "items" in body
        assert "metrics" in body
        assert len(body["items"]) == 3
        assert body["metrics"]["routed"] == 3

    def test_limit_param_caps_items(self, client):
        fake = FakeIntentSource()
        intent_source.register_source(fake)
        for i in range(5):
            client.post("/rum/errors", json={
                "message": f"e{i}", "release": "1.0.0",
                "stack": f"a{i}.js:1:2",
            })
        resp = client.get("/rum/errors/recent?limit=2")
        assert len(resp.json()["items"]) == 2

    def test_limit_validation(self, client):
        resp = client.get("/rum/errors/recent?limit=0")
        assert resp.status_code == 422  # FastAPI Query validation


# ── /rum/health ─────────────────────────────────────────────────


class TestHealth:

    def test_health_reports_zero_when_idle(self, client):
        resp = client.get("/rum/health")
        body = resp.json()
        assert body["ok"] is True
        assert body["vitals"]["total_samples"] == 0
        assert body["errors"]["routed"] == 0

    def test_health_after_ingest(self, client):
        client.post("/rum/vitals", json={"name": "LCP", "value": 2200})
        resp = client.get("/rum/health")
        body = resp.json()
        assert body["vitals"]["total_samples"] == 1
        assert body["vitals"]["active_buckets"] >= 1
