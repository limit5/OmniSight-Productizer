"""FS.5.1 -- Vercel Cron background job adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.background_jobs.base import (
    BackgroundJobRequest,
    MissingBackgroundJobScopeError,
)
from backend.background_jobs.vercel_cron import VercelCronBackgroundJobAdapter

S = "https://app.example.com"


def _mk_adapter(**kw):
    return VercelCronBackgroundJobAdapter(
        token="cron_ABCDEF0123456789",
        base_url=S,
        **kw,
    )


def _request() -> BackgroundJobRequest:
    return BackgroundJobRequest(
        name="catalog-sync",
        payload={"tenant_id": "t1"},
        idempotency_key="sync-t1",
        cron="0 0 * * *",
        endpoint_path="/api/internal/cron/catalog-sync",
    )


class TestVercelCronBackgroundJob:

    @respx.mock
    async def test_dispatch_job_happy(self):
        route = respx.post(f"{S}/api/internal/cron/catalog-sync").mock(
            return_value=httpx.Response(
                200,
                json={"job_id": "cron_123", "status": "accepted"},
            ),
        )

        result = await _mk_adapter().dispatch_job(_request())

        assert result.provider == "vercel-cron"
        assert result.job_id == "cron_123"
        assert result.status == "accepted"
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer cron_ABCDEF0123456789"
        assert req.headers["idempotency-key"] == "sync-t1"
        body = httpx.Response(200, content=req.read()).json()
        assert body == {
            "name": "catalog-sync",
            "payload": {"tenant_id": "t1"},
        }

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(f"{S}/api/internal/cron/catalog-sync").mock(
            return_value=httpx.Response(403, json={"message": "denied"}),
        )
        with pytest.raises(MissingBackgroundJobScopeError):
            await _mk_adapter().dispatch_job(_request())

    @respx.mock
    async def test_falls_back_to_idempotency_key_as_job_id(self):
        respx.post(f"{S}/api/internal/cron/catalog-sync").mock(
            return_value=httpx.Response(204),
        )

        result = await _mk_adapter().dispatch_job(_request())

        assert result.job_id == "sync-t1"

    def test_cron_descriptor(self):
        desc = _mk_adapter().cron_descriptor(_request())
        assert desc.provider == "vercel-cron"
        assert desc.schedule == "0 0 * * *"
        assert desc.target == "/api/internal/cron/catalog-sync"
        assert desc.raw == {
            "path": "/api/internal/cron/catalog-sync",
            "schedule": "0 0 * * *",
        }

    def test_default_endpoint_path(self):
        req = BackgroundJobRequest(name="daily-rollup", cron="0 0 * * *")
        desc = _mk_adapter().cron_descriptor(req)
        assert desc.target == "/api/cron/daily-rollup"
