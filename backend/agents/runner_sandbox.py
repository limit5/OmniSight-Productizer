"""OP-845 — Bubblewrap sandbox wrapper for runner CLI invocations.

L1 *prevention* layer that complements OP-836's L1 detection (sentinel +
cwd-perm check). The CLI we launch (claude / codex) is exposed to operator
prompts that may themselves be injection-bearing, and Anthropic's 2026-05-06
guidance is unambiguous: "sandbox is the only defense that survives prompt
injection". This module wraps the CLI subprocess in an OS-level filesystem
+ network jail so that even an injection-driven attempt to write
``/home/user/.ssh/authorized_keys`` or to ``curl <attacker>`` is refused by
the kernel before reaching userland.

Two backends:

* **Linux** — ``bubblewrap`` (``bwrap``), the unprivileged-user namespace
  jail that backs Flatpak. RO-binds ``/usr`` ``/lib`` ``/etc`` ``/bin``;
  RW-binds *only* the assigned worktree + ``/tmp/runner-<ticket>/``;
  ``--unshare-net`` denies the network by default.
* **macOS** — ``sandbox-exec`` with an SBPL profile that allows file-read
  everywhere (macOS can't selectively unmount) but file-write only inside
  the worktree + ``/tmp/runner-<ticket>``; network deny by default.

The ENFORCE gate (``OMNISIGHT_RUNNER_SANDBOX_ENFORCE``) controls failure
mode when the platform's sandbox binary is absent:

* unset / ``0`` — degrade gracefully: log a warning, return the raw argv.
  This is the dev default so a workstation without bubblewrap installed
  doesn't break the runner.
* ``1`` — required: raise :class:`SandboxBinaryMissing`. Production sets
  this so a missing binary visibly aborts the pickup instead of silently
  running raw.

Network deny by default. ``OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW=<reason>``
flips the default at the env-var layer; every override is logged with the
ticket key and reason so the audit trail records who needed it and why.

Relation to OP-836:

* OP-836 sentinel lives **inside** the worktree, so the bubblewrap RW-bind
  of the worktree keeps it writable by the wrapped CLI. The post-CLI
  sentinel-verify is unchanged.
* If the wrapped CLI tries to ``git reset`` the main repo or write outside
  the worktree, bubblewrap denies it with ``EROFS`` / ``EACCES`` — the
  win condition (:class:`SandboxPermissionDenied` in the error catalog).
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)


ENV_ENFORCE = "OMNISIGHT_RUNNER_SANDBOX_ENFORCE"
"""When set to truthy (1/true/yes/on), a missing *or unsupported* sandbox
becomes a hard abort instead of a degraded raw invocation. Fail-closed:
the runner never spawns the agent CLI raw when this is on (OP-1777, L5)."""


ENV_FLEET_SERVING = "OMNISIGHT_RUNNER_FLEET_SERVING"
"""Marks this process as part of the customer-serving runner fleet. When
truthy, :func:`assert_sandbox_enforced_for_fleet` requires the sandbox to be
both enforced (``ENV_ENFORCE``) *and* available at startup, aborting the
process before any ticket is processed. Dev workstations leave it unset so a
laptop without bubblewrap still runs (OP-1777)."""


# ─── Env-scrub allowlist (OP-1777) ──────────────────────────────────
#
# SINGLE SOURCE OF TRUTH for the runner's environment allowlist. Reused
# across all three env-injecting call sites so they cannot drift:
#
#   1. the bubblewrap jail (``--clearenv`` + ``--setenv`` per name) — here;
#   2. the CLI ``subprocess.Popen(env=…)`` — ``auto-runner-jira.py``;
#   3. the git child ``subprocess.run(env=…)`` — ``jira_dispatch.py``.
#
# Closes design leaks L1 (no ``--clearenv``), L2 (Popen with no ``env=``) and
# L3 (git ``os.environ.copy()``): the agent CLI + its git children inherit
# ONLY these variables — never the runner's ``OMNISIGHT_*`` infra secrets
# (JIRA tokens, project-state API tokens, fleet-health canary keys, …).
ENV_ALLOWLIST: tuple[str, ...] = (
    # --- process runtime ---
    "PATH",
    "HOME",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "TERM",
    "TZ",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # --- git / ssh transport (gerrit fetch + push) ---
    "GIT_SSH_COMMAND",
    "SSH_AUTH_SOCK",
    # --- agent CLI credentials + model selection (the "bot creds") ---
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "CLAUDE_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    # --- agent CLI config / cache / state dirs ---
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    # --- agent CLI config dirs: forwarded so the degraded/raw spawn still
    # finds the bot's config when bwrap is absent. On the bwrap path they are
    # --setenv'd to the writable per-ticket cli-home instead (OP-1834,
    # superseding the OP-1803 §2b RO config bind that broke session init).
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)
"""Names of the only env vars projected into the agent CLI + its git
children. Anything not listed here (notably every ``OMNISIGHT_*`` infra
secret) is scrubbed. Edit in ONE place; all three call sites reuse it."""


def build_allowlisted_env(
    base: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a scrubbed env containing only :data:`ENV_ALLOWLIST` names.

    Args:
        base: source mapping to scrub. Defaults to ``os.environ``.
        extra: variables to set/override *after* scrubbing — e.g. the git
            call sites inject a freshly-computed ``GIT_SSH_COMMAND``. Only
            non-None values are applied.

    Returns:
        A fresh dict holding each allowlisted key present in ``base`` (plus
        any ``extra``). The runner's ``OMNISIGHT_*`` secrets never appear.
    """
    src = os.environ if base is None else base
    env = {name: src[name] for name in ENV_ALLOWLIST if name in src}
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


