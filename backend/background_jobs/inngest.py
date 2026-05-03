"""FS.5.1 -- Inngest background job adapter."""

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

INNGEST_API_BASE = "https://inn.gs"


class InngestBackgroundJobAdapter(BackgroundJobAdapter):
    """Inngest event adapter (``provider='inngest'``)."""

    provider = "inngest"

    def _configure(
        self,
        *,
        event_key: str,
        api_base: str = INNGEST_API_BASE,
        **_: Any,
    ) -> None:
        if not event_key:
            raise ValueError("InngestBackgroundJobAdapter requires event_key")
        self._event_key = event_key
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _payload(self, request: BackgroundJobRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": request.name,
            "data": dict(request.payload),
        }
        if request.idempotency_key:
            body["id"] = request.idempotency_key
        return body

    async def dispatch_job(
        self,
        request: BackgroundJobRequest,
        **kwargs: Any,
    ) -> BackgroundJobResult:
        del kwargs
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/e/{self._event_key}",
                headers=self._headers(),
                json=self._payload(request),
            )
        raise_for_background_job_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        job_id = str(data.get("ids", [None])[0] or data.get("id") or "")
        if not job_id:
            raise BackgroundJobError(
                "Inngest response missing event id",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info("inngest.job_dispatch name=%s job_id=%s fp=%s",
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
            "id": request.name,
            "cron": request.cron,
            "event": request.name,
        }
        return CronDescriptor(
            provider=self.provider,
            name=request.name,
            schedule=request.cron,
            target=request.name,
            raw=raw,
        )


__all__ = ["INNGEST_API_BASE", "InngestBackgroundJobAdapter"]
