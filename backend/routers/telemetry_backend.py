"""C17 — L4-CORE-17 Telemetry backend endpoints (#231).

REST endpoints for SDK profile queries, event type schemas, ingestion
(batched POST + retry queue), storage/retention, privacy/PII redaction,
consent management, dashboard panel queries, and telemetry test execution.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import telemetry_backend as tel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telemetry", tags=["telemetry"])


# -- Request models --

class IngestRequest(BaseModel):
    device_id: str = Field(..., description="Device identifier")
    events: list[dict[str, Any]] = Field(..., description="Batch of telemetry events")
    opt_in: bool = Field(default=False, description="Device opt-in consent flag")


class ConsentRequest(BaseModel):
    device_id: str = Field(..., description="Device identifier")
    opted_in: bool = Field(..., description="Whether the device opts in to telemetry")


class RetentionPurgeRequest(BaseModel):
    event_type: str = Field(..., description="Event type to purge (crash_dump, usage_event, perf_metric)")


class DashboardQueryRequest(BaseModel):
    dashboard_id: str = Field(..., description="Dashboard ID (fleet_health, crash_rate, adoption)")
    panel_id: str = Field(..., description="Panel ID within the dashboard")
    params: dict[str, Any] = Field(default_factory=dict, description="Optional query parameters")


class TelemetryTestRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")
    target: str = Field(default="localhost", description="Target device or host")
    work_dir: str = Field(default="/tmp/telemetry-test", description="Working directory for test artifacts")


class RetryQueueAddRequest(BaseModel):
    events: list[dict[str, Any]] = Field(..., description="Events to add to retry queue")


class FlushQueueRequest(BaseModel):
    device_id: str = Field(..., description="Device identifier")
    events: list[dict[str, Any]] = Field(..., description="Offline-queued events to flush")
    opt_in: bool = Field(default=False, description="Device opt-in consent flag")


class RedactRequest(BaseModel):
    event_data: dict[str, Any] = Field(..., description="Event data to redact PII from")


# -- SDK profile endpoints --

@router.get("/sdk-profiles")
async def list_sdk_profiles():
    profiles = tel.list_sdk_profiles()
    return [p.to_dict() for p in profiles]


@router.get("/sdk-profiles/{profile_id}")
async def get_sdk_profile(profile_id: str):
    profile = tel.get_sdk_profile(profile_id)
    if profile is None:
        raise HTTPException(404, f"SDK profile not found: {profile_id}")
    return profile.to_dict()


# -- Event type endpoints --

@router.get("/event-types")
async def list_event_types():
    types = tel.list_event_types()
    return [t.to_dict() for t in types]


@router.get("/event-types/{type_id}")
async def get_event_type(type_id: str):
    et = tel.get_event_type(type_id)
    if et is None:
        raise HTTPException(404, f"Event type not found: {type_id}")
    return et.to_dict()


# -- Ingestion endpoints --

@router.get("/ingestion/config")
async def get_ingestion_config():
    return tel.get_ingestion_config().to_dict()


@router.post("/ingest")
async def ingest_events(req: IngestRequest):
    result = tel.ingest_events(req.device_id, req.events, req.opt_in)
    status_code = 200
    if result.status == tel.IngestStatus.rate_limited:
        status_code = 429
    elif result.status == tel.IngestStatus.consent_required:
        status_code = 403
    elif result.status == tel.IngestStatus.rejected:
        status_code = 400
    if status_code != 200:
        raise HTTPException(status_code, detail=result.to_dict())
    return result.to_dict()


@router.post("/ingest/flush")
async def flush_offline_queue(req: FlushQueueRequest):
    result = tel.flush_offline_queue(req.device_id, req.events, req.opt_in)
    status_code = 200
    if result.status == tel.IngestStatus.rate_limited:
        status_code = 429
    elif result.status == tel.IngestStatus.consent_required:
        status_code = 403
    elif result.status == tel.IngestStatus.rejected:
        status_code = 400
    if status_code != 200:
        raise HTTPException(status_code, detail=result.to_dict())
    return result.to_dict()


# -- Retry queue endpoints --

@router.get("/retry-queue/status")
async def get_retry_queue_status():
    return tel.get_retry_queue_status()


@router.post("/retry-queue/add")
async def add_to_retry_queue(req: RetryQueueAddRequest):
    added = tel.add_to_retry_queue(req.events)
    return {"added": added, "queue_size": len(tel._RETRY_QUEUE)}


@router.post("/retry-queue/drain")
async def drain_retry_queue():
    drained = tel.drain_retry_queue()
    return {"drained_count": len(drained), "events": drained}


# -- Storage endpoints --

@router.get("/storage/config")
async def get_storage_config():
    return tel.get_storage_config().to_dict()


@router.post("/storage/purge")
async def run_retention_purge(req: RetentionPurgeRequest):
    result = tel.run_retention_purge(req.event_type)
    return result.to_dict()


# -- Privacy endpoints --

@router.get("/privacy/config")
async def get_privacy_config():
    cfg = tel.get_privacy_config()
    d = cfg.to_dict()
    d.pop("redaction_salt", None)
    return d


@router.post("/privacy/redact")
async def redact_pii(req: RedactRequest):
    return tel.redact_pii(req.event_data)


@router.post("/privacy/consent")
async def record_consent(req: ConsentRequest):
    record = tel.record_consent(req.device_id, req.opted_in)
    return record.to_dict()


@router.get("/privacy/consent/{device_id}")
async def get_consent(device_id: str):
    record = tel.get_consent(device_id)
    return record.to_dict()


# -- Dashboard endpoints --

@router.get("/dashboards")
async def list_dashboards():
    dashboards = tel.list_dashboards()
    return [d.to_dict() for d in dashboards]


@router.get("/dashboards/{dashboard_id}")
async def get_dashboard(dashboard_id: str):
    dashboard = tel.get_dashboard(dashboard_id)
    if dashboard is None:
        raise HTTPException(404, f"Dashboard not found: {dashboard_id}")
    return dashboard.to_dict()


@router.post("/dashboards/query")
async def query_dashboard_panel(req: DashboardQueryRequest):
    result = tel.query_dashboard_panel(req.dashboard_id, req.panel_id, req.params)
    return result.to_dict()


# -- Test recipe endpoints --

@router.get("/test-recipes")
async def list_test_recipes(domain: str | None = None):
    if domain:
        recipes = tel.get_recipes_by_domain(domain)
    else:
        recipes = tel.list_telemetry_test_recipes()
    return [r.to_dict() for r in recipes]


@router.get("/test-recipes/{recipe_id}")
async def get_test_recipe(recipe_id: str):
    recipe = tel.get_telemetry_test_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, f"Test recipe not found: {recipe_id}")
    return recipe.to_dict()


@router.post("/test-recipes/{recipe_id}/run")
async def run_test(recipe_id: str, req: TelemetryTestRequest | None = None):
    target = req.target if req else "localhost"
    work_dir = req.work_dir if req else "/tmp/telemetry-test"
    result = tel.run_telemetry_test(recipe_id, target, work_dir)
    if result.status == tel.TestStatus.error:
        raise HTTPException(404, detail=result.to_dict())
    return result.to_dict()


# -- SoC compatibility endpoints --

@router.get("/socs")
async def list_compatible_socs():
    socs = tel.list_compatible_socs()
    return [s.to_dict() for s in socs]


@router.get("/socs/{soc_id}")
async def check_soc_support(soc_id: str):
    support = tel.check_soc_telemetry_support(soc_id)
    return support.to_dict()


# -- Artifact definition endpoints --

@router.get("/artifacts")
async def list_artifact_definitions():
    defs = tel.list_artifact_definitions()
    return [d.to_dict() for d in defs]


@router.get("/artifacts/{artifact_id}")
async def get_artifact_definition(artifact_id: str):
    ad = tel.get_artifact_definition(artifact_id)
    if ad is None:
        raise HTTPException(404, f"Artifact definition not found: {artifact_id}")
    return ad.to_dict()


# -- Cert endpoints --

@router.get("/certs")
async def get_certs():
    return tel.get_telemetry_certs()


@router.post("/certs/generate/{soc_id}")
async def generate_certs(soc_id: str):
    certs = tel.generate_cert_artifacts(soc_id)
    if not certs:
        raise HTTPException(404, f"SoC not supported: {soc_id}")
    return [c.to_dict() for c in certs]
