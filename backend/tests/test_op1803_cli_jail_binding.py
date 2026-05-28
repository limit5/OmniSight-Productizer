"""OP-1803 (OP-1783 P1) — bind the agent-CLI toolchain + config into the jail.

Exercises the three locked decisions (design A1 §6), all INERT until an
operator re-enables bwrap:

* §2a — ``_build_bubblewrap_argv`` RO-binds the resolved nvm node-version
  subtree (node + claude/codex + node_modules), NOT all of ``~/.nvm``.
* §2b — ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` are added to ``ENV_ALLOWLIST``.
  NOTE: the original §2b RO-bind-at-host-path was SUPERSEDED by OP-1834 — the
  CLI must WRITE its session/cache/state, so the config dirs are now seeded
  into a writable per-ticket cli-home and the vars point there (RO-bind →
  EROFS at init). The two §2b argv tests below assert the OP-1834 redirect.
* §2c (v1) — the agent-CLI wrap in ``auto-runner-jira.py`` passes
  ``network=True`` (blanket-allow for v1).

Argv-shape only: these assert the argv that *would* run, so they pass with
bwrap absent (current degraded fleet state).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from backend.agents import runner_sandbox as rs


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_op1803_uut", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_op1803_uut", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _force_platform(monkeypatch, name: str) -> None:
    monkeypatch.setattr(rs, "detect_platform", lambda: name)


def _force_which(monkeypatch, mapping: dict[str, str | None]) -> None:
    monkeypatch.setattr(rs, "_which", lambda b: mapping.get(b))


def _setenv_map(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, tok in enumerate(argv):
        if tok == "--setenv":
            out[argv[i + 1]] = argv[i + 2]
    return out


# ─── §2a: node toolchain subtree resolution + RO-bind ───────────────


def test_nvm_version_dir_resolves_subtree_from_node_path(tmp_path):
    """``which node`` under ``versions/node/<ver>/bin/node`` → the ``<ver>/`` dir."""
    ver = tmp_path / ".nvm" / "versions" / "node" / "v20.11.0"
    (ver / "bin").mkdir(parents=True)
    node = ver / "bin" / "node"
    node.write_text("")
    assert rs._nvm_version_dir_of(node) == ver


def test_nvm_version_dir_returns_none_for_distro_node():
    """A distro ``/usr/bin/node`` is not under nvm → None (it's under the
    already-RO-bound ``/usr``, so no extra mount is needed)."""
    assert rs._nvm_version_dir_of(Path("/usr/bin/node")) is None


def test_nvm_default_version_dir_prefers_alias_then_newest(tmp_path):
    nvm = tmp_path / ".nvm"
    for v in ("v18.19.0", "v20.11.0"):
        (nvm / "versions" / "node" / v).mkdir(parents=True)
    # No alias → newest (lexically greatest).
    assert rs._nvm_default_version_dir(nvm).name == "v20.11.0"
    # alias/default pins the older line by version prefix.
    alias = nvm / "alias" / "default"
    alias.parent.mkdir(parents=True)
    alias.write_text("18\n")
    assert rs._nvm_default_version_dir(nvm).name == "v18.19.0"


def test_argv_ro_binds_resolved_node_toolchain_subtree(tmp_path, monkeypatch):
    """§2a: the resolved ``<ver>/`` subtree is RO-bound at its absolute path."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    worktree = tmp_path / "wt"
    worktree.mkdir()
    toolchain = tmp_path / ".nvm" / "versions" / "node" / "v20.11.0"
    toolchain.mkdir(parents=True)
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: toolchain)

    argv = rs.wrap_in_bubblewrap(
        ["claude", "-p", "hi"], worktree_path=worktree, ticket_key="OP-1803",
    )

    tc = str(toolchain)
    idx = argv.index(tc)
    assert argv[idx - 1] == "--ro-bind"
    assert argv[idx + 1] == tc
    # Bind the SPECIFIC version dir, never all of ~/.nvm.
    assert str(tmp_path / ".nvm") not in argv


