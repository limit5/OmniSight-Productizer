"""FS.5.1 -- Trigger.dev background job adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.background_jobs.base import (
    BackgroundJobAdapter,
    BackgroundJobError,
    BackgroundJobRequest,
    BackgroundJobResult,
    CronDescriptor,
)
from backend.background_jobs.http import raise_for_background_job_response

logger = logging.getLogger(__name__)

TRIGGER_DEV_API_BASE = "https://api.trigger.dev"


class TriggerDevBackgroundJobAdapter(BackgroundJobAdapter):
    """Trigger.dev task adapter (``provider='trigger-dev'``)."""

    provider = "trigger-dev"

    def _configure(
        self,
        *,
        api_base: str = TRIGGER_DEV_API_BASE,
        **_: Any,
    ) -> None:
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _payload(self, request: BackgroundJobRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"payload": dict(request.payload)}
        options: dict[str, Any] = {}
        if request.idempotency_key:
            options["idempotencyKey"] = request.idempotency_key
        if options:
            body["options"] = options
        return body

    async def dispatch_job(
        self,
        request: BackgroundJobRequest,
        **kwargs: Any,
    ) -> BackgroundJobResult:
        del kwargs
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/api/v1/tasks/{request.name}/trigger",
                headers=self._headers(),
                json=self._payload(request),
            )
        raise_for_background_job_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        job_id = str(data.get("id") or data.get("runId") or "")
        if not job_id:
            raise BackgroundJobError(
                "Trigger.dev response missing run id",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info("trigger_dev.job_dispatch name=%s job_id=%s fp=%s",
                    request.name, job_id, self.token_fp())
        return BackgroundJobResult(
            provider=self.provider,
            job_id=job_id,
            status=str(data.get("status") or "queued"),
            raw=data,
        )

    def cron_descriptor(self, request: BackgroundJobRequest) -> CronDescriptor:
        if not request.cron:
            raise ValueError("cron schedule is required")
        raw = {
            "task": request.name,
            "cron": request.cron,
        }
        return CronDescriptor(
            provider=self.provider,
            name=request.name,
            schedule=request.cron,
            target=request.name,
            raw=raw,
        )


__all__ = ["TRIGGER_DEV_API_BASE", "TriggerDevBackgroundJobAdapter"]
