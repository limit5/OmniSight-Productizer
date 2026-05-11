"""OP-864 D2 — contract tests for the runner image build/sign/retention pipeline.

Pins the load-bearing invariants of:
  - `Dockerfile.runner`
  - `scripts/build_image.sh`
  - `scripts/gc_image_registry.sh`
  - `.github/workflows/image-build.yml`
  - `docs/operations/image-pipeline-runbook.md`

5-case test plan (see ticket OP-864 §Test plan):
  1. build sanity        — Dockerfile + build script shape + tag CLI works
  2. sign verifies       — cosign key path wired end-to-end
  3. registry push       — semver-tagged ref points at the OP-864 registry
  4. retention removes   — gc_image_registry.sh enforces last 30d OR last 20
  5. reproducibility     — SOURCE_DATE_EPOCH propagated through build args

No Docker / cosign / registry required — these are pure source-scan
contract tests.
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
DOCKERFILE = REPO_ROOT / "Dockerfile.runner"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_image.sh"
GC_SCRIPT = REPO_ROOT / "scripts" / "gc_image_registry.sh"
WF_PATH = REPO_ROOT / ".github" / "workflows" / "image-build.yml"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "image-pipeline-runbook.md"

EXPECTED_REGISTRY = "registry.sora.services:5000"
EXPECTED_IMAGE_PATH = "omnisight/runner"
EXPECTED_KEY_PATH = "/home/user/.config/omnisight/cosign-private-key"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WF_PATH.exists(), f"workflow file missing: {WF_PATH}"
    return yaml.safe_load(WF_PATH.read_text())


# ---------------------------------------------------------------------------
# (Case 1) build sanity — Dockerfile shape + CLI accepts --tag
# ---------------------------------------------------------------------------


def test_dockerfile_runner_is_multi_stage_alpine_python_312_nonroot() -> None:
    """AC #1: multi-stage Dockerfile, Python 3.12 + alpine + non-root user."""
    assert DOCKERFILE.exists(), f"missing: {DOCKERFILE}"
    body = DOCKERFILE.read_text()

    # Multi-stage — two FROM lines with named stages.
    from_lines = [ln for ln in body.splitlines() if ln.lstrip().startswith("FROM ")]
    assert len(from_lines) >= 2, "Dockerfile.runner must be multi-stage"
    assert any("AS builder" in ln for ln in from_lines), "missing 'AS builder' stage"
    assert any("AS runner" in ln for ln in from_lines), "missing 'AS runner' stage"

    # Python 3.12 + alpine base — both stages.
    assert all("python:3.12-alpine" in ln for ln in from_lines), \
        "all stages must use python:3.12-alpine"

    # Non-root user — uid 65532 added + `USER app:app`.
    assert "addgroup -g 65532" in body, "must create non-root group uid 65532"
    assert "adduser -u 65532" in body, "must create non-root user uid 65532"
    assert re.search(r"^\s*USER\s+app(:app)?\s*$", body, re.MULTILINE), \
        "Dockerfile must drop to USER app before CMD"


def test_build_image_script_exists_and_accepts_tag_flag() -> None:
    """AC #1 (CLI): `scripts/build_image.sh --tag <semver>` is the canonical entrypoint."""
    assert BUILD_SCRIPT.exists(), f"missing: {BUILD_SCRIPT}"
    # Must be executable — operators run it directly.
    mode = BUILD_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "build_image.sh must have +x"

    body = BUILD_SCRIPT.read_text()
    assert "--tag" in body, "script must accept --tag"

    # --help should not require docker / cosign — invoke it.
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--help"],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, \
        f"--help should exit 0, got {result.returncode}; stderr={result.stderr.decode()}"

    # Reject missing --tag with the documented error code 4 (bad invocation).
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 4, \
        f"empty invocation must exit 4 (bad invocation), got {result.returncode}"
    assert b"--tag" in result.stderr, "error message must mention --tag"


# ---------------------------------------------------------------------------
# (Case 2) sign verifies — cosign key path wired end-to-end
# ---------------------------------------------------------------------------


def test_build_script_signs_with_documented_key_path() -> None:
    """AC #2: cosign signs with key at /home/user/.config/omnisight/cosign-private-key."""
    body = BUILD_SCRIPT.read_text()

    # The exact path from the ticket spec must appear as the default.
    assert EXPECTED_KEY_PATH in body, \
        f"build_image.sh must default cosign key to {EXPECTED_KEY_PATH}"

    # cosign sign --key invocation must exist (key-based, not keyless).
    assert "cosign sign --key" in body, \
        "build_image.sh must invoke `cosign sign --key` (key-based signing)"

    # Missing-key path emits the documented exit code 2 (CosignSignFailed).
    assert "CosignSignFailed" in body, \
        "error catalog must reference CosignSignFailed by symbol"
    assert "exit 2" in body, "CosignSignFailed must exit with code 2"


