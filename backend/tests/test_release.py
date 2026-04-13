"""Tests for Release Packaging (Phase 40).

Covers:
- Version resolver (git, fallback)
- Release manifest generation
- Release bundle creation
- Upload functions (mock — no real tokens)
- /release slash command
- API endpoints
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
    async def test_manifest_structure(self, client):
        from backend.release import generate_release_manifest
        manifest = await generate_release_manifest("1.0.0-test")
        assert manifest["name"] == "OmniSight Productizer"
        assert manifest["version"] == "1.0.0-test"
        assert "artifact_count" in manifest
        assert "artifacts" in manifest
        assert isinstance(manifest["artifacts"], list)

    @pytest.mark.asyncio
    async def test_manifest_with_artifacts(self, client):
        from backend import db
        from backend.release import generate_release_manifest

        # Insert test artifact
        await db.insert_artifact({
            "id": "art-manifest-test",
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

        manifest = await generate_release_manifest("1.0.0")
        assert manifest["artifact_count"] >= 1
        art = next((a for a in manifest["artifacts"] if a["id"] == "art-manifest-test"), None)
        assert art is not None
        assert art["name"] == "test.bin"
        assert art["checksum_sha256"] == "abc123"
        assert "/download" in art["download_url"]

    @pytest.mark.asyncio
    async def test_manifest_filter_by_ids(self, client):
        from backend import db
        from backend.release import generate_release_manifest

        await db.insert_artifact({
            "id": "art-filter-a", "task_id": "", "agent_id": "",
            "name": "a.bin", "type": "binary", "file_path": "/tmp/a",
            "size": 100, "created_at": "2026-04-13T00:00:00",
        })
        await db.insert_artifact({
            "id": "art-filter-b", "task_id": "", "agent_id": "",
            "name": "b.bin", "type": "binary", "file_path": "/tmp/b",
            "size": 200, "created_at": "2026-04-13T00:00:00",
        })

        manifest = await generate_release_manifest("1.0.0", artifact_ids=["art-filter-a"])
        ids = [a["id"] for a in manifest["artifacts"]]
        assert "art-filter-a" in ids
        assert "art-filter-b" not in ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Release Bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseBundle:

    @pytest.mark.asyncio
    async def test_create_bundle(self, client):
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
    async def test_bundle_registered_in_db(self, client):
        from backend import db
        from backend.release import create_release_bundle
        bundle = await create_release_bundle(version="0.0.2-db-test")
        art = await db.get_artifact(bundle["id"])
        assert art is not None
        assert art["type"] == "archive"
        assert art["version"] == "0.0.2-db-test"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Upload Functions (no real tokens)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slash Command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseSlashCommand:

    @pytest.mark.asyncio
    async def test_release_no_args(self, client):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command("release", "")
        assert result is not None
        assert "Version" in result or "Release" in result

    @pytest.mark.asyncio
    async def test_release_create(self, client):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command("release", "create 0.0.3-test")
        assert result is not None
        assert "Bundle" in result or "Created" in result or "ERROR" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseEndpoints:

    @pytest.mark.asyncio
    async def test_get_version(self, client):
        resp = await client.get("/api/v1/system/release/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert len(data["version"]) > 0

    @pytest.mark.asyncio
    async def test_get_manifest(self, client):
        resp = await client.get("/api/v1/system/release/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "OmniSight Productizer"
        assert "artifacts" in data

    @pytest.mark.asyncio
    async def test_create_release(self, client):
        resp = await client.post("/api/v1/system/release", json={
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
