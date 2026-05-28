"""OP-1835 — bind the resolved /etc/resolv.conf into the jail (in-jail DNS).

Follow-up to OP-1834. The 2026-05-28 bwrap canary got past toolchain (OP-1803)
and session-init (OP-1834) but then HUNG on the model-API call: in-jail DNS was
dead. On this WSL2 host ``/etc/resolv.conf`` is a SYMLINK → ``/mnt/wsl/resolv.conf``;
the jail RO-binds ``/etc`` (carrying the symlink in AS-IS) but NOT ``/mnt/wsl``,
so inside the jail the link dangles → ``Temporary failure in name resolution`` →
the CLI can't reach api.anthropic.com.

The fix resolves the host symlink chain and RO-binds the REAL target file at the
canonical in-jail path ``/etc/resolv.conf`` — so DNS works regardless of where
the host keeps the file (WSL2 ``/mnt/wsl``, systemd-resolved stub, or a plain
file), without binding all of ``/mnt/wsl``.

Argv/shape only (degraded-safe): the unit tests assert the wrap that *would*
run, so they pass with bwrap absent (the current degraded fleet — this fix is
INERT until an operator re-enables + re-canaries bwrap). One bwrap-gated e2e
proves the resolved file actually overlays the dangling symlink inside the jail.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.agents import runner_sandbox as rs


HAS_BWRAP = shutil.which("bwrap") is not None


def _force_linux_bwrap(monkeypatch) -> None:
    monkeypatch.setattr(rs, "detect_platform", lambda: rs.PLATFORM_LINUX)
    monkeypatch.setattr(rs, "_which", lambda b: {"bwrap": "/usr/bin/bwrap"}.get(b))
    # Keep the argv clean of an unrelated toolchain bind.
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: None)


def _ro_bind_pairs(argv: list[str]) -> list[tuple[str, str]]:
    """Return every ``(src, dst)`` of a ``--ro-bind <src> <dst>`` in ``argv``."""
    return [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok == "--ro-bind"
    ]


def _stub_wsl_symlink(tmp_path: Path) -> tuple[Path, Path]:
    """Build a WSL2-shaped stub: a symlink → a real file in a sibling 'wsl' dir.

    Returns ``(symlink, real_target)``. The symlink stands in for the host
    ``/etc/resolv.conf``; the real target stands in for ``/mnt/wsl/resolv.conf``.
    """
    wsl = tmp_path / "mnt" / "wsl"
    wsl.mkdir(parents=True)
    real_target = wsl / "resolv.conf"
    real_target.write_text("nameserver 10.255.255.254\n")
    symlink = tmp_path / "etc-resolv.conf"
    symlink.symlink_to(real_target)
    return symlink, real_target


# ─── _resolv_conf_bind: resolution + graceful skip ───────────────────


def test_resolv_bind_resolves_symlink_to_real_target(tmp_path, monkeypatch):
    """The helper binds the REAL target (not the symlink) at the jail path."""
    symlink, real_target = _stub_wsl_symlink(tmp_path)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))

    bind = rs._resolv_conf_bind()
    assert bind == (str(real_target), "/etc/resolv.conf")
    # The symlink itself is never the bind source.
    assert bind[0] != str(symlink)


def test_resolv_bind_handles_plain_file(tmp_path, monkeypatch):
    """A plain (non-symlink) host resolv.conf binds itself at the jail path."""
    plain = tmp_path / "resolv.conf"
    plain.write_text("nameserver 1.1.1.1\n")
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(plain))

    assert rs._resolv_conf_bind() == (str(plain), "/etc/resolv.conf")


def test_resolv_bind_skips_absent_file(tmp_path, monkeypatch):
    """Absent host resolv.conf → None (skipped gracefully, no crash)."""
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(tmp_path / "nope.conf"))
    assert rs._resolv_conf_bind() is None


def test_resolv_bind_skips_dangling_symlink(tmp_path, monkeypatch):
    """A symlink whose target is missing on the host too → None (no crash)."""
    dangling = tmp_path / "dangling.conf"
    dangling.symlink_to(tmp_path / "missing-target.conf")
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(dangling))
    assert rs._resolv_conf_bind() is None


# ─── argv shape: --ro-bind <resolved-target> /etc/resolv.conf ────────


def test_argv_ro_binds_resolved_resolv_conf(tmp_path, monkeypatch):
    """AC (Exercised): the wrap argv RO-binds the RESOLVED target at
    ``/etc/resolv.conf`` when the host resolv.conf is a symlink."""
    _force_linux_bwrap(monkeypatch)
    symlink, real_target = _stub_wsl_symlink(tmp_path)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1835-a",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    # The REAL target is the bind source; the jail dest is /etc/resolv.conf.
    assert (str(real_target), "/etc/resolv.conf") in _ro_bind_pairs(argv)
    # Located as a contiguous `--ro-bind <real> /etc/resolv.conf` triple.
    idx = argv.index(str(real_target))
    assert argv[idx - 1] == "--ro-bind"
    assert argv[idx + 1] == "/etc/resolv.conf"


def test_argv_resolv_bind_overlays_after_etc(tmp_path, monkeypatch):
    """The resolv.conf bind comes AFTER the /etc RO-bind so it overlays the
    dangling symlink /etc carried in (bwrap applies binds in order)."""
    _force_linux_bwrap(monkeypatch)
    symlink, real_target = _stub_wsl_symlink(tmp_path)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1835-order",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    # /etc is RO-bound; the resolved resolv.conf bind follows it.
    etc_idx = next(
        i for i, t in enumerate(argv)
        if t == "/etc" and argv[i - 1] == "--ro-bind"
    )
    resolv_idx = argv.index(str(real_target))
    assert etc_idx < resolv_idx


def test_argv_binds_only_single_file_not_mnt_wsl(tmp_path, monkeypatch):
    """SAFETY: only the single resolved file is bound — NOT all of /mnt/wsl
    (the symlink's parent dir)."""
    _force_linux_bwrap(monkeypatch)
    symlink, real_target = _stub_wsl_symlink(tmp_path)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1835-single",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    wsl_dir = str(real_target.parent)
    # The parent (stand-in for /mnt/wsl) is never bound; the file is.
    assert wsl_dir not in argv
    assert all(src != wsl_dir for src, _ in _ro_bind_pairs(argv))
    assert str(real_target) in argv


def test_argv_skips_resolv_bind_when_absent(tmp_path, monkeypatch):
    """Absent host resolv.conf → no resolv bind appended, still a valid wrap."""
    _force_linux_bwrap(monkeypatch)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(tmp_path / "nope.conf"))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1835-absent",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    # No --ro-bind dst points at /etc/resolv.conf when the host file is absent.
    assert all(dst != "/etc/resolv.conf" for _, dst in _ro_bind_pairs(argv))
    # Sanity: still a well-formed wrap with the /etc system mount present.
    assert ("/etc", "/etc") in _ro_bind_pairs(argv)
    assert argv[argv.index("--") + 1:] == ["claude"]


