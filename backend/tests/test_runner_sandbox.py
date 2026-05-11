"""OP-845 — Runner sandbox wrapper tests.

Validates the bubblewrap (Linux) / seatbelt (macOS) prefix builder, the
ENFORCE-gated failure path, the network override audit, and the
integration with OP-836's sentinel (the sentinel lives inside the
worktree so the wrapped CLI can still write it).

Several tests need the real ``bwrap`` binary to assert filesystem
isolation end-to-end. Those tests are skipped when bwrap is absent
(typical dev laptops); the argv-shape tests cover the same logic by
asserting the argv that *would* have been invoked.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from backend.agents import runner_sandbox as rs
from backend.agents import runner_workspace_safety as wss


HAS_BWRAP = shutil.which("bwrap") is not None
HAS_SANDBOX_EXEC = shutil.which("sandbox-exec") is not None


# ─── Helpers ────────────────────────────────────────────────────────


def _force_platform(monkeypatch, name: str) -> None:
    """Pin :func:`runner_sandbox.detect_platform` to ``name`` for the test."""
    monkeypatch.setattr(rs, "detect_platform", lambda: name)


def _force_which(monkeypatch, mapping: dict[str, str | None]) -> None:
    """Pin :func:`runner_sandbox._which` to return values from ``mapping``."""
    monkeypatch.setattr(rs, "_which", lambda b: mapping.get(b))


# ─── argv shape on Linux ───────────────────────────────────────────


def test_argv_shape_linux_basic_invocation(tmp_path, monkeypatch):
    """Linux + bwrap on PATH → argv prefixes with bwrap and binds the
    worktree RW + /tmp/runner-<ticket> RW + system dirs RO."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})

    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude", "-p", "hello"],
        worktree_path=worktree, ticket_key="OP-TEST",
    )

    assert argv[0] == "/usr/bin/bwrap"
    assert "--die-with-parent" in argv
    assert "--new-session" in argv
    assert "--unshare-net" in argv  # default deny

    # Worktree is RW-bound; we look for `--bind <abs_path> <abs_path>`.
    worktree_abs = str(worktree.resolve())
    idx = argv.index(worktree_abs)
    assert argv[idx - 1] == "--bind"

    # /tmp/runner-OP-TEST is RW-bound
    tmp_dir = "/tmp/runner-OP-TEST"
    assert tmp_dir in argv
    assert argv[argv.index(tmp_dir) - 1] == "--bind"

    # Underlying cmd preserved verbatim after `--`
    sep = argv.index("--")
    assert argv[sep + 1:] == ["claude", "-p", "hello"]


def test_argv_shape_linux_includes_readonly_system_mounts(tmp_path, monkeypatch):
    """RO mounts: /usr /lib /etc /bin must all appear with --ro-bind."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
    )

    # Each --ro-bind appears as `--ro-bind <src> <dst>` with src == dst.
    # AC#2 names /usr /lib /etc /bin explicitly.
    for required in ("/usr", "/lib", "/etc", "/bin"):
        if not Path(required).exists():
            continue
        # Find the first occurrence as a src slot (preceded by --ro-bind).
        found = False
        for i, tok in enumerate(argv):
            if tok == required and i >= 1 and argv[i - 1] == "--ro-bind":
                assert argv[i + 1] == required, (
                    f"--ro-bind {required} missing dst slot"
                )
                found = True
                break
        assert found, f"missing --ro-bind for {required}"


def test_argv_shape_linux_binds_linked_git_metadata(tmp_path, monkeypatch):
    """Linked worktrees need their out-of-worktree git metadata mounted."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    (main / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=main, check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/sandbox", str(worktree)],
        cwd=main, check=True, capture_output=True,
    )

    argv = rs.wrap_in_bubblewrap(
        ["git", "status"], worktree_path=worktree, ticket_key="OP-862",
    )

    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()
    expected_mounts = {
        str((worktree / common_dir).resolve()),
        str((worktree / git_dir).resolve()),
    }
    for mount in expected_mounts:
        idx = argv.index(mount)
        assert argv[idx - 1] == "--bind"
        assert argv[idx + 1] == mount


# ─── argv shape on macOS ───────────────────────────────────────────


