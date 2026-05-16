"""OP-1159 -- build-time image manifest contract."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bake-image-manifest.sh"
ISO_8601_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_manifest(
    tmp_path: Path,
    *,
    alembic_output: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "alembic",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$ALEMBIC_HEADS_OUTPUT\"\n",
    )
    _write_executable(
        bin_dir / "git",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  'rev-parse HEAD') printf '%s\\n' git-fallback-sha ;;\n"
        "  'symbolic-ref HEAD') printf '%s\\n' refs/heads/git-fallback-ref ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
    )
    run_env = os.environ.copy()
    run_env.update(env)
    run_env["ALEMBIC_HEADS_OUTPUT"] = alembic_output
    run_env["PATH"] = f"{bin_dir}:{run_env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=run_env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_manifest_script_outputs_valid_json_with_required_fields(tmp_path: Path) -> None:
    result = _run_manifest(
        tmp_path,
        alembic_output="0237_runner_audit_events (head)",
        env={"GITHUB_SHA": "abc123", "GITHUB_REF_NAME": "feature/op-1159"},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["image_sha"] == "abc123"
    assert manifest["git_ref"] == "feature/op-1159"
    assert manifest["alembic_head_in_image"] == "0237_runner_audit_events"
    assert ISO_8601_UTC.match(manifest["build_time"])


def test_manifest_script_rejects_multi_head_with_remediation(tmp_path: Path) -> None:
    result = _run_manifest(
        tmp_path,
        alembic_output="head_one (head)\nhead_two (head)",
        env={"GITHUB_SHA": "abc123", "GITHUB_REF_NAME": "feature/op-1159"},
    )

    assert result.returncode != 0
    event = json.loads(result.stderr)
    assert event["event"] == "multi_head_remediation"
    assert "head_one" in event["details"]
    assert "head_two" in event["details"]


def test_manifest_script_falls_back_to_git_when_github_env_is_missing(tmp_path: Path) -> None:
    result = _run_manifest(
        tmp_path,
        alembic_output="0237_runner_audit_events (head)",
        env={"GITHUB_SHA": "", "GITHUB_REF_NAME": ""},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["image_sha"] == "git-fallback-sha"
    assert manifest["git_ref"] == "refs/heads/git-fallback-ref"


def test_manifest_output_matches_strict_schema(tmp_path: Path) -> None:
    result = _run_manifest(
        tmp_path,
        alembic_output="0237_runner_audit_events (head)",
        env={"GITHUB_SHA": "abc123", "GITHUB_REF_NAME": "feature/op-1159"},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert set(manifest) == {
        "image_sha",
        "build_time",
        "git_ref",
        "alembic_head_in_image",
    }
    assert all(isinstance(value, str) and value for value in manifest.values())
    assert ISO_8601_UTC.match(manifest["build_time"])
