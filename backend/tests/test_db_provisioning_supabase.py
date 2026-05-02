"""FS.1.1 — Supabase DB provisioning adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.db_provisioning.base import (
    DBProvisionConflictError,
    DBProvisionRateLimitError,
    InvalidDBProvisionTokenError,
    MissingDBProvisionScopeError,
)
from backend.db_provisioning.supabase import SUPABASE_API_BASE, SupabaseDBProvisionAdapter

S = SUPABASE_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return SupabaseDBProvisionAdapter(
        token="sbp_ABCDEF0123456789",
        database_name="tenant-demo",
        organization_id="org_123",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_project_when_absent(self):
        respx.get(f"{S}/projects").mock(return_value=_ok([]))
        route = respx.post(f"{S}/projects").mock(
            return_value=_ok({
                "id": "prj_123",
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_123",
                "name": "tenant-demo",
                "region": "us-east-1",
                "status": "ACTIVE",
                "database": {"host": "db.abcdefghijklmnopqrst.supabase.co"},
            }, status=201),
        )
        result = await _mk_adapter().provision_database(db_pass="p=word")
        assert result.created is True
        assert result.database_id == "abcdefghijklmnopqrst"
        assert result.status == "ACTIVE"
        assert result.encryption_at_rest is not None
        assert result.encryption_at_rest.provider_tier == "free"
        assert result.encryption_at_rest.enabled is True
        assert result.backup_schedule is not None
        assert result.backup_schedule.provider_tier == "free"
        assert result.backup_schedule.enabled is False
        assert result.backup_schedule.schedule == "manual-offsite"
        assert result.pep_hold is not None
        assert result.pep_hold.provider_tier == "free"
        assert result.pep_hold.required is True
        assert result.pep_hold.cost_estimate.monthly_low_usd == 0.0
        assert result.connection_url == (
            "postgresql://postgres.abcdefghijklmnopqrst:p%3Dword@"
            "db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
        )
        body = route.calls.last.request.read()
        assert b'"organization_id":"org_123"' in body
        assert b'"db_pass":"p=word"' in body

    @respx.mock
    async def test_reuses_existing_project_by_name_and_org(self):
        respx.get(f"{S}/projects").mock(
            return_value=_ok([
                {"id": "other", "organization_id": "org_123", "name": "other"},
                {
                    "id": "prj_123",
                    "ref": "abcdefghijklmnopqrst",
                    "organization_id": "org_123",
                    "name": "tenant-demo",
                    "region": "us-east-1",
                    "database": {"host": "db.abcdefghijklmnopqrst.supabase.co"},
                },
            ]),
        )
        result = await _mk_adapter().provision_database(db_pass="secret")
        assert result.created is False
        assert result.database_id == "abcdefghijklmnopqrst"
        assert result.connection_url is not None

    @respx.mock
    async def test_provider_tier_controls_encryption_policy_metadata(self):
        respx.get(f"{S}/projects").mock(
            return_value=_ok([{
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_123",
                "name": "tenant-demo",
            }]),
        )
        result = await _mk_adapter(provider_tier="team").provision_database(
            db_pass="secret",
        )
        assert result.encryption_at_rest is not None
        assert result.encryption_at_rest.provider_tier == "team"

    @respx.mock
    async def test_provider_tier_controls_backup_schedule_metadata(self):
        respx.get(f"{S}/projects").mock(
            return_value=_ok([{
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_123",
                "name": "tenant-demo",
            }]),
        )
        result = await _mk_adapter(provider_tier="team").provision_database(
            db_pass="secret",
        )
        assert result.backup_schedule is not None
        assert result.backup_schedule.provider_tier == "team"
        assert result.backup_schedule.enabled is True
        assert result.backup_schedule.schedule == "daily"
        assert result.pep_hold is not None
        assert result.pep_hold.provider_tier == "team"
        assert result.pep_hold.cost_estimate.currency == "USD"

    @respx.mock
    async def test_create_requires_db_pass_when_absent(self):
        respx.get(f"{S}/projects").mock(return_value=_ok([]))
        with pytest.raises(ValueError):
            await _mk_adapter().provision_database()

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{S}/projects").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidDBProvisionTokenError):
            await _mk_adapter().provision_database(db_pass="x")
        respx.get(f"{S}/projects").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingDBProvisionScopeError):
            await _mk_adapter().provision_database(db_pass="x")

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{S}/projects").mock(return_value=_ok([]))
        respx.post(f"{S}/projects").mock(return_value=_err(422, "taken"))
        with pytest.raises(DBProvisionConflictError):
            await _mk_adapter().provision_database(db_pass="x")

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{S}/projects").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "9"}, json={"message": "slow"},
            ),
        )
        with pytest.raises(DBProvisionRateLimitError) as excinfo:
            await _mk_adapter().provision_database(db_pass="x")
        assert excinfo.value.retry_after == 9


class TestGetConnectionUrl:

    @respx.mock
    async def test_url_cached_after_provision(self):
        respx.get(f"{S}/projects").mock(
            return_value=_ok([{
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_123",
                "name": "tenant-demo",
                "database": {"host": "db.abcdefghijklmnopqrst.supabase.co"},
            }]),
        )
        adapter = _mk_adapter()
        await adapter.provision_database(db_pass="secret")
        assert adapter.get_connection_url() == (
            "postgresql://postgres.abcdefghijklmnopqrst:secret@"
            "db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
        )
