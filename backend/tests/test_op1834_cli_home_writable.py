"""OP-1834 — writable per-ticket CLI state/session/cache home.

Follow-up to OP-1803 (§2b). That ticket RO-bound the agent-CLI config dirs
(``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR``) at their host path; the 2026-05-28
bwrap canary then died at session init with ``Read-only file system`` because
the CLI must WRITE its session/cache/state into those dirs.

The fix gives the wrapped CLI a WRITABLE, per-ticket home under the already-RW
``/tmp/runner-<ticket>`` scratch (``cli-home``), seeds it with the host CLI
auth/config (so the CLI still authenticates), and redirects ``HOME`` +
``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR`` / ``XDG_*`` there — WITHOUT RW-binding
the shared host config dir (tenant isolation) or widening the worktree.

Argv/env-shape only: these assert the wrap that *would* run, so they pass with
bwrap absent (the current degraded fleet — this fix is INERT until an operator
re-enables + re-canaries bwrap).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.agents import runner_sandbox as rs


def _force_linux_bwrap(monkeypatch) -> None:
    monkeypatch.setattr(rs, "detect_platform", lambda: rs.PLATFORM_LINUX)
    monkeypatch.setattr(rs, "_which", lambda b: {"bwrap": "/usr/bin/bwrap"}.get(b))
    # Keep the argv clean of an unrelated toolchain bind.
    monkeypatch.setattr(rs, "_nvm_node_toolchain_subtree", lambda env: None)


def _setenv_map(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, tok in enumerate(argv):
        if tok == "--setenv":
            out[argv[i + 1]] = argv[i + 2]
    return out


def _bind_targets(argv: list[str], flag: str) -> set[str]:
    """Return the src path of every ``<flag> <src> <dst>`` occurrence."""
    out: set[str] = set()
    for i, tok in enumerate(argv):
        if tok == flag:
            out.add(argv[i + 1])
    return out


@pytest.fixture(autouse=True)
def _cleanup_cli_homes():
    """Wipe any per-ticket cli-home this module seeds into real ``/tmp``."""
    yield
    for p in Path("/tmp").glob("runner-OP-1834*"):
        shutil.rmtree(p, ignore_errors=True)


# ─── cli_home_for: path under the per-ticket scratch ────────────────


def test_cli_home_is_under_per_ticket_scratch():
    home = rs.cli_home_for("OP-1834-x")
    assert home == Path("/tmp/runner-OP-1834-x/cli-home")
    # Pure: must not create the dir.
    assert not home.exists()


def test_cli_home_sanitises_ticket_key():
    home = rs.cli_home_for("../../etc/OP")
    assert ".." not in str(home.parent.name)
    assert str(home).startswith("/tmp/runner-")


# ─── (a) writable bind for the CLI home ─────────────────────────────


def test_argv_rw_binds_cli_home_under_scratch(tmp_path, monkeypatch):
    """AC (a): the CLI home is RW-bound (``--bind``, not ``--ro-bind``) and
    sits under ``/tmp/runner-<ticket>``."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-a",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    cli_home = str(rs.cli_home_for("OP-1834-a"))
    assert cli_home.startswith("/tmp/runner-OP-1834-a/")
    # Present as a writable bind, never read-only.
    assert cli_home in _bind_targets(argv, "--bind")
    assert cli_home not in _bind_targets(argv, "--ro-bind")
    # `--bind <cli_home> <cli_home>` (src == dst).
    idx = argv.index(cli_home)
    assert argv[idx - 1] == "--bind"
    assert argv[idx + 1] == cli_home


# ─── (b) state env vars point inside the CLI home ───────────────────