def test_workflow_stages_cosign_key_and_invokes_build_script(workflow: dict) -> None:
    """AC #2 (CI): workflow stages the secret to the documented key path."""
    build_job = workflow["jobs"]["build"]
    step_runs = " ".join(s.get("run", "") for s in build_job["steps"])

    # Cosign installer step present.
    step_uses = [s.get("uses", "") for s in build_job["steps"]]
    assert any("sigstore/cosign-installer" in u for u in step_uses), \
        "workflow must install cosign"

    # build_image.sh is invoked with --tag.
    assert "scripts/build_image.sh --tag" in step_runs, \
        "workflow must call build_image.sh --tag"

    # Key is staged from a secret (SORA_COSIGN_KEY) — the exact secret
    # name is wire-protocol with the GH Actions secret store.
    body = WF_PATH.read_text()
    assert "SORA_COSIGN_KEY" in body, \
        "workflow must reference SORA_COSIGN_KEY secret"


# ---------------------------------------------------------------------------
# (Case 3) registry push — semver-tagged ref points at the OP-864 registry
# ---------------------------------------------------------------------------


def test_build_script_pushes_to_self_hosted_sora_registry() -> None:
    """AC #3: image lands at registry.sora.services:5000/omnisight/runner:<semver>."""
    body = BUILD_SCRIPT.read_text()

    # Default registry + image path must match the ticket exactly.
    assert EXPECTED_REGISTRY in body, \
        f"build_image.sh must default REGISTRY to {EXPECTED_REGISTRY}"
    assert EXPECTED_IMAGE_PATH in body, \
        f"build_image.sh must default IMAGE_PATH to {EXPECTED_IMAGE_PATH}"

    # `docker push` is invoked.
    assert "docker push" in body, "build_image.sh must docker push"

    # Both the semver ref AND the sha-shortref are pushed (the latter
    # is what verifiers pin to for immutability).
    assert "SEMVER_REF" in body, "semver-tagged ref must be a named var"
    assert "SHA_REF" in body, "sha-tagged ref must be a named var"

    # 401/403 path maps to RegistryPushUnauthorized + exit code 3.
    assert "RegistryPushUnauthorized" in body, \
        "error catalog must reference RegistryPushUnauthorized"
    assert "exit 3" in body, "RegistryPushUnauthorized must exit with code 3"


def test_workflow_logs_into_self_hosted_registry(workflow: dict) -> None:
    """AC #3 (CI): docker login points at registry.sora.services:5000."""
    build_job = workflow["jobs"]["build"]
    login_step = next(
        (s for s in build_job["steps"] if s.get("uses", "").startswith("docker/login-action")),
        None,
    )
    assert login_step is not None, "workflow must log into the self-hosted registry"
    # The `registry:` parameter resolves from env.REGISTRY via ${{ env.REGISTRY }}.
    body = WF_PATH.read_text()
    assert f"REGISTRY: {EXPECTED_REGISTRY}" in body, \
        f"workflow env.REGISTRY must be exactly '{EXPECTED_REGISTRY}'"


# ---------------------------------------------------------------------------
# (Case 4) retention removes — last 30 days OR last 20 (whichever more)
# ---------------------------------------------------------------------------


def test_gc_script_enforces_30d_or_20_tag_policy() -> None:
    """AC #4: retention keeps last MAX_AGE_DAYS days OR MIN_KEEP tags."""
    assert GC_SCRIPT.exists(), f"missing: {GC_SCRIPT}"
    mode = GC_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "gc_image_registry.sh must have +x"

    body = GC_SCRIPT.read_text()

    # Defaults must match the ticket spec exactly.
    assert 'MIN_KEEP="${MIN_KEEP:-20}"' in body, \
        "MIN_KEEP must default to 20 (last 20 tags floor)"
    assert 'MAX_AGE_DAYS="${MAX_AGE_DAYS:-30}"' in body, \
        "MAX_AGE_DAYS must default to 30 (last 30 days window)"

    # The "whichever more" semantics — floor + window are BOTH applied.
    assert "floor" in body.lower(), "policy must implement a MIN_KEEP floor"
    assert "cutoff" in body, "policy must implement a MAX_AGE_DAYS cutoff"

    # Deletion uses the v2 registry API.
    assert "/v2/" in body, "GC must use Registry HTTP API v2"
    assert re.search(r"-X\s+DELETE", body), \
        "GC must DELETE via the registry API"
    assert "/manifests/" in body, \
        "GC deletes manifests (not blobs — layer GC is registry-side)"

    # RetentionGCFailed is the error symbol on non-recoverable failure.
    assert "RetentionGCFailed" in body, \
        "error catalog must reference RetentionGCFailed"


