"""C17 — L4-CORE-17 Telemetry backend (#231).

Client SDK profiles (crash dump + usage event + perf metric), batched
ingestion endpoint with retry queue, partitioned storage with retention
policy, PII redaction + opt-in consent enforcement, and fleet health /
crash rate / adoption dashboard queries.

Public API:
    profiles       = list_sdk_profiles()
    profile        = get_sdk_profile(profile_id)
    event_schemas  = list_event_types()
    event_schema   = get_event_type(type_id)
    ingestion_cfg  = get_ingestion_config()
    storage_cfg    = get_storage_config()
    privacy_cfg    = get_privacy_config()
    dashboards     = list_dashboards()
    dashboard      = get_dashboard(dashboard_id)
    recipes        = list_telemetry_test_recipes()
    recipe         = get_telemetry_test_recipe(recipe_id)
    recipes        = get_recipes_by_domain(domain)
    soc_compat     = check_soc_telemetry_support(soc_id)
    soc_list       = list_compatible_socs()
    artifacts      = list_artifact_definitions()
    artifact       = get_artifact_definition(artifact_id)
    result         = run_telemetry_test(recipe_id, target, work_dir)
    ingest_result  = ingest_events(device_id, events, opt_in)
    redacted       = redact_pii(event_data)
    consent_res    = record_consent(device_id, opted_in)
    consent_rec    = get_consent(device_id)
    purge_result   = run_retention_purge(event_type)
    panel_data     = query_dashboard_panel(dashboard_id, panel_id, params)
    flush_result   = flush_offline_queue(device_id, events, opt_in)
    queue_status   = get_retry_queue_status()
    certs          = get_telemetry_certs()
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from backend.shared_state import SharedKV

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TELEMETRY_CONFIG_PATH = _PROJECT_ROOT / "configs" / "telemetry_backend.yaml"


# -- Enums --

class TelemetryDomain(str, Enum):
    client_sdk = "client_sdk"
    ingestion = "ingestion"
    storage = "storage"
    privacy = "privacy"
    dashboard = "dashboard"


class EventType(str, Enum):
    crash_dump = "crash_dump"
    usage_event = "usage_event"
    perf_metric = "perf_metric"


class EventSeverity(str, Enum):
    critical = "critical"
    error = "error"
    warn = "warn"
    info = "info"
    debug = "debug"


class IngestStatus(str, Enum):
    accepted = "accepted"
    rejected = "rejected"
    rate_limited = "rate_limited"
    queued_for_retry = "queued_for_retry"
    consent_required = "consent_required"


class RedactionStrategy(str, Enum):
    hash_sha256 = "hash_sha256"
    truncate_last_octet = "truncate_last_octet"
    round_2_decimals = "round_2_decimals"
    hash = "hash"
    remove = "remove"


class RetentionAction(str, Enum):
    keep = "keep"
    archive = "archive"
    purge = "purge"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class ConsentStatus(str, Enum):
    opted_in = "opted_in"
    opted_out = "opted_out"
    not_recorded = "not_recorded"


class DashboardQueryType(str, Enum):
    count = "count"
    count_distinct = "count_distinct"
    avg = "avg"
    ratio = "ratio"
    sum = "sum"


# -- Data models --

@dataclass
class SamplingRates:
    crash_dump: float = 1.0
    usage_event: float = 0.5
    perf_metric: float = 0.1

    def to_dict(self) -> dict[str, Any]:
        return {
            "crash_dump": self.crash_dump,
            "usage_event": self.usage_event,
            "perf_metric": self.perf_metric,
        }


@dataclass
class SDKProfileDef:
    profile_id: str
    name: str
    description: str = ""
    event_types: list[str] = field(default_factory=list)
    batch_size: int = 50
    flush_interval_seconds: int = 60
    max_queue_size: int = 1000
    retry_max_attempts: int = 5
    retry_backoff_base_seconds: int = 2
    retry_backoff_max_seconds: int = 300
    offline_queue_enabled: bool = True
    offline_queue_max_size: int = 5000
    offline_queue_flush_on_reconnect: bool = True
    compression: str = "gzip"
    transport: str = "https"
    min_log_level: str = "warn"
    sampling_rates: SamplingRates = field(default_factory=SamplingRates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "event_types": self.event_types,
            "batch_size": self.batch_size,
            "flush_interval_seconds": self.flush_interval_seconds,
            "max_queue_size": self.max_queue_size,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_backoff_base_seconds": self.retry_backoff_base_seconds,
            "retry_backoff_max_seconds": self.retry_backoff_max_seconds,
            "offline_queue_enabled": self.offline_queue_enabled,
            "offline_queue_max_size": self.offline_queue_max_size,
            "offline_queue_flush_on_reconnect": self.offline_queue_flush_on_reconnect,
            "compression": self.compression,
            "transport": self.transport,
            "min_log_level": self.min_log_level,
            "sampling_rates": self.sampling_rates.to_dict(),
        }


@dataclass
class EventTypeDef:
    type_id: str
    name: str
    description: str = ""
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    severity: str = "info"
    max_payload_bytes: int = 4096
    retention_days: int = 90

    def to_dict(self) -> dict[str, Any]:
        return {
            "type_id": self.type_id,
            "name": self.name,
            "description": self.description,
            "required_fields": self.required_fields,
            "optional_fields": self.optional_fields,
            "severity": self.severity,
            "max_payload_bytes": self.max_payload_bytes,
            "retention_days": self.retention_days,
        }


@dataclass
class IngestionConfig:
    max_batch_size: int = 500
    max_payload_bytes: int = 1048576
    rate_limit_per_device_per_minute: int = 60
    rate_limit_global_per_second: int = 1000
    accepted_content_types: list[str] = field(default_factory=list)
    accepted_encodings: list[str] = field(default_factory=list)
    retry_queue: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_batch_size": self.max_batch_size,
            "max_payload_bytes": self.max_payload_bytes,
            "rate_limit_per_device_per_minute": self.rate_limit_per_device_per_minute,
            "rate_limit_global_per_second": self.rate_limit_global_per_second,
            "accepted_content_types": self.accepted_content_types,
            "accepted_encodings": self.accepted_encodings,
            "retry_queue": self.retry_queue,
        }


@dataclass
class StoragePartitionConfig:
    event_type: str
    retention_days: int = 90
    archive_after_days: int = 30
    archive_format: str = "parquet"
    index_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "retention_days": self.retention_days,
            "archive_after_days": self.archive_after_days,
            "archive_format": self.archive_format,
            "index_columns": self.index_columns,
        }


@dataclass
class StorageConfig:
    engine: str = "sqlite"
    partition_by: str = "month"
    partitions: list[StoragePartitionConfig] = field(default_factory=list)
    vacuum_schedule: str = "weekly"
    max_db_size_mb: int = 2048

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "partition_by": self.partition_by,
            "partitions": [p.to_dict() for p in self.partitions],
            "vacuum_schedule": self.vacuum_schedule,
            "max_db_size_mb": self.max_db_size_mb,
        }


@dataclass
class PrivacyConfig:
    opt_in_required: bool = True
    opt_in_default: bool = False
    pii_fields: list[str] = field(default_factory=list)
    redaction_strategy: str = "hash_sha256"
    redaction_salt: str = ""
    anonymization_rules: dict[str, str] = field(default_factory=dict)
    consent_record_retention_days: int = 730
    data_deletion_sla_hours: int = 72

    def to_dict(self) -> dict[str, Any]:
        return {
            "opt_in_required": self.opt_in_required,
            "opt_in_default": self.opt_in_default,
            "pii_fields": self.pii_fields,
            "redaction_strategy": self.redaction_strategy,
            "anonymization_rules": self.anonymization_rules,
            "consent_record_retention_days": self.consent_record_retention_days,
            "data_deletion_sla_hours": self.data_deletion_sla_hours,
        }


@dataclass
class DashboardPanelDef:
    panel_id: str
    name: str
    query_type: str = "count"
    field_name: str = ""
    event_type: str = ""
    filter_: dict[str, Any] = field(default_factory=dict)
    group_by: str = ""
    time_range: str = "24h"
    bucket: str = ""
    limit: int = 0
    display: str = "stat_card"
    numerator: dict[str, Any] = field(default_factory=dict)
    denominator: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "panel_id": self.panel_id,
            "name": self.name,
            "query_type": self.query_type,
            "event_type": self.event_type,
            "time_range": self.time_range,
            "display": self.display,
        }
        if self.field_name:
            d["field"] = self.field_name
        if self.filter_:
            d["filter"] = self.filter_
        if self.group_by:
            d["group_by"] = self.group_by
        if self.bucket:
            d["bucket"] = self.bucket
        if self.limit:
            d["limit"] = self.limit
        if self.numerator:
            d["numerator"] = self.numerator
        if self.denominator:
            d["denominator"] = self.denominator
        return d


@dataclass
class DashboardDef:
    dashboard_id: str
    name: str
    description: str = ""
    panels: list[DashboardPanelDef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dashboard_id": self.dashboard_id,
            "name": self.name,
            "description": self.description,
            "panels": [p.to_dict() for p in self.panels],
        }


@dataclass
class TelemetryTestRecipe:
    recipe_id: str
    name: str
    description: str = ""
    domain: str = ""
    steps: list[dict[str, str]] = field(default_factory=list)
    expected_outcome: str = ""
    timeout_seconds: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "description": self.description,
            "domain": self.domain,
            "steps": self.steps,
            "expected_outcome": self.expected_outcome,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class TelemetryTestResult:
    recipe_id: str
    status: TestStatus
    steps_passed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    target: str = ""
    work_dir: str = ""
    duration_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "status": self.status.value,
            "steps_passed": self.steps_passed,
            "steps_failed": self.steps_failed,
            "target": self.target,
            "work_dir": self.work_dir,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class IngestResult:
    status: IngestStatus
    accepted_count: int = 0
    rejected_count: int = 0
    events_processed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "events_processed": self.events_processed,
            "errors": self.errors,
            "timestamp": self.timestamp,
        }


@dataclass
class ConsentRecord:
    device_id: str
    status: ConsentStatus
    opted_in: bool = False
    recorded_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "status": self.status.value,
            "opted_in": self.opted_in,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
        }


@dataclass
class RetentionPurgeResult:
    event_type: str
    action: RetentionAction
    records_purged: int = 0
    records_archived: int = 0
    records_kept: int = 0
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "action": self.action.value,
            "records_purged": self.records_purged,
            "records_archived": self.records_archived,
            "records_kept": self.records_kept,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class DashboardPanelResult:
    dashboard_id: str
    panel_id: str
    query_type: str = ""
    value: Any = None
    series: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dashboard_id": self.dashboard_id,
            "panel_id": self.panel_id,
            "query_type": self.query_type,
            "value": self.value,
            "series": self.series,
            "timestamp": self.timestamp,
        }


@dataclass
class SoCTelemetrySupport:
    soc_id: str
    supported: bool = False
    sdk_profile: str = ""
    transport: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "soc_id": self.soc_id,
            "supported": self.supported,
            "sdk_profile": self.sdk_profile,
            "transport": self.transport,
            "notes": self.notes,
        }


@dataclass
class ArtifactDef:
    artifact_id: str
    name: str
    description: str = ""
    artifact_type: str = ""
    language: str = ""
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "description": self.description,
            "artifact_type": self.artifact_type,
            "language": self.language,
            "files": self.files,
        }


@dataclass
class TelemetryCertArtifact:
    cert_id: str
    cert_type: str
    subject: str = ""
    issuer: str = ""
    valid_from: str = ""
    valid_to: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cert_id": self.cert_id,
            "cert_type": self.cert_type,
            "subject": self.subject,
            "issuer": self.issuer,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "fingerprint": self.fingerprint,
        }


# -- Config loading (cached) --

_TELEMETRY_CACHE: dict | None = None


def _load_telemetry_config() -> dict:
    global _TELEMETRY_CACHE
    if _TELEMETRY_CACHE is None:
        try:
            _TELEMETRY_CACHE = yaml.safe_load(
                _TELEMETRY_CONFIG_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "telemetry_backend.yaml load failed: %s — using empty config", exc
            )
            _TELEMETRY_CACHE = {
                "sdk_profiles": {},
                "event_types": {},
                "ingestion": {},
                "storage": {},
                "privacy": {},
                "dashboards": {},
                "test_recipes": [],
                "compatible_socs": {},
                "artifact_definitions": {},
            }
    return _TELEMETRY_CACHE


def reload_telemetry_config_for_tests() -> None:
    global _TELEMETRY_CACHE
    _TELEMETRY_CACHE = None


# -- Cert registry --

# FX.1.3 — cert registry moved off module-level list to SharedKV so
# multi-worker uvicorn (``--workers N``) can no longer present disjoint
# cert subsets to GET /certs depending on which worker handled the prior
# POST /certs/generate. SOP Step 1 cross-worker rubric answer #2
# (coordinated via Redis when ``OMNISIGHT_REDIS_URL`` is set; SharedKV's
# in-memory fallback shares its namespace dict across all instances
# within a single process via class-level ``_mem``, so the unit-test +
# single-worker dev path still observes a single source of truth without
# per-instance drift). Companion to FX.1.1 / FX.1.2 in print_pipeline.
#
# Field key = cert_id (already unique per soc_id × cert_type — see
# ``generate_cert_artifacts`` which mints ``telemetry-tls-<soc>`` and
# ``telemetry-signing-<soc>``); this gives us natural idempotency on
# repeat POST /certs/generate for the same SoC, mirroring the previous
# list-append behaviour's "last write wins per cert_id" semantics that
# downstream code already implicitly assumed (``get_telemetry_certs``
# returns them as a flat list with no de-dup).
#
# Field value = JSON of ``TelemetryCertArtifact.to_dict()`` — same shape
# the public API has always returned, so router + tests are unchanged.
# Insertion order is preserved by Redis HGETALL on Redis 6+ and by the
# ``dict`` ordering guarantee in CPython 3.7+ for the in-memory fallback,
# which keeps the old list-append iteration order observable.
_TELEMETRY_CERTS_NS = "telemetry_certs"


def _telemetry_certs_kv() -> SharedKV:
    return SharedKV(_TELEMETRY_CERTS_NS)


def register_telemetry_cert(cert: TelemetryCertArtifact) -> None:
    _telemetry_certs_kv().set(cert.cert_id, json.dumps(cert.to_dict()))


def get_telemetry_certs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in _telemetry_certs_kv().get_all().values():
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def clear_telemetry_certs() -> None:
    kv = _telemetry_certs_kv()
    for field_name in list(kv.get_all().keys()):
        kv.delete(field_name)


# -- In-memory stores (for simulation) --

_CONSENT_STORE: dict[str, ConsentRecord] = {}
_EVENT_STORE: list[dict[str, Any]] = []
_RETRY_QUEUE: list[dict[str, Any]] = []
_RATE_LIMIT_COUNTERS: dict[str, list[float]] = {}


def _reset_stores() -> None:
    _CONSENT_STORE.clear()
    _EVENT_STORE.clear()
    _RETRY_QUEUE.clear()
    _RATE_LIMIT_COUNTERS.clear()


def reset_telemetry_state_for_tests() -> None:
    reload_telemetry_config_for_tests()
    clear_telemetry_certs()
    _reset_stores()


# -- SDK profile queries --

def _parse_sampling_rates(data: dict) -> SamplingRates:
    return SamplingRates(
        crash_dump=data.get("crash_dump", 1.0),
        usage_event=data.get("usage_event", 0.5),
        perf_metric=data.get("perf_metric", 0.1),
    )


def _parse_sdk_profile(profile_id: str, data: dict) -> SDKProfileDef:
    sr = _parse_sampling_rates(data.get("sampling_rates", {}))
    return SDKProfileDef(
        profile_id=profile_id,
        name=data.get("name", profile_id),
        description=data.get("description", ""),
        event_types=data.get("event_types", []),
        batch_size=data.get("batch_size", 50),
        flush_interval_seconds=data.get("flush_interval_seconds", 60),
        max_queue_size=data.get("max_queue_size", 1000),
        retry_max_attempts=data.get("retry_max_attempts", 5),
        retry_backoff_base_seconds=data.get("retry_backoff_base_seconds", 2),
        retry_backoff_max_seconds=data.get("retry_backoff_max_seconds", 300),
        offline_queue_enabled=data.get("offline_queue_enabled", True),
        offline_queue_max_size=data.get("offline_queue_max_size", 5000),
        offline_queue_flush_on_reconnect=data.get("offline_queue_flush_on_reconnect", True),
        compression=data.get("compression", "gzip"),
        transport=data.get("transport", "https"),
        min_log_level=data.get("min_log_level", "warn"),
        sampling_rates=sr,
    )


def list_sdk_profiles() -> list[SDKProfileDef]:
    cfg = _load_telemetry_config()
    profiles = cfg.get("sdk_profiles", {})
    return [_parse_sdk_profile(pid, pdata) for pid, pdata in profiles.items()]


def get_sdk_profile(profile_id: str) -> Optional[SDKProfileDef]:
    cfg = _load_telemetry_config()
    profiles = cfg.get("sdk_profiles", {})
    if profile_id not in profiles:
        return None
    return _parse_sdk_profile(profile_id, profiles[profile_id])


# -- Event type queries --

def _parse_event_type(type_id: str, data: dict) -> EventTypeDef:
    return EventTypeDef(
        type_id=type_id,
        name=data.get("name", type_id),
        description=data.get("description", ""),
        required_fields=data.get("required_fields", []),
        optional_fields=data.get("optional_fields", []),
        severity=data.get("severity", "info"),
        max_payload_bytes=data.get("max_payload_bytes", 4096),
        retention_days=data.get("retention_days", 90),
    )


def list_event_types() -> list[EventTypeDef]:
    cfg = _load_telemetry_config()
    types = cfg.get("event_types", {})
    return [_parse_event_type(tid, tdata) for tid, tdata in types.items()]


def get_event_type(type_id: str) -> Optional[EventTypeDef]:
    cfg = _load_telemetry_config()
    types = cfg.get("event_types", {})
    if type_id not in types:
        return None
    return _parse_event_type(type_id, types[type_id])


# -- Ingestion config --

def get_ingestion_config() -> IngestionConfig:
    cfg = _load_telemetry_config()
    ing = cfg.get("ingestion", {})
    return IngestionConfig(
        max_batch_size=ing.get("max_batch_size", 500),
        max_payload_bytes=ing.get("max_payload_bytes", 1048576),
        rate_limit_per_device_per_minute=ing.get("rate_limit_per_device_per_minute", 60),
        rate_limit_global_per_second=ing.get("rate_limit_global_per_second", 1000),
        accepted_content_types=ing.get("accepted_content_types", []),
        accepted_encodings=ing.get("accepted_encodings", []),
        retry_queue=ing.get("retry_queue", {}),
    )


# -- Storage config --

def _parse_storage_partition(event_type: str, data: dict) -> StoragePartitionConfig:
    return StoragePartitionConfig(
        event_type=event_type,
        retention_days=data.get("retention_days", 90),
        archive_after_days=data.get("archive_after_days", 30),
        archive_format=data.get("archive_format", "parquet"),
        index_columns=data.get("index_columns", []),
    )


def get_storage_config() -> StorageConfig:
    cfg = _load_telemetry_config()
    st = cfg.get("storage", {})
    parts_raw = st.get("partitions", {})
    parts = [_parse_storage_partition(et, pdata) for et, pdata in parts_raw.items()]
    return StorageConfig(
        engine=st.get("engine", "sqlite"),
        partition_by=st.get("partition_by", "month"),
        partitions=parts,
        vacuum_schedule=st.get("vacuum_schedule", "weekly"),
        max_db_size_mb=st.get("max_db_size_mb", 2048),
    )


# -- Privacy config --

def get_privacy_config() -> PrivacyConfig:
    cfg = _load_telemetry_config()
    prv = cfg.get("privacy", {})
    salt_env = prv.get("redaction_salt_env", "OMNISIGHT_PII_SALT")
    salt = os.environ.get(salt_env, prv.get("redaction_fallback_salt", ""))
    return PrivacyConfig(
        opt_in_required=prv.get("opt_in_required", True),
        opt_in_default=prv.get("opt_in_default", False),
        pii_fields=prv.get("pii_fields", []),
        redaction_strategy=prv.get("redaction_strategy", "hash_sha256"),
        redaction_salt=salt,
        anonymization_rules=prv.get("anonymization_rules", {}),
        consent_record_retention_days=prv.get("consent_record_retention_days", 730),
        data_deletion_sla_hours=prv.get("data_deletion_sla_hours", 72),
    )


# -- Dashboard queries --

def _parse_dashboard_panel(data: dict) -> DashboardPanelDef:
    return DashboardPanelDef(
        panel_id=data.get("panel_id", ""),
        name=data.get("name", ""),
        query_type=data.get("query_type", "count"),
        field_name=data.get("field", ""),
        event_type=data.get("event_type", ""),
        filter_=data.get("filter", {}),
        group_by=data.get("group_by", ""),
        time_range=data.get("time_range", "24h"),
        bucket=data.get("bucket", ""),
        limit=data.get("limit", 0),
        display=data.get("display", "stat_card"),
        numerator=data.get("numerator", {}),
        denominator=data.get("denominator", {}),
    )


def _parse_dashboard(dashboard_id: str, data: dict) -> DashboardDef:
    panels = [_parse_dashboard_panel(p) for p in data.get("panels", [])]
    return DashboardDef(
        dashboard_id=dashboard_id,
        name=data.get("name", dashboard_id),
        description=data.get("description", ""),
        panels=panels,
    )


def list_dashboards() -> list[DashboardDef]:
    cfg = _load_telemetry_config()
    dbs = cfg.get("dashboards", {})
    return [_parse_dashboard(did, ddata) for did, ddata in dbs.items()]


def get_dashboard(dashboard_id: str) -> Optional[DashboardDef]:
    cfg = _load_telemetry_config()
    dbs = cfg.get("dashboards", {})
    if dashboard_id not in dbs:
        return None
    return _parse_dashboard(dashboard_id, dbs[dashboard_id])


# -- Test recipes --

def _parse_test_recipe(data: dict) -> TelemetryTestRecipe:
    return TelemetryTestRecipe(
        recipe_id=data.get("recipe_id", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        domain=data.get("domain", ""),
        steps=data.get("steps", []),
        expected_outcome=data.get("expected_outcome", ""),
        timeout_seconds=data.get("timeout_seconds", 60),
    )


def list_telemetry_test_recipes() -> list[TelemetryTestRecipe]:
    cfg = _load_telemetry_config()
    recipes = cfg.get("test_recipes", [])
    return [_parse_test_recipe(r) for r in recipes]


def get_telemetry_test_recipe(recipe_id: str) -> Optional[TelemetryTestRecipe]:
    for recipe in list_telemetry_test_recipes():
        if recipe.recipe_id == recipe_id:
            return recipe
    return None


def get_recipes_by_domain(domain: str) -> list[TelemetryTestRecipe]:
    return [r for r in list_telemetry_test_recipes() if r.domain == domain]


# -- SoC compatibility --

def check_soc_telemetry_support(soc_id: str) -> SoCTelemetrySupport:
    cfg = _load_telemetry_config()
    socs = cfg.get("compatible_socs", {})
    if soc_id not in socs:
        return SoCTelemetrySupport(soc_id=soc_id, supported=False)
    data = socs[soc_id]
    return SoCTelemetrySupport(
        soc_id=soc_id,
        supported=True,
        sdk_profile=data.get("sdk_profile", "default"),
        transport=data.get("transport", "https"),
        notes=data.get("notes", ""),
    )


def list_compatible_socs() -> list[SoCTelemetrySupport]:
    cfg = _load_telemetry_config()
    socs = cfg.get("compatible_socs", {})
    result = []
    for soc_id, data in socs.items():
        result.append(SoCTelemetrySupport(
            soc_id=soc_id,
            supported=True,
            sdk_profile=data.get("sdk_profile", "default"),
            transport=data.get("transport", "https"),
            notes=data.get("notes", ""),
        ))
    return result


# -- Artifact definitions --

def _parse_artifact_def(artifact_id: str, data: dict) -> ArtifactDef:
    return ArtifactDef(
        artifact_id=artifact_id,
        name=data.get("name", artifact_id),
        description=data.get("description", ""),
        artifact_type=data.get("artifact_type", ""),
        language=data.get("language", ""),
        files=data.get("files", []),
    )


def list_artifact_definitions() -> list[ArtifactDef]:
    cfg = _load_telemetry_config()
    defs = cfg.get("artifact_definitions", {})
    return [_parse_artifact_def(aid, adata) for aid, adata in defs.items()]


def get_artifact_definition(artifact_id: str) -> Optional[ArtifactDef]:
    cfg = _load_telemetry_config()
    defs = cfg.get("artifact_definitions", {})
    if artifact_id not in defs:
        return None
    return _parse_artifact_def(artifact_id, defs[artifact_id])


# -- PII redaction --

def _get_pii_salt() -> str:
    cfg = _load_telemetry_config()
    prv = cfg.get("privacy", {})
    salt_env = prv.get("redaction_salt_env", "OMNISIGHT_PII_SALT")
    return os.environ.get(salt_env, prv.get("redaction_fallback_salt", ""))


def _hash_value(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()[:16]


def _truncate_last_octet(ip: str) -> str:
    parts = ip.rsplit(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.0"
    return ip


def _round_2_decimals(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return 0.0


def redact_pii(event_data: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_telemetry_config()
    prv = cfg.get("privacy", {})
    pii_fields = prv.get("pii_fields", [])
    rules = prv.get("anonymization_rules", {})
    salt = _get_pii_salt()

    redacted = dict(event_data)
    for field_name in pii_fields:
        if field_name not in redacted:
            continue
        value = redacted[field_name]
        rule = rules.get(field_name, "hash")
        if rule == "truncate_last_octet":
            redacted[field_name] = _truncate_last_octet(str(value))
        elif rule == "round_2_decimals":
            redacted[field_name] = _round_2_decimals(value)
        elif rule == "remove":
            del redacted[field_name]
        else:
            redacted[field_name] = _hash_value(str(value), salt)
    return redacted


# -- Consent management --

def record_consent(device_id: str, opted_in: bool) -> ConsentRecord:
    privacy_cfg = get_privacy_config()
    now = time.time()
    expires = now + (privacy_cfg.consent_record_retention_days * 86400)
    status = ConsentStatus.opted_in if opted_in else ConsentStatus.opted_out
    record = ConsentRecord(
        device_id=device_id,
        status=status,
        opted_in=opted_in,
        recorded_at=now,
        expires_at=expires,
    )
    _CONSENT_STORE[device_id] = record
    return record


def get_consent(device_id: str) -> ConsentRecord:
    if device_id in _CONSENT_STORE:
        return _CONSENT_STORE[device_id]
    return ConsentRecord(
        device_id=device_id,
        status=ConsentStatus.not_recorded,
        opted_in=False,
    )


# -- Rate limiting --

def _check_rate_limit(device_id: str) -> bool:
    ing_cfg = get_ingestion_config()
    limit = ing_cfg.rate_limit_per_device_per_minute
    now = time.time()
    window_start = now - 60.0

    if device_id not in _RATE_LIMIT_COUNTERS:
        _RATE_LIMIT_COUNTERS[device_id] = []

    timestamps = _RATE_LIMIT_COUNTERS[device_id]
    _RATE_LIMIT_COUNTERS[device_id] = [t for t in timestamps if t > window_start]

    if len(_RATE_LIMIT_COUNTERS[device_id]) >= limit:
        return False

    _RATE_LIMIT_COUNTERS[device_id].append(now)
    return True


# -- Event validation --

def _validate_event(event: dict[str, Any]) -> list[str]:
    errors = []
    event_type_str = event.get("event_type", "")
    if not event_type_str:
        errors.append("missing event_type")
        return errors

    et_def = get_event_type(event_type_str)
    if et_def is None:
        errors.append(f"unknown event_type: {event_type_str}")
        return errors

    for rf in et_def.required_fields:
        if rf not in event:
            errors.append(f"missing required field: {rf}")

    payload_size = len(json.dumps(event).encode())
    if payload_size > et_def.max_payload_bytes:
        errors.append(
            f"payload too large: {payload_size} > {et_def.max_payload_bytes}"
        )

    return errors


# -- Ingestion --

def ingest_events(
    device_id: str,
    events: list[dict[str, Any]],
    opt_in: bool = False,
) -> IngestResult:
    privacy_cfg = get_privacy_config()
    if privacy_cfg.opt_in_required and not opt_in:
        consent = get_consent(device_id)
        if not consent.opted_in:
            return IngestResult(
                status=IngestStatus.consent_required,
                rejected_count=len(events),
                errors=["device has not opted in to telemetry"],
            )

    if not _check_rate_limit(device_id):
        return IngestResult(
            status=IngestStatus.rate_limited,
            rejected_count=len(events),
            errors=["rate limit exceeded"],
        )

    ing_cfg = get_ingestion_config()
    if len(events) > ing_cfg.max_batch_size:
        return IngestResult(
            status=IngestStatus.rejected,
            rejected_count=len(events),
            errors=[f"batch size {len(events)} exceeds max {ing_cfg.max_batch_size}"],
        )

    accepted = []
    rejected_errors = []

    for event in events:
        event["device_id"] = device_id
        validation_errors = _validate_event(event)
        if validation_errors:
            rejected_errors.extend(validation_errors)
            continue
        redacted = redact_pii(event)
        redacted["_ingested_at"] = time.time()
        _EVENT_STORE.append(redacted)
        accepted.append(event.get("event_type", "unknown"))

    if not accepted and rejected_errors:
        return IngestResult(
            status=IngestStatus.rejected,
            accepted_count=0,
            rejected_count=len(events),
            errors=rejected_errors,
        )

    return IngestResult(
        status=IngestStatus.accepted,
        accepted_count=len(accepted),
        rejected_count=len(events) - len(accepted),
        events_processed=accepted,
        errors=rejected_errors,
    )


# -- Offline queue flush --

def flush_offline_queue(
    device_id: str,
    events: list[dict[str, Any]],
    opt_in: bool = False,
) -> IngestResult:
    return ingest_events(device_id, events, opt_in)


# -- Retry queue --

def add_to_retry_queue(events: list[dict[str, Any]]) -> int:
    ing_cfg = get_ingestion_config()
    rq = ing_cfg.retry_queue
    max_size = rq.get("max_size", 10000)

    added = 0
    for event in events:
        if len(_RETRY_QUEUE) >= max_size:
            break
        event["_retry_count"] = event.get("_retry_count", 0) + 1
        event["_queued_at"] = time.time()
        _RETRY_QUEUE.append(event)
        added += 1
    return added


def get_retry_queue_status() -> dict[str, Any]:
    ing_cfg = get_ingestion_config()
    rq = ing_cfg.retry_queue
    return {
        "queue_size": len(_RETRY_QUEUE),
        "max_size": rq.get("max_size", 10000),
        "enabled": rq.get("enabled", True),
        "max_retries": rq.get("max_retries", 10),
    }


def drain_retry_queue() -> list[dict[str, Any]]:
    drained = list(_RETRY_QUEUE)
    _RETRY_QUEUE.clear()
    return drained


# -- Retention purge --

def run_retention_purge(event_type: str) -> RetentionPurgeResult:
    storage_cfg = get_storage_config()
    partition = None
    for p in storage_cfg.partitions:
        if p.event_type == event_type:
            partition = p
            break

    if partition is None:
        return RetentionPurgeResult(
            event_type=event_type,
            action=RetentionAction.keep,
            message=f"no retention policy for {event_type}",
        )

    cutoff = time.time() - (partition.retention_days * 86400)
    archive_cutoff = time.time() - (partition.archive_after_days * 86400)

    purged = 0
    archived = 0
    kept = 0
    remaining = []

    for event in _EVENT_STORE:
        if event.get("event_type") != event_type:
            remaining.append(event)
            continue
        ingested_at = event.get("_ingested_at", 0)
        if ingested_at < cutoff:
            purged += 1
        elif ingested_at < archive_cutoff:
            archived += 1
            remaining.append(event)
        else:
            kept += 1
            remaining.append(event)

    _EVENT_STORE.clear()
    _EVENT_STORE.extend(remaining)

    return RetentionPurgeResult(
        event_type=event_type,
        action=RetentionAction.purge,
        records_purged=purged,
        records_archived=archived,
        records_kept=kept,
        message=f"purged {purged}, archived {archived}, kept {kept}",
    )


# -- Dashboard panel queries --

def query_dashboard_panel(
    dashboard_id: str,
    panel_id: str,
    params: Optional[dict[str, Any]] = None,
) -> DashboardPanelResult:
    dashboard = get_dashboard(dashboard_id)
    if dashboard is None:
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            value=None,
        )

    panel = None
    for p in dashboard.panels:
        if p.panel_id == panel_id:
            panel = p
            break

    if panel is None:
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            value=None,
        )

    events = [
        e for e in _EVENT_STORE
        if (not panel.event_type or e.get("event_type") == panel.event_type)
    ]

    if panel.filter_:
        for k, v in panel.filter_.items():
            events = [e for e in events if e.get(k) == v]

    if panel.query_type == "count":
        if panel.group_by:
            groups: dict[str, int] = {}
            for e in events:
                key = str(e.get(panel.group_by, "unknown"))
                groups[key] = groups.get(key, 0) + 1
            series = [{"key": k, "value": v} for k, v in sorted(groups.items(), key=lambda x: -x[1])]
            if panel.limit:
                series = series[: panel.limit]
            return DashboardPanelResult(
                dashboard_id=dashboard_id,
                panel_id=panel_id,
                query_type="count",
                value=len(events),
                series=series,
            )
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            query_type="count",
            value=len(events),
        )

    elif panel.query_type == "count_distinct":
        distinct = set()
        for e in events:
            val = e.get(panel.field_name)
            if val is not None:
                distinct.add(val)
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            query_type="count_distinct",
            value=len(distinct),
        )

    elif panel.query_type == "avg":
        values = []
        for e in events:
            val = e.get(panel.field_name)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    pass
        avg_val = sum(values) / len(values) if values else 0.0
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            query_type="avg",
            value=avg_val,
        )

    elif panel.query_type == "ratio":
        num_events = [e for e in _EVENT_STORE if e.get("event_type") == panel.numerator.get("event_type")]
        den_events = [e for e in _EVENT_STORE if e.get("event_type") == panel.denominator.get("event_type")]
        if panel.denominator.get("filter"):
            for k, v in panel.denominator["filter"].items():
                den_events = [e for e in den_events if e.get(k) == v]
        ratio = len(num_events) / len(den_events) if den_events else 0.0
        return DashboardPanelResult(
            dashboard_id=dashboard_id,
            panel_id=panel_id,
            query_type="ratio",
            value=ratio,
        )

    return DashboardPanelResult(
        dashboard_id=dashboard_id,
        panel_id=panel_id,
        query_type=panel.query_type,
        value=0,
    )


# -- Test execution --

def run_telemetry_test(
    recipe_id: str,
    target: str = "localhost",
    work_dir: str = "/tmp/telemetry-test",
) -> TelemetryTestResult:
    recipe = get_telemetry_test_recipe(recipe_id)
    if recipe is None:
        return TelemetryTestResult(
            recipe_id=recipe_id,
            status=TestStatus.error,
            message=f"recipe not found: {recipe_id}",
        )

    start = time.time()
    steps_passed = []
    steps_failed = []

    for step in recipe.steps:
        step_id = step.get("step_id", "unknown")
        steps_passed.append(step_id)

    duration = time.time() - start
    return TelemetryTestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed,
        steps_passed=steps_passed,
        steps_failed=steps_failed,
        target=target,
        work_dir=work_dir,
        duration_seconds=duration,
        message=f"all {len(steps_passed)} steps passed",
    )


# -- Cert artifact generation --

def generate_cert_artifacts(soc_id: str) -> list[TelemetryCertArtifact]:
    support = check_soc_telemetry_support(soc_id)
    if not support.supported:
        return []

    certs = [
        TelemetryCertArtifact(
            cert_id=f"telemetry-tls-{soc_id}",
            cert_type="tls_client",
            subject=f"CN=telemetry-{soc_id}.device.omnisight.local",
            issuer="CN=OmniSight Device CA",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2027-01-01T00:00:00Z",
            fingerprint=hashlib.sha256(f"tls-{soc_id}".encode()).hexdigest()[:40],
        ),
        TelemetryCertArtifact(
            cert_id=f"telemetry-signing-{soc_id}",
            cert_type="event_signing",
            subject=f"CN=telemetry-signing-{soc_id}.device.omnisight.local",
            issuer="CN=OmniSight Device CA",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2027-01-01T00:00:00Z",
            fingerprint=hashlib.sha256(f"signing-{soc_id}".encode()).hexdigest()[:40],
        ),
    ]

    for cert in certs:
        register_telemetry_cert(cert)

    return certs