def test_argv_skips_toolchain_bind_when_unresolved(tmp_path, monkeypatch):
    """No nvm toolchain found → no extra RO-bind (distro node under /usr)."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1803",
    )
    # Sanity: still a well-formed wrap.
    assert argv[0] == "/usr/bin/bwrap"
    assert argv[argv.index("--") + 1:] == ["claude"]


# ─── §2b: CLI config dirs in allowlist + RO-bind + --setenv ──────────


def test_env_allowlist_includes_cli_config_dirs():
    """§2b: the allowlist gains CLAUDE_CONFIG_DIR / CODEX_HOME so the
    degraded/raw spawn forwards them too."""
    assert "CLAUDE_CONFIG_DIR" in rs.ENV_ALLOWLIST
    assert "CODEX_HOME" in rs.ENV_ALLOWLIST


def test_argv_redirects_cli_config_dirs_into_writable_cli_home(tmp_path, monkeypatch):
    """§2b SUPERSEDED by OP-1834: the host ``~/.claude`` / ``~/.codex`` are
    NOT bound (RW or RO); instead CLAUDE_CONFIG_DIR / CODEX_HOME / HOME are
    --setenv'd into the writable per-ticket cli-home."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    host_home = tmp_path / "home"
    claude_cfg = host_home / ".claude"
    codex_cfg = host_home / ".codex"
    claude_cfg.mkdir(parents=True)
    codex_cfg.mkdir()

    base = {"PATH": "/usr/bin", "HOME": str(host_home)}
    try:
        argv = rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-1834T1", env=base,
        )

        # The shared host config dirs are NEVER bound (no RW-, no RO-bind).
        assert str(claude_cfg) not in argv
        assert str(codex_cfg) not in argv

        cli_home = rs.cli_home_for("OP-1834T1")
        setenv = _setenv_map(argv)
        assert setenv["HOME"] == str(cli_home)
        assert setenv["CLAUDE_CONFIG_DIR"] == str(cli_home / ".claude")
        assert setenv["CODEX_HOME"] == str(cli_home / ".codex")
        # The CLI config var resolves under the (writable) HOME, not apart.
        assert setenv["CLAUDE_CONFIG_DIR"].startswith(setenv["HOME"])
        # Each set exactly once — skipped in the generic allowlist projection.
        for var in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            assert sum(
                1 for i, t in enumerate(argv)
                if t == "--setenv" and argv[i + 1] == var
            ) == 1
    finally:
        rs.cleanup_cli_home("OP-1834T1")


def test_argv_explicit_config_override_seeds_into_cli_home(tmp_path, monkeypatch):
    """An explicit ``CLAUDE_CONFIG_DIR`` is the seed SOURCE (its contents are
    copied into the cli-home), but the in-jail var points at the writable
    per-ticket cli-home — never the host path, which is not bound (OP-1834)."""
    _force_platform(monkeypatch, rs.PLATFORM_LINUX)
    _force_which(monkeypatch, {"bwrap": "/usr/bin/bwrap"})
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    custom = tmp_path / "custom-claude"
    custom.mkdir()
    (custom / "auth.json").write_text("SEED")

    base = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "home"),  # default ~/.codex absent on host
        "CLAUDE_CONFIG_DIR": str(custom),
    }
    try:
        # Seeding is the runner's explicit step (keeps the argv builder pure).
        rs.prepare_cli_home("OP-1834T2", env=base)
        argv = rs.wrap_in_bubblewrap(
            ["claude"], worktree_path=worktree, ticket_key="OP-1834T2", env=base,
        )

        cli_home = rs.cli_home_for("OP-1834T2")
        setenv = _setenv_map(argv)
        # Var points into the writable cli-home, not the custom host path.
        assert setenv["CLAUDE_CONFIG_DIR"] == str(cli_home / ".claude")
        assert str(custom) not in argv
        assert sum(
            1 for i, t in enumerate(argv)
            if t == "--setenv" and argv[i + 1] == "CLAUDE_CONFIG_DIR"
        ) == 1
        # The explicit override's contents are seeded in + readable.
        assert (cli_home / ".claude" / "auth.json").read_text() == "SEED"
        # CODEX_HOME is still redirected (the writable dir is created empty even
        # though the host ~/.codex was absent — no longer skipped).
        assert setenv["CODEX_HOME"] == str(cli_home / ".codex")
        assert (cli_home / ".codex").is_dir()
    finally:
        rs.cleanup_cli_home("OP-1834T2")


# ─── §2c: agent-CLI wrap is network-allowed ─────────────────────────


def test_invoke_cli_wraps_agent_with_network_allowed(tmp_path, monkeypatch):
    """§2c: ``_invoke_cli`` requests ``network=True`` on the sandbox wrap
    (v1 blanket-allow) so the jailed CLI can reach the model API + remote."""
    monkeypatch.delenv(rs.ENV_ENFORCE, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/bot")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    mod = _load_jira_runner()
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod, "CLAUDE_WORKTREE", str(worktree))

    captured: dict[str, Any] = {}

    def _fake_wrap(cmd, **kwargs):
        captured.update(kwargs)
        return list(cmd)

    monkeypatch.setattr(mod.runner_sandbox, "wrap_in_bubblewrap", _fake_wrap)

    class _FakeProc:
        returncode = 0

        def communicate(self, *a, **k):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: _FakeProc())

    rc = mod._invoke_cli(
        "subscription-claude", "do the thing",
        ticket_key="OP-1803", worktree_path=worktree,
    )

    assert rc == 0
    assert captured.get("network") is True
