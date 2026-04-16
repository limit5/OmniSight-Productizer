"""W10 #284 — End-to-end integration tests across the observability stack.

Pins the cross-component contracts:

  * Browser snippets emitted by every RUM adapter beacon to the SAME
    path that the FastAPI router serves (``/api/v1/rum/vitals``).
  * A vital ingested through the router lands in the same aggregator
    that the dashboard endpoint reads from.
  * An error ingested through the router routes through the same
    IntentSource registry the rest of the orchestrator uses.
  * The set of CWV metric names emitted by the snippet matches the
    base module's ``KNOWN_VITALS`` constant.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import intent_source
from backend.intent_source import IntentStory, SubtaskRef
from backend.observability import (
    KNOWN_VITALS,
    get_default_aggregator,
    get_rum_adapter,
    list_providers,
    reset_default_aggregator,
    reset_default_router,
)
from backend.routers.web_observability import router as rum_router


@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(rum_router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture(autouse=True)
def fresh():
    reset_default_aggregator()
    reset_default_router()
    intent_source.reset_registry_for_tests()
    yield
    reset_default_aggregator()
    reset_default_router()
    intent_source.reset_registry_for_tests()


class FakeJira:
    vendor = "jira"
    def __init__(self): self.created = []
    async def fetch_story(self, t): return IntentStory(vendor="jira", ticket=t, summary="")
    async def update_status(self, *a, **k): return {"ok": True}
    async def comment(self, *a, **k): return {"ok": True}
    async def verify_webhook(self, *a, **k): return True
    def parse_webhook(self, *a, **k): return ("", "")
    async def create_subtasks(self, parent, payloads):
        out = [SubtaskRef(vendor="jira", ticket=f"OMNI-{i+1}",
                          url=f"http://j/{i+1}", parent=parent)
               for i in range(len(payloads))]
        self.created.append((parent, list(payloads)))
        return out


# ── Snippet ↔ router path contract ──────────────────────────────


class TestSnippetRouterContract:

    @pytest.mark.parametrize("provider", list_providers())
    def test_every_snippet_targets_router_path(self, provider):
        """Snippet → /api/v1/rum/vitals; the router mounts under that
        same prefix in main.py. A drift here means deployed pages
        beacon to a 404."""
        cls = get_rum_adapter(provider)
        if provider == "datadog":
            adapter = cls(dsn="dsn-token", application_id="app-1")
        else:
            adapter = cls(dsn="https://k@o1.ingest.sentry.io/1")
        snippet = adapter.browser_snippet()
        assert "/api/v1/rum/vitals" in snippet

    @pytest.mark.parametrize("provider", list_providers())
    def test_snippet_emits_every_known_vital(self, provider):
        """The snippet wires onLCP/onINP/onCLS/onTTFB/onFCP — when CWV
        adds a new metric, base.KNOWN_VITALS must update AND every
        snippet must wire the new on* call."""
        cls = get_rum_adapter(provider)
        if provider == "datadog":
            adapter = cls(dsn="dsn-token", application_id="app-1")
        else:
            adapter = cls(dsn="https://k@o1.ingest.sentry.io/1")
        snippet = adapter.browser_snippet()
        for name in KNOWN_VITALS:
            assert f"on{name}" in snippet, f"{provider} snippet missing on{name}"


# ── End-to-end ingest → aggregator ──────────────────────────────


class TestIngestToDashboard:

    def test_post_vital_lands_in_dashboard(self, app_client):
        for v in (1500, 2500, 3500, 4500):
            app_client.post("/api/v1/rum/vitals", json={
                "name": "LCP", "value": v, "page": "/",
            })
        resp = app_client.get("/api/v1/rum/dashboard?page=/&metric=LCP")
        body = resp.json()
        assert len(body["metrics"]) == 1
        m = body["metrics"][0]
        assert m["count"] == 4
        assert m["good_count"] == 2  # 1500, 2500
        assert m["needs_improvement_count"] == 1  # 3500
        assert m["poor_count"] == 1  # 4500

    def test_router_uses_same_aggregator_as_module_singleton(self, app_client):
        app_client.post("/api/v1/rum/vitals", json={
            "name": "INP", "value": 150, "page": "/",
        })
        # Module-level singleton sees what the router recorded.
        snap = get_default_aggregator().snapshot()
        assert snap.total_samples == 1


class TestErrorToJiraEndToEnd:

    def test_error_ingest_creates_real_subtask(self, app_client):
        fake = FakeJira()
        intent_source.register_source(fake)
        resp = app_client.post("/api/v1/rum/errors", json={
            "message": "TypeError: x is undefined",
            "page": "/blog",
            "release": "1.42.0",
            "stack": "at app.js:1:2\nat react.js:5:6",
        })
        body = resp.json()
        assert body["routed"] is True
        assert body["ticket"] == "OMNI-1"
        # Look at the actual SubtaskPayload that landed in JIRA.
        assert len(fake.created) == 1
        parent, payloads = fake.created[0]
        assert parent == "OMNI-RUM-1.42.0"
        assert "TypeError: x is undefined" in payloads[0].title
        # The acceptance criteria should be self-contained for triage.
        ac = payloads[0].acceptance_criteria
        assert "/blog" in ac
        assert "1.42.0" in ac

    def test_dedup_across_endpoint_calls(self, app_client):
        fake = FakeJira()
        intent_source.register_source(fake)
        payload = {"message": "boom", "release": "1.0", "stack": "a.js:1:2"}
        for _ in range(5):
            app_client.post("/api/v1/rum/errors", json=payload)
        # Only one ticket created despite five posts.
        assert len(fake.created) == 1


class TestProviderParity:
    """Both Sentry and Datadog adapters must expose the same surface
    so the orchestrator can treat them interchangeably."""

    @pytest.mark.parametrize("provider", list_providers())
    def test_provider_constructible_with_dsn_only(self, provider):
        cls = get_rum_adapter(provider)
        if provider == "datadog":
            # Datadog also requires an application_id.
            adapter = cls(dsn="dsn-x", application_id="app-x")
        else:
            adapter = cls(dsn="https://k@o1.ingest.sentry.io/1")
        assert adapter.provider == provider

    @pytest.mark.parametrize("provider", list_providers())
    def test_browser_snippet_omit_dsn_uses_env(self, provider):
        cls = get_rum_adapter(provider)
        if provider == "datadog":
            adapter = cls(dsn="dsn-x", application_id="app-x")
        else:
            adapter = cls(dsn="https://k@o1.ingest.sentry.io/1")
        snippet = adapter.browser_snippet(include_dsn=False)
        assert "process.env" in snippet
