"""OP-726 — drift scanner test suite.

Three acceptance-criteria scenarios from the ticket are encoded as
explicit tests:

  AC1. Pin docker image to old SHA, push new commit to develop →
       next scan fires DEGRADED with code='image_drift'.
  AC2. Hand-edit refs/meta/config without updating sample → alert fires.
  AC3. No drift → scan exits 0 with INFO log, no alert.

Plus narrower unit-level checks for each kind so a regression in one
check (e.g. schema, main, bridge) is still caught individually.

The scanner is exercised as a subprocess so tests cover the public CLI
contract — argparse parsing, exit codes, stderr/stdout shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "drift_scanner.py"


# ─────────────────────────────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = os.environ.copy()
    # Deterministic identity so commit hashes are reproducible enough
    # that a failure prints a hash, not a "your name is missing" error.
    full_env.update({
        "GIT_AUTHOR_NAME": "drift-test",
        "GIT_AUTHOR_EMAIL": "drift-test@example.com",
        "GIT_COMMITTER_NAME": "drift-test",
        "GIT_COMMITTER_EMAIL": "drift-test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    })
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=full_env,
    )
    return proc.stdout.strip()


def _make_alembic_layout(repo: Path, head_revision: str = "0202") -> Path:
    """Create a minimal alembic versions tree where ``head_revision`` is
    the leaf migration."""
    versions = repo / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    # Two-step chain: 0201 → head_revision. Just enough to exercise
    # _collect_migration_revisions's leaf detection.
    (versions / "0201_seed.py").write_text(
        'revision = "0201"\ndown_revision = None\n', encoding="utf-8",
    )
    (versions / f"{head_revision}_head.py").write_text(
        f'revision = "{head_revision}"\ndown_revision = "0201"\n',
        encoding="utf-8",
    )
    return versions


def _make_gerrit_sample(repo: Path, body: str) -> Path:
    gerrit = repo / ".gerrit"
    gerrit.mkdir()
    (gerrit / "project.config.example").write_text(body, encoding="utf-8")
    return gerrit


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A tmp git repo with `develop` and `main` branches and the same
    layout the scanner expects (``backend/alembic/versions/``,
    ``.gerrit/project.config.example``)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _make_alembic_layout(repo)
    _make_gerrit_sample(repo, "[access \"refs/*\"]\n  read = group Anonymous Users\n")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    # Create develop branch pointing at the seed commit, plus an
    # `origin` remote backed by a bare clone so `origin/develop` and
    # `origin/main` resolve.
    _git(repo, "branch", "develop")
    bare = tmp_path / "remote.git"
    _git(repo, "clone", "--bare", "-q", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "-q", "origin")
    return repo


def _run_scanner(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    """Invoke the scanner against `repo` and return the completed process."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *extra],
        capture_output=True, text=True, check=False,
    )


# ─────────────────────────────────────────────────────────────────────
# AC1 — image_drift
# ─────────────────────────────────────────────────────────────────────


