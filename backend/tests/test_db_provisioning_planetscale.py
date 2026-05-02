"""FS.1.1 — PlanetScale DB provisioning adapter tests (respx-mocked)."""

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
from backend.db_provisioning.planetscale import (
    PLANETSCALE_API_BASE,
    PlanetScaleDBProvisionAdapter,
)

P = PLANETSCALE_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return PlanetScaleDBProvisionAdapter(
        token="pscale_ABCDEF0123456789",
        database_name="tenant-demo",
        organization="org-demo",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_database_when_absent_and_password(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_err(404, "missing"),
        )
        create = respx.post(f"{P}/organizations/org-demo/databases").mock(
            return_value=_ok({
                "id": "db_123",
                "name": "tenant-demo",
                "state": "ready",
                "region": {"slug": "us-east"},
            }, status=201),
        )
        password = respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "u/ser",
                "plain_text": "p=word",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )
        result = await _mk_adapter().provision_database(password_name="omnisight")
        assert result.created is True
        assert result.database_id == "db_123"
        assert result.encryption_at_rest is not None
        assert result.encryption_at_rest.provider_tier == "scaler-pro"
        assert result.encryption_at_rest.enabled is True
        assert result.backup_schedule is not None
        assert result.backup_schedule.provider_tier == "scaler-pro"
        assert result.backup_schedule.enabled is True
        assert result.backup_schedule.schedule == "twice-daily"
        assert result.connection_url == (
            "mysql://u%2Fser:p%3Dword@aws.connect.psdb.cloud/"
            "tenant-demo?sslaccept=strict"
        )
        assert create.called
        assert password.called

    @respx.mock
    async def test_reuses_existing_database_and_creates_password(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_ok({
                "id": "db_123",
                "name": "tenant-demo",
                "state": "ready",
                "region": {"slug": "us-east"},
            }),
        )
        respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "user",
                "plain_text": "secret",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )
        result = await _mk_adapter().provision_database(role="admin")
        assert result.created is False
        assert result.database_id == "db_123"
        assert result.connection_url == (
            "mysql://user:secret@aws.connect.psdb.cloud/tenant-demo?sslaccept=strict"
        )

    @respx.mock
    async def test_provider_tier_controls_encryption_policy_metadata(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_ok({"id": "db_123", "name": "tenant-demo"}),
        )
        respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "user",
                "plain_text": "secret",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )
        result = await _mk_adapter(provider_tier="enterprise").provision_database()
        assert result.encryption_at_rest is not None
        assert result.encryption_at_rest.provider_tier == "enterprise-multi-tenant"

    @respx.mock
    async def test_provider_tier_controls_backup_schedule_metadata(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_ok({"id": "db_123", "name": "tenant-demo"}),
        )
        respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "user",
                "plain_text": "secret",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )
        result = await _mk_adapter(provider_tier="enterprise").provision_database()
        assert result.backup_schedule is not None
        assert result.backup_schedule.provider_tier == "enterprise-multi-tenant"
        assert result.backup_schedule.schedule == "twice-daily"

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_err(401, "bad"),
        )
        with pytest.raises(InvalidDBProvisionTokenError):
            await _mk_adapter().provision_database()
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_err(403, "scope"),
        )
        with pytest.raises(MissingDBProvisionScopeError):
            await _mk_adapter().provision_database()

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_err(404, "missing"),
        )
        respx.post(f"{P}/organizations/org-demo/databases").mock(
            return_value=_err(422, "taken"),
        )
        with pytest.raises(DBProvisionConflictError):
            await _mk_adapter().provision_database()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "13"}, json={"message": "slow"},
            ),
        )
        with pytest.raises(DBProvisionRateLimitError) as excinfo:
            await _mk_adapter().provision_database()
        assert excinfo.value.retry_after == 13


class TestGetConnectionUrl:

    @respx.mock
    async def test_url_cached_after_provision(self):
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_ok({"id": "db_123", "name": "tenant-demo"}),
        )
        respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "user",
                "plain_text": "secret",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )
        adapter = _mk_adapter()
        await adapter.provision_database()
        assert adapter.get_connection_url() == (
            "mysql://user:secret@aws.connect.psdb.cloud/tenant-demo?sslaccept=strict"
        )