def test_argv_shape_macos_uses_sandbox_exec_with_profile(tmp_path, monkeypatch):
    """macOS + sandbox-exec on PATH → argv prefixes with sandbox-exec -p
    <profile> and the SBPL profile permits writes only inside worktree +
    /tmp/runner-<ticket>."""
    _force_platform(monkeypatch, rs.PLATFORM_MACOS)
    _force_which(monkeypatch, {"sandbox-exec": "/usr/bin/sandbox-exec"})

    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude", "-p", "hello"],
        worktree_path=worktree, ticket_key="OP-TEST",
    )

    assert argv[0] == "/usr/bin/sandbox-exec"
    assert argv[1] == "-p"
    profile = argv[2]
    worktree_abs = str(worktree.resolve())
    assert "(deny default)" in profile
    assert "(deny network*)" in profile  # default
    assert f'(allow file-write* (subpath "{worktree_abs}"))' in profile
    assert '(allow file-write* (subpath "/tmp/runner-OP-TEST"))' in profile
    # Underlying cmd preserved after the profile.
    assert argv[3:] == ["claude", "-p", "hello"]


# ─── ENFORCE-gated fallback when binary missing ────────────────────


def test_fallback_returns_raw_cmd_when_bwrap_missing_and_enforce_off(
    tmp_path, monkeypatch, caplog,
):
    """ENFORCE=0 + bwrap absent → return cmd unchanged + emit a warning."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": None})
    monkeypatch.delenv(rs.ENV_ENFORCE, raising=False)

    worktree = tmp_path / "wt"
    worktree.mkdir()

    with caplog.at_level("WARNING"):
        argv = rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
        )

    assert argv == ["claude"]
    assert "sandbox-binary-missing" in caplog.text
    assert rs.LOG_SANDBOX_DEGRADED in caplog.text


def test_fallback_raises_when_bwrap_missing_and_enforce_on(tmp_path, monkeypatch):
    """ENFORCE=1 + bwrap absent → raise SandboxBinaryMissing so the runner
    can operator-alert + abort the pickup."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": None})
    monkeypatch.setenv(rs.ENV_ENFORCE, "1")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(rs.SandboxBinaryMissing) as ei:
        rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
        )
    assert ei.value.binary == "bwrap"
    assert ei.value.platform == rs.PLATFORM_LINUX


def test_unsupported_platform_returns_identity(tmp_path, monkeypatch, caplog):
    """Non-Linux non-macOS platforms → identity + log sandbox=disabled."""
    _force_platform(monkeypatch, "freebsd")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with caplog.at_level("WARNING"):
        argv = rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
        )
    assert argv == ["claude"]
    assert rs.LOG_SANDBOX_DISABLED in caplog.text


# ─── Network policy ─────────────────────────────────────────────────


def test_network_blocked_by_default_linux(tmp_path, monkeypatch):
    """Default ``network=False`` → bwrap argv includes --unshare-net."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.delenv(rs.ENV_NETWORK_ALLOW, raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
    )
    assert "--unshare-net" in argv


def test_network_env_override_is_audited(tmp_path, monkeypatch, caplog):
    """ENV_NETWORK_ALLOW=<reason> → no --unshare-net + log the override
    with ticket key + reason for the audit trail."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.setenv(rs.ENV_NETWORK_ALLOW, "OP-XYZ needs PyPI fetch")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with caplog.at_level("WARNING"):
        argv = rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-XYZ",
        )

    assert "--unshare-net" not in argv
    assert "sandbox-network-override" in caplog.text
    assert "OP-XYZ" in caplog.text
    assert "PyPI fetch" in caplog.text


def test_network_caller_override_wins_without_env(tmp_path, monkeypatch):
    """Explicit ``network=True`` caller arg → no --unshare-net even when
    the env override is absent."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.delenv(rs.ENV_NETWORK_ALLOW, raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-TEST",
        network=True,
    )
    assert "--unshare-net" not in argv


# ─── End-to-end: real bwrap behaviour ───────────────────────────────


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_worktree_writable_inside_jail(tmp_path):
    """Wrapped CLI can write files INSIDE the worktree RW-bind."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = worktree / "scratch.txt"

    cmd = ["sh", "-c", f"echo hello > {target}"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-TEST",
    )
    rc = subprocess.run(argv, capture_output=True).returncode
    assert rc == 0
    assert target.read_text() == "hello\n"


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_tmp_runner_dir_writable_inside_jail(tmp_path):
    """``/tmp/runner-<ticket>`` is RW-bound, separate from worktree."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = "/tmp/runner-OP-E2E/inside.txt"

    cmd = ["sh", "-c", f"echo ok > {target}"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-E2E",
    )
    rc = subprocess.run(argv, capture_output=True).returncode
    assert rc == 0
    assert Path(target).read_text() == "ok\n"


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_etc_is_readonly_inside_jail(tmp_path):
    """/etc is RO-bound — write attempts inside the jail fail with EROFS."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cmd = ["sh", "-c", "echo hostile > /etc/runner-poison && echo WROTE || echo BLOCKED"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-TEST",
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    # The write fails (RO mount) so the shell falls through to "BLOCKED".
    assert "BLOCKED" in out.stdout, (
        f"sandbox did not block /etc write: stdout={out.stdout!r} "
        f"stderr={out.stderr!r}"
    )
    assert "WROTE" not in out.stdout


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_write_outside_worktree_blocked(tmp_path):
    """The home directory is not bound at all → writes fail. This is the
    AC#4 'write outside worktree' integration case."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    forbidden = tmp_path / "outside" / "leak.txt"
    forbidden.parent.mkdir()

    cmd = [
        "sh", "-c",
        f"echo leaked > {forbidden} && echo WROTE || echo BLOCKED",
    ]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-TEST",
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    assert "BLOCKED" in out.stdout
    assert "WROTE" not in out.stdout
    assert not forbidden.exists()


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_git_commit_in_linked_worktree_inside_jail(tmp_path):
    """Regression for OP-862 audit: wrapped CLI can commit in a git worktree."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    (main / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=main, check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/sandbox", str(worktree)],
        cwd=main, check=True, capture_output=True,
    )

    cmd = [
        "sh", "-c",
        (
            "echo sandbox >> README.md && git add README.md && "
            "git -c commit.gpgsign=false commit -m sandbox-commit"
        ),
    ]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-862",
    )
    out = subprocess.run(argv, capture_output=True, text=True)

    assert out.returncode == 0, out.stderr
    head = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == "sandbox-commit"


