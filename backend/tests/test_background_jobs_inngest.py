"""FS.5.1 -- Inngest background job adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.background_jobs.base import (
    BackgroundJobError,
    BackgroundJobRateLimitError,
    BackgroundJobRequest,
    InvalidBackgroundJobTokenError,
)
from backend.background_jobs.inngest import INNGEST_API_BASE, InngestBackgroundJobAdapter

S = INNGEST_API_BASE


def _mk_adapter(**kw):
    return InngestBackgroundJobAdapter(
        token="inngest_ABCDEF0123456789",
        event_key="event-key",
        **kw,
    )


def _request() -> BackgroundJobRequest:
    return BackgroundJobRequest(
        name="catalog.sync",
        payload={"tenant_id": "t1"},
        idempotency_key="sync-t1",
        cron="*/15 * * * *",
    )


class TestInngestBackgroundJob:

    @respx.mock
    async def test_dispatch_job_happy(self):
        route = respx.post(f"{S}/e/event-key").mock(
            return_value=httpx.Response(200, json={"ids": ["evt_123"]}),
        )

        result = await _mk_adapter().dispatch_job(_request())

        assert result.provider == "inngest"
        assert result.job_id == "evt_123"
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer inngest_ABCDEF0123456789"
        body = httpx.Response(200, content=req.read()).json()
        assert body == {
            "name": "catalog.sync",
            "data": {"tenant_id": "t1"},
            "id": "sync-t1",
        }

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(f"{S}/e/event-key").mock(
            return_value=httpx.Response(401, json={"message": "bad token"}),
        )
        with pytest.raises(InvalidBackgroundJobTokenError):
            await _mk_adapter().dispatch_job(_request())

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(f"{S}/e/event-key").mock(
            return_value=httpx.Response(
                429,
                json={"message": "slow"},
                headers={"Retry-After": "7"},
            ),
        )
        with pytest.raises(BackgroundJobRateLimitError) as excinfo:
            await _mk_adapter().dispatch_job(_request())
        assert excinfo.value.retry_after == 7

    @respx.mock
    async def test_missing_event_id_rejected(self):
        respx.post(f"{S}/e/event-key").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(BackgroundJobError, match="event id"):
            await _mk_adapter().dispatch_job(_request())

    def test_cron_descriptor(self):
        desc = _mk_adapter().cron_descriptor(_request())
        assert desc.provider == "inngest"
        assert desc.schedule == "*/15 * * * *"
        assert desc.target == "catalog.sync"
        assert desc.raw == {
            "id": "catalog.sync",
            "cron": "*/15 * * * *",
            "event": "catalog.sync",
        }