ENV_NETWORK_ALLOW = "OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW"
"""When non-empty, value is the audit reason for permitting network access.
Logged via :class:`SandboxNetworkPolicyOverride`."""


PLATFORM_LINUX = "linux"
PLATFORM_MACOS = "darwin"

LINUX_BINARY = "bwrap"
MACOS_BINARY = "sandbox-exec"


LINUX_READONLY_MOUNTS: tuple[str, ...] = (
    "/usr", "/lib", "/lib64", "/etc", "/bin", "/sbin",
)
"""Paths the bubblewrap jail RO-binds when present. ``/lib64`` and ``/sbin``
are absent on some distros; we skip mounts that don't exist."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_git_path(raw: str, worktree_path: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = worktree_path / path
    return path.resolve()


def _git_metadata_mounts(worktree_path: Path) -> tuple[Path, ...]:
    """Return git metadata dirs outside ``worktree_path`` that need RW bind.

    Linked git worktrees have a ``.git`` pointer file inside the worktree
    that refers to ``<main>/.git/worktrees/<name>`` plus the shared common
    dir. Without those mounts, ``git commit`` inside the wrapped CLI sees
    editable files but cannot update git metadata.
    """
    worktree_abs = worktree_path.resolve()
    try:
        out = subprocess.run(
            [
                "git", "-C", str(worktree_abs), "rev-parse",
                "--git-dir", "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return ()

    mounts: list[Path] = []
    for raw in out:
        raw = raw.strip()
        if not raw:
            continue
        path = _resolve_git_path(raw, worktree_abs)
        if not path.exists() or _is_relative_to(path, worktree_abs):
            continue
        if path not in mounts:
            mounts.append(path)
    return tuple(mounts)


def _nvm_version_dir_of(node_path: Path) -> Path | None:
    """Return the ``.../versions/node/<ver>/`` dir containing ``node_path``.

    The nvm layout is ``<NVM_DIR>/versions/node/<ver>/bin/node``; the subtree
    we want to RO-bind is ``<ver>/`` (it holds ``bin/node`` plus the globally
    installed ``claude`` / ``codex`` shims under ``bin/`` and their
    ``lib/node_modules``). Returns None when ``node_path`` is not under an nvm
    versions tree (e.g. a distro ``/usr/bin/node``, already covered by the
    ``/usr`` RO-bind).
    """
    for parent in node_path.parents:
        gp = parent.parent
        if gp.name == "node" and gp.parent.name == "versions":
            return parent if parent.is_dir() else None
    return None


def _nvm_default_version_dir(nvm_dir: Path) -> Path | None:
    """Resolve ``$NVM_DIR``'s active node version dir without a live ``node``.

    Prefers the ``alias/default`` pin (matched by exact name or version
    prefix); falls back to the lexically-greatest installed version. Best
    effort — returns None when ``$NVM_DIR/versions/node`` has no entries.
    """
    versions = nvm_dir / "versions" / "node"
    if not versions.is_dir():
        return None
    installed = sorted(
        (d for d in versions.iterdir() if d.is_dir()), reverse=True
    )
    if not installed:
        return None
    alias = nvm_dir / "alias" / "default"
    if alias.is_file():
        try:
            want = alias.read_text().strip().lstrip("v")
        except OSError:
            want = ""
        if want:
            for cand in installed:
                if cand.name.lstrip("v").startswith(want):
                    return cand
    return installed[0]


def _nvm_node_toolchain_subtree(env: Mapping[str, str]) -> Path | None:
    """Resolve the node-version subtree to RO-bind into the jail (OP-1803 §2a).

    Resolves the live interpreter via ``which node`` (using the jail's PATH so
    it matches what will run) and returns the enclosing
    ``~/.nvm/versions/node/<ver>/`` directory — node + the ``claude`` / ``codex``
    CLIs + their ``node_modules`` — NOT all of ``~/.nvm``. Falls back to
    ``$NVM_DIR``'s default-aliased version when ``node`` isn't on PATH. Returns
    None when no nvm-managed toolchain is found (distro node lives under the
    already-bound ``/usr``).
    """
    node = shutil.which("node", path=env.get("PATH"))
    if node:
        subtree = _nvm_version_dir_of(Path(node).resolve())
        if subtree is not None:
            return subtree
    nvm_dir = env.get("NVM_DIR")
    if nvm_dir:
        return _nvm_default_version_dir(Path(nvm_dir))
    return None


# Writable per-ticket CLI home (OP-1834). The wrapped CLI must WRITE its
# session / cache / state, so we give it a HOME under the already-RW
# /tmp/runner-<ticket> scratch, seed it with the host auth/config (so the CLI
# still authenticates), and redirect HOME + the CLI/XDG dirs there. RW-binding
# the *shared* host config dir would break tenant isolation, so we copy instead.
_CLI_HOME_DIRNAME = "cli-home"


# Agent-CLI config dirs (OP-1834): (ENV_NAME, relative subdir). The relative
# name is BOTH the conventional host default (``~/.claude`` / ``~/.codex``) and
# the subdir created inside the writable CLI home that the host contents are
# seeded into + the var is pointed at.
_CLI_CONFIG_DIRS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CONFIG_DIR", ".claude"),
    ("CODEX_HOME", ".codex"),
)


# XDG base dirs projected inside the writable CLI home (OP-1834). The CLI may
# write to each, so they are redirected off any RO/absent host path into the
# per-ticket writable home. ``HOME`` itself is the cli-home root.
_CLI_HOME_XDG_DIRS: tuple[tuple[str, str], ...] = (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_STATE_HOME", ".local/state"),
)


def _cli_home_redirect_env(ticket_key: str) -> dict[str, str]:
    """Return the ``{HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_*}`` → in-jail
    path mapping for the per-ticket CLI home (OP-1834).

    Pure: derived from :func:`cli_home_for` only — no host values, no FS
    writes. Used by the argv builder to ``--setenv`` the vars and by
    :func:`prepare_cli_home` to know where to create/seed. Keeping it pure is
    what lets the argv builder stay side-effect-free (the actual seeding is the
    runner's explicit :func:`prepare_cli_home` step).
    """
    cli_home = cli_home_for(ticket_key)
    env: dict[str, str] = {"HOME": str(cli_home)}
    for name, rel in _CLI_CONFIG_DIRS:
        env[name] = str(cli_home / rel)
    for name, rel in _CLI_HOME_XDG_DIRS:
        env[name] = str(cli_home / rel)
    return env


def prepare_cli_home(
    ticket_key: str, env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create + seed the writable per-ticket CLI home (OP-1834).

    The jailed CLI must WRITE its session/cache/state, but RW-binding the
    shared host ``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR`` would break tenant
    isolation. So the runner calls this once, before spawning the wrapped CLI,
    to:

    * create ``cli_home`` (a subdir of the RW ``/tmp/runner-<ticket>`` scratch);
    * COPY each host CLI config dir's CONTENTS into ``cli_home/<rel>`` so the
      bot's auth is seeded + readable AND the CLI writes its session/cache to
      the SAME writable dir (the host dir is never bound, so writes never leak
      back to the shared config — isolation preserved);
    * create the XDG base dirs inside ``cli_home``.

    Deliberately SEPARATE from :func:`wrap_in_bubblewrap` so building the wrap
    argv has no filesystem side effects, and so the degraded fleet (no bwrap)
    never copies the bot's creds into ``/tmp`` — the runner only calls this when
    :func:`sandbox_available` is true, keeping the fix INERT until re-enabled.

    The copy is best-effort per host dir: an unreadable/special file (e.g. a
    cache pack with restrictive perms) is logged and skipped rather than
    aborting the seed — the auth files copy first and are what the CLI needs.

    Returns the same redirect mapping as :func:`_cli_home_redirect_env`.
    """
    src_env = os.environ if env is None else env
    cli_home = cli_home_for(ticket_key)
    cli_home.mkdir(parents=True, exist_ok=True)
    host_home = src_env.get("HOME") or os.path.expanduser("~")

    for name, rel in _CLI_CONFIG_DIRS:
        host_raw = src_env.get(name) or os.path.join(host_home, rel)
        host_path = Path(host_raw)
        dest = cli_home / rel
        if host_path.is_dir():
            try:
                shutil.copytree(
                    host_path, dest, dirs_exist_ok=True,
                    ignore_dangling_symlinks=True,
                )
            except (shutil.Error, OSError) as exc:
                # copytree copies everything it can and raises at the end with
                # the unreadable files; the auth/config we need is already in.
                logger.warning(
                    "[cli-home-seed] partial copy of %s for %s: %s",
                    host_path, ticket_key, exc,
                )
        else:
            dest.mkdir(parents=True, exist_ok=True)

    for _, rel in _CLI_HOME_XDG_DIRS:
        (cli_home / rel).mkdir(parents=True, exist_ok=True)

    return _cli_home_redirect_env(ticket_key)


LOG_SANDBOX_WRAPPED = "sandbox=wrapped"
LOG_SANDBOX_DEGRADED = "sandbox=degraded"
LOG_SANDBOX_DISABLED = "sandbox=disabled"


class SandboxBinaryMissing(RuntimeError):
    """Raised when the platform's sandbox binary is absent and ENFORCE=1.

    Caller (the runner) is expected to operator-alert + abort the ticket
    pickup rather than spawning the CLI raw — production deploys turn
    ENFORCE on precisely because they want a missing binary to block.
    """

    def __init__(self, platform_name: str, binary: str) -> None:
        self.platform = platform_name
        self.binary = binary
        super().__init__(
            f"sandbox binary {binary!r} not on PATH for platform "
            f"{platform_name!r}; install it (apt install bubblewrap on "
            f"Debian/Ubuntu) or unset {ENV_ENFORCE} to permit raw runs"
        )


class SandboxUnsupportedPlatform(RuntimeError):
    """Raised when ENFORCE=1 and the OS has no supported sandbox backend.

    Fail-closed counterpart of :class:`SandboxBinaryMissing` for the L5 leak
    (sandbox degrades to raw exec). With ENFORCE on, an unsupported platform
    (neither Linux/bubblewrap nor macOS/seatbelt) must abort the pickup
    rather than spawning the agent CLI unsandboxed.
    """

    def __init__(self, platform_name: str) -> None:
        self.platform = platform_name
        super().__init__(
            f"no supported sandbox backend for platform {platform_name!r}; "
            f"the runner is configured fail-closed ({ENV_ENFORCE}=1) so it "
            f"refuses to spawn the agent CLI raw — unset {ENV_ENFORCE} only "
            f"on a trusted dev host to permit a degraded run"
        )


class SandboxNotEnforced(RuntimeError):
    """Raised at startup when the customer-serving fleet is not fail-closed.

    The serving fleet (``OMNISIGHT_RUNNER_FLEET_SERVING`` truthy) MUST have
    the sandbox enforced *and* available before processing any ticket. If
    ENFORCE is off, or the platform's sandbox binary is missing, this aborts
    startup so a misconfigured fleet node never serves customer pickups with
    an env-leaking, unsandboxed CLI (OP-1777).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"customer-serving fleet requires an enforced sandbox: {reason}"
        )


class SandboxNetworkPolicyOverride(RuntimeError):
    """Informational: a network-deny default was overridden for this ticket.

    Not raised at the call site — instantiated by :func:`wrap_in_bubblewrap`
    and emitted to the audit log so the trail records who needed network,
    for which ticket, and why.
    """

    def __init__(self, ticket_key: str, reason: str) -> None:
        self.ticket_key = ticket_key
        self.reason = reason
        super().__init__(
            f"sandbox network policy overridden for {ticket_key}: {reason}"
        )


class SandboxPermissionDenied(RuntimeError):
    """Raised when the wrapped CLI attempted to write outside its jail.

    This is the *win condition* — the bubblewrap RO-mount surface caught a
    write that would have escaped the worktree. Not constructed by this
    module; the runner detects ``EACCES`` / ``EROFS`` on the CLI's stderr
    + correlates with sandbox state to wrap into this typed error.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"sandbox blocked CLI write outside worktree: {path}"
        )


