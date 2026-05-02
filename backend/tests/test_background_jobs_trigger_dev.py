"""FS.5.1 -- Trigger.dev background job adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.background_jobs.base import (
    BackgroundJobConflictError,
    BackgroundJobError,
    BackgroundJobRequest,
)
from backend.background_jobs.trigger_dev import (
    TRIGGER_DEV_API_BASE,
    TriggerDevBackgroundJobAdapter,
)

S = TRIGGER_DEV_API_BASE


def _mk_adapter(**kw):
    return TriggerDevBackgroundJobAdapter(
        token="tr_ABCDEF0123456789",
        **kw,
    )


def _request() -> BackgroundJobRequest:
    return BackgroundJobRequest(
        name="catalog-sync",
        payload={"tenant_id": "t1"},
        idempotency_key="sync-t1",
        cron="0 * * * *",
    )


class TestTriggerDevBackgroundJob:

    @respx.mock
    async def test_dispatch_job_happy(self):
        route = respx.post(f"{S}/api/v1/tasks/catalog-sync/trigger").mock(
            return_value=httpx.Response(
                200,
                json={"id": "run_123", "status": "PENDING"},
            ),
        )

        result = await _mk_adapter().dispatch_job(_request())

        assert result.provider == "trigger-dev"
        assert result.job_id == "run_123"
        assert result.status == "PENDING"
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer tr_ABCDEF0123456789"
        body = httpx.Response(200, content=req.read()).json()
        assert body == {
            "payload": {"tenant_id": "t1"},
            "options": {"idempotencyKey": "sync-t1"},
        }

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.post(f"{S}/api/v1/tasks/catalog-sync/trigger").mock(
            return_value=httpx.Response(422, json={"message": "bad payload"}),
        )
        with pytest.raises(BackgroundJobConflictError):
            await _mk_adapter().dispatch_job(_request())

    @respx.mock
    async def test_missing_run_id_rejected(self):
        respx.post(f"{S}/api/v1/tasks/catalog-sync/trigger").mock(
            return_value=httpx.Response(200, json={}),
        )
        with pytest.raises(BackgroundJobError, match="run id"):
            await _mk_adapter().dispatch_job(_request())

    def test_cron_descriptor(self):
        desc = _mk_adapter().cron_descriptor(_request())
        assert desc.provider == "trigger-dev"
        assert desc.schedule == "0 * * * *"
        assert desc.target == "catalog-sync"
        assert desc.raw == {"task": "catalog-sync", "cron": "0 * * * *"}
