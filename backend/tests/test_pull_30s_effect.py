"""L10 #337 — capstone contract tests for the "pull 30 秒" effect.

The four prior L10 items each pin one precondition:

    #1 `.github/workflows/docker-publish.yml`  → tag push publishes
       `omnisight-{backend,frontend}` images to `ghcr.io` so they
       exist to be pulled.
    #2 `docker-compose.prod.yml` image-first   → both services
       declare `image: ghcr.io/...` + `pull_policy: missing` + keep
       `build:` as fallback, so `docker compose up` pulls first and
       only rebuilds when pull 404s.
    #3 Multi-arch build (amd64 + arm64)        → operators on
       Raspberry Pi / Apple-silicon CI resolve the same tag to an
       arm64 layer, not a forced rebuild.
    #4 Image size optimization                 → backend < 500 MiB /
       frontend < 200 MiB uncompressed; measured compressed pull
       sizes 103 MiB + 68 MiB ≈ 171 MiB combined.

This file pins the **emergent effect**: given all four preconditions
hold, first-deploy wall time is dominated by network transfer of
compressed layers and fits in ≤ 30 s at a 50 Mbps downlink (the FCC
"broadband served" floor since 2015). Without any one precondition
the effect silently decays back to "local build 5-10 分鐘":

    no publish        → `docker compose pull` 404s → local build
    not image-first   → compose ignores `image:` and always builds
    not multi-arch    → arm64 hosts bypass pull and emulate-build
    oversized layers  → even compressed, > 30 s at 50 Mbps

Two layers of defence (mirrors L10 #4's split):

    * Static contract tests — arithmetic + YAML/script scans. Run in
      every CI lane without Docker.
    * Live opt-in measurement — when OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1
      is set and slim images exist locally, derive wall-clock pull
      time from the same `{{.Size}}` signal used by L10 #4 and assert
      it stays under 30 s at 50 Mbps.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.prod.yml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
QUICK_START_PATH = REPO_ROOT / "scripts" / "quick-start.sh"


# ---------------------------------------------------------------------------
# Pull-budget arithmetic — the load-bearing math for the 30 s claim.
# ---------------------------------------------------------------------------

# 50 Mbps: the baseline FCC has tracked as "broadband served" since 2015 and
# the low-end of the "30 秒" promise. If the operator has more bandwidth
# the pull is faster; if they have less, they get a proportionally longer
# pull + the same no-local-build win over the 5-10 min baseline.
REFERENCE_BANDWIDTH_MBPS = 50

# The L10 #337 promise, verbatim from TODO.md / HANDOFF.md.
PULL_BUDGET_SECONDS = 30

# Network units are decimal (Mbps, MB/s), storage units are binary (MiB).
# Keeping them separate prevents the classic 1024 vs 1000 off-by-4-percent
# that makes an allegedly-fitting image "just barely" miss the 30 s
# promise on a real 50 Mbps link.
_BITS_PER_MB_NET = 1_000_000
_BYTES_PER_MIB = 1024 * 1024

BANDWIDTH_BYTES_PER_SEC = REFERENCE_BANDWIDTH_MBPS * _BITS_PER_MB_NET / 8
# ≈ 6_250_000 bytes/s = 5.96 MiB/s
PULL_BUDGET_BYTES = int(BANDWIDTH_BYTES_PER_SEC * PULL_BUDGET_SECONDS)
# ≈ 187_500_000 bytes = 178.8 MiB — the envelope any combined pull must fit.
PULL_BUDGET_MIB = PULL_BUDGET_BYTES / _BYTES_PER_MIB

# Per-image compressed-pull-size budgets. Measured baseline after L10 #4:
# backend 103 MiB, frontend 68 MiB (via `docker image inspect .Size` — the
# signal Docker reports for locally-stored layer bytes, which correlates
# closely with registry-transferred gzipped bytes for these specific
# images; combined 171 MiB). The 30 s × 50 Mbps envelope is 178.8 MiB —
# giving only ~7.8 MiB of real-world slack. Budgets carry modest
# headroom (backend +5, frontend +2) so a normal dependency bump still
# passes; a material regression (a dev venv leaking back in, or dropping
# the slim Alpine base) blows the combined envelope audibly.
BACKEND_COMPRESSED_BUDGET_MIB = 108
FRONTEND_COMPRESSED_BUDGET_MIB = 70


def test_pull_budget_arithmetic_is_internally_consistent() -> None:
    # Guard the math itself first — a refactor tweaking any constant
    # should surface here before cascading into the composite tests.
    expected_bps = REFERENCE_BANDWIDTH_MBPS * 1_000_000 / 8
    assert abs(BANDWIDTH_BYTES_PER_SEC - expected_bps) < 1, (
        "bandwidth conversion drifted — recheck Mbps→bytes/s math"
    )
    expected_envelope_bytes = expected_bps * PULL_BUDGET_SECONDS
    assert abs(PULL_BUDGET_BYTES - expected_envelope_bytes) < 1, (
        "pull envelope in bytes drifted — recheck seconds × bytes/s"
    )
    # Sanity: 30 s × 50 Mbps lands in the 150-200 MiB band. If this
    # assertion ever fires, someone moved a decimal point.
    assert 150 < PULL_BUDGET_MIB < 200, (
        f"PULL_BUDGET_MIB={PULL_BUDGET_MIB:.1f} is outside the expected "
        f"150-200 MiB band for 30 s × 50 Mbps"
    )


def test_combined_image_budgets_fit_the_30s_envelope() -> None:
    # THE load-bearing assertion for L10 #337's effect. If a future
    # PR pushes the per-image compressed budgets up without shrinking
    # the other, this fires and forces a deliberate "update the 30 秒
    # promise or re-optimize" decision.
    combined = BACKEND_COMPRESSED_BUDGET_MIB + FRONTEND_COMPRESSED_BUDGET_MIB
    assert combined <= PULL_BUDGET_MIB, (
        f"combined compressed pull budget {combined} MiB exceeds the "
        f"{PULL_BUDGET_MIB:.1f} MiB envelope at {REFERENCE_BANDWIDTH_MBPS} "
        f"Mbps × {PULL_BUDGET_SECONDS} s. Either shrink an image budget "
        f"(see L10 #4 slim-Dockerfile strategies) or update the "
        f"documented '30 秒' promise in TODO.md + HANDOFF.md."
    )


# ---------------------------------------------------------------------------
# Precondition: docker-compose.prod.yml actually uses the pull path.
# Without `image:` + `pull_policy: missing`, compose skips the network and
# goes straight to local build — the 30 s promise becomes unreachable.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def compose_yaml() -> dict:
    assert COMPOSE_PATH.exists(), f"missing: {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_pull_path_reachable_via_compose_image_first(compose_yaml: dict) -> None:
    services = compose_yaml.get("services", {})
    for svc_name in ("backend", "frontend"):
        svc = services.get(svc_name)
        assert svc is not None, f"compose missing required service: {svc_name}"
        image = svc.get("image", "")
        assert image.startswith("ghcr.io/"), (
            f"{svc_name} must declare `image: ghcr.io/...` — without it "
            f"compose never pulls and the 30 s promise is unreachable "
            f"(got: {image!r})"
        )
        # `pull_policy: missing` is the Compose default, but pinning it
        # prevents a "let's always pull latest" PR from quietly breaking
        # the 30 s promise (every run would re-pull) or a "let's always
        # rebuild for reproducibility" PR from killing it entirely.
        assert svc.get("pull_policy") == "missing", (
            f"{svc_name} must set pull_policy=missing — drift to "
            f"`always`/`build` defeats the 30 s first-deploy promise"
        )


# ---------------------------------------------------------------------------
# Precondition: quick-start.sh actually exercises the pull path.
# A stray `--build` flag forces local rebuild even when GHCR has the image.
# Prior test (test_compose_prod_image_first.py) scans for this too; pinning
# here as defence-in-depth for the specific 30 s effect because this is the
# single change most likely to silently regress the promise.
# ---------------------------------------------------------------------------

def test_quickstart_does_not_force_build_on_compose_up() -> None:
    assert QUICK_START_PATH.exists(), f"missing: {QUICK_START_PATH}"
    script = QUICK_START_PATH.read_text()
    # Only real command invocations matter — comments, echo lines
    # (user-facing copy), and heredoc content can legitimately mention
    # `--build` (documenting the escape hatch on line 1061).
    offending: list[str] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("echo ") or line.startswith("echo\t"):
            continue
        # Match the shape `docker compose ... up [flags]` — allowing
        # leading `if !` or piped `| tee` wrappers that currently
        # surround the call site (quick-start.sh:483).
        if "docker compose" not in line:
            continue
        if re.search(r"\bup\b", line) is None:
            continue
        if "--build" in line:
            offending.append(raw_line)
    assert not offending, (
        "quick-start.sh has a `docker compose ... up --build` invocation; "
        "that forces a local rebuild even when GHCR has the image and "
        "defeats the 30 s pull promise. Offending line(s):\n  "
        + "\n  ".join(offending)
    )


# ---------------------------------------------------------------------------
# Precondition: the publish workflow actually pushes. Without published
# images, there is nothing to pull; compose 404s and falls back to build.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def workflow_yaml() -> dict:
    assert WORKFLOW_PATH.exists(), f"missing: {WORKFLOW_PATH}"
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_workflow_actually_pushes_so_pull_is_possible(workflow_yaml: dict) -> None:
    # Find a `docker/build-push-action` step anywhere in the workflow
    # and assert `push: true`. A regression flipping `push: false` (for
    # "let's test the workflow without publishing") would let CI stay
    # green while silently breaking the 30 s promise in prod.
    pushes_found = 0
    for job_def in workflow_yaml.get("jobs", {}).values():
        for step in job_def.get("steps", []) or []:
            uses = step.get("uses", "") if isinstance(step, dict) else ""
            if uses.startswith("docker/build-push-action"):
                with_block = step.get("with", {}) or {}
                push_flag = with_block.get("push")
                # YAML booleans come through as Python True/False; a
                # stringified "true" would be equally valid so accept both.
                if push_flag is True or str(push_flag).lower() == "true":
                    pushes_found += 1
    assert pushes_found >= 1, (
        "docker-publish.yml must have at least one build-push-action "
        "step with `push: true` — without it, nothing reaches GHCR and "
        "the 30 s pull promise has no image to pull"
    )


# ---------------------------------------------------------------------------
# Live opt-in: given the slim images exist locally, derive the wall-clock
# pull time at 50 Mbps and assert it fits in 30 s. Skipped by default.
# ---------------------------------------------------------------------------

def _live_measurement_enabled() -> bool:
    return os.environ.get("OMNISIGHT_TEST_DOCKER_IMAGE_SIZE", "").lower() in (
        "1", "true", "yes", "on",
    )


def _docker_image_size_bytes(tag: str) -> int | None:
    """Same helper as test_dockerfile_image_size.py; returns the Docker
    `{{.Size}}` signal (sum of layer bytes on local disk) or None when
    the image / daemon isn't available."""
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Size}}", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


@pytest.mark.skipif(
    not _live_measurement_enabled(),
    reason="set OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1 to measure live pull time",
)
def test_live_combined_pull_under_30s_at_50mbps() -> None:
    backend = _docker_image_size_bytes("omnisight-productizer-backend:slim")
    frontend = _docker_image_size_bytes("omnisight-productizer-frontend:slim")
    if backend is None or frontend is None:
        pytest.skip(
            "both omnisight-productizer-{backend,frontend}:slim must exist "
            "locally to measure live pull time"
        )
    combined_bytes = backend + frontend
    seconds = combined_bytes / BANDWIDTH_BYTES_PER_SEC
    backend_mib = backend / _BYTES_PER_MIB
    frontend_mib = frontend / _BYTES_PER_MIB
    assert seconds <= PULL_BUDGET_SECONDS, (
        f"combined pull at {REFERENCE_BANDWIDTH_MBPS} Mbps would take "
        f"{seconds:.1f} s (backend={backend_mib:.1f} MiB, "
        f"frontend={frontend_mib:.1f} MiB) — over the "
        f"{PULL_BUDGET_SECONDS} s promise. Re-run L10 #4 optimizations "
        f"or adjust the documented bandwidth floor."
    )