def _enforce_enabled() -> bool:
    val = os.environ.get(ENV_ENFORCE, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def detect_platform() -> str:
    """Return the lowercase OS name — ``linux``, ``darwin``, or other."""
    return platform.system().lower()


def _platform_binary(plat: str) -> str | None:
    """Return the expected sandbox binary name for ``plat``, else None."""
    if plat == PLATFORM_LINUX:
        return LINUX_BINARY
    if plat == PLATFORM_MACOS:
        return MACOS_BINARY
    return None


def _which(binary: str) -> str | None:
    return shutil.which(binary)


def _safe_ticket(ticket_key: str) -> str:
    """Sanitise a ticket key for use as a ``/tmp/runner-*`` path component."""
    return ticket_key.replace("/", "_").replace("..", "_")


def _tmp_dir_for(ticket_key: str) -> Path:
    """Return the ``/tmp/runner-<ticket_key>`` path, creating it if absent."""
    tmp = Path("/tmp") / f"runner-{_safe_ticket(ticket_key)}"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def cli_home_for(ticket_key: str) -> Path:
    """Return the writable per-ticket CLI-home path (OP-1834).

    A subdir of the already-RW per-ticket scratch (``/tmp/runner-<ticket>``)
    that the jail uses as the wrapped CLI's ``HOME`` + state/config/cache base.
    Pure path computation — does NOT create the dir (the runner seeds it via
    :func:`prepare_cli_home`; callers resolving HOME-relative mounts only need
    the path).
    """
    return Path("/tmp") / f"runner-{_safe_ticket(ticket_key)}" / _CLI_HOME_DIRNAME


def cleanup_cli_home(ticket_key: str) -> None:
    """Remove the seeded per-ticket CLI home so creds never outlive the run.

    The jail seeds the CLI's auth/config into :func:`cli_home_for` (OP-1834),
    so once the wrapped CLI exits we must wipe it — otherwise the bot's
    credentials linger in ``/tmp`` past the pickup. Best-effort: a missing dir
    (degraded/raw spawn never seeds one) is a silent no-op.
    """
    shutil.rmtree(cli_home_for(ticket_key), ignore_errors=True)


def _resolve_network(ticket_key: str, network: bool) -> bool:
    """Resolve the effective network policy + log audit on override.

    Caller-passed ``network=True`` always wins (the call site knows what it
    is doing). Otherwise, ``OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW`` flips
    the default and the override is logged.
    """
    if network:
        return True
    env_reason = os.environ.get(ENV_NETWORK_ALLOW, "").strip()
    if env_reason:
        override = SandboxNetworkPolicyOverride(ticket_key, env_reason)
        logger.warning("[sandbox-network-override] %s", override)
        return True
    return False


def _build_bubblewrap_argv(
    cmd: list[str],
    *,
    worktree_path: Path,
    ticket_key: str,
    network: bool,
    bwrap_bin: str,
    env: Mapping[str, str],
    dep_cache_mounts: Sequence[tuple[str, str]] | None = None,
) -> list[str]:
    """Compose the bubblewrap argv prefix for ``cmd``.

    The argv shape is asserted in tests — keep flag order stable so test
    regressions point at real changes (mount surface drift) rather than
    cosmetic re-orderings.

    ``--clearenv`` wipes the inherited environment inside the jail; the
    allowlisted ``env`` is then re-projected one ``--setenv`` at a time
    (OP-1777, L1). HOME / TMPDIR are pinned to the jail paths below rather
    than carried over from the host.

    ``dep_cache_mounts`` (OP-1781, 1A.4) are ``(host_src, jail_dst)`` pairs
    RO-bound *after* the worktree bind so the pre-warmed per-tenant
    dependency cache layers on top of the worktree HOME at the package
    manager's default location. RO so the jailed build can read but never
    mutate the shared-per-tenant cache, and the cache buys offline
    resolution while ``--unshare-net`` stays the default.

    OP-1803 (§2a): the resolved nvm node-version subtree (node + the
    ``claude`` / ``codex`` CLIs + ``node_modules``) is RO-bound so the jailed
    CLI can actually ``execvp``.

    OP-1834: the wrapped CLI gets a WRITABLE per-ticket home under the RW
    ``/tmp/runner-<ticket>`` scratch (``cli-home``), RW-bound here, with
    ``HOME`` + ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` / ``XDG_*`` ``--setenv``'d
    there so session/cache/state writes succeed (the OP-1803 RO config bind
    made them fail with ``EROFS``). The dir's CONTENTS are seeded with the host
    auth/config by the runner's :func:`prepare_cli_home` BEFORE spawn (kept out
    of this builder so it has no filesystem side effects); the shared host
    config dir is NEVER bound, so tenant isolation holds. These pinned vars are
    skipped in the generic allowlist projection and set explicitly below.
    """
    worktree_abs = str(worktree_path.resolve())
    tmp_dir = str(_tmp_dir_for(ticket_key))

    # --clearenv must precede every --setenv: bwrap applies flags in order,
    # so clearing first then re-setting yields exactly the allowlist.
    argv: list[str] = [
        bwrap_bin, "--die-with-parent", "--new-session", "--clearenv",
    ]

    if not network:
        argv.append("--unshare-net")

    argv += ["--proc", "/proc", "--dev", "/dev"]

    for ro in LINUX_READONLY_MOUNTS:
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]

    # OP-1803 (§2a): RO-bind the resolved nvm node-version subtree so the jail
    # can execvp the agent CLI. We bind the specific <ver>/ dir (node + the
    # claude/codex shims + node_modules), NOT all of ~/.nvm.
    toolchain = _nvm_node_toolchain_subtree(env)
    if toolchain is not None and toolchain.exists():
        tc_abs = str(toolchain)
        argv += ["--ro-bind", tc_abs, tc_abs]

    argv += ["--bind", worktree_abs, worktree_abs]
    argv += ["--bind", tmp_dir, tmp_dir]

    # OP-1834: RW-bind the writable per-ticket CLI home under the RW scratch so
    # the CLI can write session/cache/state. The dir's CONTENTS are seeded with
    # the host auth/config by the runner's prepare_cli_home() before spawn (NOT
    # here — the argv builder stays side-effect-free; the shared host config dir
    # is never bound). ``cli_env`` maps HOME + CLAUDE_CONFIG_DIR / CODEX_HOME /
    # XDG_* to paths inside it; they are --setenv'd below (and skipped in the
    # generic allowlist projection so host values never override them).
    cli_home_abs = str(cli_home_for(ticket_key))
    cli_env = _cli_home_redirect_env(ticket_key)
    argv += ["--bind", cli_home_abs, cli_home_abs]

    for git_dir in _git_metadata_mounts(worktree_path):
        git_dir_abs = str(git_dir)
        argv += ["--bind", git_dir_abs, git_dir_abs]

    # OP-1781 (1A.4): RO-bind the pre-warmed per-tenant dependency cache at the
    # package manager's default HOME-relative location so the build finds it
    # offline. Bound after the RW homes so it layers correctly; RO so the build
    # can't mutate the shared cache. The caller computes the dst against the
    # jail's HOME — the writable per-ticket cli-home as of OP-1834.
    for src, dst in dep_cache_mounts or ():
        argv += ["--ro-bind", src, dst]

    # Re-project the scrubbed allowlist into the cleared jail env. TMPDIR is
    # pinned to the jail path below; HOME + CLAUDE_CONFIG_DIR / CODEX_HOME /
    # XDG_* are pinned to the writable per-ticket CLI home (OP-1834) — so skip
    # any host-inherited values for all of them here (the explicit values win).
    pinned = {"TMPDIR", *cli_env}
    for name, value in build_allowlisted_env(env).items():
        if name in pinned:
            continue
        argv += ["--setenv", name, value]

    # Pin TMPDIR inside the jail; point HOME + the CLI/XDG dirs at the writable
    # per-ticket cli-home (OP-1834) so session/cache/state writes succeed. cwd
    # stays the worktree — the CLI works on the code there, HOME lives apart.
    argv += ["--setenv", "TMPDIR", tmp_dir]
    for name, value in cli_env.items():
        argv += ["--setenv", name, value]
    argv += ["--chdir", worktree_abs]

    argv += ["--"]
    argv += list(cmd)
    return argv


