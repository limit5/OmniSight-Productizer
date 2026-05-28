"""OP-1781 (1A.4) — pre-warmed per-tenant dependency cache tests.

Covers the three deliverables of the ticket:

* **Code** (``backend/sandbox_prewarm.py`` + ``runner_sandbox.py``): a
  per-tenant cache is RO-mounted into the bubblewrap jail and
  ``--unshare-net`` stays the default, so a build resolves its deps from the
  cache with no live network.
* **Integration**: the cache is per-tenant — tenant A's cache is neither on
  tenant B's path nor RO-bound for B.
* **Exercised**: an end-to-end bwrap run with the network denied reads from
  the pre-warmed cache (skipped where ``bwrap`` is absent; the argv-shape
  tests cover the same logic without the binary).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend import sandbox_prewarm as pw
from backend.agents import runner_sandbox as rs


HAS_BWRAP = shutil.which("bwrap") is not None


def _prepare_cli_home(ticket_key: str, tmp_path: Path) -> Path:
    host_home = tmp_path / f"{ticket_key}-home"
    host_home.mkdir()
    rs.prepare_cli_home(
        ticket_key,
        env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )
    return rs.cli_home_for(ticket_key)


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect tenant_fs roots to tmp_path so tests never touch real data."""
    monkeypatch.setattr("backend.tenant_fs._DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        "backend.tenant_fs._TENANTS_ROOT", tmp_path / "data" / "tenants"
    )
    monkeypatch.setattr("backend.tenant_fs._INGEST_BASE", tmp_path / "ingest")
    monkeypatch.setattr("backend.tenant_fs._PROJECT_ROOT", tmp_path)
    from backend.db_context import set_tenant_id
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _force_linux_bwrap(monkeypatch):
    monkeypatch.setattr(rs, "detect_platform", lambda: rs.PLATFORM_LINUX)
    monkeypatch.setattr(rs, "_which", lambda b: {"bwrap": "/usr/bin/bwrap"}.get(b))


# ─── cache root: per-tenant isolation ──────────────────────────────────


def test_dep_cache_root_under_tenant_namespace():
    root = pw.tenant_dep_cache_root("t-alpha")
    assert root.name == pw._DEP_CACHE_DIRNAME
    assert root.parent.name == "t-alpha"
    assert root.is_dir()


def test_dep_cache_root_isolated_between_tenants():
    a = pw.tenant_dep_cache_root("t-alpha")
    b = pw.tenant_dep_cache_root("t-beta")
    assert a != b
    # A file in A's cache is invisible on B's path.
    (a / "npm").mkdir()
    (a / "npm" / "secret.tgz").write_bytes(b"alpha-only")
    assert not (b / "npm" / "secret.tgz").exists()


def test_ensure_dep_cache_dirs_creates_all_ecosystems():
    root = pw.ensure_dep_cache_dirs("t-gamma")
    for eco in pw.DEP_CACHE_ECOSYSTEMS:
        assert (root / eco.name).is_dir()


# ─── mount resolution ──────────────────────────────────────────────────


def test_dep_cache_mounts_only_existing_subdirs(tmp_path):
    """A missing ecosystem subdir is skipped (bwrap fails on a missing
    bind source); only warmed ecosystems are mounted."""
    root = pw.tenant_dep_cache_root("t-alpha")
    (root / "npm").mkdir()  # only npm warmed
    home = tmp_path / "wt"
    mounts = pw.dep_cache_mounts("t-alpha", home=home)
    assert len(mounts) == 1
    src, dst = mounts[0]
    assert src == str(root / "npm")
    assert dst == str(home / ".npm")


def test_dep_cache_mounts_dst_is_home_relative(tmp_path):
    pw.ensure_dep_cache_dirs("t-alpha")
    home = tmp_path / "wt"
    mounts = dict(pw.dep_cache_mounts("t-alpha", home=home))
    by_rel = {eco.name: eco.jail_mount_rel for eco in pw.DEP_CACHE_ECOSYSTEMS}
    for src, dst in mounts.items():
        eco_name = Path(src).name
        assert dst == str(home / by_rel[eco_name])


def test_dep_cache_mounts_empty_without_tenant(tmp_path):
    assert pw.dep_cache_mounts(None, home=tmp_path) == []
    assert pw.dep_cache_mounts("", home=tmp_path) == []


def test_dep_cache_mounts_isolated_between_tenants(tmp_path):
    """AC integration: A's mounts never reference B's cache path."""
    pw.ensure_dep_cache_dirs("t-alpha")
    pw.ensure_dep_cache_dirs("t-beta")
    home = tmp_path / "wt"
    a_srcs = {src for src, _ in pw.dep_cache_mounts("t-alpha", home=home)}
    b_srcs = {src for src, _ in pw.dep_cache_mounts("t-beta", home=home)}
    assert a_srcs and b_srcs
    assert a_srcs.isdisjoint(b_srcs)
    assert all("t-alpha" in s for s in a_srcs)
    assert all("t-beta" in s for s in b_srcs)


# ─── runner_sandbox: RO-bind + net-deny preserved ──────────────────────


