"""OP-763 D2 — contract tests for the image build/sign/retention pipeline.

Pins the load-bearing invariants of `.github/workflows/build-images.yml`,
the bridge Dockerfile, the cosign public-key placeholder, the verifier
script, and the retention script. Failures here mean a future edit
silently broke an OP-763 acceptance criterion.

No network / no Docker / no cosign installation required: parses YAML +
scans script source + runs the verifier in --help mode only.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WF_PATH = REPO_ROOT / ".github" / "workflows" / "build-images.yml"
BRIDGE_DOCKERFILE = REPO_ROOT / "Dockerfile.bridge"
COSIGN_PUB = REPO_ROOT / "deploy" / "cosign" / "cosign.pub"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_image_signature.sh"
RETENTION_SCRIPT = REPO_ROOT / "scripts" / "enforce_image_retention.sh"
DOCS_PATH = REPO_ROOT / "docs" / "operations" / "release-image-pipeline.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WF_PATH.exists(), f"workflow file missing: {WF_PATH}"
    return yaml.safe_load(WF_PATH.read_text())


# ---------------------------------------------------------------------------
# (AC 1) workflow builds backend + frontend + bridge on main push
# ---------------------------------------------------------------------------


def test_workflow_triggers_include_main_push_and_tags(workflow: dict) -> None:
    # PyYAML parses the YAML key `on:` as the bool True (because `on` is
    # a YAML 1.1 keyword). Accept either spelling.
    on = workflow.get("on") or workflow.get(True)
    assert on is not None, "workflow has no `on:` triggers"
    push = on["push"]
    assert "main" in push["branches"], "must trigger on main pushes"
    assert any(p.startswith("v") for p in push["tags"]), "must trigger on v* tags"


def test_workflow_runs_weekly_cron_for_retention(workflow: dict) -> None:
    on = workflow.get("on") or workflow.get(True)
    schedule = on.get("schedule") or []
    assert schedule, "retention requires a scheduled trigger"
    crons = [s["cron"] for s in schedule]
    assert any("* *" in c for c in crons), "scheduled trigger must be cron-like"


def test_build_matrix_covers_backend_frontend_bridge(workflow: dict) -> None:
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    names = {entry["name"] for entry in matrix}
    assert names == {"backend", "frontend", "bridge"}
    images = {entry["image"] for entry in matrix}
    assert images == {"omnisight-backend", "omnisight-frontend", "omnisight-bridge"}
    dockerfiles = {entry["dockerfile"] for entry in matrix}
    assert dockerfiles == {"Dockerfile.backend", "Dockerfile.frontend", "Dockerfile.bridge"}


# ---------------------------------------------------------------------------
# (AC 2) images tagged with <git-sha> AND <vX.Y.Z> if release tag
# ---------------------------------------------------------------------------


def test_workflow_emits_sha_tag_always_and_version_tag_on_releases() -> None:
    body = WF_PATH.read_text()
    # The "derive tags" step computes `:sha-<short_sha>` for every run
    # and additional `:vX.Y.Z` + `:latest` tags only when GITHUB_REF
    # starts with `refs/tags/v*`. Pin both.
    assert ":sha-${short_sha}" in body, "sha-tag derivation missing"
    assert "refs/tags/v*" in body, "release-tag branch missing"
    assert ":${version}" in body and ":latest" in body, "version + latest tags missing"


# ---------------------------------------------------------------------------
# (AC 3) cosign signature attached to each image
# ---------------------------------------------------------------------------


def test_workflow_installs_cosign_and_signs_each_image(workflow: dict) -> None:
    steps = workflow["jobs"]["build"]["steps"]
    step_names = [s.get("name", "") for s in steps]
    assert any("cosign" in n.lower() and "install" in n.lower() for n in step_names), \
        "cosign installer step missing"
    sign_steps = [s for s in steps if "sign" in s.get("name", "").lower() and "keyless" in s.get("name", "").lower()]
    assert sign_steps, "keyless sign step missing"
    sign_body = sign_steps[0].get("run", "")
    assert "cosign sign" in sign_body, "cosign sign command missing"
    # We MUST sign by digest, not by mutable tag — pin this invariant.
    assert "@${digest}" in sign_body, "must sign by digest, not by tag"


def test_workflow_grants_id_token_for_keyless_signing(workflow: dict) -> None:
    perms = workflow["permissions"]
    assert perms.get("id-token") == "write", \
        "cosign keyless requires id-token: write at workflow level"
    assert perms.get("packages") == "write", "GHCR push requires packages: write"


# ---------------------------------------------------------------------------
# (AC 4) retention policy enforced
# ---------------------------------------------------------------------------


def test_retention_job_present_and_calls_retention_script(workflow: dict) -> None:
    jobs = workflow["jobs"]
    assert "retention" in jobs, "retention job missing"
    retention = jobs["retention"]
    matrix = retention["strategy"]["matrix"]["package"]
    assert set(matrix) == {"omnisight-backend", "omnisight-frontend", "omnisight-bridge"}
    runs = " ".join(s.get("run", "") for s in retention["steps"] if isinstance(s, dict))
    assert "scripts/enforce_image_retention.sh" in runs, \
        "retention job must invoke enforce_image_retention.sh"


def test_retention_script_pins_min_keep_and_max_age() -> None:
    assert RETENTION_SCRIPT.exists()
    body = RETENTION_SCRIPT.read_text()
    # Defaults are the OP-763 spec: 20 / 30. They can be overridden
    # via env, but the defaults must match the spec.
    assert 'MIN_KEEP="${MIN_KEEP:-20}"' in body, "MIN_KEEP default must be 20 (spec floor)"
    assert 'MAX_AGE_DAYS="${MAX_AGE_DAYS:-30}"' in body, "MAX_AGE_DAYS default must be 30 (spec)"
    # Tagged images must never be touched.
    assert "(.tags | length) == 0" in body, \
        "retention script must filter to untagged-only — tagged images are forever"


# ---------------------------------------------------------------------------
# (AC 5) verify_image_signature.sh exists, executable, returns OK/FAIL
# ---------------------------------------------------------------------------


def test_verify_script_exists_and_is_executable() -> None:
    assert VERIFY_SCRIPT.exists()
    mode = VERIFY_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "verify_image_signature.sh must be executable"


def test_verify_script_help_mode_returns_2_with_usage() -> None:
    # AC contract: callable, has self-documenting --help, exits non-zero
    # on bad invocation. Exit 2 is reserved for usage / missing-tool;
    # exit 1 is reserved for the FAIL outcome on an unsigned image.
    result = subprocess.run(
        [str(VERIFY_SCRIPT), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr or "Usage:" in result.stdout


def test_verify_script_emits_OK_string_only_in_success_paths() -> None:
    body = VERIFY_SCRIPT.read_text()
    # Two success branches: keyless verify and key-based verify. Both
    # must echo "OK" + exit 0. FAIL paths must echo "FAIL" + exit 1.
    assert body.count('echo "OK"') >= 2, \
        "both keyless and key-based success branches must echo OK"
    assert 'echo "FAIL' in body, "FAIL output missing"
    # The fingerprint of the placeholder must be detected so we don't
    # try to use the placeholder as a real PEM key.
    assert "BEGIN PUBLIC KEY" in body, "key auto-detection probe missing"


def test_verify_script_handles_missing_cosign_gracefully(tmp_path: Path) -> None:
    # Run the script with a PATH that has bash + jq but NOT cosign.
    # Symlinking only what we need lets us keep the script invokable
    # while making `command -v cosign` return false.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("bash", "sh", "env", "head", "grep", "dirname", "tr", "cut", "date"):
        src = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        if src:
            (fake_bin / tool).symlink_to(src)
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = str(fake_bin)
    result = subprocess.run(
        [str(VERIFY_SCRIPT), "ghcr.io/example/img:tag"],
        capture_output=True, text=True, check=False, env=env,
    )
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}: {result.stderr}"
    assert "cosign" in result.stderr.lower()


# ---------------------------------------------------------------------------
# (AC 6) synthetic test — verifier job inside the same workflow
# ---------------------------------------------------------------------------


def test_workflow_has_post_build_verify_job(workflow: dict) -> None:
    jobs = workflow["jobs"]
    assert "verify" in jobs, \
        "synthetic post-build verify job missing — AC requires that signed images are retrievable"
    verify = jobs["verify"]
    assert verify.get("needs") == "build", "verify must depend on build"
    matrix = verify["strategy"]["matrix"]["image"]
    assert set(matrix) == {"omnisight-backend", "omnisight-frontend", "omnisight-bridge"}
    runs = " ".join(s.get("run", "") for s in verify["steps"] if isinstance(s, dict))
    assert "scripts/verify_image_signature.sh" in runs


# ---------------------------------------------------------------------------
# Required-files inventory (Files section of the ticket)
# ---------------------------------------------------------------------------


def test_all_op763_required_files_present() -> None:
    required = [
        WF_PATH,
        COSIGN_PUB,
        VERIFY_SCRIPT,
        DOCS_PATH,
        BRIDGE_DOCKERFILE,
        RETENTION_SCRIPT,
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.exists()]
    assert not missing, f"OP-763 files missing: {missing}"


def test_cosign_pub_is_placeholder_until_offline_mode() -> None:
    # The shipped file must be the placeholder — populating it
    # accidentally would silently switch the verifier to key-based
    # mode without any signing being done with that key.
    body = COSIGN_PUB.read_text()
    assert "COSIGN-PLACEHOLDER" in body, \
        "cosign.pub must be the placeholder; replace with a real key only when switching to offline mode"


# ---------------------------------------------------------------------------
# Bridge Dockerfile sanity
# ---------------------------------------------------------------------------


def test_bridge_dockerfile_packages_hardware_daemon() -> None:
    body = BRIDGE_DOCKERFILE.read_text()
    assert "tools/hardware_daemon" in body, \
        "bridge image must package tools/hardware_daemon"
    assert "tools.hardware_daemon.app:app" in body, \
        "bridge image must launch the daemon app via uvicorn"
    # Non-root runtime — matches Dockerfile.backend's H4 audit decision.
    assert "USER app" in body, "bridge image must drop to non-root"


# ---------------------------------------------------------------------------
# Audit / D18 metadata structure
# ---------------------------------------------------------------------------


def test_workflow_emits_audit_metadata_artifact(workflow: dict) -> None:
    steps = workflow["jobs"]["build"]["steps"]
    audit_step = next((s for s in steps if "audit metadata" in s.get("name", "").lower() and "emit" in s.get("name", "").lower()), None)
    assert audit_step is not None, "audit metadata emission step missing"
    body = audit_step.get("run", "")
    for required_field in (
        '"event": "image.signed_pushed"',
        '"digest"', '"git_sha"', '"git_ref"', '"signed_with": "cosign-keyless"',
    ):
        assert required_field in body, f"audit JSON missing field: {required_field}"

    upload = next((s for s in steps if "upload" in s.get("name", "").lower() and "audit" in s.get("name", "").lower()), None)
    assert upload is not None, "upload-artifact step for audit JSON missing"
    assert "actions/upload-artifact" in upload["uses"]
