"""OP-1513 / OP-1478-act-T1.3 — scripts/emit_bundle_json.sh contract.

Covers AC #1 (env vars + bundle_id format + valid JSON to stdout) and the
guardrails that make the audit-emit ``bundle-assert`` job meaningful
(rejects stub digests / bad SHAs at emit time, not just post-build).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "emit_bundle_json.sh"
SCHEMA = REPO_ROOT / "omnisight-bundle.schema.json"

ZERO_DIGEST = "sha256:" + ("0" * 64)
DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)
DIGEST_C = "sha256:" + ("c" * 64)
ZERO_HASH64 = "0" * 64
GOOD_SHA = "7a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=run_env,
        check=False,
        text=True,
        capture_output=True,
    )


def _good_env(**overrides: str) -> dict[str, str]:
    base = {
        "GIT_SHA": GOOD_SHA,
        "GIT_REF": "refs/tags/v0.5.0-rc6",
        "IMAGE_DIGEST_BACKEND": DIGEST_A,
        "IMAGE_DIGEST_FRONTEND": DIGEST_B,
        "IMAGE_DIGEST_BRIDGE": DIGEST_C,
        "API_REQUIRED": "v1",
        "OPENAPI_HASH": ZERO_HASH64,
        "DB_MIGRATION_HEAD": "0245_settings_registry",
        "BUILD_TIME": "2026-05-20T12:34:56Z",
    }
    base.update(overrides)
    return base


def test_emit_produces_valid_json_with_required_top_level_fields() -> None:
    result = _run(_good_env())

    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    # AC #1: matches omnisight-bundle.schema.json's required keys.
    required = {"bundle_id", "git_ref", "git_sha", "build_time", "images", "contracts"}
    assert required.issubset(bundle.keys())
    assert set(bundle["images"]) == {"backend", "frontend", "bridge"}
    assert set(bundle["contracts"]) == {
        "api_required",
        "api_supported",
        "openapi_hash",
        "db_migration_head",
        "frontend_built_against_api",
    }


def test_bundle_id_follows_ac_format() -> None:
    """AC #1: bundle_id == '<git_ref_short>+<YYYYMMDD>.sha<short_sha>'."""
    result = _run(_good_env())
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)

    # git_ref tail = "v0.5.0-rc6"; YYYYMMDD = "20260520"; short SHA = first 7 of GOOD_SHA.
    expected = f"v0.5.0-rc6+20260520.sha{GOOD_SHA[:7]}"
    assert bundle["bundle_id"] == expected
    # Also assert the regex shape so future ref types still pass the
    # audit-emit guard.
    assert re.match(r"^.+\+\d{8}\.sha[0-9a-f]{7,}$", bundle["bundle_id"])


def test_emit_passes_through_real_image_digests() -> None:
    result = _run(_good_env())
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    assert bundle["images"]["backend"]["digest"] == DIGEST_A
    assert bundle["images"]["frontend"]["digest"] == DIGEST_B
    assert bundle["images"]["bridge"]["digest"] == DIGEST_C


def test_emit_accepts_placeholder_zero_digests_for_pre_build_stage() -> None:
    """The .gitlab-ci.yml prepare-bundle job must be able to emit before
    real digests exist; schema-valid placeholders are intentional."""
    result = _run(_good_env(
        IMAGE_DIGEST_BACKEND=ZERO_DIGEST,
        IMAGE_DIGEST_FRONTEND=ZERO_DIGEST,
        IMAGE_DIGEST_BRIDGE=ZERO_DIGEST,
    ))
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    # Identity must still be real even when digests are stub.
    assert bundle["bundle_id"] != "local-dev"
    assert bundle["git_sha"] == GOOD_SHA
    assert bundle["git_ref"] == "refs/tags/v0.5.0-rc6"


def test_emit_defaults_api_supported_and_frontend_built_against_api() -> None:
    env = _good_env()
    env.pop("API_SUPPORTED", None)
    env.pop("FRONTEND_BUILT_AGAINST_API", None)
    result = _run(env)

    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    assert bundle["contracts"]["api_supported"] == ["v1", "v2"]
    assert bundle["contracts"]["frontend_built_against_api"] == "v1"


def test_emit_respects_api_supported_override() -> None:
    result = _run(_good_env(API_SUPPORTED="v1,v2,v3"))
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    assert bundle["contracts"]["api_supported"] == ["v1", "v2", "v3"]


def test_emit_respects_bundle_id_override() -> None:
    result = _run(_good_env(BUNDLE_ID="explicit-bundle-id"))
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)
    assert bundle["bundle_id"] == "explicit-bundle-id"


@pytest.mark.parametrize(
    "missing",
    [
        "GIT_SHA",
        "GIT_REF",
        "IMAGE_DIGEST_BACKEND",
        "IMAGE_DIGEST_FRONTEND",
        "IMAGE_DIGEST_BRIDGE",
        "API_REQUIRED",
        "OPENAPI_HASH",
        "DB_MIGRATION_HEAD",
    ],
)
def test_emit_rejects_missing_required_env_var(missing: str) -> None:
    env = _good_env()
    del env[missing]
    # Also unset from inherited environment so the script truly sees blank.
    env[missing] = ""
    result = _run(env)
    assert result.returncode != 0
    assert missing in result.stderr


def test_emit_rejects_bad_git_sha() -> None:
    result = _run(_good_env(GIT_SHA="not-a-sha"))
    assert result.returncode != 0
    assert "GIT_SHA" in result.stderr


def test_emit_rejects_bad_image_digest() -> None:
    result = _run(_good_env(IMAGE_DIGEST_BACKEND="not-a-digest"))
    assert result.returncode != 0
    assert "IMAGE_DIGEST_BACKEND" in result.stderr


def test_emit_rejects_bad_openapi_hash() -> None:
    result = _run(_good_env(OPENAPI_HASH="deadbeef"))
    assert result.returncode != 0
    assert "OPENAPI_HASH" in result.stderr


def test_emit_matches_schema_required_fields() -> None:
    schema = json.loads(SCHEMA.read_text())
    result = _run(_good_env())
    assert result.returncode == 0, result.stderr
    bundle = json.loads(result.stdout)

    for key in schema["required"]:
        assert key in bundle, f"schema requires {key!r}"
    for key in schema["properties"]["contracts"]["required"]:
        assert key in bundle["contracts"], f"contracts requires {key!r}"
    for key in schema["properties"]["images"]["required"]:
        assert key in bundle["images"], f"images requires {key!r}"
    # git_sha pattern check (the schema requires 40-char lowercase hex).
    assert re.match(schema["properties"]["git_sha"]["pattern"], bundle["git_sha"])