def test_state_env_vars_point_inside_cli_home(tmp_path, monkeypatch):
    """AC (b): HOME + CODEX_HOME + CLAUDE_CONFIG_DIR + XDG_* all resolve
    inside the writable per-ticket cli-home, each set exactly once."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-b",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )

    cli_home = str(rs.cli_home_for("OP-1834-b"))
    setenv = _setenv_map(argv)

    assert setenv["HOME"] == cli_home
    assert setenv["CLAUDE_CONFIG_DIR"] == str(Path(cli_home) / ".claude")
    assert setenv["CODEX_HOME"] == str(Path(cli_home) / ".codex")
    assert setenv["XDG_CONFIG_HOME"] == str(Path(cli_home) / ".config")
    assert setenv["XDG_CACHE_HOME"] == str(Path(cli_home) / ".cache")
    assert setenv["XDG_STATE_HOME"] == str(Path(cli_home) / ".local/state")
    assert setenv["XDG_DATA_HOME"] == str(Path(cli_home) / ".local/share")

    # Every redirected var lives under HOME and is set exactly once.
    for var in (
        "HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
    ):
        assert setenv[var].startswith(cli_home)
        assert sum(
            1 for i, t in enumerate(argv)
            if t == "--setenv" and argv[i + 1] == var
        ) == 1


def test_chdir_stays_worktree_not_cli_home(tmp_path, monkeypatch):
    """The CLI cwd is still the worktree (code lives there); HOME is apart."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-chdir",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )
    chdir = argv[argv.index("--chdir") + 1]
    assert chdir == str(worktree.resolve())
    assert chdir != _setenv_map(argv)["HOME"]


# ─── (c) seeded auth present + readable ──────────────────────────────


def test_seeded_auth_is_present_and_readable(tmp_path):
    """AC (c): ``prepare_cli_home`` copies the host CLI auth/config CONTENTS
    into the writable cli-home so the jailed CLI still authenticates."""
    host_home = tmp_path / "home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text("CLAUDE-TOKEN")
    (host_home / ".claude.json").write_text('{"primary":"CLAUDE-CONFIG"}')
    (host_home / ".codex").mkdir()
    (host_home / ".codex" / "auth.json").write_text("CODEX-TOKEN")

    redirects = rs.prepare_cli_home(
        "OP-1834-c", env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )

    cli_home = rs.cli_home_for("OP-1834-c")
    claude_cred = cli_home / ".claude" / ".credentials.json"
    claude_config = cli_home / ".claude.json"
    codex_cred = cli_home / ".codex" / "auth.json"
    assert claude_cred.read_text() == "CLAUDE-TOKEN"
    assert claude_config.read_text() == '{"primary":"CLAUDE-CONFIG"}'
    assert codex_cred.read_text() == "CODEX-TOKEN"
    # The returned redirects match the in-jail --setenv paths.
    assert redirects["CLAUDE_CONFIG_DIR"] == str(cli_home / ".claude")
    assert redirects["CODEX_HOME"] == str(cli_home / ".codex")


def test_missing_home_root_config_file_is_skipped(tmp_path):
    """OP-1838: missing HOME-root CLI config files are best-effort and do
    not abort seeding the cli-home."""
    host_home = tmp_path / "home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text("CLAUDE-TOKEN")

    rs.prepare_cli_home(
        "OP-1834-missing-root", env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )

    cli_home = rs.cli_home_for("OP-1834-missing-root")
    assert (
        cli_home / ".claude" / ".credentials.json"
    ).read_text() == "CLAUDE-TOKEN"
    assert not (cli_home / ".claude.json").exists()


def test_prepare_redirects_match_argv_setenv(tmp_path, monkeypatch):
    """The paths ``prepare_cli_home`` creates are exactly the ones the wrap
    argv ``--setenv``'s — so the seeded files land where the jail points."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    redirects = rs.prepare_cli_home(
        "OP-1834-match", env={"PATH": "/usr/bin", "HOME": str(tmp_path / "h")},
    )
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-match",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "h")},
    )
    setenv = _setenv_map(argv)
    for var in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert setenv[var] == redirects[var]


def test_explicit_codex_home_is_seed_source(tmp_path, monkeypatch):
    """An explicit ``CODEX_HOME`` is the seed source; the var still points at
    the writable cli-home, not the host path."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    custom_codex = tmp_path / "custom-codex"
    custom_codex.mkdir()
    (custom_codex / "auth.json").write_text("CUSTOM-CODEX")

    seed_env = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(custom_codex),
    }
    rs.prepare_cli_home("OP-1834-codex", env=seed_env)
    argv = rs.wrap_in_bubblewrap(
        ["codex"], worktree_path=worktree, ticket_key="OP-1834-codex",
        env=seed_env,
    )

    cli_home = rs.cli_home_for("OP-1834-codex")
    setenv = _setenv_map(argv)
    assert setenv["CODEX_HOME"] == str(cli_home / ".codex")
    assert (cli_home / ".codex" / "auth.json").read_text() == "CUSTOM-CODEX"
    # The host custom path is never bound into the jail.
    assert str(custom_codex) not in argv


