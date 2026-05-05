"""BP.W3.14 — CI stale frontend bundle detector contract."""

from __future__ import annotations

from pathlib import Path
import subprocess

from scripts import check_frontend_stale as detector


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"edit {path}"], cwd=repo, check=True)
    return _git(repo, "rev-parse", "HEAD")


def test_detector_fails_when_frontend_commits_exceed_threshold(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True)

    base = _commit(tmp_path, "README.md", "base\n")
    for idx in range(6):
        _commit(tmp_path, f"app/page-{idx}.tsx", f"export default {idx}\n")

    assert detector.main([
        "--repo", str(tmp_path),
        "--head-ref", "HEAD",
        "--deploy-commit", base,
        "--threshold", "5",
    ]) == 1


def test_detector_passes_when_frontend_commits_within_threshold(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True)

    base = _commit(tmp_path, "README.md", "base\n")
    _commit(tmp_path, "backend/only.py", "print('backend')\n")
    _commit(tmp_path, "components/card.tsx", "export const card = true\n")

    assert detector.main([
        "--repo", str(tmp_path),
        "--head-ref", "HEAD",
        "--deploy-commit", base,
        "--threshold", "5",
    ]) == 0
