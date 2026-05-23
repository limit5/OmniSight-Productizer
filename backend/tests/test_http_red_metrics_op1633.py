"""OP-1633 — HTTP RED metrics for SLO monitors."""

from __future__ import annotations

import re

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from backend import http_red_metrics
from backend import metrics as m
from backend.routers.observability import router as observability_router


prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


@pytest.fixture(autouse=True)
def _fresh_metrics():
    m.reset_for_tests()
    yield
    m.reset_for_tests()


@pytest.fixture
def client():
    app = FastAPI()
    router = APIRouter()

    @router.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @router.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=503, detail="synthetic failure")

    http_red_metrics.register_middleware(app)
    app.include_router(router)
    app.include_router(observability_router, prefix="/api/v1")
    return TestClient(app)


def _samples(text: str, family_name: str):
    for family in text_string_to_metric_families(text):
        if family.name == family_name:
            return list(family.samples)
    return []


@prom_only
def test_metrics_endpoint_exposes_route_and_raw_status_counter(client):
    resp = client.get("/items/abc")
    assert resp.status_code == 200

    text = client.get("/api/v1/metrics").text
    samples = _samples(text, "http_requests")
    hit = [
        s for s in samples
        if s.name == "http_requests_total"
        and s.labels == {"route": "/items/{item_id}", "status": "200"}
    ]
    assert hit and hit[0].value == 1.0
    assert "handler=" not in text
    assert 'status="2xx"' not in text


@prom_only
def test_duration_histogram_bucket_uses_route_status_and_le(client):
    resp = client.get("/items/abc")
    assert resp.status_code == 200

    text = client.get("/api/v1/metrics").text
    samples = _samples(text, "http_request_duration_seconds")
    buckets = [
        s for s in samples
        if s.name == "http_request_duration_seconds_bucket"
        and s.labels.get("route") == "/items/{item_id}"
        and s.labels.get("status") == "200"
        and "le" in s.labels
    ]
    assert buckets


@prom_only
def test_5xx_response_increments_raw_status_for_slo_regex(client):
    resp = client.get("/boom")
    assert resp.status_code == 503

    text = client.get("/api/v1/metrics").text
    samples = _samples(text, "http_requests")
    error_samples = [
        s for s in samples
        if s.name == "http_requests_total"
        and s.labels.get("route") == "/boom"
        and re.fullmatch(r"5..", s.labels.get("status", ""))
    ]
    total = sum(s.value for s in error_samples)
    assert total == 1.0
    assert 'status="5xx"' not in text
