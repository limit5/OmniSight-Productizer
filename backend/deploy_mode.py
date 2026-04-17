"""L7 — Deploy-mode detection.

Rich successor to the single-line ``_detect_deploy_mode()`` skeleton that
lives inside :mod:`backend.routers.bootstrap`. The wizard's Step 4
launcher decides *how* to bring the OmniSight services up based on the
host topology:

  * ``systemd``         → units are installed and PID 1 is systemd;
                          launcher exec's ``systemctl start omnisight-*``
                          (needs K1's scoped sudoers to be in place).
  * ``docker-compose``  → a docker daemon is reachable (socket mounted
                          or the ``docker`` CLI is on PATH);
                          launcher exec's
                          ``docker compose -f docker-compose.prod.yml up -d``.
  * ``dev``             → neither systemd nor a docker daemon is
                          reachable, OR we're running *inside* a
                          dev container; the Step 4 endpoint short-
                          circuits because ``uvicorn`` / ``next dev``
                          are already up.

:func:`detect_deploy_mode` returns a :class:`DeployModeDetection` so
callers get not just the mode but *why* — which signal tipped the
decision — plus the individual probe results. The bootstrap router
keeps its thin private wrapper for backwards compatibility; new call
sites should use this module directly.

The probes are intentionally conservative: each one catches its own
``OSError`` and returns ``False`` so a hostile /proc layout or a
revoked socket permission fails closed to ``dev`` rather than
crashing the wizard.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

DeployMode = Literal["systemd", "docker-compose", "dev"]

_VALID_MODES: frozenset[str] = frozenset({"systemd", "docker-compose", "dev"})

# Env override — honoured first so operators can pin a mode when the
# auto-detect guesses wrong (CI, nested-container setups, testing).
_OVERRIDE_ENV = "OMNISIGHT_DEPLOY_MODE"

# Filesystem signals. Paths are kept module-level so tests can monkey-
# patch them onto a tmp_path fixture without touching the real host.
_DOCKERENV_MARKER = Path("/.dockerenv")
_CGROUP_PATH = Path("/proc/1/cgroup")
_SYSTEMD_RUN_DIR = Path("/run/systemd/system")
_DOCKER_SOCKET = Path("/var/run/docker.sock")


@dataclass(frozen=True)
class DeployModeDetection:
    """Outcome of :func:`detect_deploy_mode`.

    Exposes the final ``mode`` plus every individual signal that fed the
    decision so the wizard UI (and tests) can explain the choice. The
    ``reason`` field is a short human-readable blurb — intended for the
    wizard Step 4 tooltip / the audit row's metadata, not for machine
    dispatch.
    """

    mode: DeployMode
    in_docker: bool
    has_systemd: bool
    has_docker_socket: bool
    has_docker_binary: bool
    has_systemctl_binary: bool
    override_source: Optional[str] = None
    reason: str = ""
    signals: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signal probes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _is_in_docker() -> tuple[bool, str]:
    """True if this process appears to run inside a Docker container.

    Two heuristics:
      1. ``/.dockerenv`` exists — docker-engine writes this on container
         start; podman/containerd do not, but they mount a cgroup path
         that the second probe catches.
      2. ``/proc/1/cgroup`` contains ``docker`` or ``containerd``.

    Returns ``(detected, evidence)`` where *evidence* is a short string
    the caller can stash in ``signals`` for debugging.
    """
    try:
        if _DOCKERENV_MARKER.exists():
            return True, f"{_DOCKERENV_MARKER} present"
    except OSError as exc:
        logger.debug("deploy_mode: dockerenv probe failed (%s)", exc)

    try:
        if _CGROUP_PATH.exists():
            content = _CGROUP_PATH.read_text(encoding="utf-8", errors="replace")
            for token in ("docker", "containerd", "kubepods"):
                if token in content:
                    return True, f"{_CGROUP_PATH} contains {token!r}"
    except OSError as exc:
        logger.debug("deploy_mode: cgroup probe failed (%s)", exc)

    return False, "no docker/containerd marker"


def _has_systemd() -> tuple[bool, str]:
    """True if systemd is the init system and units can be started here.

    Two heuristics:
      1. ``/run/systemd/system`` exists — systemd creates this dir on
         boot; its presence is the canonical "PID 1 is systemd" signal.
      2. ``systemctl`` is on PATH — covers hosts where the wizard runs
         from a user session and ``/run/systemd/system`` is not readable
         but ``systemctl --user`` / sudo systemctl still work.

    Both must agree for us to return ``True`` with high confidence; if
    only the binary exists we still report ``True`` but the reason
    string makes that distinction clear.
    """
    run_dir_present = False
    try:
        run_dir_present = _SYSTEMD_RUN_DIR.is_dir()
    except OSError as exc:
        logger.debug("deploy_mode: systemd run-dir probe failed (%s)", exc)

    has_binary = shutil.which("systemctl") is not None

    if run_dir_present and has_binary:
        return True, f"{_SYSTEMD_RUN_DIR} present + systemctl on PATH"
    if run_dir_present:
        return True, f"{_SYSTEMD_RUN_DIR} present (systemctl missing)"
    if has_binary:
        return True, "systemctl on PATH (no /run/systemd/system)"
    return False, "no systemd run-dir + no systemctl binary"


def _has_docker_socket() -> tuple[bool, str]:
    """True if ``/var/run/docker.sock`` is reachable as a socket.

    A plain ``exists()`` is not enough — the wizard only cares whether
    compose can talk to a daemon. We check the path is a socket file;
    permission errors are swallowed (common when the wizard process is
    not in the ``docker`` group) and treated as "not reachable".
    """
    try:
        if _DOCKER_SOCKET.exists() and _DOCKER_SOCKET.is_socket():
            return True, f"{_DOCKER_SOCKET} is a socket"
    except (OSError, PermissionError) as exc:
        logger.debug("deploy_mode: docker socket probe failed (%s)", exc)
    return False, "no docker socket at /var/run/docker.sock"


def _has_docker_binary() -> bool:
    return shutil.which("docker") is not None


def _has_systemctl_binary() -> bool:
    return shutil.which("systemctl") is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def detect_deploy_mode() -> DeployModeDetection:
    """Probe the host topology and return the recommended deploy mode.

    Decision table (first match wins):

      +---+-----------------------------------------+-------------------+
      | # | Condition                                | mode              |
      +---+-----------------------------------------+-------------------+
      | 0 | ``OMNISIGHT_DEPLOY_MODE`` env override   | (whatever env    |
      |   |                                          |  says)            |
      | 1 | in_docker AND has_docker_socket          | docker-compose    |
      |   |   (nested docker with mounted socket —   |  (compose-in-     |
      |   |    prefer compose over the fragile       |  docker works;    |
      |   |    systemd-in-container path)            |  systemd does     |
      |   |                                          |  not)             |
      | 2 | in_docker AND NOT has_docker_socket      | dev               |
      |   |   (running as a container with no way to |  (already up      |
      |   |    control the host — Step 4 no-op)      |  inside this      |
      |   |                                          |  container)       |
      | 3 | has_systemd                              | systemd           |
      | 4 | has_docker_socket OR has_docker_binary   | docker-compose    |
      | 5 | (nothing usable)                         | dev               |
      +---+-----------------------------------------+-------------------+

    The env override is validated against :data:`_VALID_MODES`; an
    unknown value is ignored and a warning logged so a typo does not
    silently coerce the launcher into ``dev``.
    """
    in_docker, docker_evidence = _is_in_docker()
    has_systemd_signal, systemd_evidence = _has_systemd()
    has_socket, socket_evidence = _has_docker_socket()
    has_docker_bin = _has_docker_binary()
    has_systemctl_bin = _has_systemctl_binary()

    signals = {
        "docker": docker_evidence,
        "systemd": systemd_evidence,
        "docker_socket": socket_evidence,
        "docker_binary": "on PATH" if has_docker_bin else "not on PATH",
        "systemctl_binary": "on PATH" if has_systemctl_bin else "not on PATH",
    }

    raw_override = (os.environ.get(_OVERRIDE_ENV) or "").strip().lower()
    if raw_override:
        if raw_override in _VALID_MODES:
            return DeployModeDetection(
                mode=raw_override,  # type: ignore[arg-type]
                in_docker=in_docker,
                has_systemd=has_systemd_signal,
                has_docker_socket=has_socket,
                has_docker_binary=has_docker_bin,
                has_systemctl_binary=has_systemctl_bin,
                override_source=_OVERRIDE_ENV,
                reason=f"env {_OVERRIDE_ENV}={raw_override!r}",
                signals=signals,
            )
        logger.warning(
            "deploy_mode: %s=%r not in %s — ignoring override, falling back to auto-detect",
            _OVERRIDE_ENV,
            raw_override,
            sorted(_VALID_MODES),
        )

    if in_docker and has_socket:
        mode: DeployMode = "docker-compose"
        reason = "running inside container with /var/run/docker.sock mounted — compose-in-docker"
    elif in_docker:
        mode = "dev"
        reason = "running inside container, no docker socket — services already up in this container"
    elif has_systemd_signal:
        mode = "systemd"
        reason = systemd_evidence
    elif has_socket or has_docker_bin:
        mode = "docker-compose"
        if has_socket and has_docker_bin:
            reason = "docker daemon reachable via /var/run/docker.sock + docker CLI on PATH"
        elif has_socket:
            reason = "docker daemon reachable via /var/run/docker.sock"
        else:
            reason = "docker CLI on PATH (socket not visible — will use default context)"
    else:
        mode = "dev"
        reason = "no systemd, no docker daemon — assuming dev (uvicorn / next dev)"

    return DeployModeDetection(
        mode=mode,
        in_docker=in_docker,
        has_systemd=has_systemd_signal,
        has_docker_socket=has_socket,
        has_docker_binary=has_docker_bin,
        has_systemctl_binary=has_systemctl_bin,
        override_source=None,
        reason=reason,
        signals=signals,
    )