def test_workflow_runs_retention_on_weekly_cron(workflow: dict) -> None:
    """AC #4 (CI): retention is cron'd weekly."""
    on = workflow.get("on") or workflow.get(True)  # PyYAML parses `on:` as True
    schedule = on.get("schedule") or []
    assert schedule, "workflow must declare a schedule trigger"
    crons = [s["cron"] for s in schedule]
    # Weekly == 5-part cron with DOW field. Reject hourly / daily.
    weekly = [c for c in crons if re.fullmatch(r"\d+\s+\d+\s+\*\s+\*\s+[0-6]", c)]
    assert weekly, f"need a weekly cron (DOW pinned), got {crons!r}"

    # Retention job exists, runs on schedule, and invokes the GC script.
    assert "retention" in workflow["jobs"], "workflow must define a retention job"
    retention = workflow["jobs"]["retention"]
    retention_runs = " ".join(s.get("run", "") for s in retention["steps"])
    assert "scripts/gc_image_registry.sh" in retention_runs, \
        "retention job must invoke gc_image_registry.sh"


# ---------------------------------------------------------------------------
# (Case 5) reproducibility — 2 builds at same git SHA → identical digest
# ---------------------------------------------------------------------------


def test_build_pins_source_date_epoch_for_reproducibility() -> None:
    """AC #5: SOURCE_DATE_EPOCH is pinned to commit time, propagated to docker build."""
    body = BUILD_SCRIPT.read_text()

    # Resolution: SOURCE_DATE_EPOCH derived from commit author time.
    assert "SOURCE_DATE_EPOCH" in body, "must pin SOURCE_DATE_EPOCH"
    assert "git show -s --format=%ct" in body, \
        "must derive SOURCE_DATE_EPOCH from commit author time"

    # Propagation: passed both as env (for buildkit) and --build-arg (for the Dockerfile).
    assert "--build-arg" in body and "SOURCE_DATE_EPOCH=" in body, \
        "must pass SOURCE_DATE_EPOCH as a build-arg"

    # BuildKit's timestamp rewrite is enabled.
    assert "rewrite-timestamp=true" in body, \
        "must pass rewrite-timestamp=true to BuildKit output"

    # Dockerfile accepts the arg in both stages so layer timestamps
    # use the pinned value, not wall clock.
    docker_body = DOCKERFILE.read_text()
    arg_lines = [ln for ln in docker_body.splitlines() if "ARG SOURCE_DATE_EPOCH" in ln]
    assert len(arg_lines) >= 2, \
        "Dockerfile must declare ARG SOURCE_DATE_EPOCH in BOTH stages"

    # Dockerfile sets PYTHONDONTWRITEBYTECODE — pyc mtimes are a common
    # nondeterminism source.
    assert "PYTHONDONTWRITEBYTECODE=1" in docker_body, \
        "Dockerfile must disable .pyc generation for reproducibility"


def test_runbook_documents_reproducibility_verification_procedure() -> None:
    """AC #5 (docs): runbook tells operators how to verify reproducibility."""
    assert RUNBOOK.exists(), f"missing: {RUNBOOK}"
    body = RUNBOOK.read_text()

    # Both ACs referenced in the runbook.
    assert "Reproducibility" in body, "runbook must have a Reproducibility section"
    assert "SOURCE_DATE_EPOCH" in body, "runbook must explain SOURCE_DATE_EPOCH"
    # Operator-facing verify-procedure (DIGEST1 == DIGEST2 check).
    assert "DIGEST1" in body and "DIGEST2" in body, \
        "runbook must show the 2-build digest-comparison procedure"


# ---------------------------------------------------------------------------
# Cross-cutting: error catalog is documented in the runbook
# ---------------------------------------------------------------------------


def test_runbook_error_catalog_covers_all_four_codes() -> None:
    body = RUNBOOK.read_text()
    for symbol in (
        "ImageBuildFailed",
        "CosignSignFailed",
        "RegistryPushUnauthorized",
        "RetentionGCFailed",
    ):
        assert symbol in body, f"runbook must document error code {symbol}"
