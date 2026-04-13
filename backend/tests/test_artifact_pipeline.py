"""Tests for Artifact Pipeline (Phase 39).

Covers:
- ArtifactType expanded enum
- Artifact model version/checksum fields
- DB insert with new fields
- _guess_artifact_type extension mapping
- _collect_build_artifacts from workspace
- register_build_artifact tool
- Download endpoint MIME types
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ArtifactType + Artifact Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArtifactTypeEnum:

    def test_original_types_exist(self):
        from backend.models import ArtifactType
        assert ArtifactType.pdf == "pdf"
        assert ArtifactType.markdown == "markdown"
        assert ArtifactType.log == "log"

    def test_binary_types_exist(self):
        from backend.models import ArtifactType
        assert ArtifactType.binary == "binary"
        assert ArtifactType.firmware == "firmware"
        assert ArtifactType.kernel_module == "kernel_module"
        assert ArtifactType.sdk == "sdk"
        assert ArtifactType.model == "model"
        assert ArtifactType.archive == "archive"

    def test_total_count(self):
        from backend.models import ArtifactType
        assert len(ArtifactType) == 11


class TestArtifactModel:

    def test_version_checksum_fields(self):
        from backend.models import Artifact
        a = Artifact(id="test", name="driver.ko", version="1.0.0", checksum="abc123")
        assert a.version == "1.0.0"
        assert a.checksum == "abc123"

    def test_defaults(self):
        from backend.models import Artifact
        a = Artifact(id="test", name="output.bin")
        assert a.version == ""
        assert a.checksum == ""
        assert a.type == "markdown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB Insert with version/checksum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArtifactDB:

    @pytest.mark.asyncio
    async def test_insert_with_version_checksum(self, client):
        from backend import db
        await db.insert_artifact({
            "id": "art-test-v",
            "task_id": "t1",
            "agent_id": "fw-1",
            "name": "sensor.ko",
            "type": "kernel_module",
            "file_path": "/tmp/test.ko",
            "size": 4096,
            "created_at": "2026-04-13T00:00:00",
            "version": "2.1.0",
            "checksum": "abcdef1234567890",
        })
        art = await db.get_artifact("art-test-v")
        assert art is not None
        assert art["version"] == "2.1.0"
        assert art["checksum"] == "abcdef1234567890"
        assert art["type"] == "kernel_module"

    @pytest.mark.asyncio
    async def test_insert_without_version_defaults(self, client):
        from backend import db
        await db.insert_artifact({
            "id": "art-test-nv",
            "task_id": "",
            "agent_id": "",
            "name": "report.md",
            "type": "markdown",
            "file_path": "/tmp/report.md",
            "size": 100,
            "created_at": "2026-04-13T00:00:00",
        })
        art = await db.get_artifact("art-test-nv")
        assert art["version"] == ""
        assert art["checksum"] == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _guess_artifact_type
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGuessArtifactType:

    def test_firmware_types(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("image.bin") == "firmware"
        assert _guess_artifact_type("boot.hex") == "firmware"

    def test_kernel_module(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("sensor.ko") == "kernel_module"

    def test_binary(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("driver.elf") == "binary"
        assert _guess_artifact_type("libcam.so") == "binary"

    def test_model(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("detect.rknn") == "model"
        assert _guess_artifact_type("face.tflite") == "model"
        assert _guess_artifact_type("yolo.onnx") == "model"

    def test_archive(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("release.tar.gz") == "archive"
        assert _guess_artifact_type("sdk.zip") == "archive"

    def test_sdk(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("omnisight-sdk.deb") == "sdk"

    def test_unknown_defaults_binary(self):
        from backend.workspace import _guess_artifact_type
        assert _guess_artifact_type("output.xyz") == "binary"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _collect_build_artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCollectBuildArtifacts:

    @pytest.mark.asyncio
    async def test_collects_from_build_dir(self, client, tmp_path):
        from backend.workspace import _collect_build_artifacts

        # Create mock build outputs
        build_dir = tmp_path / "build" / "output"
        build_dir.mkdir(parents=True)
        (build_dir / "driver.ko").write_bytes(b"ELF mock kernel module content here")
        (build_dir / "firmware.bin").write_bytes(b"\x00\x01\x02\x03" * 100)

        collected = await _collect_build_artifacts(tmp_path, "agent-1", "task-1")
        assert len(collected) == 2
        names = {a["name"] for a in collected}
        assert "driver.ko" in names
        assert "firmware.bin" in names
        # Verify checksum is populated
        for a in collected:
            assert len(a["checksum"]) == 64  # SHA-256 hex

    @pytest.mark.asyncio
    async def test_skips_tiny_files(self, client, tmp_path):
        from backend.workspace import _collect_build_artifacts

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "empty.ko").write_bytes(b"tiny")  # < 10 bytes

        collected = await _collect_build_artifacts(tmp_path, "agent-1", "task-1")
        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_no_build_dir_returns_empty(self, client, tmp_path):
        from backend.workspace import _collect_build_artifacts
        collected = await _collect_build_artifacts(tmp_path, "agent-1", "task-1")
        assert collected == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  register_build_artifact tool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRegisterBuildArtifactTool:

    def test_tool_in_registry(self):
        from backend.agents.tools import TOOL_MAP
        assert "register_build_artifact" in TOOL_MAP

    def test_all_agents_have_tool(self):
        from backend.agents.tools import AGENT_TOOLS
        for agent_type in ("firmware", "software", "validator", "reporter", "general", "devops"):
            tools = {t.name for t in AGENT_TOOLS[agent_type]}
            assert "register_build_artifact" in tools, f"{agent_type} missing register_build_artifact"

    @pytest.mark.asyncio
    async def test_register_file(self, workspace, client):
        from backend.agents.tools import register_build_artifact

        # Create a file in workspace
        test_file = workspace / "build" / "sensor.ko"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(b"mock kernel module " * 10)

        result = await register_build_artifact.ainvoke({
            "file_path": "build/sensor.ko",
            "task_id": "test-task",
        })
        assert "[OK]" in result
        assert "sensor.ko" in result
        assert "SHA-256" in result

    @pytest.mark.asyncio
    async def test_register_nonexistent_file(self, workspace):
        from backend.agents.tools import register_build_artifact
        result = await register_build_artifact.ainvoke({"file_path": "nonexistent.bin"})
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_register_path_traversal_blocked(self, workspace):
        from backend.agents.tools import register_build_artifact
        result = await register_build_artifact.ainvoke({"file_path": "../../etc/passwd"})
        assert "[BLOCKED]" in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Download endpoint MIME types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDownloadMIME:

    def test_binary_types_have_mime(self):
        """New artifact types should have MIME mappings in the download endpoint."""
        import importlib
        from backend.routers import artifacts as art_mod
        importlib.reload(art_mod)

        import inspect
        source = inspect.getsource(art_mod.download_artifact)
        for atype in ("binary", "firmware", "kernel_module", "sdk", "model", "archive"):
            assert f'"{atype}"' in source, f"Missing MIME mapping for {atype}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M8: Download endpoint end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDownloadEndToEnd:

    @pytest.mark.asyncio
    async def test_create_and_download_artifact(self, client):
        """Full cycle: create file → register in DB → download via API."""
        from backend import db
        from backend.routers.artifacts import get_artifacts_root

        # Create a real artifact file
        art_root = get_artifacts_root()
        task_dir = art_root / "e2e-test"
        task_dir.mkdir(parents=True, exist_ok=True)
        test_file = task_dir / "firmware.bin"
        content = b"\x00\x01\x02\x03OMNISIGHT_FIRMWARE" * 5
        test_file.write_bytes(content)

        # Register in DB
        await db.insert_artifact({
            "id": "art-e2e-dl",
            "task_id": "e2e-test",
            "agent_id": "fw-1",
            "name": "firmware.bin",
            "type": "firmware",
            "file_path": str(test_file),
            "size": len(content),
            "created_at": "2026-04-13T00:00:00",
            "version": "1.0.0",
            "checksum": "abc123",
        })

        # Download via API
        resp = await client.get("/api/v1/artifacts/art-e2e-dl/download")
        assert resp.status_code == 200
        assert resp.content == content
        assert "application/octet-stream" in resp.headers.get("content-type", "")

        # Cleanup
        test_file.unlink(missing_ok=True)
        task_dir.rmdir()

    @pytest.mark.asyncio
    async def test_download_nonexistent_returns_404(self, client):
        resp = await client.get("/api/v1/artifacts/nonexistent-xyz/download")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_removes_file_and_db(self, client):
        """Delete should remove both file and DB record."""
        from backend import db
        from backend.routers.artifacts import get_artifacts_root

        art_root = get_artifacts_root()
        task_dir = art_root / "del-test"
        task_dir.mkdir(parents=True, exist_ok=True)
        test_file = task_dir / "to_delete.bin"
        test_file.write_bytes(b"delete me")

        await db.insert_artifact({
            "id": "art-del-test",
            "task_id": "del-test",
            "agent_id": "",
            "name": "to_delete.bin",
            "type": "binary",
            "file_path": str(test_file),
            "size": 9,
            "created_at": "2026-04-13T00:00:00",
        })

        resp = await client.delete("/api/v1/artifacts/art-del-test")
        assert resp.status_code == 204
        assert not test_file.exists()
        assert await db.get_artifact("art-del-test") is None

        # Cleanup dir
        if task_dir.exists():
            task_dir.rmdir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M9: register_build_artifact DB verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRegisterArtifactDBPersistence:

    @pytest.mark.asyncio
    async def test_registered_artifact_persists_in_db(self, workspace, client):
        """After register_build_artifact, artifact must exist in DB."""
        from backend.agents.tools import register_build_artifact
        from backend import db

        # Create file
        test_file = workspace / "build" / "driver.elf"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(b"ELF binary content " * 20)

        result = await register_build_artifact.ainvoke({
            "file_path": "build/driver.elf",
            "task_id": "persist-test",
            "version": "2.0.0",
        })
        assert "[OK]" in result

        # Extract artifact ID from result
        import re
        match = re.search(r"ID: (art-[a-f0-9]+)", result)
        assert match, f"Could not find artifact ID in: {result}"
        art_id = match.group(1)

        # Verify DB persistence
        art = await db.get_artifact(art_id)
        assert art is not None
        assert art["name"] == "driver.elf"
        assert art["type"] == "binary"
        assert art["version"] == "2.0.0"
        assert len(art["checksum"]) == 64  # SHA-256
        assert art["size"] > 0
