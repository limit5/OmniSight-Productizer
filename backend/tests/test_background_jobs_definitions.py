"""FS.5.2 -- Tests for the background job definition scaffold."""

from __future__ import annotations

import pytest

from backend.background_jobs import (
    BACKGROUND_JOB_DEFINITION_IDS,
    BACKGROUND_JOB_DEFINITION_ITEMS,
    BACKGROUND_JOB_DEFINITIONS,
    BackgroundJobDefinition,
    BackgroundJobRequest,
    get_background_job_definition,
    list_background_job_definitions,
)


class TestBackgroundJobDefinitionRegistry:

    def test_list_background_job_definitions_pins_fs_5_2_catalog(self):
        assert list_background_job_definitions() == [
            "decision-timeout-sweep",
            "tenant-quota-sweep",
            "user-drafts-gc",
            "workspace-gc",
        ]
        assert BACKGROUND_JOB_DEFINITION_IDS == tuple(
            list_background_job_definitions()
        )

    def test_mapping_matches_catalog_items(self):
        assert (
            tuple(item.job_id for item in BACKGROUND_JOB_DEFINITION_ITEMS)
            == BACKGROUND_JOB_DEFINITION_IDS
        )
        assert tuple(BACKGROUND_JOB_DEFINITIONS) == BACKGROUND_JOB_DEFINITION_IDS

    @pytest.mark.parametrize("job_id", BACKGROUND_JOB_DEFINITION_IDS)
    def test_get_background_job_definition_returns_catalog_entry(self, job_id):
        item = get_background_job_definition(job_id)
        assert item.job_id == job_id
        assert item.display_name
        assert item.description
        assert item.handler.startswith("backend.")
        assert item.cron

    def test_get_background_job_definition_accepts_underscore_alias(self):
        item = get_background_job_definition("USER_DRAFTS_GC")
        assert item.job_id == "user-drafts-gc"

    def test_get_background_job_definition_rejects_unknown(self):
        with pytest.raises(KeyError, match="unknown background job definition"):
            get_background_job_definition("receipt-email")


class TestBackgroundJobDefinitionRequests:

    def test_to_request_merges_default_payload_and_overrides(self):
        definition = BackgroundJobDefinition(
            job_id="catalog-source-sync",
            display_name="Catalog source sync",
            description="Sync one catalog source.",
            handler="backend.catalog_sync.sync_source",
            cron="*/15 * * * *",
            endpoint_path="api/internal/cron/catalog-source-sync",
            default_payload={"tenant_id": "t-default", "full": False},
        )

        request = definition.to_request(
            payload={"full": True},
            idempotency_key="sync-default",
        )

        assert isinstance(request, BackgroundJobRequest)
        assert request.to_dict() == {
            "name": "catalog-source-sync",
            "payload": {"tenant_id": "t-default", "full": True},
            "idempotency_key": "sync-default",
            "cron": "*/15 * * * *",
            "endpoint_path": "/api/internal/cron/catalog-source-sync",
        }

    def test_cron_request_requires_cron(self):
        definition = BackgroundJobDefinition(
            job_id="catalog-source-sync",
            display_name="Catalog source sync",
            description="Sync one catalog source.",
            handler="backend.catalog_sync.sync_source",
        )

        with pytest.raises(ValueError, match="does not define a cron"):
            definition.cron_request()

    def test_to_dict_is_json_safe(self):
        item = get_background_job_definition("tenant-quota-sweep")

        assert item.to_dict() == {
            "job_id": "tenant-quota-sweep",
            "display_name": "Tenant quota sweep",
            "description": (
                "Check tenant storage pressure and run LRU cleanup when needed."
            ),
            "handler": "backend.tenant_quota.sweep_all_tenants",
            "cron": "*/5 * * * *",
            "endpoint_path": None,
            "default_payload": {},
            "tags": {"subsystem": "storage"},
        }

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"job_id": " ", "display_name": "x", "handler": "h"}, "job_id"),
            ({"job_id": "x", "display_name": " ", "handler": "h"}, "display_name"),
            ({"job_id": "x", "display_name": "x", "handler": " "}, "handler"),
        ],
    )
    def test_definition_requires_identity_fields(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            BackgroundJobDefinition(description="x", **kwargs)
