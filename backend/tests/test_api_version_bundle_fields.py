"""OP-1479 — /api/version surfaces the bundle manifest contract fields.

Pins the JSON contract for ``GET /api/version`` after the bundle
manifest landed: the response must carry bundle_id, api_required,
api_supported, openapi_hash, and db_migration_head so operators can
prove the running backend's bundle id matches the bundle artifact
they meant to deploy (Deploy AC of OP-1479).

Two layers of coverage:

* ``build_version_payload`` — exercised in isolation with fixture
  paths so the test does not need a baked /app/bundle.json.
* the mounted FastAPI route — exercised via TestClient with the
  module's module-level path swapped via monkeypatch.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api_versioning as av


_FIXTURE_BUNDLE = {
    "bundle_id": "v0.5.0-rc3-3f1c0a4e",
    "git_ref": "refs/tags/v0.5.0-rc3",
    "git_sha": "3f1c0a4e" + "0" * 32,
    "build_time": "2026-05-18T00:00:00Z",
    "images": {
        "backend":  {"digest": "sha256:" + "a" * 64},
        "frontend": {"digest": "sha256:" + "b" * 64},
        "bridge":   {"digest": "sha256:" + "c" * 64},
    },
    "contracts": {
        "api_required": "v1",
        "api_supported": ["v1", "v2"],
        "openapi_hash": "d" * 64,
        "db_migration_head": "0237_runner_audit_events",
        "frontend_built_against_api": "v1",
    },
    "signatures": [],
}


def _write_bundle(path: Path, payload: dict | None = None) -> Path:
    path.write_text(json.dumps(payload or _FIXTURE_BUNDLE), encoding="utf-8")
    return path


def test_build_version_payload_includes_all_bundle_fields(tmp_path):
    bundle = _write_bundle(tmp_path / "bundle.json")
    payload = av.build_version_payload(
        bundle_path=bundle,
        image_manifest_path=tmp_path / "MANIFEST.json",
    )

    assert payload["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert payload["api_required"] == "v1"
    assert payload["api_supported"] == ["v1", "v2"]
    assert payload["openapi_hash"] == "d" * 64
    assert payload["db_migration_head"] == "0237_runner_audit_events"


def test_build_version_payload_preserves_legacy_version_fields(tmp_path):
    bundle = _write_bundle(tmp_path / "bundle.json")
    payload = av.build_version_payload(
        bundle_path=bundle,
        image_manifest_path=tmp_path / "MANIFEST.json",
    )

    assert payload["supported_versions"] == list(av.SUPPORTED_API_VERSIONS)
    assert payload["default_version"] == av.DEFAULT_API_VERSION
    assert payload["min_frontend_api_version"] == av.MIN_FRONTEND_API_VERSION
    assert "v1" in payload["deprecated_versions"]


def test_build_version_payload_falls_back_to_image_manifest_db_head(tmp_path):
    bundle_payload = json.loads(json.dumps(_FIXTURE_BUNDLE))
    bundle_payload["contracts"].pop("db_migration_head")
    bundle = _write_bundle(tmp_path / "bundle.json", bundle_payload)

    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(
        json.dumps({"alembic_head_in_image": "0911_from_manifest"}),
        encoding="utf-8",
    )

    payload = av.build_version_payload(
        bundle_path=bundle,
        image_manifest_path=manifest,
    )
    assert payload["db_migration_head"] == "0911_from_manifest"


def test_build_version_payload_handles_missing_bundle_and_manifest(tmp_path):
    payload = av.build_version_payload(
        bundle_path=tmp_path / "no-bundle.json",
        image_manifest_path=tmp_path / "no-manifest.json",
    )
    assert payload["bundle_id"] is None
    assert payload["openapi_hash"] is None
    assert payload["db_migration_head"] is None
    # Fallbacks to module-level defaults so the negotiation contract
    # still works when the runtime image predates OP-1479.
    assert payload["api_required"] == av.MIN_FRONTEND_API_VERSION
    assert payload["api_supported"] == list(av.SUPPORTED_API_VERSIONS)


def test_build_version_payload_handles_malformed_bundle(tmp_path):
    bad = tmp_path / "bundle.json"
    bad.write_text("not-json{{", encoding="utf-8")
    payload = av.build_version_payload(
        bundle_path=bad,
        image_manifest_path=tmp_path / "MANIFEST.json",
    )
    assert payload["bundle_id"] is None
    assert payload["api_supported"] == list(av.SUPPORTED_API_VERSIONS)


def test_install_version_metadata_endpoint_returns_bundle_fields(tmp_path, monkeypatch):
    bundle = _write_bundle(tmp_path / "bundle.json")
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(
        json.dumps({"alembic_head_in_image": "0237_runner_audit_events"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", bundle)
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", manifest)

    app = FastAPI()
    av.install_version_metadata_endpoint(app)
    client = TestClient(app)
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()

    for key in (
        "bundle_id",
        "api_required",
        "api_supported",
        "openapi_hash",
        "db_migration_head",
    ):
        assert key in body, f"/api/version response missing {key}"

    assert body["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert body["api_supported"] == ["v1", "v2"]
    assert body["openapi_hash"] == "d" * 64
    assert body["db_migration_head"] == "0237_runner_audit_events"
