"""Phase 64-C-LOCAL S1 — T3 runner resolver.

Historical T3 design assumed "a remote hardware daemon". That's fine
for Jetson / Rockchip / MCU targets but overkill for the 90% use
case: host == target (AMD 9950X WSL deploying to its own x86_64
Linux). This module is the resolver layer that picks the right
runner class based on arch/OS match + available capability.

Hierarchy (top first; later SSH/QEMU land in follow-up phases):

    required_tier=t3 → resolve_t3_runner(task, target_profile)
      ├─ host_arch == target_arch && host_os == target_os → LOCAL  ⭐
      ├─ registered_remote_runner_matches                  → SSH    (64-C-SSH)
      ├─ can_qemu_emulate(target_arch)                     → QEMU   (64-C-QEMU)
      └─ fallback                                          → BUNDLE (current)

The resolver is consulted by:
  * dag_validator — `tier_violation` rule passes when LOCAL (or any
    live runner) can serve the task, fails otherwise.
  * container.py T3 executor — dispatches to the chosen runner.
  * Ops Summary panel — shows per-runner dispatch counts.

Opt-out: `OMNISIGHT_T3_LOCAL_ENABLED=false` forces BUNDLE even when
LOCAL would match. Safe default for paranoid deployments; prod use
keeps it enabled so single-arch deploys stay automated.
"""

from __future__ import annotations

import logging
import os
import platform as _stdlib_platform
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from backend.routers.system import _canon_arch

logger = logging.getLogger(__name__)


class T3RunnerKind(str, Enum):
    """What kind of runner will serve this task. String values match
    the metric label + UI chip copy so ops dashboards don't need a
    second enum."""
    LOCAL = "local"
    SSH = "ssh"      # Phase 64-C-SSH
    QEMU = "qemu"    # Phase 64-C-QEMU
    BUNDLE = "bundle"


@dataclass(frozen=True)
class T3Resolution:
    kind: T3RunnerKind
    reason: str                 # human-readable "why this runner"
    target_arch: str            # canonical
    target_os: str              # linux|darwin|windows|…
    host_arch: str
    host_os: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Host introspection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def host_arch() -> str:
    """Canonical host arch. Thin wrapper so tests can monkeypatch."""
    return _canon_arch(_stdlib_platform.machine())


def host_os() -> str:
    """Canonical host OS. Linux / darwin / windows; `wsl` folded into
    `linux` because WSL2 runs a real Linux kernel."""
    return _stdlib_platform.system().lower() or "linux"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ssh_enabled() -> bool:
    """Kill-switch for SSH runner. Default on."""
    raw = (os.environ.get("OMNISIGHT_SSH_RUNNER_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _local_enabled() -> bool:
    """Kill-switch env. Default on; set `false` to force BUNDLE even
    when LOCAL would match."""
    raw = (os.environ.get("OMNISIGHT_T3_LOCAL_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def native_arch_matches(target_arch: str, target_os: str = "linux") -> bool:
    """Return True iff the host can natively execute the target's
    binaries — same arch, same OS. Empty / unknown target arguments
    intentionally return False: a missing target_arch must not be
    silently treated as "matches host", or an under-specified DAG
    would auto-route to LOCAL and hide its real target requirement.
    """
    if not target_arch or not target_os:
        return False
    return (
        _canon_arch(target_arch) == host_arch()
        and target_os.lower() == host_os()
    )


def resolve_t3_runner(
    target_arch: str = "",
    target_os: str = "linux",
) -> T3Resolution:
    """Pick a T3 runner for the given target. Single entry point —
    both the validator (S3) and the executor (S2) go through here so
    their decisions can't drift.

    Order of preference: LOCAL → (SSH → QEMU in future phases) →
    BUNDLE. Each candidate is checked in turn; the first that's
    available wins.
    """
    h_arch = host_arch()
    h_os = host_os()
    t_arch = _canon_arch(target_arch) if target_arch else ""
    t_os = (target_os or "linux").lower()

    # 1. LOCAL — same arch, same OS, kill-switch not pulled.
    if _local_enabled() and native_arch_matches(target_arch, target_os):
        return T3Resolution(
            kind=T3RunnerKind.LOCAL,
            reason=f"host ({h_arch}/{h_os}) matches target ({t_arch}/{t_os})",
            target_arch=t_arch, target_os=t_os,
            host_arch=h_arch, host_os=h_os,
        )

    # 2. SSH — Phase 64-C-SSH. Check if a registered remote runner
    #    matches the target arch.
    if _ssh_enabled() and t_arch:
        from backend.ssh_runner import find_target_for_arch
        ssh_target = find_target_for_arch(t_arch, t_os)
        if ssh_target is not None:
            return T3Resolution(
                kind=T3RunnerKind.SSH,
                reason=(
                    f"host ({h_arch}/{h_os}) ≠ target ({t_arch}/{t_os}); "
                    f"SSH runner registered at {ssh_target.host}"
                ),
                target_arch=t_arch, target_os=t_os,
                host_arch=h_arch, host_os=h_os,
            )

    # 3. QEMU — deferred (Phase 64-C-QEMU).

    # 4. BUNDLE — current fallback: package artefact + install.sh,
    #    operator runs on target manually.
    if not t_arch:
        reason = "target arch not specified — cannot match host; producing bundle"
    elif not _local_enabled():
        reason = "OMNISIGHT_T3_LOCAL_ENABLED=false; producing bundle"
    else:
        reason = (
            f"host ({h_arch}/{h_os}) does not match target ({t_arch}/{t_os}); "
            "producing bundle (no SSH/QEMU runner matched)"
        )
    return T3Resolution(
        kind=T3RunnerKind.BUNDLE,
        reason=reason,
        target_arch=t_arch, target_os=t_os,
        host_arch=h_arch, host_os=h_os,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metric bump (Phase 64-C-LOCAL S4 will render this on Ops Summary)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def record_dispatch(kind: T3RunnerKind) -> None:
    """Best-effort Prometheus bump. Swallows errors — metric unavailable
    is not a good reason to abort task dispatch."""
    try:
        from backend import metrics as _m
        if hasattr(_m, "t3_runner_dispatch_total"):
            _m.t3_runner_dispatch_total.labels(runner=kind.value).inc()
    except Exception as exc:
        logger.debug("t3 dispatch metric bump failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Target-profile convenience
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def resolve_from_profile(profile: Optional[dict]) -> T3Resolution:
    """Resolve against a `configs/platforms/*.yaml` parsed dict
    directly. Handles the legacy profiles (which may not declare
    `target_os`) by defaulting to linux."""
    profile = profile or {}
    # Profiles declare arch under `kernel_arch` (historical) or `arch`.
    # host_native intentionally leaves `kernel_arch` empty so we treat
    # it as "whatever the host is" — resolve to LOCAL unconditionally.
    name = (profile.get("platform") or "").strip().lower()
    if name == "host_native":
        return resolve_t3_runner(host_arch(), host_os())
    arch = profile.get("kernel_arch") or profile.get("arch") or ""
    target_os = profile.get("target_os") or profile.get("os") or "linux"
    return resolve_t3_runner(arch, target_os)
