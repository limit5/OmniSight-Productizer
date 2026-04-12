"""Tests for System Integration Settings (Phase 34)."""

import pytest


class TestSettingsEndpoint:

    @pytest.mark.asyncio
    async def test_get_settings(self, client):
        resp = await client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm" in data
        assert "git" in data
        assert "gerrit" in data
        assert "jira" in data
        assert "slack" in data
        assert "docker" in data

    @pytest.mark.asyncio
    async def test_settings_masks_tokens(self, client):
        resp = await client.get("/api/v1/system/settings")
        data = resp.json()
        # Tokens should be masked or empty
        git_token = data["git"]["github_token"]
        assert git_token == "" or "***" in git_token

    @pytest.mark.asyncio
    async def test_update_settings_valid(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {"llm_temperature": 0.5}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_temperature" in data["applied"]

    @pytest.mark.asyncio
    async def test_update_settings_rejected(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {"dangerous_field": "hack"}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "dangerous_field" in data["rejected"]

    @pytest.mark.asyncio
    async def test_update_empty(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {}
        })
        assert resp.status_code == 200


class TestConnectionEndpoints:

    @pytest.mark.asyncio
    async def test_test_ssh(self, client):
        resp = await client.post("/api/v1/system/test/ssh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_gerrit(self, client):
        resp = await client.post("/api/v1/system/test/gerrit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_github(self, client):
        resp = await client.post("/api/v1/system/test/github")
        assert resp.status_code == 200
        # Without token: not_configured
        assert resp.json()["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_jira(self, client):
        resp = await client.post("/api/v1/system/test/jira")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_test_slack(self, client):
        resp = await client.post("/api/v1/system/test/slack")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_integration(self, client):
        resp = await client.post("/api/v1/system/test/nonexistent")
        assert resp.status_code == 400


class TestVendorSDKCRUD:

    @pytest.mark.asyncio
    async def test_create_vendor_sdk(self, client):
        resp = await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-vendor-crud",
            "label": "Test Vendor",
            "vendor_id": "test-v",
            "toolchain": "aarch64-linux-gnu-gcc",
            "cross_prefix": "aarch64-linux-gnu-",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
        # Cleanup
        await client.delete("/api/v1/system/vendor/sdks/test-vendor-crud")

    @pytest.mark.asyncio
    async def test_create_duplicate_rejected(self, client):
        await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-dup", "label": "Dup", "vendor_id": "dup",
        })
        resp = await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-dup", "label": "Dup2", "vendor_id": "dup2",
        })
        assert resp.status_code == 409
        await client.delete("/api/v1/system/vendor/sdks/test-dup")

    @pytest.mark.asyncio
    async def test_delete_vendor_sdk(self, client):
        await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-del", "label": "Del", "vendor_id": "del",
        })
        resp = await client.delete("/api/v1/system/vendor/sdks/test-del")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_builtin_blocked(self, client):
        resp = await client.delete("/api/v1/system/vendor/sdks/aarch64")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/v1/system/vendor/sdks/nonexistent-xyz")
        assert resp.status_code == 404


class TestMaskFunction:

    def test_mask_short(self):
        from backend.routers.integration import _mask
        assert _mask("abc") == "***"
        assert _mask("") == ""

    def test_mask_long(self):
        from backend.routers.integration import _mask
        result = _mask("ghp_abcdefghijklmnop")
        assert result.startswith("ghp")
        assert result.endswith("nop")
        assert "***" in result or "*" in result

    def test_updatable_fields_whitelist(self):
        from backend.routers.integration import _UPDATABLE_FIELDS
        assert "llm_provider" in _UPDATABLE_FIELDS
        assert "gerrit_enabled" in _UPDATABLE_FIELDS
        assert "notification_jira_url" in _UPDATABLE_FIELDS
        # Dangerous fields should NOT be updatable
        assert "app_name" not in _UPDATABLE_FIELDS


class TestComponentExists:

    def test_integration_settings_component(self):
        from pathlib import Path
        comp = Path(__file__).resolve().parent.parent.parent / "components" / "omnisight" / "integration-settings.tsx"
        assert comp.exists()
        content = comp.read_text()
        assert "IntegrationSettings" in content
        assert "SettingsButton" in content
        assert "TEST" in content