def _build_sandbox_exec_argv(
    cmd: list[str],
    *,
    worktree_path: Path,
    ticket_key: str,
    network: bool,
    sandbox_exec_bin: str,
) -> list[str]:
    """Compose the seatbelt (``sandbox-exec``) argv prefix for ``cmd``.

    macOS sandbox can't selectively unmount — file-read is permitted
    everywhere, file-write only inside the worktree + ``/tmp/runner-X``.
    Network: deny by default, allow when override is in force.
    """
    worktree_abs = str(worktree_path.resolve())
    tmp_dir = str(_tmp_dir_for(ticket_key))

    net_clause = "(allow network*)" if network else "(deny network*)"
    profile_lines = (
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal)",
        "(allow file-read*)",
        f'(allow file-write* (subpath "{worktree_abs}"))',
        f'(allow file-write* (subpath "{tmp_dir}"))',
        '(allow file-write* (subpath "/private/tmp"))',
        "(allow sysctl-read)",
        net_clause,
    )
    profile = "\n".join(profile_lines) + "\n"
    return [sandbox_exec_bin, "-p", profile, *cmd]


def wrap_in_bubblewrap(
    cmd: list[str],
    *,
    worktree_path: Path,
    ticket_key: str = "default",
    network: bool = False,
    env: Mapping[str, str] | None = None,
    dep_cache_mounts: Sequence[tuple[str, str]] | None = None,
) -> list[str]:
    """Return ``cmd`` prefixed with a sandbox-runner argv.

    Platform dispatch: Linux → bubblewrap; macOS → seatbelt; anything else
    → identity (log a warning so the operator sees ``sandbox=disabled`` on
    process startup).

    Network is denied by default. Two ways to override:

    * pass ``network=True`` at the call site (caller-acknowledged need);
    * set ``OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW=<reason>`` in the env.
      Every env-driven override is logged with the ticket key + reason.

    ENFORCE behaviour (``OMNISIGHT_RUNNER_SANDBOX_ENFORCE``) controls the
    missing-binary path:

    * unset / ``0`` — degrade: log + return raw ``cmd`` so the runner can
      still ship work. Dev default.
    * ``1`` — required: raise :class:`SandboxBinaryMissing`. Production
      sets this so a missing binary visibly aborts the pickup.

    Args:
        cmd: The argv to wrap, e.g. ``["claude", "-p", "<prompt>"]`` or
            ``["codex", "exec", "--cd", "<dir>", "--yolo"]``.
        worktree_path: The ticket's assigned worktree — the *only* RW-bound
            path inside the jail (besides ``/tmp/runner-<ticket_key>``).
        ticket_key: Ticket identifier used to namespace the per-jail
            ``/tmp/runner-<ticket_key>`` scratch directory. Defaults to
            ``"default"`` so callers that don't know the ticket (tests,
            ad-hoc invocations) still get a consistent layout.
        network: ``False`` (deny, the default) or ``True`` (allow). The env
            override flips the default to allow when set.
        env: source environment to scrub through :data:`ENV_ALLOWLIST`
            before projecting into the jail. Defaults to ``os.environ``.
            The same allowlist gates the caller's ``Popen(env=…)`` so the
            jail and the (degraded/macOS) raw spawn see identical vars.
        dep_cache_mounts: OP-1781 (1A.4) ``(host_src, jail_dst)`` pairs for
            the pre-warmed per-tenant dependency cache. Each is RO-bound into
            the Linux jail at ``jail_dst`` (the package manager's default
            HOME-relative cache location), so a build resolves offline while
            ``--unshare-net`` stays the default. Ignored on macOS — seatbelt
            permits file-read everywhere already and cannot bind-relocate a
            path, so the customer-serving (Linux/bubblewrap) fleet is where
            the offline cache is enforced.

    Returns:
        The full argv to spawn. On a missing binary with ENFORCE=0, this is
        simply ``list(cmd)`` so the caller's spawn is a no-op-wrapping path.

    Raises:
        SandboxBinaryMissing: when ENFORCE=1 and the platform binary is
            absent. Caller is expected to operator-alert + abort.
        SandboxUnsupportedPlatform: when ENFORCE=1 on a platform with no
            supported sandbox backend (L5 fail-closed) — never returns raw.
    """
    effective_network = _resolve_network(ticket_key, network)
    scrub_src = os.environ if env is None else env
    plat = detect_platform()

    if plat == PLATFORM_LINUX:
        bwrap = _which(LINUX_BINARY)
        if bwrap is None:
            return _handle_missing_binary(plat, LINUX_BINARY, cmd)
        argv = _build_bubblewrap_argv(
            cmd,
            worktree_path=worktree_path,
            ticket_key=ticket_key,
            network=effective_network,
            bwrap_bin=bwrap,
            env=scrub_src,
            dep_cache_mounts=dep_cache_mounts,
        )
        logger.info(
            "[%s] ticket=%s argv0=%s network=%s worktree=%s",
            LOG_SANDBOX_WRAPPED, ticket_key, cmd[0], effective_network,
            worktree_path,
        )
        return argv

    if plat == PLATFORM_MACOS:
        sb = _which(MACOS_BINARY)
        if sb is None:
            return _handle_missing_binary(plat, MACOS_BINARY, cmd)
        argv = _build_sandbox_exec_argv(
            cmd,
            worktree_path=worktree_path,
            ticket_key=ticket_key,
            network=effective_network,
            sandbox_exec_bin=sb,
        )
        logger.info(
            "[%s] ticket=%s argv0=%s network=%s worktree=%s",
            LOG_SANDBOX_WRAPPED, ticket_key, cmd[0], effective_network,
            worktree_path,
        )
        return argv

    # Unsupported platform. Fail-closed under ENFORCE (L5): never spawn the
    # agent CLI raw when the operator demanded a sandbox.
    if _enforce_enabled():
        raise SandboxUnsupportedPlatform(plat)
    logger.warning(
        "[sandbox-unsupported-platform] platform=%s; %s — invoking raw cmd",
        plat, LOG_SANDBOX_DISABLED,
    )
    return list(cmd)


