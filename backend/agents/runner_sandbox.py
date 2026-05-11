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
from pathlib import Path

logger = logging.getLogger(__name__)


ENV_ENFORCE = "OMNISIGHT_RUNNER_SANDBOX_ENFORCE"
"""When set to truthy (1/true/yes/on), missing sandbox binary becomes a hard
abort instead of a degraded raw invocation."""


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


def _tmp_dir_for(ticket_key: str) -> Path:
    """Return the ``/tmp/runner-<ticket_key>`` path, creating it if absent."""
    safe = ticket_key.replace("/", "_").replace("..", "_")
    tmp = Path("/tmp") / f"runner-{safe}"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


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
) -> list[str]:
    """Compose the bubblewrap argv prefix for ``cmd``.

    The argv shape is asserted in tests — keep flag order stable so test
    regressions point at real changes (mount surface drift) rather than
    cosmetic re-orderings.
    """
    worktree_abs = str(worktree_path.resolve())
    tmp_dir = str(_tmp_dir_for(ticket_key))

    argv: list[str] = [bwrap_bin, "--die-with-parent", "--new-session"]

    if not network:
        argv.append("--unshare-net")

    argv += ["--proc", "/proc", "--dev", "/dev"]

    for ro in LINUX_READONLY_MOUNTS:
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]

    argv += ["--bind", worktree_abs, worktree_abs]
    argv += ["--bind", tmp_dir, tmp_dir]

    # Pin HOME / TMPDIR / cwd inside the jail so the CLI doesn't probe
    # for a writable $HOME outside the worktree.
    argv += ["--setenv", "HOME", worktree_abs]
    argv += ["--setenv", "TMPDIR", tmp_dir]
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

    Returns:
        The full argv to spawn. On a missing binary with ENFORCE=0, this is
        simply ``list(cmd)`` so the caller's spawn is a no-op-wrapping path.

    Raises:
        SandboxBinaryMissing: when ENFORCE=1 and the platform binary is
            absent. Caller is expected to operator-alert + abort.
    """
    effective_network = _resolve_network(ticket_key, network)
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


__all__ = [
    "ENV_ENFORCE",
    "ENV_NETWORK_ALLOW",
    "LINUX_BINARY",
    "MACOS_BINARY",
    "LINUX_READONLY_MOUNTS",
    "LOG_SANDBOX_WRAPPED",
    "LOG_SANDBOX_DEGRADED",
    "LOG_SANDBOX_DISABLED",
    "SandboxBinaryMissing",
    "SandboxNetworkPolicyOverride",
    "SandboxPermissionDenied",
    "detect_platform",
    "sandbox_available",
    "wrap_in_bubblewrap",
]