# ─── (d) shared host config dir is NOT RW-exposed ───────────────────


def test_host_config_dir_not_rw_bound(tmp_path, monkeypatch):
    """AC (d): the shared host ``~/.claude`` / ``~/.codex`` are never bound —
    not RW (would break tenant isolation) and not RO either."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    host_home = tmp_path / "home"
    claude_cfg = host_home / ".claude"
    codex_cfg = host_home / ".codex"
    claude_cfg.mkdir(parents=True)
    codex_cfg.mkdir()

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-d",
        env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )

    rw = _bind_targets(argv, "--bind")
    ro = _bind_targets(argv, "--ro-bind")
    assert str(claude_cfg) not in rw and str(claude_cfg) not in ro
    assert str(codex_cfg) not in rw and str(codex_cfg) not in ro
    # The host config paths do not appear anywhere in the wrapped argv.
    assert str(claude_cfg) not in argv
    assert str(codex_cfg) not in argv


def test_worktree_rw_bind_unchanged(tmp_path, monkeypatch):
    """Scope guard: the worktree is still the RW-bound code surface (the CLI
    home is ADDED, not a replacement) and is not widened to RO-read host."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-wt",
        env={"PATH": "/usr/bin", "HOME": str(tmp_path / "home")},
    )
    worktree_abs = str(worktree.resolve())
    idx = argv.index(worktree_abs)
    assert argv[idx - 1] == "--bind"  # RW
    assert argv[idx + 1] == worktree_abs


# ─── allowlist + cleanup ─────────────────────────────────────────────


def test_xdg_state_home_is_allowlisted():
    """XDG_STATE_HOME joins the allowlist so the redirected state dir survives
    the --clearenv scrub (and the degraded/raw spawn forwards it too)."""
    assert "XDG_STATE_HOME" in rs.ENV_ALLOWLIST


def test_cleanup_removes_seeded_creds(tmp_path):
    """AC 'cleaned up after': after the run, the seeded per-ticket home (and
    the creds copied into it) are gone — nothing lingers in ``/tmp``."""
    host_home = tmp_path / "home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text("SECRET")

    rs.prepare_cli_home(
        "OP-1834-clean", env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )
    cli_home = rs.cli_home_for("OP-1834-clean")
    assert (cli_home / ".claude" / ".credentials.json").exists()  # seeded

    rs.cleanup_cli_home("OP-1834-clean")
    assert not cli_home.exists()


def test_cleanup_is_noop_when_absent():
    """Cleanup tolerates a never-seeded home (degraded/raw spawn path)."""
    rs.cleanup_cli_home("OP-1834-never-existed")  # must not raise


# ─── degraded fleet: inert (no seeding when bwrap absent) ────────────


def test_degraded_wrap_is_side_effect_free(tmp_path, monkeypatch):
    """INERT guard: with bwrap absent + ENFORCE off, wrap returns the raw cmd
    and (since seeding is the runner's separate, sandbox-gated step) building
    the wrap copies nothing into /tmp."""
    monkeypatch.setattr(rs, "detect_platform", lambda: rs.PLATFORM_LINUX)
    monkeypatch.setattr(rs, "_which", lambda b: None)  # bwrap absent
    monkeypatch.delenv(rs.ENV_ENFORCE, raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    host_home = tmp_path / "home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text("SECRET")

    argv = rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-degraded",
        env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )

    assert argv == ["claude"]  # raw, unwrapped
    # The runner only seeds when sandbox_available() — never on the degraded
    # path — so nothing is copied here.
    assert not rs.cli_home_for("OP-1834-degraded").exists()


def test_wrap_argv_build_does_not_seed(tmp_path, monkeypatch):
    """Building the wrap argv (even with bwrap forced present) has NO
    filesystem side effect — seeding is the runner's explicit step, so an
    argv-shape assertion never copies the bot's creds into /tmp."""
    _force_linux_bwrap(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    host_home = tmp_path / "home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text("SECRET")

    rs.wrap_in_bubblewrap(
        ["claude"], worktree_path=worktree, ticket_key="OP-1834-nofx",
        env={"PATH": "/usr/bin", "HOME": str(host_home)},
    )
    # No cli-home created, nothing seeded.
    assert not rs.cli_home_for("OP-1834-nofx").exists()
