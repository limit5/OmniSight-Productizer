"""OP-2101 (vmnda C5) — vendor mirror artifact resolver."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import git_credentials
from backend.agents import mirror_artifact as ma
from backend.config import settings


SHA = "a" * 64


@pytest.fixture(autouse=True)
def _reset_catalog_dir(monkeypatch):
    monkeypatch.setattr(
        settings, "mirror_catalog_dir", "third_party/vendor-mirror-catalog",
        raising=False,
    )


def _write_catalog(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "vendor-mirror-catalog"
    mirror_dir = root / "mirror"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "qualcomm.yaml").write_text(body, encoding="utf-8")
    return root


def _point_settings(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(settings, "mirror_catalog_dir", str(root), raising=False)


def _fake_pick_by_id(monkeypatch, row):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return row

    _fake.seen = None
    monkeypatch.setattr(git_credentials, "pick_by_id", _fake)
    return _fake


def test_yes_blob_resolves_to_resolved_blob(tmp_path, monkeypatch):
    root = _write_catalog(tmp_path, f"""
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-qcs6490-aarch64
    nda_posture: YES
    artifact_shape: blob
    mirror_url: https://gitlab.internal/packages/qcs6490.tar.xz
    sha256: {SHA}
    git_account_ref: vmnda-catalog-ro
    bot_pull: allow
""")
    _point_settings(monkeypatch, root)

    resolved = ma.resolve("qualcomm-qcs6490-aarch64")

    assert resolved == ma.ResolvedBlob(
        mirror_url="https://gitlab.internal/packages/qcs6490.tar.xz",
        sha256=SHA,
        git_account_ref="vmnda-catalog-ro",
    )


def test_no_posture_raises_pointer_only_with_portal_url(tmp_path, monkeypatch):
    root = _write_catalog(tmp_path, f"""
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-human-only-sdk
    nda_posture: NO
    artifact_shape: blob
    mirror_url: null
    sha256: {SHA}
    portal_url: https://vendor.example/downloads/sdk
    stage_path: /bench/vendor/qualcomm-human-only-sdk.tar.xz
    bot_pull: deny
""")
    _point_settings(monkeypatch, root)

    with pytest.raises(ma.PointerOnlyArtifactError) as excinfo:
        ma.resolve("qualcomm-human-only-sdk")

    assert excinfo.value.portal_url == "https://vendor.example/downloads/sdk"
    assert excinfo.value.sha256 == SHA
    assert excinfo.value.stage_path == "/bench/vendor/qualcomm-human-only-sdk.tar.xz"


def test_git_shape_resolves_to_overlay(tmp_path, monkeypatch):
    root = _write_catalog(tmp_path, """
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-qcs6490-bsp-overlay
    nda_posture: YES
    artifact_shape: git
    mirror_url: null
    overlay_ref: omnisight/qcs6490-linux-6.1-omni.1
    vendor_ref: vendor/qcs6490-linux-6.1
    git_account_ref: vmnda-overlay-ro
    bot_pull: allow
""")
    _point_settings(monkeypatch, root)

    resolved = ma.resolve("qualcomm-qcs6490-bsp-overlay")

    assert resolved == ma.ResolvedOverlay(
        overlay_ref="omnisight/qcs6490-linux-6.1-omni.1",
        vendor_ref="vendor/qcs6490-linux-6.1",
        git_account_ref="vmnda-overlay-ro",
    )


def test_bot_pull_deny_under_bot_identity_raises(tmp_path, monkeypatch):
    root = _write_catalog(tmp_path, f"""
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-human-build-blob
    nda_posture: YES
    artifact_shape: blob
    mirror_url: https://gitlab.internal/packages/human-build.tar.xz
    sha256: {SHA}
    bot_pull: deny
""")
    _point_settings(monkeypatch, root)

    with pytest.raises(ma.BotPullDeniedError):
        ma.resolve("qualcomm-human-build-blob", is_bot_identity=True)


def test_missing_snapshot_dir_raises_not_vendored(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path / "missing-catalog")

    with pytest.raises(ma.MirrorCatalogNotVendoredError):
        ma.resolve("qualcomm-qcs6490-aarch64")


def test_credential_resolves_by_git_account_ref_never_inline_token(monkeypatch):
    row = {"id": "vmnda-catalog-ro", "token": "glpat_real_secret"}
    fake = _fake_pick_by_id(monkeypatch, row)
    artifact = ma.ResolvedBlob(
        mirror_url="https://gitlab.internal/packages/qcs6490.tar.xz",
        sha256=SHA,
        git_account_ref="vmnda-catalog-ro",
    )

    resolved = asyncio.run(
        ma.resolve_mirror_artifact_credential(artifact, tenant_id="t-op")
    )

    assert resolved is row
    assert fake.seen == ("vmnda-catalog-ro", "t-op")


def test_inline_token_field_in_catalog_is_rejected(tmp_path, monkeypatch):
    root = _write_catalog(tmp_path, f"""
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-inline-secret
    nda_posture: YES
    artifact_shape: blob
    mirror_url: https://gitlab.internal/packages/qcs6490.tar.xz
    sha256: {SHA}
    token: glpat_forbidden
    git_account_ref: vmnda-catalog-ro
    bot_pull: allow
""")
    _point_settings(monkeypatch, root)

    with pytest.raises(ma.MirrorArtifactError, match="git_account_ref"):
        ma.resolve("qualcomm-inline-secret")
