"""FS.5.5 -- Integration tests for the background jobs surface."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.background_jobs import (
    BACKGROUND_JOB_DEFINITION_IDS,
    BackgroundJobRetryPolicy,
    InMemoryBackgroundJobDeadLetterQueue,
    build_cron_schedule_manifest,
    dispatch_background_job_with_retry,
    get_adapter,
    get_cron_schedule_binding,
    list_providers,
)
from backend.background_jobs.base import InvalidBackgroundJobTokenError
from backend.background_jobs.inngest import INNGEST_API_BASE
from backend.background_jobs.trigger_dev import TRIGGER_DEV_API_BASE

VERCEL_BASE_URL = "https://app.example.com"


def _adapter(provider: str):
    cls = get_adapter(provider)
    if provider == "inngest":
        return cls.from_plaintext_token("inngest_ABCDEF0123456789", event_key="evt-key")
    if provider == "vercel-cron":
        return cls.from_plaintext_token(
            "cron_ABCDEF0123456789",
            base_url=VERCEL_BASE_URL,
        )
    return cls.from_plaintext_token("tr_ABCDEF0123456789")


def _dispatch_url(provider: str, job_id: str) -> str:
    if provider == "inngest":
        return f"{INNGEST_API_BASE}/e/evt-key"
    if provider == "trigger-dev":
        return f"{TRIGGER_DEV_API_BASE}/api/v1/tasks/{job_id}/trigger"
    return f"{VERCEL_BASE_URL}/api/cron/{job_id}"


def _success_response(provider: str) -> httpx.Response:
    if provider == "inngest":
        return httpx.Response(200, json={"ids": ["evt_decision"], "status": "queued"})
    if provider == "trigger-dev":
        return httpx.Response(200, json={"id": "run_decision", "status": "PENDING"})
    return httpx.Response(
        200,
        json={"job_id": "cron_decision", "status": "accepted"},
    )


class TestBackgroundJobsIntegration:

    @pytest.mark.parametrize("provider", ["inngest", "trigger-dev", "vercel-cron"])
    def test_cron_manifest_covers_catalog_for_every_provider(self, provider):
        manifest = build_cron_schedule_manifest(_adapter(provider))

        assert manifest["provider"] == provider
        assert tuple(item["job_id"] for item in manifest["schedules"]) == (
            BACKGROUND_JOB_DEFINITION_IDS
        )
        assert all(item["provider"] == provider for item in manifest["schedules"])
        assert all(item["request"]["cron"] for item in manifest["schedules"])
        assert all(item["descriptor"]["schedule"] for item in manifest["schedules"])

    @respx.mock
    @pytest.mark.parametrize("provider", ["inngest", "trigger-dev", "vercel-cron"])
    async def test_catalog_request_dispatches_through_retry_for_each_provider(
        self,
        provider,
    ):
        adapter = _adapter(provider)
        binding = get_cron_schedule_binding(adapter, "decision-timeout-sweep")
        route = respx.post(_dispatch_url(provider, binding.job_id)).mock(
            side_effect=[
                httpx.Response(503, json={"message": "temporary"}),
                _success_response(provider),
            ],
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        result = await dispatch_background_job_with_retry(
            adapter,
            binding.request,
            policy=BackgroundJobRetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=1,
            ),
            sleep=_sleep,
        )

        assert result.provider == provider
        assert result.job_id in {"evt_decision", "run_decision", "cron_decision"}
        assert route.call_count == 2
        assert sleeps == [0.0]

    @respx.mock
    async def test_non_retryable_provider_failure_goes_to_dlq_with_catalog_request(
        self,
    ):
        adapter = _adapter("inngest")
        binding = get_cron_schedule_binding(adapter, "tenant-quota-sweep")
        dlq = InMemoryBackgroundJobDeadLetterQueue()
        route = respx.post(_dispatch_url("inngest", binding.job_id)).mock(
            return_value=httpx.Response(401, json={"message": "bad token"}),
        )

        with pytest.raises(InvalidBackgroundJobTokenError):
            await dispatch_background_job_with_retry(adapter, binding.request, dlq=dlq)

        entries = await dlq.list_entries()
        assert route.call_count == 1
        assert len(entries) == 1
        assert entries[0].provider == "inngest"
        assert entries[0].job_name == "tenant-quota-sweep"
        assert entries[0].request["cron"] == "*/5 * * * *"
        assert entries[0].attempts_made == 1
        assert entries[0].retryable is False

    def test_provider_list_matches_fs_5_test_matrix(self):
        assert list_providers() == ["inngest", "trigger-dev", "vercel-cron"]
