"""v2-⑤-1a release-version image surfacing contract."""

from pathlib import Path

import pytest

from backend import release
from backend.routers import system


RELEASE_VERSION_URL = "/api/v1/runtime/release/version"


def _write_manifest(path: Path) -> None:
    path.write_text(
        (
            '{"image_sha":"sha256:op1173","build_time":"2026-05-16T00:00:00Z",'
            '"git_ref":"refs/heads/feature/OP-1173",'
            '"alembic_head_in_image":"0237_runner_audit_events"}'
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def release_version_manifest(tmp_path, monkeypatch) -> Path:
    manifest = tmp_path / "MANIFEST.json"
    _write_manifest(manifest)
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", manifest)

    async def _resolve_version() -> str:
        return "0.0.0-op1173"

    monkeypatch.setattr(release, "resolve_version", _resolve_version)
    return manifest


@pytest.mark.asyncio
async def test_release_version_response_has_v2_5_1a_required_fields(
    client,
    release_version_manifest,
):
    resp = await client.get(RELEASE_VERSION_URL)

    assert resp.status_code == 200
    data = resp.json()
    assert data["image_sha"] == "sha256:op1173"
    assert data["build_time"] == "2026-05-16T00:00:00Z"
    assert data["git_ref"] == "refs/heads/feature/OP-1173"
    assert data["alembic_head_in_image"] == "0237_runner_audit_events"


@pytest.mark.asyncio
async def test_release_version_response_preserves_existing_fields(
    client,
    release_version_manifest,
):
    resp = await client.get(RELEASE_VERSION_URL)

    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "0.0.0-op1173"


@pytest.mark.asyncio
async def test_release_version_handles_missing_manifest_gracefully(
    client,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(system, "_IMAGE_MANIFEST_PATH", tmp_path / "MANIFEST.json")

    async def _resolve_version() -> str:
        return "0.0.0-op1173"

    monkeypatch.setattr(release, "resolve_version", _resolve_version)

    resp = await client.get(RELEASE_VERSION_URL)

    assert resp.status_code == 200
    assert resp.json() == {"version": "0.0.0-op1173"}