def _handle_missing_binary(plat: str, binary: str, cmd: list[str]) -> list[str]:
    """Common path for ``binary not on PATH`` on Linux + macOS.

    ENFORCE=1 → raise; ENFORCE=0 → log degraded + return raw cmd.
    """
    if _enforce_enabled():
        raise SandboxBinaryMissing(plat, binary)
    logger.warning(
        "[sandbox-binary-missing] %s not on PATH; %s — invoking raw cmd",
        binary, LOG_SANDBOX_DEGRADED,
    )
    return list(cmd)


def sandbox_available() -> bool:
    """Return True when the platform's sandbox binary is on PATH.

    Used at startup so the runner can log ``sandbox=wrapped`` vs
    ``sandbox=degraded`` before processing any ticket, giving the operator
    a clear journalctl signal that bubblewrap is wired."""
    plat = detect_platform()
    binary = _platform_binary(plat)
    if binary is None:
        return False
    return _which(binary) is not None


def _fleet_serving() -> bool:
    val = os.environ.get(ENV_FLEET_SERVING, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def assert_sandbox_enforced_for_fleet() -> None:
    """Startup gate (OP-1777): customer-serving fleet must be fail-closed.

    No-op unless ``OMNISIGHT_RUNNER_FLEET_SERVING`` is truthy. On the serving
    fleet, requires BOTH ``OMNISIGHT_RUNNER_SANDBOX_ENFORCE`` enabled AND the
    platform sandbox binary present — otherwise raises
    :class:`SandboxNotEnforced` so the node aborts before serving any pickup
    with an env-leaking, unsandboxed CLI.

    Raises:
        SandboxNotEnforced: serving fleet with ENFORCE off or sandbox absent.
    """
    if not _fleet_serving():
        return
    if not _enforce_enabled():
        raise SandboxNotEnforced(
            f"{ENV_FLEET_SERVING} is set but {ENV_ENFORCE} is not enabled"
        )
    if not sandbox_available():
        plat = detect_platform()
        binary = _platform_binary(plat) or "<none>"
        raise SandboxNotEnforced(
            f"{ENV_ENFORCE} is enabled but sandbox binary {binary!r} is "
            f"absent on platform {plat!r}"
        )


__all__ = [
    "ENV_ENFORCE",
    "ENV_FLEET_SERVING",
    "ENV_NETWORK_ALLOW",
    "ENV_ALLOWLIST",
    "LINUX_BINARY",
    "MACOS_BINARY",
    "LINUX_READONLY_MOUNTS",
    "LOG_SANDBOX_WRAPPED",
    "LOG_SANDBOX_DEGRADED",
    "LOG_SANDBOX_DISABLED",
    "SandboxBinaryMissing",
    "SandboxUnsupportedPlatform",
    "SandboxNotEnforced",
    "SandboxNetworkPolicyOverride",
    "SandboxPermissionDenied",
    "build_allowlisted_env",
    "assert_sandbox_enforced_for_fleet",
    "cli_home_for",
    "cleanup_cli_home",
    "prepare_cli_home",
    "detect_platform",
    "sandbox_available",
    "wrap_in_bubblewrap",
]
