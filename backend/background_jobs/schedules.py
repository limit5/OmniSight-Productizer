"""FS.5.3 -- Cron schedule wiring for background job definitions.

This module connects the FS.5.2 immutable job catalog to the FS.5.1
provider adapters. It mirrors the registry helpers in
``backend.background_jobs.definitions``: callers pass an adapter, and
the helper returns fresh provider-specific cron descriptors without
performing network IO, env reads, dispatch, retry, or DLQ handling.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable dataclasses/functions only and reads the
immutable FS.5.2 catalog. Every manifest build derives fresh request
and descriptor objects from that catalog, so uvicorn workers do not
share mutable runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backend.background_jobs.base import (
    BackgroundJobAdapter,
    BackgroundJobRequest,
    CronDescriptor,
)
from backend.background_jobs.definitions import (
    BACKGROUND_JOB_DEFINITION_ITEMS,
    BackgroundJobDefinition,
    get_background_job_definition,
)


@dataclass(frozen=True)
class CronScheduleBinding:
    """One provider-specific cron schedule bound to one job definition."""

    job_id: str
    provider: str
    handler: str
    request: BackgroundJobRequest
    descriptor: CronDescriptor
    tags: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "provider": self.provider,
            "handler": self.handler,
            "request": self.request.to_dict(),
            "descriptor": self.descriptor.to_dict(),
            "tags": dict(self.tags),
        }


def build_cron_schedule_bindings(
    adapter: BackgroundJobAdapter,
    definitions: Iterable[BackgroundJobDefinition] = BACKGROUND_JOB_DEFINITION_ITEMS,
) -> tuple[CronScheduleBinding, ...]:
    """Return provider-specific cron bindings for cron-backed definitions."""
    bindings: list[CronScheduleBinding] = []
    for definition in definitions:
        if not definition.cron:
            continue
        request = definition.cron_request()
        descriptor = adapter.cron_descriptor(request)
        bindings.append(
            CronScheduleBinding(
                job_id=definition.job_id,
                provider=adapter.provider,
                handler=definition.handler,
                request=request,
                descriptor=descriptor,
                tags=dict(definition.tags),
            )
        )
    return tuple(bindings)


def get_cron_schedule_binding(
    adapter: BackgroundJobAdapter,
    job_id: str,
) -> CronScheduleBinding:
    """Return one provider-specific cron binding by job definition id."""
    definition = get_background_job_definition(job_id)
    if not definition.cron:
        raise ValueError(f"job {definition.job_id!r} does not define a cron schedule")
    bindings = build_cron_schedule_bindings(adapter, (definition,))
    return bindings[0]


def build_cron_schedule_manifest(
    adapter: BackgroundJobAdapter,
    definitions: Iterable[BackgroundJobDefinition] = BACKGROUND_JOB_DEFINITION_ITEMS,
) -> dict[str, Any]:
    """Return a JSON-safe manifest for provider cron schedule deployment."""
    bindings = build_cron_schedule_bindings(adapter, definitions)
    return {
        "provider": adapter.provider,
        "schedules": [binding.to_dict() for binding in bindings],
    }


__all__ = [
    "CronScheduleBinding",
    "build_cron_schedule_bindings",
    "build_cron_schedule_manifest",
    "get_cron_schedule_binding",
]
