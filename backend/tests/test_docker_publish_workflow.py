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
# Matrix — both backend and frontend must ship
# ---------------------------------------------------------------------------

def _publish_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "publish" in jobs, "expected job `publish` in workflow"
    return jobs["publish"]


def test_matrix_covers_backend_and_frontend(workflow: dict) -> None:
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    includes = matrix.get("include") or []
    names = {entry["name"] for entry in includes}
    assert {"backend", "frontend"} <= names, (
        "matrix must build both images — L10 #337 requires parity "
        f"between backend and frontend (found: {names})"
    )


def test_matrix_uses_repo_dockerfiles(workflow: dict) -> None:
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    includes = matrix.get("include") or []
    dockerfiles = {entry["dockerfile"] for entry in includes}
    assert dockerfiles == {"Dockerfile.backend", "Dockerfile.frontend"}
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


def test_image_names_are_omnisight_backend_and_frontend(workflow: dict) -> None:
    steps = _steps(workflow)
    build_push = next(
        s for s in steps if isinstance(s.get("uses"), str)
        and s["uses"].startswith("docker/build-push-action@")
    )
    tags = str(build_push["with"]["tags"])
    # Both image names must appear in the template — L10 #337 pins
    # these as the public contract.
    assert "omnisight-backend" in tags or "${{ matrix.image }}" in tags
    matrix = _publish_job(workflow).get("strategy", {}).get("matrix", {})
    images = {entry["image"] for entry in matrix.get("include", [])}
    assert images == {"omnisight-backend", "omnisight-frontend"}


def test_uses_ghcr_registry(workflow: dict) -> None:
    env = workflow.get("env") or {}
    assert env.get("REGISTRY") == "ghcr.io", (
        "public contract is ghcr.io/<owner>/omnisight-{backend,frontend}"
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
