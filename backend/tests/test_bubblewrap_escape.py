"""OP-862 — bubblewrap profile escape probes.

Runs the OP-845 runner sandbox against sensitive host paths that must not
be readable from a wrapped Story-completion CLI. The CI job for this file
installs ``bubblewrap`` first so these are hard runtime checks there; local
developer machines without ``bwrap`` skip the probes.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.agents import runner_sandbox as rs


HAS_BWRAP = shutil.which("bwrap") is not None


def _prepare_cli_home(ticket_key: str, tmp_path: Path) -> None:
    host_home = tmp_path / f"{ticket_key}-home"
    host_home.mkdir()
    rs.prepare_cli_home(
        ticket_key,
        env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )


@dataclass(frozen=True)
class EscapeProbe:
    name: str
    command: str


ESCAPE_PROBES: tuple[EscapeProbe, ...] = (
    EscapeProbe("etc_shadow", "cat /etc/shadow"),
    EscapeProbe("proc_1_environ", "cat /proc/1/environ"),
    EscapeProbe("home_ssh", "ls -la ~/.ssh/"),
)


def _init_story_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=path,
        check=True,
    )


def _git_output(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


STORY_COMPLETION_SCRIPT = """
cat > story_module.py <<'PY'
def answer() -> int:
    return 42
PY
python3 -B - <<'PY'
from pathlib import Path
compile(Path("story_module.py").read_text(), "story_module.py", "exec")
PY
git add story_module.py
git -c commit.gpgsign=false commit -m "[OP-862] Synthetic story completion"
"""


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
@pytest.mark.parametrize("probe", ESCAPE_PROBES, ids=lambda p: p.name)
def test_sensitive_host_paths_are_not_accessible_inside_bubblewrap(
    tmp_path: Path,
    probe: EscapeProbe,
) -> None:
    """Sensitive host paths must fail closed inside the runner sandbox.

    The probe deliberately redirects command output into the sandbox scratch
    dir so a failed assertion never prints host file contents to pytest logs.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    scratch = Path("/tmp/runner-OP-862")
    stdout_path = scratch / f"{probe.name}.stdout"
    stderr_path = scratch / f"{probe.name}.stderr"
    _prepare_cli_home("OP-862", tmp_path)

    cmd = [
        "sh",
        "-c",
        (
            f"({probe.command}) > {stdout_path} 2> {stderr_path} "
            "&& echo ESCAPE || echo BLOCKED"
        ),
    ]
    argv = rs.wrap_in_bubblewrap(
        cmd,
        worktree_path=worktree,
        ticket_key="OP-862",
    )

    out = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert out.returncode == 0
    assert out.stdout.strip() == "BLOCKED"
    assert "ESCAPE" not in out.stdout


def test_escape_probe_inventory_is_explicit() -> None:
    """Drift guard: OP-862 requires all three sensitive targets."""
    assert {probe.name for probe in ESCAPE_PROBES} == {
        "etc_shadow",
        "proc_1_environ",
        "home_ssh",
    }


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_sandboxed_story_completion_matches_unsandboxed_baseline(
    tmp_path: Path,
) -> None:
    """Synthetic Story completion produces the same diff inside bwrap."""
    main = tmp_path / "main"
    _init_story_repo(main)
    outside = tmp_path / "outside"
    inside = tmp_path / "inside"
    subprocess.run(
        ["git", "worktree", "add", "-b", "story-outside", str(outside)],
        cwd=main,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "story-inside", str(inside)],
        cwd=main,
        check=True,
        capture_output=True,
    )

    outside_result = subprocess.run(
        ["sh", "-c", STORY_COMPLETION_SCRIPT],
        cwd=outside,
        capture_output=True,
        text=True,
        timeout=20,
    )
    _prepare_cli_home("OP-862", tmp_path)
    inside_argv = rs.wrap_in_bubblewrap(
        ["sh", "-c", STORY_COMPLETION_SCRIPT],
        worktree_path=inside,
        ticket_key="OP-862",
    )
    inside_result = subprocess.run(
        inside_argv,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert outside_result.returncode == 0, outside_result.stderr
    assert inside_result.returncode == 0, inside_result.stderr
    assert _git_output(outside, "show", "--format=", "--numstat", "HEAD") == (
        _git_output(inside, "show", "--format=", "--numstat", "HEAD")
    )
    assert _git_output(outside, "status", "--short") == ""
    assert _git_output(inside, "status", "--short") == ""
