"""FS.5.3 -- Tests for cron schedule wiring."""

from __future__ import annotations

import pytest

from backend.background_jobs import (
    BACKGROUND_JOB_DEFINITION_IDS,
    BackgroundJobDefinition,
    CronScheduleBinding,
    build_cron_schedule_bindings,
    build_cron_schedule_manifest,
    get_adapter,
    get_cron_schedule_binding,
)


def _adapter(provider: str):
    cls = get_adapter(provider)
    if provider == "inngest":
        return cls.from_plaintext_token("inngest_ABCDEF0123456789", event_key="evt-key")
    if provider == "vercel-cron":
        return cls.from_plaintext_token(
            "cron_ABCDEF0123456789",
            base_url="https://app.example.com",
        )
    return cls.from_plaintext_token("tr_ABCDEF0123456789")


class TestCronScheduleBindings:

    @pytest.mark.parametrize("provider", ["inngest", "trigger-dev", "vercel-cron"])
    def test_build_cron_schedule_bindings_wires_catalog_to_provider(self, provider):
        bindings = build_cron_schedule_bindings(_adapter(provider))

        assert tuple(binding.job_id for binding in bindings) == (
            BACKGROUND_JOB_DEFINITION_IDS
        )
        assert all(isinstance(binding, CronScheduleBinding) for binding in bindings)
        assert all(binding.provider == provider for binding in bindings)
        assert all(binding.handler.startswith("backend.") for binding in bindings)
        assert all(binding.request.cron for binding in bindings)
        assert all(binding.descriptor.provider == provider for binding in bindings)

    def test_vercel_manifest_uses_http_cron_paths(self):
        manifest = build_cron_schedule_manifest(_adapter("vercel-cron"))

        assert manifest["provider"] == "vercel-cron"
        by_id = {item["job_id"]: item for item in manifest["schedules"]}
        assert by_id["decision-timeout-sweep"]["descriptor"] == {
            "provider": "vercel-cron",
            "name": "decision-timeout-sweep",
            "schedule": "*/1 * * * *",
            "target": "/api/cron/decision-timeout-sweep",
        }
        assert by_id["tenant-quota-sweep"]["request"]["cron"] == "*/5 * * * *"
        assert by_id["workspace-gc"]["tags"] == {"subsystem": "workspace"}

    def test_provider_descriptor_shapes_are_preserved(self):
        inngest = get_cron_schedule_binding(_adapter("inngest"), "decision-timeout-sweep")
        trigger = get_cron_schedule_binding(_adapter("trigger-dev"), "tenant-quota-sweep")

        assert inngest.descriptor.raw == {
            "id": "decision-timeout-sweep",
            "cron": "*/1 * * * *",
            "event": "decision-timeout-sweep",
        }
        assert trigger.descriptor.raw == {
            "task": "tenant-quota-sweep",
            "cron": "*/5 * * * *",
        }

    def test_non_cron_definitions_are_skipped_in_manifest(self):
        definitions = (
            BackgroundJobDefinition(
                job_id="manual-only",
                display_name="Manual only",
                description="Manual dispatch only.",
                handler="backend.manual.run_once",
            ),
            BackgroundJobDefinition(
                job_id="cron-backed",
                display_name="Cron backed",
                description="Cron dispatch.",
                handler="backend.cron.run_once",
                cron="0 0 * * *",
            ),
        )

        bindings = build_cron_schedule_bindings(
            _adapter("trigger-dev"),
            definitions,
        )

        assert tuple(binding.job_id for binding in bindings) == ("cron-backed",)

    def test_get_cron_schedule_binding_accepts_underscore_alias(self):
        binding = get_cron_schedule_binding(_adapter("vercel-cron"), "USER_DRAFTS_GC")

        assert binding.job_id == "user-drafts-gc"
        assert binding.descriptor.target == "/api/cron/user-drafts-gc"
