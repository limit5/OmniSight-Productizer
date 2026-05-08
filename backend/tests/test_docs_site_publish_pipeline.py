"""OP-792 contract tests for the docs-site publish pipeline."""

from __future__ import annotations

from pathlib import Path

import yaml

from backend import docs_static_site

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docs-site-publish.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "operations" / "docs-site-pipeline.md"


def _workflow() -> dict:
    assert WORKFLOW_PATH.exists(), f"workflow missing: {WORKFLOW_PATH}"
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True)


def _run_blocks(job: dict) -> str:
    return "\n".join(
        step.get("run", "") + "\n" + str(step.get("with", {}))
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_workflow_triggers_on_develop_push() -> None:
    push = _triggers(_workflow())["push"]

    assert push["branches"] == ["develop"]


def test_build_installs_framework_deps_and_builds_static_site() -> None:
    build = _workflow()["jobs"]["build"]
    runs = _run_blocks(build)

    assert build["timeout-minutes"] == 5
    assert "pnpm install --frozen-lockfile --prefer-offline" in runs
    assert "python -m backend.docs_static_site --out docs-site-dist" in runs
    assert "actions/upload-pages-artifact@v3" in str(build["steps"])
    assert "path': 'docs-site-dist" in runs or "docs-site-dist" in runs


def test_incremental_cache_reuses_previous_docs_artifact() -> None:
    steps = _workflow()["jobs"]["build"]["steps"]
    cache = next(step for step in steps if step.get("uses") == "actions/cache@v4")
    with_block = cache["with"]

    assert with_block["path"] == "docs-site-dist"
    assert "hashFiles('docs/**/*.md'" in with_block["key"]
    assert "docs-site-${{ github.ref_name }}-" in with_block["restore-keys"]


def test_deploy_only_runs_after_successful_build() -> None:
    jobs = _workflow()["jobs"]
    deploy = jobs["deploy"]

    assert deploy["needs"] == "build"
    assert "actions/deploy-pages@v4" in str(deploy["steps"])
    assert jobs["notify-operator"]["needs"] == "build"
    assert jobs["notify-operator"]["if"] == "${{ failure() }}"


def test_pages_permissions_are_least_privilege_for_publish() -> None:
    perms = _workflow()["permissions"]

    assert perms["contents"] == "read"
    assert perms["pages"] == "write"
    assert perms["id-token"] == "write"


def test_static_builder_outputs_core_pages(tmp_path: Path) -> None:
    count, out_dir = docs_static_site.build(tmp_path)

    assert count >= 35
    assert out_dir == tmp_path
    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "docs" / "adr" / "index.html").exists()
    assert (tmp_path / "docs" / "sop" / "lessons" / "index.html").exists()
    assert (tmp_path / "docs" / "operator" / "en" / "index.html").exists()
    assert "Architecture Decision Records" in (
        tmp_path / "docs" / "adr" / "index.html"
    ).read_text(encoding="utf-8")


def test_runbook_documents_failure_and_time_contract() -> None:
    body = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "push to `develop`" in body
    assert "5-minute timeout" in body
    assert "previous successful Pages deployment remains live" in body
    assert "notify-operator" in body