def test_ac1_image_pin_falls_behind_new_develop_commit(fake_repo: Path) -> None:
    """AC1: pin image to old SHA, push new commit to develop, scan
    fires DEGRADED with code='image_drift'."""
    # Snapshot the current develop SHA — this is the "old" SHA the
    # operator pinned the image to.
    old_sha = _git(fake_repo, "rev-parse", "develop")

    # Land a new commit on develop and push to origin so origin/develop
    # advances. The deployed image (still pinned to old_sha) is now stale.
    _git(fake_repo, "checkout", "-q", "develop")
    (fake_repo / "feature.txt").write_text("new\n", encoding="utf-8")
    _git(fake_repo, "add", "feature.txt")
    _git(fake_repo, "commit", "-q", "-m", "feature commit")
    _git(fake_repo, "push", "-q", "origin", "develop")
    _git(fake_repo, "fetch", "-q", "origin")
    _git(fake_repo, "checkout", "-q", "main")

    proc = _run_scanner(
        fake_repo,
        "--image-revision", old_sha,
        "--skip", "schema_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 1, (
        f"expected exit 1 (drift), got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "code=image_drift" in proc.stderr
    assert "DEGRADED" in proc.stderr


def test_image_drift_clean_when_pin_matches_develop(fake_repo: Path) -> None:
    """Sanity inverse of AC1: when the pinned image SHA equals
    origin/develop HEAD, no drift fires."""
    sha = _git(fake_repo, "rev-parse", "develop")
    proc = _run_scanner(
        fake_repo,
        "--image-revision", sha,
        "--skip", "schema_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 0, proc.stderr
    assert "DEGRADED" not in proc.stderr
    assert "code=image_drift severity=INFO" in proc.stderr


# ─────────────────────────────────────────────────────────────────────
# AC2 — refs_meta_config_drift
# ─────────────────────────────────────────────────────────────────────


def test_ac2_remote_config_diverges_from_sample(
    fake_repo: Path, tmp_path: Path,
) -> None:
    """AC2: hand-edit refs/meta/config without updating
    .gerrit/project.config.example → alert fires."""
    live = tmp_path / "live-meta-config"
    live.mkdir()
    # Same filename as the sample but with extra ACL entry — operator
    # forgot to copy it back to the sample.
    (live / "project.config").write_text(
        "[access \"refs/*\"]\n"
        "  read = group Anonymous Users\n"
        "  read = group Registered Users\n",  # ← extra rule, not in sample
        encoding="utf-8",
    )

    proc = _run_scanner(
        fake_repo,
        "--remote-config-path", str(live),
        "--skip", "image_drift",
        "--skip", "schema_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 1, proc.stderr
    assert "code=refs_meta_config_drift" in proc.stderr
    assert "DEGRADED" in proc.stderr


def test_refs_meta_config_clean_when_normalised_match(
    fake_repo: Path, tmp_path: Path,
) -> None:
    """Sample and live with cosmetic differences (comments, whitespace)
    must NOT alert — _normalise_config strips those."""
    live_file = tmp_path / "project.config"
    live_file.write_text(
        "; deployed copy with a comment\n"
        "\n"
        "[access \"refs/*\"]\n"
        "  read = group Anonymous Users  ; inline comment\n",
        encoding="utf-8",
    )
    proc = _run_scanner(
        fake_repo,
        "--remote-config-path", str(live_file),
        "--skip", "image_drift",
        "--skip", "schema_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 0, proc.stderr
    assert "DEGRADED" not in proc.stderr


# ─────────────────────────────────────────────────────────────────────
# AC3 — no drift, exit 0
# ─────────────────────────────────────────────────────────────────────


def test_ac3_no_drift_exits_zero_with_info_only(
    fake_repo: Path, tmp_path: Path,
) -> None:
    """AC3: when every input matches expected, scanner exits 0 with
    INFO logs and no DEGRADED line."""
    sha = _git(fake_repo, "rev-parse", "develop")

    # Live refs/meta/config matches the sample (.gerrit/project.config.example).
    sample = (fake_repo / ".gerrit" / "project.config.example").read_text(
        encoding="utf-8",
    )
    live = tmp_path / "project.config"
    live.write_text(sample, encoding="utf-8")

    proc = _run_scanner(
        fake_repo,
        "--image-revision", sha,
        "--db-version", "0202",  # matches alembic head
        "--remote-config-path", str(live),
        "--bridge-revision", sha,  # bridge also at develop HEAD
        # main is identical to origin/main by construction (fake_repo
        # was bare-cloned right after the seed commit, no further push).
    )
    assert proc.returncode == 0, (
        f"expected exit 0 (clean), got {proc.returncode}\n"
        f"stderr: {proc.stderr}"
    )
    assert "DEGRADED" not in proc.stderr
    # Each check should have logged INFO at least once.
    for code in ("image_drift", "schema_drift", "refs_meta_config_drift",
                 "bridge_drift", "main_branch_drift"):
        assert f"code={code} severity=INFO" in proc.stderr, (
            f"missing INFO log for {code}\nstderr: {proc.stderr}"
        )


# ─────────────────────────────────────────────────────────────────────
# Per-check unit-level scenarios (non-AC)
# ─────────────────────────────────────────────────────────────────────


def test_schema_drift_detects_stale_alembic_version(fake_repo: Path) -> None:
    """When the deployed DB row points at an older revision than the
    leaf on disk, schema_drift fires DEGRADED."""
    proc = _run_scanner(
        fake_repo,
        "--db-version", "0201",  # one step behind 0202 leaf
        "--skip", "image_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 1
    assert "code=schema_drift" in proc.stderr
    assert "DEGRADED" in proc.stderr


def test_main_branch_drift_detects_local_ahead(fake_repo: Path) -> None:
    """Local main has unpushed commits → DEGRADED with ahead direction."""
    _git(fake_repo, "checkout", "-q", "main")
    (fake_repo / "local.txt").write_text("ahead\n", encoding="utf-8")
    _git(fake_repo, "add", "local.txt")
    _git(fake_repo, "commit", "-q", "-m", "unpushed local commit")

    proc = _run_scanner(
        fake_repo,
        "--skip", "image_drift",
        "--skip", "schema_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "bridge_drift",
    )
    assert proc.returncode == 1
    assert "code=main_branch_drift" in proc.stderr
    assert "ahead" in proc.stderr


def test_bridge_drift_detects_stale_revision(fake_repo: Path) -> None:
    """Deployed bridge revision predating origin/develop fires DEGRADED."""
    sha = _git(fake_repo, "rev-parse", "develop")
    # Advance origin/develop one commit so the bridge_revision (=sha)
    # is now stale.
    _git(fake_repo, "checkout", "-q", "develop")
    (fake_repo / "advance.txt").write_text("x\n", encoding="utf-8")
    _git(fake_repo, "add", "advance.txt")
    _git(fake_repo, "commit", "-q", "-m", "advance develop")
    _git(fake_repo, "push", "-q", "origin", "develop")
    _git(fake_repo, "fetch", "-q", "origin")
    _git(fake_repo, "checkout", "-q", "main")

    proc = _run_scanner(
        fake_repo,
        "--bridge-revision", sha,
        "--skip", "image_drift",
        "--skip", "schema_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 1
    assert "code=bridge_drift" in proc.stderr


def test_skipped_checks_log_info_not_drift(fake_repo: Path) -> None:
    """A check whose input is missing should INFO-log a 'skipped'
    explanation, not blow up or silently exit-1."""
    proc = _run_scanner(fake_repo, "--skip", "main_branch_drift",
                        "--skip", "bridge_drift")
    # Returns 0 because no DEGRADED — every check is INFO-skipped.
    assert proc.returncode == 0, proc.stderr
    assert "skipped" in proc.stderr


def test_json_output_shape(fake_repo: Path) -> None:
    """--json emits a parseable {"results": [...]} payload to stdout."""
    proc = _run_scanner(
        fake_repo, "--json",
        "--skip", "bridge_drift", "--skip", "main_branch_drift",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "results" in payload
    codes = {r["code"] for r in payload["results"]}
    assert codes == {"image_drift", "schema_drift", "refs_meta_config_drift"}
    for r in payload["results"]:
        assert r["severity"] in ("INFO", "DEGRADED")


def test_quiet_suppresses_info(fake_repo: Path) -> None:
    """--quiet hides INFO entries; only DEGRADED would print. With no
    drift the stderr should be empty."""
    proc = _run_scanner(
        fake_repo, "--quiet",
        "--skip", "bridge_drift", "--skip", "main_branch_drift",
    )
    assert proc.returncode == 0, proc.stderr
    assert "INFO" not in proc.stderr
    assert "DEGRADED" not in proc.stderr


def test_auto_fix_logs_classification(fake_repo: Path) -> None:
    """--auto-fix logs whether each detected drift is in the
    auto-fixable set; it must not actually mutate state in this rev."""
    sha = _git(fake_repo, "rev-parse", "develop")
    _git(fake_repo, "checkout", "-q", "develop")
    (fake_repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(fake_repo, "add", "x.txt")
    _git(fake_repo, "commit", "-q", "-m", "advance")
    _git(fake_repo, "push", "-q", "origin", "develop")
    _git(fake_repo, "fetch", "-q", "origin")
    _git(fake_repo, "checkout", "-q", "main")

    proc = _run_scanner(
        fake_repo,
        "--auto-fix",
        "--image-revision", sha,
        "--skip", "schema_drift",
        "--skip", "refs_meta_config_drift",
        "--skip", "bridge_drift",
        "--skip", "main_branch_drift",
    )
    assert proc.returncode == 1
    assert "auto-fix: image_drift is safe" in proc.stderr


def test_help_runs_clean() -> None:
    """`--help` exits 0 with usage banner — guards against argparse
    syntax errors that would crash the cron wrapper before any check."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    assert "drift" in proc.stdout.lower()


def test_non_git_repo_returns_two(tmp_path: Path) -> None:
    """Pointing --repo at a non-git directory returns exit 2 (env error),
    not 1 (drift) — operators need to distinguish the two."""
    proc = _run_scanner(tmp_path)
    assert proc.returncode == 2
    assert "not a git checkout" in proc.stderr