def test_argv_ro_binds_dep_cache_after_worktree(tmp_path, monkeypatch):
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    mounts = [("/cache/t-alpha/npm", str(worktree / ".npm"))]

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1781",
        dep_cache_mounts=mounts,
    )

    # --unshare-net stays the default even with a cache mounted.
    assert "--unshare-net" in argv

    # The cache is RO-bound: `--ro-bind <src> <dst>`.
    src, dst = mounts[0]
    idx = argv.index(src)
    assert argv[idx - 1] == "--ro-bind"
    assert argv[idx + 1] == dst

    # It is bound AFTER the worktree RW-bind so the cache layers on top.
    worktree_abs = str(worktree.resolve())
    assert argv.index(worktree_abs) < idx


def test_argv_no_dep_cache_when_none(tmp_path, monkeypatch):
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1781",
    )
    assert "--ro-bind" in argv  # system mounts still present
    assert "--unshare-net" in argv
    # No cache mount points appended.
    assert str(worktree / ".npm") not in argv


# ─── warm step (online, injectable runner) ─────────────────────────────


def test_warm_env_redirects_each_ecosystem_cache():
    env = pw.warm_env("t-alpha", base={"PATH": "/usr/bin"})
    root = pw.tenant_dep_cache_root("t-alpha")
    assert env["PATH"] == "/usr/bin"  # base preserved
    for eco in pw.DEP_CACHE_ECOSYSTEMS:
        assert env[eco.warm_env] == str(root / eco.name)


def test_refresh_dep_cache_runs_commands_in_workspace(tmp_path):
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    ws = tmp_path / "ws"
    ws.mkdir()
    results = pw.refresh_dep_cache(
        "t-alpha", workspace=ws, ecosystems=["npm"], runner=fake_runner,
    )

    assert results == {"npm": "ok"}
    assert len(calls) == 1
    argv, kwargs = calls[0]
    # {cache} substituted with the tenant npm cache subdir.
    cache_dir = str(pw.tenant_dep_cache_root("t-alpha") / "npm")
    assert cache_dir in argv
    assert kwargs["cwd"] == str(ws)
    # Warm env redirects npm's cache into the tenant subdir.
    assert kwargs["env"]["npm_config_cache"] == cache_dir


def test_refresh_dep_cache_best_effort_on_failure(tmp_path):
    def boom(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="offline")

    ws = tmp_path / "ws"
    ws.mkdir()
    results = pw.refresh_dep_cache(
        "t-alpha", workspace=ws, ecosystems=["pip"], runner=boom,
    )
    assert results["pip"].startswith("error:")


def test_refresh_dep_cache_noop_without_tenant(tmp_path):
    assert pw.refresh_dep_cache(None, workspace=tmp_path) == {}


# ─── end-to-end: offline resolution from the RO cache ───────────────────


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_build_reads_cache_offline(tmp_path):
    """AC 'Exercised': with --unshare-net the jailed build still reads a
    dependency from the RO-mounted pre-warmed cache."""
    # Warm a fake npm cache entry for the tenant.
    cache_root = pw.tenant_dep_cache_root("t-alpha")
    (cache_root / "npm").mkdir(parents=True)
    (cache_root / "npm" / "left-pad-1.0.0.tgz").write_text("PREWARMED")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    cli_home = _prepare_cli_home("OP-1781", tmp_path)
    mounts = pw.dep_cache_mounts("t-alpha", home=cli_home)
    assert mounts  # npm mount present

    # The "build" reads its dep straight out of ~/.npm (HOME == cli-home).
    cmd = ["sh", "-c", "cat $HOME/.npm/left-pad-1.0.0.tgz"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-1781",
        dep_cache_mounts=mounts,
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "PREWARMED"


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_network_denied_with_cache_mounted(tmp_path):
    """The cache mount does not re-open the network: --unshare-net holds, so
    a socket connect inside the jail fails."""
    cache_root = pw.tenant_dep_cache_root("t-alpha")
    (cache_root / "npm").mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cli_home = _prepare_cli_home("OP-1781", tmp_path)
    mounts = pw.dep_cache_mounts("t-alpha", home=cli_home)

    # Python's socket connect to a routable host must fail under --unshare-net.
    probe = (
        "import socket,sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('NET_OK')\n"
        "except OSError:\n"
        "    print('NET_BLOCKED')\n"
    )
    cmd = ["python3", "-c", probe]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-1781",
        dep_cache_mounts=mounts,
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    assert "NET_BLOCKED" in out.stdout, (
        f"network not denied: stdout={out.stdout!r} stderr={out.stderr!r}"
    )
    assert "NET_OK" not in out.stdout


@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap not installed")
def test_e2e_cache_is_read_only_inside_jail(tmp_path):
    """RO mount: the jailed build cannot mutate (poison) the shared cache."""
    cache_root = pw.tenant_dep_cache_root("t-alpha")
    (cache_root / "npm").mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cli_home = _prepare_cli_home("OP-1781", tmp_path)
    mounts = pw.dep_cache_mounts("t-alpha", home=cli_home)

    cmd = ["sh", "-c", "echo poison > $HOME/.npm/evil && echo WROTE || echo BLOCKED"]
    argv = rs.wrap_in_bubblewrap(
        cmd, worktree_path=worktree, ticket_key="OP-1781",
        dep_cache_mounts=mounts,
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    assert "BLOCKED" in out.stdout
    assert not (cache_root / "npm" / "evil").exists()
