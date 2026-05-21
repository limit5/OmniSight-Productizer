"""OP-1479 / OP-1491 — /api/version surfaces the bundle manifest fields.

Pins the JSON contract for ``GET /api/version`` after the bundle
manifest landed: the response must carry bundle_id, api_required,
api_supported, openapi_hash, db_migration_head, and
frontend_built_against_api so operators can prove the running
backend's bundle id matches the bundle artifact they meant to deploy
(Deploy AC of OP-1479 / OP-1491).

Two layers of coverage:

* ``build_version_payload`` — exercised in isolation with fixture
  paths so the test does not need a baked /app/bundle.json.
* the mounted FastAPI route — exercised via TestClient with the
  module's module-level path swapped via monkeypatch.

OP-1491 specifically adds:

* ``frontend_built_against_api`` is wired from the bundle's
  ``contracts`` block.
* Missing or malformed bundle resolves to bundle_id ``"dev+unknown"``
  plus a ``warning`` field rather than ``None`` (so dev images and
  release images can be distinguished without the consumer needing
  to special-case ``None``).
* The endpoint reads the bundle once at install time and caches the
  payload — no per-request disk I/O.
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
    assert payload["frontend_built_against_api"] == "v1"
    assert payload["backend_image_digest"] == "sha256:" + "a" * 64
    assert payload["frontend_image_digest"] == "sha256:" + "b" * 64
    # Bundle is well-formed and carries a bundle_id, so no warning.
    assert "warning" not in payload


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
    # OP-1491: dev fallback rather than None so consumers don't have to
    # special-case the "no bundle baked" path.
    assert payload["bundle_id"] == av.DEV_FALLBACK_BUNDLE_ID
    assert "warning" in payload and payload["warning"]
    assert payload["openapi_hash"] is None
    assert payload["db_migration_head"] is None
    assert payload["frontend_built_against_api"] is None
    assert payload["backend_image_digest"] is None
    assert payload["frontend_image_digest"] is None
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
    # OP-1491: malformed JSON is treated the same as missing — fall back
    # to the dev placeholder + warning, never crash.
    assert payload["bundle_id"] == av.DEV_FALLBACK_BUNDLE_ID
    assert "warning" in payload and payload["warning"]
    assert payload["api_supported"] == list(av.SUPPORTED_API_VERSIONS)
    assert payload["frontend_built_against_api"] is None


def test_build_version_payload_warns_when_bundle_lacks_bundle_id(tmp_path):
    bundle_payload = json.loads(json.dumps(_FIXTURE_BUNDLE))
    bundle_payload.pop("bundle_id")
    bundle = _write_bundle(tmp_path / "bundle.json", bundle_payload)

    payload = av.build_version_payload(
        bundle_path=bundle,
        image_manifest_path=tmp_path / "MANIFEST.json",
    )
    # Bundle file exists but is malformed in a different way (no
    # bundle_id field). Operator should still see a warning and the
    # dev placeholder so they can tell something is wrong.
    assert payload["bundle_id"] == av.DEV_FALLBACK_BUNDLE_ID
    assert "warning" in payload and payload["warning"]
    # Other contract fields still flow through from the partial bundle.
    assert payload["openapi_hash"] == "d" * 64
    assert payload["frontend_built_against_api"] == "v1"


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
        "frontend_built_against_api",
        "backend_image_digest",
        "frontend_image_digest",
    ):
        assert key in body, f"/api/version response missing {key}"

    assert body["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert body["api_supported"] == ["v1", "v2"]
    assert body["openapi_hash"] == "d" * 64
    assert body["db_migration_head"] == "0237_runner_audit_events"
    assert body["frontend_built_against_api"] == "v1"
    assert body["backend_image_digest"] == "sha256:" + "a" * 64
    assert body["frontend_image_digest"] == "sha256:" + "b" * 64
    # Healthy bundle → no operator-facing warning.
    assert "warning" not in body


def test_install_version_metadata_endpoint_dev_unknown_when_bundle_missing(
    tmp_path, monkeypatch
):
    # No bundle.json on disk — simulates a local dev build / pre-V1c
    # image. /api/version must still serve, with the dev placeholder
    # bundle_id and an explicit warning field.
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "missing-manifest.json")

    app = FastAPI()
    av.install_version_metadata_endpoint(app)
    client = TestClient(app)
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bundle_id"] == av.DEV_FALLBACK_BUNDLE_ID
    assert "warning" in body
    assert isinstance(body["warning"], str) and body["warning"]
    # Backwards compat: the legacy version-routing keys are still served.
    assert body["supported_versions"] == list(av.SUPPORTED_API_VERSIONS)
    assert body["api_required"] == av.MIN_FRONTEND_API_VERSION


def test_install_version_metadata_endpoint_survives_malformed_bundle(
    tmp_path, monkeypatch
):
    # Malformed bundle.json must not crash startup; install must
    # complete and the endpoint must serve the dev fallback.
    bad = tmp_path / "bundle.json"
    bad.write_text("{not-json}", encoding="utf-8")
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", bad)
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "missing-manifest.json")

    app = FastAPI()
    # Must not raise.
    av.install_version_metadata_endpoint(app)
    client = TestClient(app)
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bundle_id"] == av.DEV_FALLBACK_BUNDLE_ID
    assert "warning" in body


def test_install_version_metadata_endpoint_caches_payload_at_install_time(
    tmp_path, monkeypatch
):
    # The bundle manifest is read once at install time and cached. If
    # the file changes (or disappears) after install, subsequent
    # /api/version requests must still return the snapshot captured at
    # install — proving we're not doing disk I/O per request.
    bundle = _write_bundle(tmp_path / "bundle.json")
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", bundle)
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "missing-manifest.json")

    app = FastAPI()
    av.install_version_metadata_endpoint(app)
    client = TestClient(app)

    first = client.get("/api/version").json()
    assert first["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"

    # Delete and rewrite the file post-install. Cached payload wins.
    bundle.unlink()
    second = client.get("/api/version").json()
    assert second["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert second == first
