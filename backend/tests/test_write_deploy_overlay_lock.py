"""OP-1606 (RT-08) — deploy-overlay lock WRITER + deploy/compose wiring.

The reader half (OP-1582, ``test_deploy_overlay_rt08.py``) was already green
but inert: nothing wrote ``/etc/omnisight/deploy-overlay.lock``, so the six
RT-08 identity fields were always null and the gate never fired. These tests
pin the writer this ticket adds and its wiring:

* ``scripts/write_deploy_overlay_lock.py`` emits the SAME six fields the reader
  requires (key set pinned against ``api_versioning._OVERLAY_LOCK_FIELDS``),
  derived from the deployed candidate bundle, and round-trips through the
  reader so a deployed backend serves live values.
* Fail-closed: a partial/malformed bundle is REFUSED and NO lock is written.
* End-to-end: a writer-produced lock + ``OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1``
  makes ``/readyz`` pass, and the absence of one makes it 503 (the gate the
  writer finally lets fire).
* Deploy wiring: ``scripts/staging_deploy.sh`` invokes the writer before
  bring-up, and ``deploy/staging/docker-compose.yml`` mounts the lock dir into
  both backend replicas; ``infra/staging/.env.template`` enables the gate.
"""
from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest
import yaml

from backend import api_versioning as av
from backend.routers import health as health_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITER_PATH = REPO_ROOT / "scripts" / "write_deploy_overlay_lock.py"
STAGING_DEPLOY_SH = REPO_ROOT / "scripts" / "staging_deploy.sh"
STAGING_COMPOSE = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
STAGING_ENV_TEMPLATE = REPO_ROOT / "infra" / "staging" / ".env.template"


