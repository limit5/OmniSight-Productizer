"""L10 #337 — contract tests for `.github/workflows/docker-publish.yml`.

Pins the structural promises the workflow makes to operators so that
an accidental edit (removed `packages: write` permission, stripped
`:latest` tag, wrong registry, etc.) fails CI before the next tag push
silently fails to publish anything.

No network / no Docker required: the workflow YAML is parsed with
``yaml.safe_load`` and its shape is asserted against a handful of
load-bearing invariants. Runs in <100 ms alongside the rest of the
backend test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW_PATH.exists(), f"workflow missing: {WORKFLOW_PATH}"
    return yaml.safe_load(WORKFLOW_PATH.read_text())


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

def test_triggered_on_tag_push(workflow: dict) -> None:
    # PyYAML turns the bareword `on:` into Python True (boolean). Accept
    # either spelling so the test isn't tripped by loader quirks.
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers is not None, "workflow must declare `on:` triggers"
    push = triggers.get("push") or {}
    tags = push.get("tags") or []
    assert "v*" in tags, "must publish on `v*` tag push (v-prefixed semver tags)"


def test_has_manual_dispatch_escape_hatch(workflow: dict) -> None:
    # Operator escape hatch for re-publishing a tag without forcing a
    # retag. Cheap to add and caught a real gap in prior iterations.
    triggers = workflow.get("on") or workflow.get(True)
    assert "workflow_dispatch" in triggers


# ---------------------------------------------------------------------------
# Permissions — packages:write is load-bearing for GHCR push
# ---------------------------------------------------------------------------

def test_grants_packages_write_permission(workflow: dict) -> None:
    perms = workflow.get("permissions") or {}
    assert perms.get("packages") == "write", (
        "GHCR push requires `packages: write` on the workflow-level "
        "permissions block — default GITHUB_TOKEN is read-only otherwise."
    )


def test_contents_permission_is_read_only(workflow: dict) -> None:
    # Least-privilege: the workflow never writes to the repo, only
    # reads sources. Demoting this from the default `write` reduces
    # blast radius if a build step is compromised.
    perms = workflow.get("permissions") or {}
    assert perms.get("contents") == "read"


# ---------------------------------------------------------------------------
# Matrix — backend, frontend, and BYOG proxy must ship
# ---------------------------------------------------------------------------

def _publish_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "publish" in jobs, "expected job `publish` in workflow"
    return jobs["publish"]


def test_matrix_covers_backend_frontend_and_proxy(workflow: dict) -> None:
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    includes = matrix.get("include") or []
    names = {entry["name"] for entry in includes}
    assert {"backend", "frontend", "proxy"} <= names, (
        "matrix must build all published images — L10 #337 requires "
        "backend/frontend and KS.3.1 requires the BYOG proxy "
        f"(found: {names})"
    )


def test_matrix_uses_repo_dockerfiles(workflow: dict) -> None:
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    includes = matrix.get("include") or []
    dockerfiles = {entry["dockerfile"] for entry in includes}
    assert dockerfiles == {
        "Dockerfile.backend",
        "Dockerfile.frontend",
        "Dockerfile.omnisight-proxy",
    }
    for df in dockerfiles:
        assert (REPO_ROOT / df).exists(), f"matrix references missing {df}"


# ---------------------------------------------------------------------------
# Steps — login + build-push + tagging contract
# ---------------------------------------------------------------------------

def _steps(workflow: dict) -> list[dict]:
    return _publish_job(workflow).get("steps") or []


def test_logs_in_to_ghcr(workflow: dict) -> None:
    steps = _steps(workflow)
    login = next(
        (s for s in steps if isinstance(s.get("uses"), str)
         and s["uses"].startswith("docker/login-action@")),
        None,
    )
    assert login is not None, "missing docker/login-action step"
    with_block = login.get("with") or {}
    registry = str(with_block.get("registry", ""))
    # Accept either the literal or a reference to env.REGISTRY — the
    # env block is asserted to be ghcr.io elsewhere.
    assert "ghcr.io" in registry or "env.REGISTRY" in registry, (
        "must log in to ghcr.io, not Docker Hub"
    )
    # GITHUB_TOKEN is scoped to the workflow run and avoids needing an
    # operator-managed PAT — regressing this would force one.
    assert "GITHUB_TOKEN" in str(with_block.get("password", ""))


def test_build_push_step_present(workflow: dict) -> None:
    steps = _steps(workflow)
    build_push = next(
        (s for s in steps if isinstance(s.get("uses"), str)
         and s["uses"].startswith("docker/build-push-action@")),
        None,
    )
    assert build_push is not None, "missing docker/build-push-action step"
    with_block = build_push.get("with") or {}
    assert with_block.get("push") is True, "build step must push (push: true)"


def test_tags_include_latest_and_version(workflow: dict) -> None:
    steps = _steps(workflow)
    build_push = next(
        s for s in steps if isinstance(s.get("uses"), str)
        and s["uses"].startswith("docker/build-push-action@")
    )
    tags = str(build_push["with"]["tags"])
    # Floating `:latest` tag is the explicit L10 #337 requirement.
    assert ":latest" in tags, "must publish `:latest` tag"
    # Immutable snapshot tag keyed off the resolved git tag, so old
    # releases remain pullable even after `:latest` advances.
    assert "${{ steps.tag.outputs.value }}" in tags


def test_image_names_are_omnisight_backend_frontend_and_proxy(workflow: dict) -> None:
    steps = _steps(workflow)
    build_push = next(
        s for s in steps if isinstance(s.get("uses"), str)
        and s["uses"].startswith("docker/build-push-action@")
    )
    tags = str(build_push["with"]["tags"])
    # Image names must appear in the template — L10 #337 pins
    # backend/frontend; KS.3.1 adds the customer-side proxy.
    assert "omnisight-backend" in tags or "${{ matrix.image }}" in tags
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    images = {entry["image"] for entry in matrix.get("include", [])}
    assert images == {"omnisight-backend", "omnisight-frontend", "omnisight-proxy"}


def test_uses_ghcr_registry(workflow: dict) -> None:
    env = workflow.get("env") or {}
    assert env.get("REGISTRY") == "ghcr.io", (
        "public contract is ghcr.io/<owner>/omnisight-{backend,frontend,proxy}"
    )


def test_owner_is_lowercased(workflow: dict) -> None:
    # GHCR rejects mixed-case namespaces. Mirrors a gotcha that would
    # only surface at tag push time for orgs with uppercase handles.
    steps = _steps(workflow)
    tr_step = next(
        (s for s in steps if "tr '[:upper:]' '[:lower:]'" in str(s.get("run", ""))),
        None,
    )
    assert tr_step is not None, (
        "workflow must lowercase github.repository_owner before using "
        "it as a GHCR namespace"
    )


# ---------------------------------------------------------------------------
# Multi-arch (L10 #337 item 3) — linux/amd64 + linux/arm64 via buildx
# ---------------------------------------------------------------------------

def _build_push_step(workflow: dict) -> dict:
    return next(
        s for s in _steps(workflow)
        if isinstance(s.get("uses"), str)
        and s["uses"].startswith("docker/build-push-action@")
    )


def test_qemu_action_present_for_arm64_emulation(workflow: dict) -> None:
    # GitHub-hosted runners are amd64; arm64 layers must be built
    # under binfmt_misc emulation via docker/setup-qemu-action.
    # Without this step, `platforms: linux/arm64` would fail with
    # "exec format error" on the first arm64 RUN.
    steps = _steps(workflow)
    qemu = next(
        (s for s in steps if isinstance(s.get("uses"), str)
         and s["uses"].startswith("docker/setup-qemu-action@")),
        None,
    )
    assert qemu is not None, (
        "multi-arch build requires docker/setup-qemu-action to "
        "register arm64 binfmt_misc handlers on amd64 runners"
    )
    with_block = qemu.get("with") or {}
    # Accept either an explicit linux/arm64 scope or the "all" default
    # (action v3 without a `with` block registers every handler).
    platforms = str(with_block.get("platforms", "")) or "all"
    assert "arm64" in platforms or platforms == "all", (
        f"QEMU must cover arm64 (got platforms={platforms!r})"
    )


def test_qemu_runs_before_buildx(workflow: dict) -> None:
    # Ordering matters: buildx needs binfmt_misc registered before
    # it initializes its builder, otherwise the builder caches
    # amd64-only capabilities and arm64 builds fail at runtime.
    steps = _steps(workflow)

    def _index(prefix: str) -> int:
        for i, s in enumerate(steps):
            if isinstance(s.get("uses"), str) and s["uses"].startswith(prefix):
                return i
        return -1

    qemu_idx = _index("docker/setup-qemu-action@")
    buildx_idx = _index("docker/setup-buildx-action@")
    assert qemu_idx >= 0 and buildx_idx >= 0, "both steps must exist"
    assert qemu_idx < buildx_idx, (
        "docker/setup-qemu-action must run before docker/setup-buildx-action"
    )


def test_build_push_publishes_amd64_and_arm64(workflow: dict) -> None:
    # The concrete L10 #337 checkbox promise: one pulled tag must
    # resolve to the right layer on both x86_64 cloud VMs and ARM
    # SBC / Apple-silicon hosts. Pin the two-platform list so that
    # a regression (e.g. dropping to amd64-only for speed) fails CI.
    with_block = _build_push_step(workflow).get("with") or {}
    platforms = str(with_block.get("platforms", ""))
    assert "linux/amd64" in platforms, (
        "multi-arch build must include linux/amd64 for cloud VMs"
    )
    assert "linux/arm64" in platforms, (
        "multi-arch build must include linux/arm64 for ARM SBC / "
        "Apple-silicon hosts — dropping this defeats the L10 #337 "
        "multi-arch contract"
    )


def test_timeout_accommodates_emulated_arm64_build(workflow: dict) -> None:
    # QEMU-emulated arm64 build roughly doubles wall time versus
    # native amd64. The default 45 min won't cover the frontend
    # (pnpm install + Next.js build) reliably — bumped to 90 min.
    timeout = _publish_job(workflow).get("timeout-minutes")
    assert isinstance(timeout, int), "timeout-minutes must be declared"
    assert timeout >= 60, (
        f"timeout-minutes={timeout} is too tight for QEMU-emulated "
        "arm64 frontend build — expect 60+ min"
    )
