"""FS.5.2 -- Background job definition scaffold.

This module mirrors ``backend.email_delivery.templates``: immutable
catalog items, a read-only mapping proxy, and small list/get helpers.
It only describes jobs; FS.5.3 owns schedule wiring and FS.5.4 owns
retry / dead-letter behavior.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable catalog tuples and a read-only mapping
proxy. Every request derives fresh ``BackgroundJobRequest`` instances
from explicit payload values; no cache, singleton, env read, network IO,
or mutable shared state is used across uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from backend.background_jobs.base import BackgroundJobRequest


@dataclass(frozen=True)
class BackgroundJobDefinition:
    """One FS.5.2 background job catalog entry."""

    job_id: str
    display_name: str
    description: str
    handler: str
    cron: str | None = None
    endpoint_path: str | None = None
    default_payload: Mapping[str, Any] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.job_id or not self.job_id.strip():
            raise ValueError("job_id is required")
        if not self.display_name or not self.display_name.strip():
            raise ValueError("display_name is required")
        if not self.handler or not self.handler.strip():
            raise ValueError("handler is required")
        if self.endpoint_path is not None and not self.endpoint_path.strip():
            raise ValueError("endpoint_path cannot be empty")

    def to_request(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        cron: str | None = None,
        endpoint_path: str | None = None,
    ) -> BackgroundJobRequest:
        """Build a provider-neutral request for this job definition."""
        merged_payload = dict(self.default_payload)
        if payload:
            merged_payload.update(dict(payload))
        return BackgroundJobRequest(
            name=self.job_id,
            payload=merged_payload,
            idempotency_key=idempotency_key,
            cron=self.cron if cron is None else cron,
            endpoint_path=self.endpoint_path if endpoint_path is None else endpoint_path,
        )

    def cron_request(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundJobRequest:
        """Build a request for cron-backed providers."""
        if not self.cron:
            raise ValueError(f"job {self.job_id!r} does not define a cron schedule")
        return self.to_request(payload=payload, idempotency_key=idempotency_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "display_name": self.display_name,
            "description": self.description,
            "handler": self.handler,
            "cron": self.cron,
            "endpoint_path": self.endpoint_path,
            "default_payload": dict(self.default_payload),
            "tags": dict(self.tags),
        }


BACKGROUND_JOB_DEFINITION_IDS: tuple[str, ...] = (
    "decision-timeout-sweep",
    "tenant-quota-sweep",
    "user-drafts-gc",
    "workspace-gc",
    "shareable-objects-expiry-cleanup",
)


BACKGROUND_JOB_DEFINITION_ITEMS: tuple[BackgroundJobDefinition, ...] = (
    BackgroundJobDefinition(
        job_id="decision-timeout-sweep",
        display_name="Decision timeout sweep",
        description="Resolve expired Decision Engine proposals.",
        handler="backend.decision_engine.sweep_timeouts",
        cron="*/1 * * * *",
        tags={"subsystem": "decision-engine"},
    ),
    BackgroundJobDefinition(
        job_id="tenant-quota-sweep",
        display_name="Tenant quota sweep",
        description="Check tenant storage pressure and run LRU cleanup when needed.",
        handler="backend.tenant_quota.sweep_all_tenants",
        cron="*/5 * * * *",
        tags={"subsystem": "storage"},
    ),
    BackgroundJobDefinition(
        job_id="user-drafts-gc",
        display_name="User drafts GC",
        description="Prune stale user_drafts rows past the 24 h retention window.",
        handler="backend.user_drafts_gc.sweep_once",
        cron="0 * * * *",
        tags={"subsystem": "drafts"},
    ),
    BackgroundJobDefinition(
        job_id="workspace-gc",
        display_name="Workspace GC",
        description="Move stale workspace leaves to trash and purge expired trash.",
        handler="backend.workspace_gc.sweep_once",
        cron="0 * * * *",
        tags={"subsystem": "workspace"},
    ),
    BackgroundJobDefinition(
        job_id="shareable-objects-expiry-cleanup",
        display_name="Shareable objects expiry cleanup",
        description="Audit and delete expired shareable_objects rows.",
        handler="backend.shareable_objects.cleanup_expired_shareable_objects",
        cron="0 * * * *",
        tags={"subsystem": "sharing"},
    ),
)


BACKGROUND_JOB_DEFINITIONS: Mapping[str, BackgroundJobDefinition] = MappingProxyType(
    {item.job_id: item for item in BACKGROUND_JOB_DEFINITION_ITEMS}
)


def list_background_job_definitions() -> list[str]:
    """Return FS.5.2 background job definition ids."""
    return list(BACKGROUND_JOB_DEFINITION_IDS)


def get_background_job_definition(job_id: str) -> BackgroundJobDefinition:
    """Return one FS.5.2 background job definition entry."""
    key = job_id.strip().lower().replace("_", "-")
    try:
        return BACKGROUND_JOB_DEFINITIONS[key]
    except KeyError:
        raise KeyError(
            f"unknown background job definition {job_id!r}; "
            f"known: {', '.join(BACKGROUND_JOB_DEFINITION_IDS)}"
        ) from None


__all__ = [
    "BACKGROUND_JOB_DEFINITION_IDS",
    "BACKGROUND_JOB_DEFINITION_ITEMS",
    "BACKGROUND_JOB_DEFINITIONS",
    "BackgroundJobDefinition",
    "get_background_job_definition",
    "list_background_job_definitions",
]
