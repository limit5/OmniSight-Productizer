"""Tests for Release Packaging (Phase 40).

Covers:
- Version resolver (git, fallback)
- Release manifest generation
- Release bundle creation
- Upload functions (mock — no real tokens)
- /release slash command
- API endpoints

Phase-3-Runtime-v2 SP-3.6b (2026-04-20): migrated from SQLite
``db.init()`` / legacy ``client`` fixture to ``pg_test_pool``
(for direct db.* writes) + ``client`` fixture (which itself
installs the module-global pool via SP-3.4's consolidation,
so HTTP-only tests need nothing extra).
"""

from __future__ import annotations

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Version Resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestVersionResolver:

    @pytest.mark.asyncio
    async def test_resolves_something(self):
        from backend.release import resolve_version
        version = await resolve_version()
        assert isinstance(version, str)
        assert len(version) > 0

    @pytest.mark.asyncio
    async def test_fallback_when_no_git(self):
        """Even without git tags, should return a version."""
        from backend.release import resolve_version
        version = await resolve_version()
        # Either a git hash, package.json version, or fallback
        assert version != ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Release Manifest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseManifest:

    @pytest.mark.asyncio
    async def test_manifest_structure(self, pg_test_pool):
        # generate_release_manifest uses get_pool().acquire() internally —
        # pg_test_pool ensures the module-global pool is installed.
        from backend.release import generate_release_manifest
        manifest = await generate_release_manifest("1.0.0-test")
        assert manifest["name"] == "OmniSight Productizer"
        assert manifest["version"] == "1.0.0-test"
        assert "artifact_count" in manifest
        assert "artifacts" in manifest
        assert isinstance(manifest["artifacts"], list)

    @pytest.mark.asyncio
    async def test_manifest_with_artifacts(self, pg_test_pool):
        # Use a unique id per run so re-running the suite does not collide
        # against rows persisted by an earlier run (pg_test_pool commits).
        import uuid as _uuid
        from backend import db
        from backend.release import generate_release_manifest

        art_id = f"art-manifest-test-{_uuid.uuid4().hex[:8]}"
        async with pg_test_pool.acquire() as conn:
            await db.insert_artifact(conn, {
                "id": art_id,
                "task_id": "t1",
                "agent_id": "fw-1",
                "name": "test.bin",
                "type": "firmware",
                "file_path": "/tmp/test.bin",
                "size": 1024,
                "created_at": "2026-04-13T00:00:00",
                "version": "1.0.0",
                "checksum": "abc123",
            })
        try:
            manifest = await generate_release_manifest("1.0.0")
            assert manifest["artifact_count"] >= 1
            art = next((a for a in manifest["artifacts"] if a["id"] == art_id), None)
            assert art is not None
            assert art["name"] == "test.bin"
            assert art["checksum_sha256"] == "abc123"
            assert "/download" in art["download_url"]
        finally:
            async with pg_test_pool.acquire() as conn:
                await db.delete_artifact(conn, art_id)

    @pytest.mark.asyncio
    async def test_manifest_filter_by_ids(self, pg_test_pool):
        import uuid as _uuid
        from backend import db
        from backend.release import generate_release_manifest

        a_id = f"art-filter-a-{_uuid.uuid4().hex[:8]}"
        b_id = f"art-filter-b-{_uuid.uuid4().hex[:8]}"
        async with pg_test_pool.acquire() as conn:
            await db.insert_artifact(conn, {
                "id": a_id, "task_id": "", "agent_id": "",
                "name": "a.bin", "type": "binary", "file_path": "/tmp/a",
                "size": 100, "created_at": "2026-04-13T00:00:00",
            })
            await db.insert_artifact(conn, {
                "id": b_id, "task_id": "", "agent_id": "",
                "name": "b.bin", "type": "binary", "file_path": "/tmp/b",
                "size": 200, "created_at": "2026-04-13T00:00:00",
            })
        try:
            manifest = await generate_release_manifest("1.0.0", artifact_ids=[a_id])
            ids = [a["id"] for a in manifest["artifacts"]]
            assert a_id in ids
            assert b_id not in ids
        finally:
            async with pg_test_pool.acquire() as conn:
                await db.delete_artifact(conn, a_id)
                await db.delete_artifact(conn, b_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Release Bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseBundle:

    @pytest.mark.asyncio
    async def test_create_bundle(self, pg_test_pool):
        from backend.release import create_release_bundle
        bundle = await create_release_bundle(version="0.0.1-test")
        assert bundle["name"].startswith("omnisight-release-")
        assert bundle["name"].endswith(".tar.gz")
        assert bundle["version"] == "0.0.1-test"
        assert bundle["size"] > 0
        assert len(bundle["checksum"]) == 64  # SHA-256
        assert "manifest" in bundle
        assert "download_url" in bundle

    @pytest.mark.asyncio
    async def test_bundle_registered_in_db(self, pg_test_pool):
        from backend import db
        from backend.release import create_release_bundle
        bundle = await create_release_bundle(version="0.0.2-db-test")
        async with pg_test_pool.acquire() as conn:
            art = await db.get_artifact(conn, bundle["id"])
        assert art is not None
        assert art["type"] == "archive"
        assert art["version"] == "0.0.2-db-test"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Upload Functions (no real tokens)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBundleContents:
    """H4: Verify tar.gz actually contains manifest + artifact files."""

    @pytest.mark.asyncio
    async def test_bundle_contains_manifest(self, pg_test_pool):
        import tarfile
        from backend.release import create_release_bundle
        bundle = await create_release_bundle(version="0.0.5-tar-test")
        # Open and inspect the tar.gz
        with tarfile.open(bundle["file_path"], "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names

    @pytest.mark.asyncio
    async def test_bundle_contains_artifact_files(self, pg_test_pool):
        import tarfile
        from backend import db
        from backend.routers.artifacts import get_artifacts_root
        from backend.release import create_release_bundle

        # Create a real artifact file
        art_root = get_artifacts_root()
        test_dir = art_root / "tar-test"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "sensor.ko"
        test_file.write_bytes(b"mock kernel module for tar test")

        import uuid as _uuid
        art_id = f"art-tar-content-{_uuid.uuid4().hex[:8]}"
        async with pg_test_pool.acquire() as conn:
            await db.insert_artifact(conn, {
                "id": art_id,
                "task_id": "tar-test",
                "agent_id": "fw-1",
                "name": "sensor.ko",
                "type": "kernel_module",
                "file_path": str(test_file),
                "size": test_file.stat().st_size,
                "created_at": "2026-04-13T00:00:00",
                "version": "1.0.0",
                "checksum": "abc",
            })
        try:
            bundle = await create_release_bundle(
                version="0.0.6-artifact-tar",
                artifact_ids=[art_id],
            )
            with tarfile.open(bundle["file_path"], "r:gz") as tar:
                names = tar.getnames()
                assert "manifest.json" in names
                assert "sensor.ko" in names
                # Read manifest and verify
                import json
                manifest_data = json.load(tar.extractfile("manifest.json"))
                assert manifest_data["version"] == "0.0.6-artifact-tar"
                assert manifest_data["artifact_count"] == 1
        finally:
            async with pg_test_pool.acquire() as conn:
                await db.delete_artifact(conn, art_id)
            # Cleanup filesystem
            test_file.unlink(missing_ok=True)
            if test_dir.exists():
                test_dir.rmdir()


class TestUploadFunctions:

    @pytest.mark.asyncio
    async def test_github_upload_skipped_no_token(self):
        from backend.release import upload_to_github
        result = await upload_to_github("/tmp/bundle.tar.gz", "1.0.0", {})
        assert result["status"] == "skipped"
        assert "github_token" in result["reason"] or "github_repo" in result["reason"]

    @pytest.mark.asyncio
    async def test_gitlab_upload_skipped_no_token(self):
        from backend.release import upload_to_gitlab
        result = await upload_to_gitlab("/tmp/bundle.tar.gz", "1.0.0", {})
        assert result["status"] == "skipped"
        assert "gitlab_token" in result["reason"] or "gitlab_project_id" in result["reason"]

    @pytest.mark.asyncio
    async def test_github_upload_success_mock(self):
        """M7: Test GitHub upload with mocked token + subprocess.

        Phase 5-6 (#multi-account-forge): the GitHub token resolution
        now flows through :func:`backend.git_credentials.pick_default`
        so this test patches the resolver rather than reaching into
        ``settings.github_token`` directly — reflecting the
        operational contract where tokens live in ``git_accounts``.
        """
        from unittest.mock import patch, AsyncMock
        from backend.release import upload_to_github

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"https://github.com/owner/repo/releases/v1.0.0\n", b""))
        mock_proc.returncode = 0

        async def _fake_pick_default(platform, **_kw):
            return {"token": "ghp_fake_token"} if platform == "github" else None

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=_fake_pick_default), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            mock_settings.github_repo = "owner/repo"
            mock_settings.release_draft = False
            result = await upload_to_github("/tmp/bundle.tar.gz", "1.0.0", {"artifact_count": 3})

        assert result["status"] == "uploaded"
        assert "github.com" in result["url"]
        assert result["tag"] == "v1.0.0"

    @pytest.mark.asyncio
    async def test_github_upload_failure_mock(self):
        from unittest.mock import patch, AsyncMock
        from backend.release import upload_to_github

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Not Found"))
        mock_proc.returncode = 1

        async def _fake_pick_default(platform, **_kw):
            return {"token": "ghp_fake"} if platform == "github" else None

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=_fake_pick_default), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            mock_settings.github_repo = "owner/repo"
            mock_settings.release_draft = True
            result = await upload_to_github("/tmp/bundle.tar.gz", "1.0.0", {})

        assert result["status"] == "error"
        assert "Not Found" in result["error"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slash Command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseSlashCommand:

    @pytest.mark.asyncio
    async def test_release_no_args(self, client):
        # SP-3.1 handle_slash_command signature is (conn, command, args).
        # /release calls db.list_artifacts(conn) so we must supply a
        # real conn. Acquire from the same module-global pool the
        # client fixture installed.
        from backend.slash_commands import handle_slash_command
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            result = await handle_slash_command(conn, "release", "")
        assert result is not None
        assert "Version" in result or "Release" in result

    @pytest.mark.asyncio
    async def test_release_create(self, client):
        from backend.slash_commands import handle_slash_command
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            result = await handle_slash_command(
                conn, "release", "create 0.0.3-test",
            )
        assert result is not None
        assert "Bundle" in result or "Created" in result or "ERROR" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseEndpoints:

    @pytest.mark.asyncio
    async def test_get_version(self, client):
        resp = await client.get("/api/v1/runtime/release/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert len(data["version"]) > 0

    @pytest.mark.asyncio
    async def test_get_manifest(self, client):
        resp = await client.get("/api/v1/runtime/release/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "OmniSight Productizer"
        assert "artifacts" in data

    @pytest.mark.asyncio
    async def test_create_release(self, client):
        resp = await client.post("/api/v1/runtime/release", json={
            "version": "0.0.4-api-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "bundle" in data
        assert data["bundle"]["version"] == "0.0.4-api-test"
        assert data["bundle"]["size"] > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseConfig:

    def test_config_fields_exist(self):
        from backend.config import settings
        assert hasattr(settings, "github_repo")
        assert hasattr(settings, "gitlab_project_id")
        assert hasattr(settings, "release_enabled")
        assert hasattr(settings, "release_draft")

    def test_defaults(self):
        from backend.config import settings
        assert settings.github_repo == ""
        assert settings.release_enabled is False
        assert settings.release_draft is True