def _load_writer():
    spec = importlib.util.spec_from_file_location("write_deploy_overlay_lock", WRITER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


writer = _load_writer()


def _bundle() -> dict:
    return {
        "bundle_id": "v0.5.0-rc4-4218e1c7",
        "git_ref": "refs/tags/v0.5.0-rc4",
        "git_sha": "4218e1c71a1d10915b42843b366464d9a83bea88",
        "images": {
            "backend": {"digest": "sha256:" + "a" * 64},
            "frontend": {"digest": "sha256:" + "b" * 64},
            "bridge": {"digest": "sha256:" + "c" * 64},
        },
    }


@pytest.fixture(autouse=True)
def _reset_overlay_cache(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", raising=False)
    av.reset_deploy_overlay_cache()
    yield
    av.reset_deploy_overlay_cache()


# ──────────────────────────────────────────────────────────────
#  Writer field set is pinned against the reader (no drift)
# ──────────────────────────────────────────────────────────────


def test_writer_fields_match_reader_lock_fields():
    """The writer must emit EXACTLY the keys the reader requires, in order.

    If either half adds/removes/reorders a field, this fails — the two halves
    of RT-08 can never silently drift.
    """
    assert writer.LOCK_FIELDS == tuple(av._OVERLAY_LOCK_FIELDS.keys())


# ──────────────────────────────────────────────────────────────
#  resolve + validate + render
# ──────────────────────────────────────────────────────────────


def test_resolve_fields_from_bundle():
    fields = writer.resolve_fields(_bundle(), tag="v0.5.0-rc4", env={})
    assert fields["OMNISIGHT_BUILD_GIT_SHA"] == "4218e1c71a1d10915b42843b366464d9a83bea88"
    assert fields["OMNISIGHT_BUILD_GIT_REF"] == "refs/tags/v0.5.0-rc4"
    assert fields["OMNISIGHT_DEPLOYED_TAG"] == "v0.5.0-rc4"
    assert fields["OMNISIGHT_DEPLOYED_DIGEST_BACKEND"] == "sha256:" + "a" * 64
    assert fields["OMNISIGHT_DEPLOYED_DIGEST_FRONTEND"] == "sha256:" + "b" * 64
    # promotion audit id defaults to the candidate bundle id.
    assert fields["OMNISIGHT_PROMOTION_AUDIT_ID"] == "v0.5.0-rc4-4218e1c7"


def test_resolve_precedence_override_beats_env_beats_bundle():
    fields = writer.resolve_fields(
        _bundle(),
        tag="v0.5.0-rc4",
        promotion_audit_id="promo-2026-05-22-001",
        overrides={"OMNISIGHT_BUILD_GIT_SHA": "deadbeef" * 5},
        env={"OMNISIGHT_BUILD_GIT_REF": "refs/heads/from-env"},
    )
    assert fields["OMNISIGHT_BUILD_GIT_SHA"] == "deadbeef" * 5  # override wins
    assert fields["OMNISIGHT_BUILD_GIT_REF"] == "refs/heads/from-env"  # env over bundle
    assert fields["OMNISIGHT_PROMOTION_AUDIT_ID"] == "promo-2026-05-22-001"


def test_validate_rejects_incomplete():
    bundle = _bundle()
    del bundle["images"]["frontend"]
    fields = writer.resolve_fields(bundle, tag="v1", env={})
    with pytest.raises(writer.OverlayLockError) as exc:
        writer.validate_fields(fields)
    assert "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND" in str(exc.value)


def test_validate_rejects_malformed_digest():
    bundle = _bundle()
    bundle["images"]["backend"]["digest"] = "not-a-digest"
    fields = writer.resolve_fields(bundle, tag="v1", env={})
    with pytest.raises(writer.OverlayLockError) as exc:
        writer.validate_fields(fields)
    assert "malformed digest" in str(exc.value)


def test_render_lock_is_env_file_shaped():
    fields = writer.validate_fields(writer.resolve_fields(_bundle(), tag="v1", env={}))
    body = writer.render_lock(fields)
    # Reader's tolerant env-file parser must read every field back.
    parsed = av._parse_env_lock(body)
    for key in writer.LOCK_FIELDS:
        assert parsed[key] == fields[key]


# ──────────────────────────────────────────────────────────────
#  write_lock — atomic, fail-closed, round-trips through the reader
# ──────────────────────────────────────────────────────────────


def test_write_lock_round_trips_through_reader(tmp_path):
    """AC: after a (simulated) deploy the lock has all 6 fields populated.

    The writer's on-disk output is read back by the SAME loader a deployed
    backend uses at startup, and yields a complete overlay (no None field).
    """
    out = tmp_path / "deploy-overlay.lock"
    fields = writer.resolve_fields(_bundle(), tag="v0.5.0-rc4", env={})
    writer.write_lock(out, fields)

    overlay = av.load_deploy_overlay(out)
    assert overlay is not None
    for field in av.OVERLAY_FIELDS:
        assert overlay[field], f"reader saw empty overlay field {field}"
    assert overlay["deployed_tag"] == "v0.5.0-rc4"
    assert overlay["deployed_digest_backend"] == "sha256:" + "a" * 64
    assert overlay["promotion_audit_id"] == "v0.5.0-rc4-4218e1c7"


def test_write_lock_is_world_readable_for_container_uid(tmp_path):
    """OP-1609: backend uid 65532 reads the lock through a read-only mount."""
    out = tmp_path / "deploy-overlay.lock"
    fields = writer.resolve_fields(_bundle(), tag="v0.5.0-rc4", env={})
    writer.write_lock(out, fields)

    assert stat.S_IMODE(out.stat().st_mode) == 0o644


def test_write_lock_refuses_incomplete_and_writes_nothing(tmp_path):
    out = tmp_path / "deploy-overlay.lock"
    bundle = _bundle()
    del bundle["images"]["backend"]
    fields = writer.resolve_fields(bundle, tag="v1", env={})
    with pytest.raises(writer.OverlayLockError):
        writer.write_lock(out, fields)
    assert not out.exists()  # fail-closed: no half-written lock left behind


def test_main_cli_writes_lock(tmp_path):
    bundle_path = tmp_path / "bundle.json"
    import json

    bundle_path.write_text(json.dumps(_bundle()), encoding="utf-8")
    out = tmp_path / "lock" / "deploy-overlay.lock"
    rc = writer.main(["--bundle", str(bundle_path), "--tag", "v0.5.0-rc4", "--out", str(out)])
    assert rc == 0
    assert av.load_deploy_overlay(out) is not None


def test_main_cli_fails_closed_on_bad_bundle(tmp_path, capsys):
    bundle_path = tmp_path / "bundle.json"
    import json

    bad = _bundle()
    del bad["images"]["frontend"]
    bundle_path.write_text(json.dumps(bad), encoding="utf-8")
    out = tmp_path / "deploy-overlay.lock"
    rc = writer.main(["--bundle", str(bundle_path), "--tag", "v1", "--out", str(out)])
    assert rc == 1
    assert not out.exists()


# ──────────────────────────────────────────────────────────────
#  End-to-end: writer output drives the /readyz gate the reader owns
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readyz_ready_with_writer_produced_lock(tmp_path, monkeypatch):
    """AC: with REQUIRE=1 a writer-produced lock makes /readyz pass.

    Closes the loop the reader tests could only assert with a hand-written
    fixture: the lock under test here is the writer's real output.
    """
    out = tmp_path / "deploy-overlay.lock"
    writer.write_lock(out, writer.resolve_fields(_bundle(), tag="v0.5.0-rc4", env={}))

    monkeypatch.setenv("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY", "1")
    monkeypatch.setattr(av, "DEPLOY_OVERLAY_LOCK_PATH", out)
    av.reset_deploy_overlay_cache()

    async def _ok():
        return True, "ok"

    monkeypatch.setattr(health_mod, "_check_db", _ok)
    monkeypatch.setattr(health_mod, "_check_migrations", _ok)
    monkeypatch.setattr(health_mod, "_check_provider_chain", lambda: (True, "ok"))

    import json as _json

    resp = await health_mod._readyz_handler()
    body = _json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["checks"]["deploy_overlay"]["ok"] is True
    assert "tag=v0.5.0-rc4" in body["checks"]["deploy_overlay"]["detail"]


# ──────────────────────────────────────────────────────────────
#  Deploy / compose wiring contracts (shape only — no Docker needed)
# ──────────────────────────────────────────────────────────────


def test_staging_deploy_sh_invokes_writer_before_bringup():
    text = STAGING_DEPLOY_SH.read_text(encoding="utf-8")
    assert "write_deploy_overlay_lock.py" in text
    assert "write_overlay_lock" in text
    assert "OMNISIGHT_DEPLOY_OVERLAY_DIR" in text
    # The lock must be written before `compose up` (fail-closed bring-up).
    assert text.index("write_overlay_lock \"$color\"") < text.index('eval "$compose up -d"')


def test_staging_deploy_sh_force_recreates_backends_after_overlay_write():
    text = STAGING_DEPLOY_SH.read_text(encoding="utf-8")
    write_idx = text.index("write_overlay_lock \"$color\"")
    recreate_idx = text.index(
        'eval "$compose up -d --no-deps --force-recreate backend-a backend-b"'
    )
    assert write_idx < recreate_idx


def test_staging_compose_mounts_overlay_lock_into_both_backends():
    compose = yaml.safe_load(STAGING_COMPOSE.read_text(encoding="utf-8"))
    mount_suffix = ":/etc/omnisight:ro"
    for svc in ("backend-a", "backend-b"):
        volumes = compose["services"][svc].get("volumes") or []
        assert any(
            str(v).endswith(mount_suffix) and "OMNISIGHT_DEPLOY_OVERLAY_DIR" in str(v)
            for v in volumes
        ), f"{svc} does not mount the RT-08 overlay lock dir"


def test_staging_env_template_enables_require_flag():
    text = STAGING_ENV_TEMPLATE.read_text(encoding="utf-8")
    assert "OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1" in text
