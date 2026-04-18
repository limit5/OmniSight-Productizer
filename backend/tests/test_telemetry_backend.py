"""C17 — Telemetry backend tests.

Covers: config loading, SDK profile queries, event type schemas, ingestion
(batched POST + rate limiting + consent enforcement), PII redaction, consent
management, storage retention purge, dashboard panel queries, offline queue
flush on reconnect, retry queue, test recipe execution, SoC compatibility,
cert artifacts, and REST endpoint smoke tests.
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend.telemetry_backend import (
    ConsentStatus,
    DashboardQueryType,
    EventSeverity,
    EventType,
    IngestStatus,
    RedactionStrategy,
    RetentionAction,
    TelemetryDomain,
    TestStatus,
    add_to_retry_queue,
    check_soc_telemetry_support,
    clear_telemetry_certs,
    drain_retry_queue,
    flush_offline_queue,
    generate_cert_artifacts,
    get_artifact_definition,
    get_consent,
    get_dashboard,
    get_event_type,
    get_ingestion_config,
    get_privacy_config,
    get_recipes_by_domain,
    get_retry_queue_status,
    get_sdk_profile,
    get_storage_config,
    get_telemetry_certs,
    get_telemetry_test_recipe,
    ingest_events,
    list_artifact_definitions,
    list_compatible_socs,
    list_dashboards,
    list_event_types,
    list_sdk_profiles,
    list_telemetry_test_recipes,
    query_dashboard_panel,
    record_consent,
    redact_pii,
    register_telemetry_cert,
    reload_telemetry_config_for_tests,
    reset_telemetry_state_for_tests,
    run_retention_purge,
    run_telemetry_test,
)


@pytest.fixture(autouse=True)
def _reload_config():
    reset_telemetry_state_for_tests()
    yield
    reset_telemetry_state_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigLoading:
    def test_sdk_profiles_loaded(self):
        profiles = list_sdk_profiles()
        assert len(profiles) == 3

    def test_event_types_loaded(self):
        types = list_event_types()
        assert len(types) == 3

    def test_dashboards_loaded(self):
        dashboards = list_dashboards()
        assert len(dashboards) == 3

    def test_test_recipes_loaded(self):
        recipes = list_telemetry_test_recipes()
        assert len(recipes) == 10

    def test_artifact_definitions_loaded(self):
        defs = list_artifact_definitions()
        assert len(defs) == 6

    def test_compatible_socs_loaded(self):
        socs = list_compatible_socs()
        assert len(socs) == 11


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SDK profile queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSDKProfiles:
    def test_get_default_profile(self):
        p = get_sdk_profile("default")
        assert p is not None
        assert p.name == "Default Telemetry SDK"
        assert p.batch_size == 50
        assert p.offline_queue_enabled is True
        assert p.offline_queue_flush_on_reconnect is True

    def test_get_low_bandwidth_profile(self):
        p = get_sdk_profile("low_bandwidth")
        assert p is not None
        assert p.batch_size == 10
        assert p.flush_interval_seconds == 300

    def test_get_high_fidelity_profile(self):
        p = get_sdk_profile("high_fidelity")
        assert p is not None
        assert p.batch_size == 100
        assert p.flush_interval_seconds == 10
        assert p.sampling_rates.crash_dump == 1.0
        assert p.sampling_rates.usage_event == 1.0
        assert p.sampling_rates.perf_metric == 1.0

    def test_get_nonexistent_profile(self):
        p = get_sdk_profile("nonexistent")
        assert p is None

    def test_profile_to_dict(self):
        p = get_sdk_profile("default")
        d = p.to_dict()
        assert d["profile_id"] == "default"
        assert "sampling_rates" in d
        assert d["sampling_rates"]["crash_dump"] == 1.0

    def test_sdk_profile_compression(self):
        default = get_sdk_profile("default")
        low = get_sdk_profile("low_bandwidth")
        assert default.compression == "gzip"
        assert low.compression == "lz4"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event type schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEventTypes:
    def test_crash_dump_schema(self):
        et = get_event_type("crash_dump")
        assert et is not None
        assert "stack_trace" in et.required_fields
        assert et.severity == "critical"
        assert et.retention_days == 365

    def test_usage_event_schema(self):
        et = get_event_type("usage_event")
        assert et is not None
        assert "event_name" in et.required_fields
        assert et.retention_days == 90

    def test_perf_metric_schema(self):
        et = get_event_type("perf_metric")
        assert et is not None
        assert "metric_name" in et.required_fields
        assert "metric_value" in et.required_fields
        assert et.retention_days == 30

    def test_nonexistent_event_type(self):
        et = get_event_type("nonexistent")
        assert et is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ingestion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIngestion:
    def test_ingestion_config(self):
        cfg = get_ingestion_config()
        assert cfg.max_batch_size == 500
        assert cfg.rate_limit_per_device_per_minute == 60

    def test_ingest_with_consent(self):
        record_consent("dev-001", True)
        events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-001",
                "timestamp": time.time(),
                "metric_name": "cpu_percent",
                "metric_value": 42.0,
            }
        ]
        result = ingest_events("dev-001", events)
        assert result.status == IngestStatus.accepted
        assert result.accepted_count == 1

    def test_ingest_without_consent_rejected(self):
        events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-002",
                "timestamp": time.time(),
                "metric_name": "cpu_percent",
                "metric_value": 42.0,
            }
        ]
        result = ingest_events("dev-002", events)
        assert result.status == IngestStatus.consent_required

    def test_ingest_with_opt_in_flag(self):
        events = [
            {
                "event_type": "usage_event",
                "device_id": "dev-003",
                "timestamp": time.time(),
                "event_name": "boot",
            }
        ]
        result = ingest_events("dev-003", events, opt_in=True)
        assert result.status == IngestStatus.accepted

    def test_ingest_invalid_event_type(self):
        record_consent("dev-004", True)
        events = [
            {
                "event_type": "invalid_type",
                "device_id": "dev-004",
                "timestamp": time.time(),
            }
        ]
        result = ingest_events("dev-004", events)
        assert result.status == IngestStatus.rejected
        assert result.rejected_count == 1

    def test_ingest_missing_required_field(self):
        record_consent("dev-005", True)
        events = [
            {
                "event_type": "crash_dump",
                "device_id": "dev-005",
                "timestamp": time.time(),
            }
        ]
        result = ingest_events("dev-005", events)
        assert result.rejected_count >= 1

    def test_ingest_batch_too_large(self):
        record_consent("dev-006", True)
        events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-006",
                "timestamp": time.time(),
                "metric_name": f"m_{i}",
                "metric_value": float(i),
            }
            for i in range(501)
        ]
        result = ingest_events("dev-006", events)
        assert result.status == IngestStatus.rejected

    def test_ingest_rate_limiting(self):
        record_consent("dev-rate", True)
        for i in range(61):
            events = [
                {
                    "event_type": "perf_metric",
                    "device_id": "dev-rate",
                    "timestamp": time.time(),
                    "metric_name": "cpu",
                    "metric_value": float(i),
                }
            ]
            result = ingest_events("dev-rate", events)
            if i >= 60:
                assert result.status == IngestStatus.rate_limited

    def test_ingest_crash_dump(self):
        record_consent("dev-crash", True)
        events = [
            {
                "event_type": "crash_dump",
                "device_id": "dev-crash",
                "timestamp": time.time(),
                "crash_signal": "SIGSEGV",
                "stack_trace": "main() -> foo() -> bar()",
                "firmware_version": "1.2.3",
            }
        ]
        result = ingest_events("dev-crash", events)
        assert result.status == IngestStatus.accepted
        assert "crash_dump" in result.events_processed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PII redaction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPIIRedaction:
    def test_redact_ip_address(self):
        event = {"ip_address": "192.168.1.100", "device_id": "dev-001"}
        redacted = redact_pii(event)
        assert redacted["ip_address"] == "192.168.1.0"

    def test_redact_mac_address(self):
        event = {"mac_address": "AA:BB:CC:DD:EE:FF", "device_id": "dev-001"}
        redacted = redact_pii(event)
        assert redacted["mac_address"] != "AA:BB:CC:DD:EE:FF"
        assert len(redacted["mac_address"]) == 16

    def test_redact_email(self):
        event = {"email": "user@example.com", "device_id": "dev-001"}
        redacted = redact_pii(event)
        assert redacted["email"] != "user@example.com"

    def test_redact_gps_coordinates(self):
        event = {
            "gps_latitude": 25.033964,
            "gps_longitude": 121.564468,
            "device_id": "dev-001",
        }
        redacted = redact_pii(event)
        assert redacted["gps_latitude"] == 25.03
        assert redacted["gps_longitude"] == 121.56

    def test_no_pii_fields_unchanged(self):
        event = {"device_id": "dev-001", "metric_name": "cpu", "metric_value": 42.0}
        redacted = redact_pii(event)
        assert redacted == event

    def test_redact_multiple_pii_fields(self):
        event = {
            "ip_address": "10.0.0.1",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "email": "test@test.com",
            "serial_number": "SN12345",
            "device_id": "dev-001",
        }
        redacted = redact_pii(event)
        assert redacted["ip_address"] == "10.0.0.0"
        assert redacted["mac_address"] != "AA:BB:CC:DD:EE:FF"
        assert redacted["email"] != "test@test.com"
        assert redacted["serial_number"] != "SN12345"

    def test_privacy_config(self):
        cfg = get_privacy_config()
        assert cfg.opt_in_required is True
        assert len(cfg.pii_fields) >= 10
        assert cfg.data_deletion_sla_hours == 72


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Consent management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConsent:
    def test_record_opt_in(self):
        record = record_consent("dev-c1", True)
        assert record.status == ConsentStatus.opted_in
        assert record.opted_in is True

    def test_record_opt_out(self):
        record = record_consent("dev-c2", False)
        assert record.status == ConsentStatus.opted_out
        assert record.opted_in is False

    def test_get_unrecorded_consent(self):
        record = get_consent("dev-new")
        assert record.status == ConsentStatus.not_recorded

    def test_consent_overwrite(self):
        record_consent("dev-c3", True)
        record = record_consent("dev-c3", False)
        assert record.opted_in is False
        stored = get_consent("dev-c3")
        assert stored.opted_in is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Storage & retention
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStorageRetention:
    def test_storage_config(self):
        cfg = get_storage_config()
        assert cfg.engine == "sqlite"
        assert cfg.partition_by == "month"
        assert len(cfg.partitions) == 3

    def test_retention_purge_no_policy(self):
        result = run_retention_purge("nonexistent_type")
        assert result.action == RetentionAction.keep

    def test_retention_purge_with_events(self):
        record_consent("dev-ret", True)
        events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-ret",
                "timestamp": time.time(),
                "metric_name": "cpu",
                "metric_value": 50.0,
            }
        ]
        ingest_events("dev-ret", events)
        result = run_retention_purge("perf_metric")
        assert result.action == RetentionAction.purge
        assert result.records_kept >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dashboard queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDashboards:
    def test_list_dashboards(self):
        dbs = list_dashboards()
        ids = [d.dashboard_id for d in dbs]
        assert "fleet_health" in ids
        assert "crash_rate" in ids
        assert "adoption" in ids

    def test_get_fleet_health_dashboard(self):
        d = get_dashboard("fleet_health")
        assert d is not None
        panel_ids = [p.panel_id for p in d.panels]
        assert "active_devices" in panel_ids
        assert "heartbeat_rate" in panel_ids
        assert "error_rate" in panel_ids

    def test_get_nonexistent_dashboard(self):
        d = get_dashboard("nonexistent")
        assert d is None

    def test_query_count_distinct_panel(self):
        record_consent("dev-d1", True)
        record_consent("dev-d2", True)
        for dev_id in ["dev-d1", "dev-d2"]:
            ingest_events(dev_id, [
                {
                    "event_type": "perf_metric",
                    "device_id": dev_id,
                    "timestamp": time.time(),
                    "metric_name": "heartbeat",
                    "metric_value": 1.0,
                }
            ])
        result = query_dashboard_panel("fleet_health", "active_devices")
        assert result.value == 2

    def test_query_count_panel(self):
        record_consent("dev-d3", True)
        ingest_events("dev-d3", [
            {
                "event_type": "perf_metric",
                "device_id": "dev-d3",
                "timestamp": time.time(),
                "metric_name": "heartbeat",
                "metric_value": 1.0,
            }
        ])
        result = query_dashboard_panel("fleet_health", "heartbeat_rate")
        assert result.value >= 1

    def test_query_ratio_panel(self):
        record_consent("dev-d4", True)
        ingest_events("dev-d4", [
            {
                "event_type": "crash_dump",
                "device_id": "dev-d4",
                "timestamp": time.time(),
                "crash_signal": "SIGSEGV",
                "stack_trace": "main()",
            },
            {
                "event_type": "perf_metric",
                "device_id": "dev-d4",
                "timestamp": time.time(),
                "metric_name": "heartbeat",
                "metric_value": 1.0,
            },
        ])
        result = query_dashboard_panel("fleet_health", "error_rate")
        assert result.value > 0

    def test_query_nonexistent_dashboard(self):
        result = query_dashboard_panel("nonexistent", "panel")
        assert result.value is None

    def test_query_nonexistent_panel(self):
        result = query_dashboard_panel("fleet_health", "nonexistent")
        assert result.value is None

    def test_query_avg_panel(self):
        record_consent("dev-d5", True)
        ingest_events("dev-d5", [
            {
                "event_type": "usage_event",
                "device_id": "dev-d5",
                "timestamp": time.time(),
                "event_name": "session",
                "duration_ms": 5000,
            },
            {
                "event_type": "usage_event",
                "device_id": "dev-d5",
                "timestamp": time.time(),
                "event_name": "session",
                "duration_ms": 3000,
            },
        ])
        result = query_dashboard_panel("adoption", "avg_session_duration")
        assert result.value == 4000.0

    def test_query_group_by_panel(self):
        record_consent("dev-d6", True)
        ingest_events("dev-d6", [
            {
                "event_type": "crash_dump",
                "device_id": "dev-d6",
                "timestamp": time.time(),
                "crash_signal": "SIGSEGV",
                "stack_trace": "main()",
            },
            {
                "event_type": "crash_dump",
                "device_id": "dev-d6",
                "timestamp": time.time(),
                "crash_signal": "SIGABRT",
                "stack_trace": "abort()",
            },
            {
                "event_type": "crash_dump",
                "device_id": "dev-d6",
                "timestamp": time.time(),
                "crash_signal": "SIGSEGV",
                "stack_trace": "foo()",
            },
        ])
        result = query_dashboard_panel("crash_rate", "top_crash_signals")
        assert len(result.series) >= 2
        assert result.series[0]["key"] == "SIGSEGV"
        assert result.series[0]["value"] == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SDK offline queue flush on reconnect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOfflineQueueFlush:
    def test_flush_queued_events_on_reconnect(self):
        record_consent("dev-offline", True)
        offline_events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-offline",
                "timestamp": time.time() - 300,
                "metric_name": f"cpu_{i}",
                "metric_value": float(i),
            }
            for i in range(10)
        ]
        result = flush_offline_queue("dev-offline", offline_events)
        assert result.status == IngestStatus.accepted
        assert result.accepted_count == 10

    def test_flush_large_offline_queue(self):
        record_consent("dev-offline2", True)
        offline_events = [
            {
                "event_type": "usage_event",
                "device_id": "dev-offline2",
                "timestamp": time.time() - i,
                "event_name": f"action_{i}",
            }
            for i in range(100)
        ]
        result = flush_offline_queue("dev-offline2", offline_events)
        assert result.status == IngestStatus.accepted
        assert result.accepted_count == 100

    def test_flush_without_consent_rejected(self):
        offline_events = [
            {
                "event_type": "perf_metric",
                "device_id": "dev-no-consent",
                "timestamp": time.time(),
                "metric_name": "cpu",
                "metric_value": 42.0,
            }
        ]
        result = flush_offline_queue("dev-no-consent", offline_events)
        assert result.status == IngestStatus.consent_required

    def test_sdk_profile_offline_queue_config(self):
        p = get_sdk_profile("default")
        assert p.offline_queue_enabled is True
        assert p.offline_queue_flush_on_reconnect is True
        assert p.offline_queue_max_size == 5000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Retry queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRetryQueue:
    def test_add_to_retry_queue(self):
        events = [{"event_type": "perf_metric", "device_id": "dev-r1"}]
        added = add_to_retry_queue(events)
        assert added == 1
        status = get_retry_queue_status()
        assert status["queue_size"] == 1

    def test_drain_retry_queue(self):
        events = [
            {"event_type": "perf_metric", "device_id": "dev-r2"},
            {"event_type": "crash_dump", "device_id": "dev-r2"},
        ]
        add_to_retry_queue(events)
        drained = drain_retry_queue()
        assert len(drained) == 2
        status = get_retry_queue_status()
        assert status["queue_size"] == 0

    def test_retry_queue_status(self):
        status = get_retry_queue_status()
        assert "queue_size" in status
        assert "max_size" in status
        assert "enabled" in status

    def test_retry_count_incremented(self):
        events = [{"event_type": "perf_metric", "device_id": "dev-r3"}]
        add_to_retry_queue(events)
        drained = drain_retry_queue()
        assert drained[0]["_retry_count"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecipes:
    def test_list_recipes(self):
        recipes = list_telemetry_test_recipes()
        assert len(recipes) == 10

    def test_get_offline_queue_recipe(self):
        recipe = get_telemetry_test_recipe("sdk_offline_queue")
        assert recipe is not None
        assert recipe.domain == "client_sdk"
        assert len(recipe.steps) == 3

    def test_get_recipes_by_domain(self):
        sdk_recipes = get_recipes_by_domain("client_sdk")
        assert len(sdk_recipes) >= 3
        privacy_recipes = get_recipes_by_domain("privacy")
        assert len(privacy_recipes) >= 2

    def test_run_test_recipe(self):
        result = run_telemetry_test("sdk_offline_queue")
        assert result.status == TestStatus.passed
        assert len(result.steps_passed) == 3

    def test_run_nonexistent_recipe(self):
        result = run_telemetry_test("nonexistent")
        assert result.status == TestStatus.error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SoC compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSoCCompatibility:
    def test_supported_soc(self):
        support = check_soc_telemetry_support("hi3516")
        assert support.supported is True
        assert support.sdk_profile == "default"

    def test_low_bandwidth_soc(self):
        support = check_soc_telemetry_support("stm32h7")
        assert support.supported is True
        assert support.sdk_profile == "low_bandwidth"

    def test_unsupported_soc(self):
        support = check_soc_telemetry_support("unknown_soc")
        assert support.supported is False

    def test_list_all_socs(self):
        socs = list_compatible_socs()
        ids = [s.soc_id for s in socs]
        assert "hi3516" in ids
        assert "esp32s3" in ids
        assert "x86_64" in ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cert artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCerts:
    def test_generate_certs_for_soc(self):
        certs = generate_cert_artifacts("hi3516")
        assert len(certs) == 2
        types = [c.cert_type for c in certs]
        assert "tls_client" in types
        assert "event_signing" in types

    def test_certs_registered(self):
        generate_cert_artifacts("rk3566")
        all_certs = get_telemetry_certs()
        assert len(all_certs) == 2

    def test_no_certs_for_unsupported_soc(self):
        certs = generate_cert_artifacts("unknown_soc")
        assert len(certs) == 0

    def test_clear_certs(self):
        generate_cert_artifacts("esp32s3")
        clear_telemetry_certs()
        assert len(get_telemetry_certs()) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifact definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestArtifacts:
    def test_get_sdk_c_artifact(self):
        a = get_artifact_definition("telemetry_sdk_c")
        assert a is not None
        assert a.language == "c"
        assert len(a.files) >= 4

    def test_get_sdk_python_artifact(self):
        a = get_artifact_definition("telemetry_sdk_python")
        assert a is not None
        assert a.language == "python"

    def test_nonexistent_artifact(self):
        a = get_artifact_definition("nonexistent")
        assert a is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum values
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnums:
    def test_telemetry_domain_values(self):
        assert TelemetryDomain.client_sdk == "client_sdk"
        assert TelemetryDomain.ingestion == "ingestion"
        assert TelemetryDomain.storage == "storage"
        assert TelemetryDomain.privacy == "privacy"
        assert TelemetryDomain.dashboard == "dashboard"

    def test_event_type_values(self):
        assert EventType.crash_dump == "crash_dump"
        assert EventType.usage_event == "usage_event"
        assert EventType.perf_metric == "perf_metric"

    def test_ingest_status_values(self):
        assert IngestStatus.accepted == "accepted"
        assert IngestStatus.rejected == "rejected"
        assert IngestStatus.rate_limited == "rate_limited"
        assert IngestStatus.consent_required == "consent_required"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def client(tmp_path, monkeypatch):
    from backend import db, bootstrap as _boot
    from backend.main import app
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    await db.init()
    # Finalize bootstrap so the gate middleware doesn't return 503.
    _boot._gate_cache["finalized"] = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    _boot._gate_cache["finalized"] = False
    await db.close()


class TestRESTEndpoints:
    @pytest.mark.anyio
    async def test_list_sdk_profiles(self, client):
        resp = await client.get("/api/v1/telemetry/sdk-profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    @pytest.mark.anyio
    async def test_get_sdk_profile(self, client):
        resp = await client.get("/api/v1/telemetry/sdk-profiles/default")
        assert resp.status_code == 200
        assert resp.json()["profile_id"] == "default"

    @pytest.mark.anyio
    async def test_get_sdk_profile_not_found(self, client):
        resp = await client.get("/api/v1/telemetry/sdk-profiles/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_list_event_types(self, client):
        resp = await client.get("/api/v1/telemetry/event-types")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    @pytest.mark.anyio
    async def test_get_ingestion_config(self, client):
        resp = await client.get("/api/v1/telemetry/ingestion/config")
        assert resp.status_code == 200
        assert "max_batch_size" in resp.json()

    @pytest.mark.anyio
    async def test_ingest_events(self, client):
        record_consent("dev-api-1", True)
        resp = await client.post("/api/v1/telemetry/ingest", json={
            "device_id": "dev-api-1",
            "events": [
                {
                    "event_type": "perf_metric",
                    "device_id": "dev-api-1",
                    "timestamp": time.time(),
                    "metric_name": "cpu",
                    "metric_value": 50.0,
                }
            ],
            "opt_in": False,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    @pytest.mark.anyio
    async def test_ingest_without_consent_403(self, client):
        resp = await client.post("/api/v1/telemetry/ingest", json={
            "device_id": "dev-api-no-consent",
            "events": [
                {
                    "event_type": "perf_metric",
                    "device_id": "dev-api-no-consent",
                    "timestamp": time.time(),
                    "metric_name": "cpu",
                    "metric_value": 50.0,
                }
            ],
            "opt_in": False,
        })
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_list_dashboards(self, client):
        resp = await client.get("/api/v1/telemetry/dashboards")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    @pytest.mark.anyio
    async def test_get_dashboard(self, client):
        resp = await client.get("/api/v1/telemetry/dashboards/fleet_health")
        assert resp.status_code == 200
        assert resp.json()["dashboard_id"] == "fleet_health"

    @pytest.mark.anyio
    async def test_list_test_recipes(self, client):
        resp = await client.get("/api/v1/telemetry/test-recipes")
        assert resp.status_code == 200
        assert len(resp.json()) == 10

    @pytest.mark.anyio
    async def test_list_socs(self, client):
        resp = await client.get("/api/v1/telemetry/socs")
        assert resp.status_code == 200
        assert len(resp.json()) == 11

    @pytest.mark.anyio
    async def test_privacy_config(self, client):
        resp = await client.get("/api/v1/telemetry/privacy/config")
        assert resp.status_code == 200
        d = resp.json()
        assert "redaction_salt" not in d

    @pytest.mark.anyio
    async def test_redact_pii_endpoint(self, client):
        resp = await client.post("/api/v1/telemetry/privacy/redact", json={
            "event_data": {
                "ip_address": "192.168.1.100",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "device_id": "dev-api-pii",
            }
        })
        assert resp.status_code == 200
        assert resp.json()["ip_address"] == "192.168.1.0"

    @pytest.mark.anyio
    async def test_consent_flow(self, client):
        resp = await client.post("/api/v1/telemetry/privacy/consent", json={
            "device_id": "dev-api-consent",
            "opted_in": True,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "opted_in"

        resp = await client.get("/api/v1/telemetry/privacy/consent/dev-api-consent")
        assert resp.status_code == 200
        assert resp.json()["opted_in"] is True

    @pytest.mark.anyio
    async def test_storage_config(self, client):
        resp = await client.get("/api/v1/telemetry/storage/config")
        assert resp.status_code == 200
        assert resp.json()["engine"] == "sqlite"

    @pytest.mark.anyio
    async def test_retry_queue_status(self, client):
        resp = await client.get("/api/v1/telemetry/retry-queue/status")
        assert resp.status_code == 200
        assert "queue_size" in resp.json()

    @pytest.mark.anyio
    async def test_list_artifacts(self, client):
        resp = await client.get("/api/v1/telemetry/artifacts")
        assert resp.status_code == 200
        assert len(resp.json()) == 6

    @pytest.mark.anyio
    async def test_generate_certs(self, client):
        resp = await client.post("/api/v1/telemetry/certs/generate/hi3516")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
