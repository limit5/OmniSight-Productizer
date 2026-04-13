"""Tests for SDK Auto-Discovery (Phase 45).

Covers:
- SDK repo scanning (sysroot, cmake, toolchain detection)
- SDK path validation
- Platform YAML sdk_git_url field
- API install/validate endpoints
- get_platform_config path warnings
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  scan_sdk_repo
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScanSDKRepo:

    def test_detect_sysroot_dir(self, tmp_path):
        from backend.sdk_provisioner import scan_sdk_repo
        # Create sysroot structure
        sysroot = tmp_path / "sysroot" / "usr" / "lib"
        sysroot.mkdir(parents=True)
        result = scan_sdk_repo(tmp_path)
        assert result["sysroot_path"] != ""
        assert "sysroot" in result["sysroot_path"]

    def test_detect_staging_dir(self, tmp_path):
        from backend.sdk_provisioner import scan_sdk_repo
        staging = tmp_path / "staging" / "usr" / "include"
        staging.mkdir(parents=True)
        result = scan_sdk_repo(tmp_path)
        assert "staging" in result["sysroot_path"]

    def test_detect_cmake_toolchain(self, tmp_path):
        from backend.sdk_provisioner import scan_sdk_repo
        tc = tmp_path / "toolchain.cmake"
        tc.write_text("set(CMAKE_SYSTEM_NAME Linux)")
        result = scan_sdk_repo(tmp_path)
        assert result["cmake_toolchain_file"] != ""
        assert "toolchain.cmake" in result["cmake_toolchain_file"]

    def test_detect_nested_cmake(self, tmp_path):
        from backend.sdk_provisioner import scan_sdk_repo
        cmake_dir = tmp_path / "cmake"
        cmake_dir.mkdir()
        (cmake_dir / "arm-toolchain.cmake").write_text("set(CMAKE_C_COMPILER arm-gcc)")
        result = scan_sdk_repo(tmp_path)
        assert len(result["toolchain_files"]) >= 1

    def test_empty_dir(self, tmp_path):
        from backend.sdk_provisioner import scan_sdk_repo
        result = scan_sdk_repo(tmp_path)
        assert result["sysroot_path"] == ""
        assert result["cmake_toolchain_file"] == ""

    def test_nonexistent_dir(self):
        from backend.sdk_provisioner import scan_sdk_repo
        result = scan_sdk_repo(Path("/nonexistent"))
        assert result["sysroot_path"] == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  validate_sdk_paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidateSDKPaths:

    def test_vendor_example_missing_paths(self):
        from backend.sdk_provisioner import validate_sdk_paths
        result = validate_sdk_paths("vendor-example")
        # vendor-example has sysroot=/opt/example-vendor/sysroot which doesn't exist
        assert result["valid"] is False
        assert len(result["missing_paths"]) > 0

    def test_nonexistent_profile(self):
        from backend.sdk_provisioner import validate_sdk_paths
        result = validate_sdk_paths("nonexistent-platform")
        assert result["valid"] is False
        assert "Profile not found" in result["warnings"][0]

    def test_aarch64_generic(self):
        from backend.sdk_provisioner import validate_sdk_paths
        result = validate_sdk_paths("aarch64")
        # aarch64 has empty sysroot — no missing paths for empty values
        missing_sysroot = [m for m in result["missing_paths"] if "sysroot" in m]
        # Empty sysroot_path should not be flagged
        assert len(missing_sysroot) == 0 or "sysroot" not in str(result["missing_paths"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform YAML sdk_git_url
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPlatformYAMLFields:

    def test_vendor_example_has_sdk_fields(self):
        import yaml
        path = Path("configs/platforms/vendor-example.yaml")
        data = yaml.safe_load(path.read_text())
        assert "sdk_git_url" in data
        assert "sdk_git_branch" in data
        assert "sdk_install_script" in data


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSDKEndpoints:

    @pytest.mark.asyncio
    async def test_validate_endpoint(self, client):
        resp = await client.get("/api/v1/system/vendor/sdks/vendor-example/validate")
        assert resp.status_code == 200
        data = resp.json()
        assert "valid" in data
        assert "missing_paths" in data

    @pytest.mark.asyncio
    async def test_validate_nonexistent(self, client):
        resp = await client.get("/api/v1/system/vendor/sdks/nonexistent-xyz/validate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False

    @pytest.mark.asyncio
    async def test_install_no_sdk_url(self, client):
        """Install on vendor-example with empty sdk_git_url should skip."""
        resp = await client.post("/api/v1/system/vendor/sdks/vendor-example/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  get_platform_config warnings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPlatformConfigWarnings:

    @pytest.mark.asyncio
    async def test_vendor_example_shows_sysroot_missing(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "vendor-example"})
        assert "SYSROOT_MISSING=true" in result

    @pytest.mark.asyncio
    async def test_vendor_example_shows_cmake_missing(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "vendor-example"})
        assert "CMAKE_TOOLCHAIN_MISSING=true" in result

    @pytest.mark.asyncio
    async def test_aarch64_no_warnings(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "aarch64"})
        assert "SYSROOT_MISSING" not in result
        assert "CMAKE_TOOLCHAIN_MISSING" not in result
