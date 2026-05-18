"""OP-1479 — unit-test ``scripts/build_image_bundle.py``.

Covers the pure ``build_bundle()`` constructor (no CLI invocation) and
the CLI entry point against a synthetic ``openapi.json`` so the tests
do not depend on the real (~1.3 MB) committed spec hash.

We also validate the produced bundle against
``omnisight-bundle.schema.json`` when ``jsonschema`` is importable —
this is the Code AC's contract gate.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


# The script lives at <repo>/scripts/build_image_bundle.py — import it
# directly rather than relying on PYTHONPATH so the test is robust
# across the various sys.path setups conftest.py installs.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "build_image_bundle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_image_bundle", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_image_bundle", module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bib():
    return _load_module()


@pytest.fixture()
def fake_openapi(tmp_path: Path) -> Path:
    path = tmp_path / "openapi.json"
    path.write_bytes(b'{"openapi": "3.1.0", "info": {"title": "fake"}}')
    return path


_GIT_SHA = "3f1c0a4e" + "b" * 32
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def test_build_bundle_returns_full_schema_shape(bib, fake_openapi):
    bundle = bib.build_bundle(
        git_ref="refs/tags/v0.5.0-rc3",
        git_sha=_GIT_SHA,
        backend_digest=_DIGEST_A,
        frontend_digest=_DIGEST_B,
        bridge_digest=_DIGEST_C,
        openapi_path=fake_openapi,
        db_migration_head="0237_runner_audit_events",
        build_time=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )

    assert bundle["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert bundle["git_ref"] == "refs/tags/v0.5.0-rc3"
    assert bundle["git_sha"] == _GIT_SHA
    assert bundle["build_time"] == "2026-05-18T00:00:00Z"
    assert bundle["images"]["backend"]["digest"] == _DIGEST_A
    assert bundle["images"]["frontend"]["digest"] == _DIGEST_B
    assert bundle["images"]["bridge"]["digest"] == _DIGEST_C
    assert bundle["contracts"]["api_required"] == "v1"
    assert bundle["contracts"]["api_supported"] == ["v1", "v2"]
    assert bundle["contracts"]["db_migration_head"] == "0237_runner_audit_events"
    # SHA-256 hex of '{"openapi": "3.1.0", "info": {"title": "fake"}}'
    assert len(bundle["contracts"]["openapi_hash"]) == 64
    assert bundle["signatures"] == []


def test_build_bundle_id_uses_ref_tail_and_short_sha(bib, fake_openapi):
    bundle = bib.build_bundle(
        git_ref="refs/heads/develop",
        git_sha="9" * 40,
        backend_digest=_DIGEST_A,
        frontend_digest=_DIGEST_B,
        bridge_digest=_DIGEST_C,
        openapi_path=fake_openapi,
        db_migration_head="0001_init",
    )
    assert bundle["bundle_id"] == "develop-99999999"


def test_build_bundle_rejects_malformed_digest(bib, fake_openapi):
    with pytest.raises(SystemExit) as exc:
        bib.build_bundle(
            git_ref="refs/tags/v0.5.0-rc3",
            git_sha=_GIT_SHA,
            backend_digest="not-a-digest",
            frontend_digest=_DIGEST_B,
            bridge_digest=_DIGEST_C,
            openapi_path=fake_openapi,
            db_migration_head="0001_init",
        )
    assert "backend-digest" in str(exc.value)


def test_build_bundle_rejects_short_sha(bib, fake_openapi):
    with pytest.raises(SystemExit):
        bib.build_bundle(
            git_ref="refs/tags/v0.5.0-rc3",
            git_sha="abc",
            backend_digest=_DIGEST_A,
            frontend_digest=_DIGEST_B,
            bridge_digest=_DIGEST_C,
            openapi_path=fake_openapi,
            db_migration_head="0001_init",
        )


def test_build_bundle_includes_repository_when_provided(bib, fake_openapi):
    bundle = bib.build_bundle(
        git_ref="refs/tags/v1",
        git_sha=_GIT_SHA,
        backend_digest=_DIGEST_A,
        frontend_digest=_DIGEST_B,
        bridge_digest=_DIGEST_C,
        openapi_path=fake_openapi,
        db_migration_head="0001_init",
        backend_repository="ghcr.io/example/omnisight-backend",
        backend_tag="sha-3f1c0a4eb000",
    )
    assert bundle["images"]["backend"]["repository"] == "ghcr.io/example/omnisight-backend"
    assert bundle["images"]["backend"]["tag"] == "sha-3f1c0a4eb000"


def test_cli_writes_valid_json(bib, fake_openapi, tmp_path):
    out_path = tmp_path / "out" / "bundle.json"
    rc = bib.main([
        "--git-ref", "refs/tags/v0.5.0-rc3",
        "--git-sha", _GIT_SHA,
        "--backend-digest", _DIGEST_A,
        "--frontend-digest", _DIGEST_B,
        "--bridge-digest", _DIGEST_C,
        "--openapi", str(fake_openapi),
        "--db-migration-head", "0001_init",
        "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["bundle_id"] == "v0.5.0-rc3-3f1c0a4e"
    assert data["contracts"]["db_migration_head"] == "0001_init"


def test_cli_output_validates_against_schema(bib, fake_openapi, tmp_path):
    """Code AC: the bundle JSON validates against omnisight-bundle.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")

    out_path = tmp_path / "bundle.json"
    bib.main([
        "--git-ref", "refs/tags/v0.5.0-rc3",
        "--git-sha", _GIT_SHA,
        "--backend-digest", _DIGEST_A,
        "--frontend-digest", _DIGEST_B,
        "--bridge-digest", _DIGEST_C,
        "--openapi", str(fake_openapi),
        "--db-migration-head", "0237_runner_audit_events",
        "--out", str(out_path),
    ])

    schema_path = _REPO_ROOT / "omnisight-bundle.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    bundle = json.loads(out_path.read_text(encoding="utf-8"))
    jsonschema.validate(bundle, schema)
