"""OP-1751 -- bake-image-manifest.sh fail-build guard invariants.

Exercises the script's ``--check`` dry/test mode, which validates injected
candidate values against the reserved fail-build exit codes
(family5 §4.2 / family6 §9.2):

    exit 91  git_ref is not a full 40-char hex SHA
    exit 92  alembic head count != 1 (zero or multi-head)

The dry mode runs the guards without resolving real git/alembic state or
writing MANIFEST.json, so the invariants are deterministic in CI. The
normal (no-arg) build path is covered separately by
backend/tests/test_bake_image_manifest.py (OP-1159) and is intentionally
left unchanged here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "bake-image-manifest.sh"

# A representative full 40-char hex SHA (matches the /version contract fixture).
VALID_SHA = "b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4"


def _check(*, git_ref: str, heads: str) -> subprocess.CompletedProcess:
    """Invoke the script's --check dry mode with injected candidate values."""
    env = os.environ.copy()
    env["BAKE_CHECK_GIT_REF"] = git_ref
    env["BAKE_CHECK_HEADS"] = heads
    return subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def _bake(*, git_ref: str) -> subprocess.CompletedProcess:
    """Invoke the normal bake path far enough to exercise git_ref guards."""
    env = os.environ.copy()
    env["GITHUB_SHA"] = VALID_SHA
    env["GITHUB_REF_NAME"] = git_ref
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_check_mode_passes_for_single_head_and_40char_ref() -> None:
    result = _check(git_ref=VALID_SHA, heads="0237_runner_audit_events")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["event"] == "guards_ok"


def test_multi_head_input_fails_exit_92() -> None:
    result = _check(git_ref=VALID_SHA, heads="head_one\nhead_two")
    assert result.returncode == 92, result.stderr
    event = json.loads(result.stderr)
    assert event["event"] == "multi_head_remediation"
    assert "head_one" in event["details"]
    assert "head_two" in event["details"]


def test_zero_head_input_fails_exit_92() -> None:
    result = _check(git_ref=VALID_SHA, heads="")
    assert result.returncode == 92, result.stderr
    assert json.loads(result.stderr)["event"] == "multi_head_remediation"


def test_branch_name_git_ref_fails_exit_91() -> None:
    result = _check(git_ref="develop", heads="0237_runner_audit_events")
    assert result.returncode == 91, result.stderr
    assert json.loads(result.stderr)["event"] == "git_ref_not_40_char"


def test_empty_git_ref_fails_exit_91() -> None:
    result = _check(git_ref="", heads="0237_runner_audit_events")
    assert result.returncode == 91, result.stderr


def test_41char_git_ref_fails_exit_91() -> None:
    result = _check(git_ref=VALID_SHA + "a", heads="0237_runner_audit_events")
    assert result.returncode == 91, result.stderr


def test_non_hex_40char_git_ref_fails_exit_91() -> None:
    result = _check(git_ref="z" * 40, heads="0237_runner_audit_events")
    assert result.returncode == 91, result.stderr


def test_git_ref_checked_before_head_count() -> None:
    # A bad git_ref short-circuits to 91 even when the head list is also bad.
    result = _check(git_ref="develop", heads="head_one\nhead_two")
    assert result.returncode == 91, result.stderr
    assert json.loads(result.stderr)["event"] == "git_ref_not_40_char"


def test_normal_bake_path_unknown_git_ref_fails_exit_91_before_alembic() -> None:
    result = _bake(git_ref="unknown")
    assert result.returncode == 91, result.stderr
    assert json.loads(result.stderr)["event"] == "git_ref_not_40_char"