# ─── OP-836 interaction ─────────────────────────────────────────────


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_op836_sentinel_writable_inside_jail(tmp_path):
    """AC#4 cross-cutting: the OP-836 sentinel lives INSIDE the worktree
    so the wrapped CLI can write it. The runner's pre+post sentinel pair
    must survive a wrapped invocation."""
    # Build a real worktree (sentinel + verify need git).
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    (main / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=main, check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/sandbox", str(worktree)],
        cwd=main, check=True, capture_output=True,
    )

    sentinel = wss.write_workspace_sentinel(worktree, "OP-845")
    assert sentinel.exists()

    # Wrap a no-op CLI invocation inside the jail. Sentinel must survive.
    argv = rs.wrap_in_bubblewrap(
        ["sh", "-c", "ls > /dev/null"],
        worktree_path=worktree, ticket_key="OP-845",
    )
    rc = subprocess.run(argv, capture_output=True).returncode
    assert rc == 0

    # OP-836 post-CLI verify still passes.
    payload = wss.verify_workspace_sentinel(sentinel, worktree)
    assert payload["ticket_key"] == "OP-845"


# ─── Performance benchmark ─────────────────────────────────────────


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_overhead_under_500ms_cold_start(tmp_path):
    """AC#6: wrapped invocation cold-start overhead < 500ms.

    Measured against ``true`` so we isolate sandbox setup from any real
    CLI work. Three consecutive runs to absorb VFS warm-up; the slowest
    must still be under the budget."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["true"], worktree_path=worktree, ticket_key="OP-PERF",
    )

    durations = []
    for _ in range(3):
        t0 = time.perf_counter()
        rc = subprocess.run(argv, capture_output=True).returncode
        durations.append(time.perf_counter() - t0)
        assert rc == 0

    slowest = max(durations)
    assert slowest < 0.5, (
        f"sandbox cold-start overhead {slowest*1000:.0f}ms exceeds 500ms "
        f"budget (samples: {[f'{d*1000:.0f}ms' for d in durations]})"
    )


# ─── sandbox_available() helper for startup log ─────────────────────


def test_sandbox_available_true_when_binary_present(monkeypatch):
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    assert rs.sandbox_available() is True


def test_sandbox_available_false_when_binary_missing(monkeypatch):
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": None})
    assert rs.sandbox_available() is False


def test_sandbox_available_false_on_unsupported_platform(monkeypatch):
    _force_platform(monkeypatch, "freebsd")
    assert rs.sandbox_available() is False


# ─── Error catalog ──────────────────────────────────────────────────


def test_sandbox_binary_missing_error_carries_platform_and_binary():
    e = rs.SandboxBinaryMissing("linux", "bwrap")
    assert e.platform == "linux"
    assert e.binary == "bwrap"
    assert "bwrap" in str(e)


def test_sandbox_network_policy_override_error_carries_ticket_and_reason():
    e = rs.SandboxNetworkPolicyOverride("OP-XYZ", "needs PyPI")
    assert e.ticket_key == "OP-XYZ"
    assert e.reason == "needs PyPI"
    assert "OP-XYZ" in str(e)
    assert "needs PyPI" in str(e)


def test_sandbox_permission_denied_error_carries_path():
    e = rs.SandboxPermissionDenied("/etc/passwd")
    assert e.path == "/etc/passwd"
    assert "/etc/passwd" in str(e)
