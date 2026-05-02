"""FS.5.1 -- Background job provider adapters package."""

from __future__ import annotations

from backend.background_jobs.base import (
    BackgroundJobAdapter,
    BackgroundJobConflictError,
    BackgroundJobError,
    BackgroundJobRateLimitError,
    BackgroundJobRequest,
    BackgroundJobResult,
    CronDescriptor,
    InvalidBackgroundJobTokenError,
    MissingBackgroundJobScopeError,
)
from backend.background_jobs.definitions import (
    BACKGROUND_JOB_DEFINITION_IDS,
    BACKGROUND_JOB_DEFINITION_ITEMS,
    BACKGROUND_JOB_DEFINITIONS,
    BackgroundJobDefinition,
    get_background_job_definition,
    list_background_job_definitions,
)
from backend.background_jobs.schedules import (
    CronScheduleBinding,
    build_cron_schedule_bindings,
    build_cron_schedule_manifest,
    get_cron_schedule_binding,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped background job adapter."""
    return ["inngest", "trigger-dev", "vercel-cron"]


def get_adapter(provider: str) -> type[BackgroundJobAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "inngest":
        from backend.background_jobs.inngest import InngestBackgroundJobAdapter
        return InngestBackgroundJobAdapter
    if key in ("trigger-dev", "triggerdev", "trigger"):
        from backend.background_jobs.trigger_dev import TriggerDevBackgroundJobAdapter
        return TriggerDevBackgroundJobAdapter
    if key in ("vercel-cron", "vercel", "cron"):
        from backend.background_jobs.vercel_cron import VercelCronBackgroundJobAdapter
        return VercelCronBackgroundJobAdapter
    raise ValueError(
        f"Unknown background job provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "BackgroundJobAdapter",
    "BackgroundJobConflictError",
    "BackgroundJobError",
    "BackgroundJobRateLimitError",
    "BackgroundJobRequest",
    "BackgroundJobResult",
    "CronDescriptor",
    "BACKGROUND_JOB_DEFINITION_IDS",
    "BACKGROUND_JOB_DEFINITION_ITEMS",
    "BACKGROUND_JOB_DEFINITIONS",
    "BackgroundJobDefinition",
    "InvalidBackgroundJobTokenError",
    "MissingBackgroundJobScopeError",
    "CronScheduleBinding",
    "build_cron_schedule_bindings",
    "build_cron_schedule_manifest",
    "get_background_job_definition",
    "get_adapter",
    "get_cron_schedule_binding",
    "list_background_job_definitions",
    "list_providers",
]