# ─── scope guard: nothing else changed ───────────────────────────────


def test_resolv_bind_does_not_touch_network_flag(tmp_path, monkeypatch):
    """Scope guard: the DNS fix does not flip egress/network flags —
    --unshare-net stays the default when network is not requested."""
    _force_linux_bwrap(monkeypatch)
    symlink, _ = _stub_wsl_symlink(tmp_path)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1835-net",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )
    assert "--unshare-net" in argv


# ─── e2e: the resolved file actually overlays the symlink in-jail ────


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_resolved_resolv_conf_readable_inside_jail(tmp_path, monkeypatch):
    """AC (Code, exercised end-to-end): inside the real bwrap jail,
    ``/etc/resolv.conf`` resolves to the RO-bound real target's CONTENT —
    proving the resolved file overlays the (otherwise dangling) symlink."""
    real_target = tmp_path / "real-resolv.conf"
    real_target.write_text("nameserver 9.9.9.9\n")
    symlink = tmp_path / "host-resolv.conf"
    symlink.symlink_to(real_target)
    monkeypatch.setattr(rs, "HOST_RESOLV_CONF", str(symlink))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    cmd = ["sh", "-c", "cat /etc/resolv.conf"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-1835-e2e",
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
    )
    out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "nameserver 9.9.9.9\n"
