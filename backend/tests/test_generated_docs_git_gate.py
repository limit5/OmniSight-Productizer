"""OP-790 generated docs git hygiene contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_DOC_PATHS = (
    "docs/adr/README.md",
    "docs/sop/lessons-learned.md",
    "docs/status/handoff_status.yaml",
)


def _pre_commit_hook() -> dict:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] == "omnisight-generated-docs-not-staged":
                return hook
    raise AssertionError("omnisight-generated-docs-not-staged hook missing")


def test_generated_docs_are_ignored_and_untracked() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for path in GENERATED_DOC_PATHS:
        assert path in gitignore

    proc = subprocess.run(
        ["git", "ls-files", "--", *GENERATED_DOC_PATHS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == ""


def test_pre_commit_gate_names_all_generated_docs() -> None:
    hook = _pre_commit_hook()
    entry = hook["entry"]

    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["always_run"] is True
    for path in GENERATED_DOC_PATHS:
        assert path in entry


def test_ci_gate_rejects_legacy_lessons_index() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = workflow["jobs"]["lessons-index-git-gate"]
    run_blocks = "\n".join(
        step.get("run", "")
        for step in job["steps"]
        if isinstance(step, dict)
    )

    assert "git ls-files --error-unmatch docs/sop/lessons-learned.md" in run_blocks
    assert "must not be committed" in run_blocks


def test_pre_commit_gate_blocks_readding_generated_docs(tmp_path: Path) -> None:
    hook = _pre_commit_hook()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

    blocked = repo / "docs" / "sop" / "lessons-learned.md"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("generated\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/sop/lessons-learned.md"], cwd=repo, check=True)

    proc = subprocess.run(
        hook["entry"],
        cwd=repo,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "Generated files must not be staged:" in proc.stderr
    assert "docs/sop/lessons-learned.md" in proc.stderr
