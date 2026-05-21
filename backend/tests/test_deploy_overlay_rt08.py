"""OP-1582 (RT-08) — /api/version + /readyz deployment overlay env lock.

Pins the read-before-start contract locked in ADR-0040 RT-08-pre:

* The deploy/promote step writes an env-file lock carrying the running
  deployment's identity — build git sha/ref, the promoted image tag, the
  backend+frontend digest PAIR (RT-21), and the promotion audit id.
* The lock is read ONCE AT STARTUP (``init_deploy_overlay`` via
  ``install_version_metadata_endpoint``) and cached; ``/api/version``
  serves the cached snapshot and ``/readyz`` gates on it.
* Fail-closed: when ``OMNISIGHT_REQUIRE_DEPLOY_OVERLAY`` is set (prod/staging
  compose sets it), a missing or incomplete lock makes ``/readyz`` fail so
  the deploy gate catches a container that started without its identity.
* In dev/CI the flag is unset and the overlay check is observational —
  never blocks readiness.

Out of scope (other tickets): staging-evidence JSONL fields = RT-05d; the
prod-restart activation exercise = RT-08b.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api_versioning as av
from backend.routers import health as health_mod


_LOCK_BODY = """\
# deploy-written overlay lock (env-file format)
OMNISIGHT_BUILD_GIT_SHA=3f1c0a4e0000000000000000000000000000abcd
OMNISIGHT_BUILD_GIT_REF=refs/heads/develop
OMNISIGHT_DEPLOYED_TAG="v0.5.0"
OMNISIGHT_DEPLOYED_DIGEST_BACKEND=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
OMNISIGHT_DEPLOYED_DIGEST_FRONTEND=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export OMNISIGHT_PROMOTION_AUDIT_ID=promo-2026-05-22-001
"""


@pytest.fixture(autouse=True)
def _reset_overlay_cache(monkeypatch):
    """Each test starts with a clean overlay cache + no fail-closed flag."""
    monkeypatch.delenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", raising=False)
    av.reset_deploy_overlay_cache()
    yield
    av.reset_deploy_overlay_cache()


def _write_lock(tmp_path, body: str = _LOCK_BODY):
    lock = tmp_path / "deploy-overlay.lock"
    lock.write_text(body, encoding="utf-8")
    return lock


# ──────────────────────────────────────────────────────────────
#  Unit — env-lock parsing + load semantics
# ──────────────────────────────────────────────────────────────


def test_load_deploy_overlay_reads_all_fields(tmp_path):
    lock = _write_lock(tmp_path)
    overlay = av.load_deploy_overlay(lock)

    assert overlay is not None
    assert overlay["build_git_sha"] == "3f1c0a4e0000000000000000000000000000abcd"
    assert overlay["build_git_ref"] == "refs/heads/develop"
    # Quoted value is unwrapped; ``export`` prefix is tolerated.
    assert overlay["deployed_tag"] == "v0.5.0"
    assert overlay["deployed_digest_backend"] == "sha256:" + "a" * 64
    assert overlay["deployed_digest_frontend"] == "sha256:" + "b" * 64
    assert overlay["promotion_audit_id"] == "promo-2026-05-22-001"


def test_load_deploy_overlay_missing_file_returns_none(tmp_path):
    # Fail-closed marker: no lock on disk → None.
    assert av.load_deploy_overlay(tmp_path / "absent.lock") is None


def test_load_deploy_overlay_incomplete_returns_none(tmp_path):
    # A partial lock (writer broke / wrote only some fields) must fail
    # closed rather than advertise a half-known identity.
    partial = "OMNISIGHT_BUILD_GIT_SHA=abc123\nOMNISIGHT_DEPLOYED_TAG=v9.9.9\n"
    lock = _write_lock(tmp_path, partial)
    assert av.load_deploy_overlay(lock) is None


def test_load_deploy_overlay_blank_value_is_incomplete(tmp_path):
    body = _LOCK_BODY.replace("promo-2026-05-22-001", "")
    lock = _write_lock(tmp_path, body)
    assert av.load_deploy_overlay(lock) is None


def test_parse_env_lock_ignores_comments_and_blanks():
    parsed = av._parse_env_lock("# comment\n\n  \nFOO=bar\nBAZ='qux'\n")
    assert parsed == {"FOO": "bar", "BAZ": "qux"}


# ──────────────────────────────────────────────────────────────
#  Unit — startup reads the lock ONCE and caches it
# ──────────────────────────────────────────────────────────────


def test_init_deploy_overlay_caches_snapshot(tmp_path, monkeypatch):
    lock = _write_lock(tmp_path)
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", lock)

    first = av.init_deploy_overlay()
    assert first is not None and first["deployed_tag"] == "v0.5.0"

    # Lock changes / disappears after startup → cached snapshot still wins,
    # proving the read is once-at-startup, not per-access.
    lock.unlink()
    assert av.get_deploy_overlay() == first


def test_get_deploy_overlay_lazy_loads_from_default_path(tmp_path, monkeypatch):
    lock = _write_lock(tmp_path)
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", lock)
    av.reset_deploy_overlay_cache()
    # First access (no prior init) reads the configured path.
    overlay = av.get_deploy_overlay()
    assert overlay is not None and overlay["build_git_ref"] == "refs/heads/develop"


# ──────────────────────────────────────────────────────────────
#  /api/version — overlay fields on the payload
# ──────────────────────────────────────────────────────────────


def test_build_version_payload_includes_overlay_fields(tmp_path):
    lock = _write_lock(tmp_path)
    overlay = av.load_deploy_overlay(lock)
    payload = av.build_version_payload(
        bundle_path=tmp_path / "no-bundle.json",
        image_manifest_path=tmp_path / "no-manifest.json",
        overlay=overlay,
    )
    for field in av.OVERLAY_FIELDS:
        assert field in payload, f"/api/version missing overlay field {field}"
    assert payload["deployed_tag"] == "v0.5.0"
    assert payload["deployed_digest_backend"] == "sha256:" + "a" * 64
    assert payload["deployed_digest_frontend"] == "sha256:" + "b" * 64
    assert payload["build_git_sha"].startswith("3f1c0a4e")
    assert payload["promotion_audit_id"] == "promo-2026-05-22-001"
    assert payload["deploy_overlay_present"] is True


def test_build_version_payload_overlay_absent_fields_present_as_none(tmp_path):
    payload = av.build_version_payload(
        bundle_path=tmp_path / "no-bundle.json",
        image_manifest_path=tmp_path / "no-manifest.json",
        overlay=None,
    )
    # Contract shape stable: fields exist but are None when no lock is read.
    for field in av.OVERLAY_FIELDS:
        assert field in payload and payload[field] is None
    assert payload["deploy_overlay_present"] is False


def test_install_version_metadata_endpoint_reads_lock_at_startup(
    tmp_path, monkeypatch
):
    """AC: a unit test proves startup reads the lock.

    ``install_version_metadata_endpoint`` runs ``init_deploy_overlay`` at
    install time, so the served ``/api/version`` carries the lock identity.
    """
    lock = _write_lock(tmp_path)
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", lock)
    monkeypatch.setattr(av, "BUNDLE_MANIFEST_PATH", tmp_path / "no-bundle.json")
    monkeypatch.setattr(av, "_IMAGE_MANIFEST_PATH", tmp_path / "no-manifest.json")
    av.reset_deploy_overlay_cache()

    app = FastAPI()
    av.install_version_metadata_endpoint(app)
    body = TestClient(app).get("/api/version").json()

    assert body["deploy_overlay_present"] is True
    assert body["deployed_tag"] == "v0.5.0"
    assert body["deployed_digest_backend"] == "sha256:" + "a" * 64
    assert body["deployed_digest_frontend"] == "sha256:" + "b" * 64
    assert body["build_git_sha"].startswith("3f1c0a4e")
    assert body["build_git_ref"] == "refs/heads/develop"
    assert body["promotion_audit_id"] == "promo-2026-05-22-001"


# ──────────────────────────────────────────────────────────────
#  /readyz — fail-closed when required, observational otherwise
# ──────────────────────────────────────────────────────────────


def test_check_deploy_overlay_observational_in_dev(tmp_path, monkeypatch):
    # No lock, flag unset → ok=True (never blocks dev/CI readiness).
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", tmp_path / "absent.lock")
    av.reset_deploy_overlay_cache()
    ok, detail = health_mod._check_deploy_overlay()
    assert ok is True
    assert "not_required" in detail


def test_check_deploy_overlay_fails_closed_when_required_and_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", "1")
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", tmp_path / "absent.lock")
    av.reset_deploy_overlay_cache()
    ok, detail = health_mod._check_deploy_overlay()
    assert ok is False
    assert detail == "deploy_overlay_lock_missing"


def test_check_deploy_overlay_passes_when_required_and_present(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", "true")
    lock = _write_lock(tmp_path)
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", lock)
    av.reset_deploy_overlay_cache()
    ok, detail = health_mod._check_deploy_overlay()
    assert ok is True
    assert "tag=v0.5.0" in detail
    assert "audit=promo-2026-05-22-001" in detail


@pytest.mark.asyncio
async def test_readyz_fails_when_overlay_required_but_lock_missing(
    tmp_path, monkeypatch
):
    """AC: missing lock → /readyz fails.

    Other gates are pinned green so the only thing flipping ``ready`` to
    False is the missing-but-required overlay lock.
    """
    monkeypatch.setenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", "1")
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", tmp_path / "absent.lock")
    av.reset_deploy_overlay_cache()

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["deploy_overlay"]["ok"] is False
    assert body["checks"]["deploy_overlay"]["detail"] == "deploy_overlay_lock_missing"


@pytest.mark.asyncio
async def test_readyz_ready_when_overlay_required_and_lock_present(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", "1")
    lock = _write_lock(tmp_path)
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", lock)
    av.reset_deploy_overlay_cache()

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["deploy_overlay"]["ok"] is True
    assert "tag=v0.5.0" in body["checks"]["deploy_overlay"]["detail"]


@pytest.mark.asyncio
async def test_readyz_overlay_observational_does_not_block_dev(tmp_path, monkeypatch):
    # Flag unset (dev/CI): no lock present, but readiness is unaffected.
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", tmp_path / "absent.lock")
    av.reset_deploy_overlay_cache()

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))

    resp = await health_mod._readyz_handler()
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["deploy_overlay"]["ok"] is True
    assert "not_required" in body["checks"]["deploy_overlay"]["detail"]
