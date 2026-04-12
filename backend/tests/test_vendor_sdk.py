"""Tests for SoC SDK/EVK integration (Phase 28)."""

from pathlib import Path

import pytest


class TestPlatformProfiles:

    def test_aarch64_has_vendor_fields(self):
        import yaml
        profile = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / "aarch64.yaml"
        data = yaml.safe_load(profile.read_text())
        assert "vendor_id" in data
        assert "sysroot_path" in data
        assert "cmake_toolchain_file" in data

    def test_vendor_example_profile(self):
        import yaml
        profile = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / "vendor-example.yaml"
        assert profile.exists()
        data = yaml.safe_load(profile.read_text())
        assert data["vendor_id"] == "example-vendor"
        assert data["sdk_version"] == "1.0.0"
        assert "deploy_method" in data
        assert "supported_boards" in data

    def test_all_profiles_have_required_fields(self):
        import yaml
        platforms_dir = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms"
        for f in platforms_dir.glob("*.yaml"):
            data = yaml.safe_load(f.read_text())
            assert "platform" in data, f"{f.name} missing 'platform'"
            assert "toolchain" in data, f"{f.name} missing 'toolchain'"


class TestHardwareManifestVendor:

    def test_manifest_has_vendor_section(self):
        import yaml
        manifest = Path(__file__).resolve().parent.parent.parent / "configs" / "hardware_manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        assert "vendor" in data
        assert "vendor_id" in data["vendor"]
        assert "soc_model" in data["vendor"]
        assert "npu_enabled" in data["vendor"]

    def test_build_has_sysroot_field(self):
        import yaml
        manifest = Path(__file__).resolve().parent.parent.parent / "configs" / "hardware_manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        assert "sysroot_path" in data["build"]
        assert "cmake_toolchain_file" in data["build"]


class TestGetPlatformConfigTool:

    def test_tool_in_tool_map(self):
        from backend.agents.tools import TOOL_MAP
        assert "get_platform_config" in TOOL_MAP

    def test_tool_in_firmware_agent(self):
        from backend.agents.tools import AGENT_TOOLS
        tool_names = [t.name for t in AGENT_TOOLS.get("firmware", [])]
        assert "get_platform_config" in tool_names

    @pytest.mark.asyncio
    async def test_get_platform_config_aarch64(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "aarch64"})
        assert "[OK]" in result
        assert "ARCH=arm64" in result
        assert "CROSS_COMPILE=aarch64-linux-gnu-" in result

    @pytest.mark.asyncio
    async def test_get_platform_config_nonexistent(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "nonexistent"})
        assert "[ERROR]" in result


class TestVendorSDKEndpoint:

    @pytest.mark.asyncio
    async def test_vendor_sdks_returns_list(self, client):
        resp = await client.get("/api/v1/system/vendor/sdks")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 4  # aarch64, armv7, riscv64, vendor-example

    @pytest.mark.asyncio
    async def test_vendor_sdks_has_required_fields(self, client):
        resp = await client.get("/api/v1/system/vendor/sdks")
        for sdk in resp.json():
            assert "platform" in sdk
            assert "vendor_id" in sdk
            assert "status" in sdk
            assert sdk["status"] in ("ready", "not_installed")

    @pytest.mark.asyncio
    async def test_generic_platform_always_ready(self, client):
        resp = await client.get("/api/v1/system/vendor/sdks")
        aarch64 = next((s for s in resp.json() if s["platform"] == "aarch64"), None)
        assert aarch64 is not None
        assert aarch64["status"] == "ready"


class TestBSPSkillParameterized:

    def test_bsp_skill_has_get_platform_config(self):
        skill_file = Path(__file__).resolve().parent.parent.parent / "configs" / "roles" / "firmware" / "bsp.skill.md"
        content = skill_file.read_text()
        assert "get_platform_config" in content
        assert "vendor" in content.lower()
        assert "CMAKE_TOOLCHAIN_FILE" in content
