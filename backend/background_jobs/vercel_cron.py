"""FS.5.1 -- Vercel Cron HTTP adapter."""

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


class VercelCronBackgroundJobAdapter(BackgroundJobAdapter):
    """Vercel Cron adapter (``provider='vercel-cron'``)."""

    provider = "vercel-cron"

    def _configure(
        self,
        *,
        base_url: str,
        **_: Any,
    ) -> None:
        if not base_url:
            raise ValueError("VercelCronBackgroundJobAdapter requires base_url")
        self._base_url = base_url.rstrip("/")

    def _headers(self, request: BackgroundJobRequest) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "vercel-cron/1.0",
        }
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key
        return headers

    def _path(self, request: BackgroundJobRequest) -> str:
        if request.endpoint_path:
            return request.endpoint_path
        return f"/api/cron/{request.name}"

    async def dispatch_job(
        self,
        request: BackgroundJobRequest,
        **kwargs: Any,
    ) -> BackgroundJobResult:
        del kwargs
        path = self._path(request)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._base_url}{path}",
                headers=self._headers(request),
                json={"name": request.name, "payload": dict(request.payload)},
            )
        raise_for_background_job_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        job_id = str(
            data.get("job_id")
            or data.get("id")
            or request.idempotency_key
            or request.name
        )
        if not job_id:
            raise BackgroundJobError(
                "Vercel Cron response missing job id",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info("vercel_cron.job_dispatch name=%s job_id=%s fp=%s",
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
        path = self._path(request)
        raw = {"path": path, "schedule": request.cron}
        return CronDescriptor(
            provider=self.provider,
            name=request.name,
            schedule=request.cron,
            target=path,
            raw=raw,
        )


__all__ = ["VercelCronBackgroundJobAdapter"]
