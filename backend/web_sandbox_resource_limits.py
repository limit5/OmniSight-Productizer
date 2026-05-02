"""W14.9 — cgroup resource limits for the web-preview sidecar.

Per-container hard caps applied at ``docker run`` time:

  * **RAM** — ``--memory <bytes>`` plus ``--memory-swap <bytes>`` (set
    equal to ``--memory`` so the container cannot escape the cap by
    swapping). Default 2 GiB matches the W14.9 row spec.
  * **CPU** — ``--cpus N`` (fractional CPUs allowed). Default 1.0
    matches the row spec.
  * **Disk** — ``--storage-opt size=<bytes>`` on the container's
    writable layer. Default 5 GiB matches the row spec. **Caveat**:
    docker only honours ``--storage-opt size=`` on storage drivers
    that support per-container quotas — overlay2 with the ``xfs``
    project-quota backing filesystem, devicemapper, or btrfs. Most
    dev boxes ship overlay2 on ext4, where docker silently ignores
    the limit. Operators on those hosts can disable the disk cap by
    setting ``OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT=off`` (W14.10's
    PG-backed audit row will track which deployments rely on this
    fallback). The bind-mounted workspace itself is never under the
    container's writable layer so the disk cap only constrains
    ``node_modules`` + Vite build output that lives on the layer.

Why a separate module
=====================

Resource limits are operator-policy cross-cutting state — used by
:mod:`backend.web_sandbox` (the launcher), :mod:`backend.routers.web_sandbox`
(env-knob → policy translation), and the future W14.10 audit row.
Splitting keeps :class:`backend.web_sandbox.WebSandboxConfig` free of
the parsing surface (docker-style ``2g``/``5g`` strings, ``--cpus``
fractional rendering) and lets the test seam be a pure-function suite
that doesn't need to spin up a manager fixture.

Module-global state audit (SOP §1)
==================================

Pure module — no module-level mutable state. Every helper is a
function of its inputs; every value class is a frozen dataclass. Each
uvicorn worker derives the same :class:`WebPreviewResourceLimits`
from the same Settings literals so cross-worker consistency is
automatic (SOP §1 type-1 answer).

Read-after-write timing audit (SOP §2)
======================================

N/A — no DB pool changes, no compat→pool migration, no asyncio
gather. The module is purely a value-object factory consumed by
:meth:`backend.web_sandbox.WebSandboxManager.launch`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


__all__ = [
    "RESOURCE_LIMITS_SCHEMA_VERSION",
    "DEFAULT_MEMORY_LIMIT_TEXT",
    "DEFAULT_CPU_LIMIT_TEXT",
    "DEFAULT_STORAGE_LIMIT_TEXT",
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "DEFAULT_CPU_LIMIT",
    "DEFAULT_STORAGE_LIMIT_BYTES",
    "MIN_MEMORY_LIMIT_BYTES",
    "MAX_MEMORY_LIMIT_BYTES",
    "MIN_CPU_LIMIT",
    "MAX_CPU_LIMIT",
    "MIN_STORAGE_LIMIT_BYTES",
    "MAX_STORAGE_LIMIT_BYTES",
    "STORAGE_LIMIT_DISABLED_TOKENS",
    "CGROUP_OOM_REASON",
    "ResourceLimitsError",
    "WebPreviewResourceLimits",
    "parse_memory_bytes",
    "parse_cpu_limit",
    "parse_storage_bytes",
    "build_docker_resource_args",
    "format_cpu_arg",
]


#: Bump on any change to :meth:`WebPreviewResourceLimits.to_dict()`
#: shape — the W14.10 audit row depends on this for forward-compat.
RESOURCE_LIMITS_SCHEMA_VERSION = "1.0.0"


# Row spec literals — pinned via drift-guard tests so a future edit
# that bumps "2g" → "4g" without updating the row notice fails CI.
DEFAULT_MEMORY_LIMIT_TEXT = "2g"
DEFAULT_CPU_LIMIT_TEXT = "1"
DEFAULT_STORAGE_LIMIT_TEXT = "5g"

DEFAULT_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_STORAGE_LIMIT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB

# Sanity floors / ceilings. The floor protects against an operator
# typo (`OMNISIGHT_WEB_SANDBOX_MEMORY_LIMIT=64`) silently halving the
# hard cap to 64 *bytes*; the ceiling prevents an honest mistake
# (`64gb`) from accidentally giving the sandbox more RAM than the
# host has.
MIN_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024            # 64 MiB
MAX_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024 * 1024     # 64 GiB
MIN_CPU_LIMIT = 0.05
MAX_CPU_LIMIT = 64.0
MIN_STORAGE_LIMIT_BYTES = 256 * 1024 * 1024          # 256 MiB
MAX_STORAGE_LIMIT_BYTES = 256 * 1024 * 1024 * 1024   # 256 GiB

#: Tokens accepted on the storage env knob to mean "disable the disk
#: cap" (operators on overlay2-on-ext4 where docker silently ignores
#: ``--storage-opt size=`` set this so the spec doesn't lie about
#: enforcement).
STORAGE_LIMIT_DISABLED_TOKENS = frozenset({"0", "off", "none", "disabled", "false", "no"})

#: Killed-reason literal recorded on
#: :class:`backend.web_sandbox.WebSandboxInstance` when the manager
#: detects an OOM-kill via inspect. W14.10 audit row + W14.6 frontend
#: panel both surface this so the operator knows the launch died
#: because the sandbox blew the 2 GiB cap, not because the dev server
#: crashed.
CGROUP_OOM_REASON = "cgroup_oom"


# Match docker's `<num><suffix>` style: "2g", "512m", "1.5G", "1024".
# The trailing "b" is optional ("2gb" + "2g" both accepted).
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)
_MEM_MULTS = {
    "": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
    "t": 1024 ** 4,
}


class ResourceLimitsError(ValueError):
    """W14.9 resource-limit configuration rejected.

    Surfaces at config-construction time (or when the router parses
    Settings) so a misconfigured env knob fails fast rather than
    blowing up mid-launch with an opaque docker-run error.
    """


def parse_memory_bytes(value: int | float | str) -> int:
    """Parse a docker-style memory spec into a positive byte count.

    Accepts:
      * ``int`` / ``float`` — interpreted as raw bytes (must be > 0).
      * ``str`` matching ``<num>[k|m|g|t][b?]`` — case-insensitive,
        optional whitespace. ``"2g"`` ⇒ ``2 * 1024**3``, ``"2gb"`` ⇒
        same, ``"512m"`` ⇒ ``512 * 1024**2``.

    Raises :class:`ResourceLimitsError` on any other shape. Note we
    deliberately reject ``bool`` even though Python's ``isinstance``
    chain says ``True`` is an ``int``; an operator who set the env to
    ``true`` almost certainly meant something else.
    """

    if isinstance(value, bool):
        raise ResourceLimitsError(
            f"memory limit must be a number/string, not bool: {value!r}"
        )
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ResourceLimitsError(
                f"memory limit must be positive: {value!r}"
            )
        return int(value)
    if not isinstance(value, str):
        raise ResourceLimitsError(
            f"memory limit must be int/float/str, got {type(value).__name__}"
        )
    match = _MEM_RE.match(value)
    if not match:
        raise ResourceLimitsError(
            "memory limit must be a docker-style size (e.g. '2g', "
            f"'512m', '1024'): got {value!r}"
        )
    num = float(match.group(1))
    unit = match.group(2).lower()
    if num <= 0:
        raise ResourceLimitsError(
            f"memory limit must be positive: {value!r}"
        )
    return int(num * _MEM_MULTS[unit])


def parse_cpu_limit(value: int | float | str) -> float:
    """Parse a docker-style ``--cpus`` value into a positive float.

    Fractional CPUs (``"0.5"``, ``0.5``) are allowed — docker maps
    them to CFS quota under the hood. Bool is rejected for the same
    reason as :func:`parse_memory_bytes`.
    """

    if isinstance(value, bool):
        raise ResourceLimitsError(
            f"cpu limit must be a number/string, not bool: {value!r}"
        )
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ResourceLimitsError(
            f"cpu limit must be int/float/str, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise ResourceLimitsError("cpu limit must be non-empty")
    try:
        return float(text)
    except ValueError as exc:
        raise ResourceLimitsError(f"cpu limit must be numeric: got {value!r}") from exc


def parse_storage_bytes(value: int | float | str) -> int:
    """Parse a docker-style ``--storage-opt size=`` value into bytes.

    Same syntax as :func:`parse_memory_bytes` — docker accepts the
    same suffixes everywhere. Kept as a thin alias so call sites
    read clearly: ``parse_storage_bytes("5g")`` makes the intent
    obvious; ``parse_memory_bytes("5g")`` would not.
    """

    return parse_memory_bytes(value)


def format_cpu_arg(cpu: float) -> str:
    """Render a CPU count for ``--cpus``.

    ``1.0`` → ``"1"`` (docker accepts both, but the integer form is
    less surprising in ``docker ps`` output); ``1.5`` → ``"1.5"``;
    ``0.25`` → ``"0.25"``. Pure function so tests can assert exact
    argv shape without touching the manager.
    """

    if not isinstance(cpu, (int, float)) or isinstance(cpu, bool):
        raise ResourceLimitsError(f"cpu must be number: {cpu!r}")
    if float(cpu).is_integer():
        return str(int(cpu))
    return f"{float(cpu):g}"


@dataclass(frozen=True)
class WebPreviewResourceLimits:
    """Frozen cgroup-limit policy consumed by the W14.2 launcher.

    Default values match the W14.9 row spec (2 GiB / 1 CPU / 5 GiB).
    Operators override per-deployment via the three
    ``OMNISIGHT_WEB_SANDBOX_*`` env knobs; per-launch override is
    available through :attr:`backend.web_sandbox.WebSandboxConfig.resource_limits`
    (the request body's ``resource_limits`` field).

    ``storage_limit_bytes=None`` ⇒ disable the writable-layer disk
    cap. Used by operators on overlay2-on-ext4 hosts where docker
    silently ignores ``--storage-opt size=``; the launcher falls
    through with a ``warnings`` entry recording the absence so the
    W14.10 audit row can flag deployments that ship without disk
    enforcement.

    ``memory_swap_disabled=True`` (default) sets ``--memory-swap``
    equal to ``--memory`` so the container cannot escape the RAM cap
    by swapping. Set to ``False`` only on dev boxes where you want
    the legacy 2x-in-swap behaviour for triage.
    """

    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES
    cpu_limit: float = DEFAULT_CPU_LIMIT
    storage_limit_bytes: int | None = DEFAULT_STORAGE_LIMIT_BYTES
    memory_swap_disabled: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.memory_limit_bytes, bool) or not isinstance(
            self.memory_limit_bytes, int
        ):
            raise ResourceLimitsError(
                f"memory_limit_bytes must be int: {self.memory_limit_bytes!r}"
            )
        if self.memory_limit_bytes <= 0:
            raise ResourceLimitsError(
                f"memory_limit_bytes must be positive: {self.memory_limit_bytes!r}"
            )
        if not (
            MIN_MEMORY_LIMIT_BYTES <= self.memory_limit_bytes <= MAX_MEMORY_LIMIT_BYTES
        ):
            raise ResourceLimitsError(
                f"memory_limit_bytes={self.memory_limit_bytes} out of range "
                f"[{MIN_MEMORY_LIMIT_BYTES}, {MAX_MEMORY_LIMIT_BYTES}]"
            )
        if isinstance(self.cpu_limit, bool):
            raise ResourceLimitsError("cpu_limit must be number, not bool")
        if not isinstance(self.cpu_limit, (int, float)):
            raise ResourceLimitsError(f"cpu_limit must be a number: {self.cpu_limit!r}")
        cpu_float = float(self.cpu_limit)
        if not (MIN_CPU_LIMIT <= cpu_float <= MAX_CPU_LIMIT):
            raise ResourceLimitsError(
                f"cpu_limit={self.cpu_limit} out of range "
                f"[{MIN_CPU_LIMIT}, {MAX_CPU_LIMIT}]"
            )
        # Force float so ``int`` inputs round-trip cleanly via
        # :meth:`to_dict`.
        object.__setattr__(self, "cpu_limit", cpu_float)
        if self.storage_limit_bytes is not None:
            if isinstance(self.storage_limit_bytes, bool) or not isinstance(
                self.storage_limit_bytes, int
            ):
                raise ResourceLimitsError(
                    "storage_limit_bytes must be int or None: "
                    f"{self.storage_limit_bytes!r}"
                )
            if self.storage_limit_bytes <= 0:
                raise ResourceLimitsError(
                    "storage_limit_bytes must be positive when set: "
                    f"{self.storage_limit_bytes!r}"
                )
            if not (
                MIN_STORAGE_LIMIT_BYTES
                <= self.storage_limit_bytes
                <= MAX_STORAGE_LIMIT_BYTES
            ):
                raise ResourceLimitsError(
                    f"storage_limit_bytes={self.storage_limit_bytes} out of "
                    f"range [{MIN_STORAGE_LIMIT_BYTES}, {MAX_STORAGE_LIMIT_BYTES}]"
                )
        if not isinstance(self.memory_swap_disabled, bool):
            raise ResourceLimitsError(
                "memory_swap_disabled must be bool: "
                f"{self.memory_swap_disabled!r}"
            )

    @classmethod
    def default(cls) -> "WebPreviewResourceLimits":
        """Return the row-spec defaults (2 GiB / 1 CPU / 5 GiB).

        Pinned by drift-guard tests so a code-level edit cannot
        silently change the operator-facing contract.
        """

        return cls()

    @classmethod
    def from_settings(cls, settings: Any) -> "WebPreviewResourceLimits":
        """Build a :class:`WebPreviewResourceLimits` from
        :class:`backend.config.Settings` (or any object with the same
        three attribute names).

        Empty values fall through to the row-spec default — operators
        who don't set the env knobs get the documented behaviour.
        Malformed values raise :class:`ResourceLimitsError` so the
        caller (router) can log + fall back to defaults rather than
        500'ing every launch.

        ``OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT`` accepts the special
        tokens in :data:`STORAGE_LIMIT_DISABLED_TOKENS` to mean "no
        disk cap" (overlay2-on-ext4 hosts).
        """

        memory_text = (
            getattr(settings, "web_sandbox_memory_limit", "") or ""
        )
        memory_text = memory_text.strip() if isinstance(memory_text, str) else memory_text
        cpu_raw = getattr(settings, "web_sandbox_cpu_limit", "") or ""
        cpu_text = str(cpu_raw).strip() if cpu_raw is not None else ""
        storage_text = (
            getattr(settings, "web_sandbox_storage_limit", "") or ""
        )
        storage_text = (
            storage_text.strip() if isinstance(storage_text, str) else storage_text
        )

        memory_bytes = (
            parse_memory_bytes(memory_text)
            if memory_text
            else DEFAULT_MEMORY_LIMIT_BYTES
        )
        cpu_value = (
            parse_cpu_limit(cpu_text) if cpu_text else DEFAULT_CPU_LIMIT
        )
        storage_bytes: int | None
        if not storage_text:
            storage_bytes = DEFAULT_STORAGE_LIMIT_BYTES
        elif (
            isinstance(storage_text, str)
            and storage_text.lower() in STORAGE_LIMIT_DISABLED_TOKENS
        ):
            storage_bytes = None
        else:
            storage_bytes = parse_storage_bytes(storage_text)
        return cls(
            memory_limit_bytes=memory_bytes,
            cpu_limit=cpu_value,
            storage_limit_bytes=storage_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for the W14.2 ``WebSandboxConfig.to_dict()``
        round-trip + future W14.10 audit row."""

        return {
            "schema_version": RESOURCE_LIMITS_SCHEMA_VERSION,
            "memory_limit_bytes": int(self.memory_limit_bytes),
            "cpu_limit": float(self.cpu_limit),
            "storage_limit_bytes": (
                None if self.storage_limit_bytes is None
                else int(self.storage_limit_bytes)
            ),
            "memory_swap_disabled": bool(self.memory_swap_disabled),
        }


def build_docker_resource_args(
    limits: WebPreviewResourceLimits | None,
) -> list[str]:
    """Return the docker-run argv extension matching ``limits``.

    Empty list when ``limits`` is ``None`` (no caps applied — only
    used in test/dev paths and operator-explicit opt-out).

    Pure function — same input always produces the same argv list.
    """

    if limits is None:
        return []
    if not isinstance(limits, WebPreviewResourceLimits):
        raise TypeError(
            f"limits must be WebPreviewResourceLimits or None: {type(limits).__name__}"
        )
    args: list[str] = ["--memory", str(int(limits.memory_limit_bytes))]
    if limits.memory_swap_disabled:
        # Setting --memory-swap == --memory disables swap usage —
        # without this the cgroup honours --memory but lets the
        # container blow through up to 2x in swap, which is exactly
        # the failure mode the W14.9 row guards against.
        args += ["--memory-swap", str(int(limits.memory_limit_bytes))]
    args += ["--cpus", format_cpu_arg(limits.cpu_limit)]
    if limits.storage_limit_bytes is not None:
        args += [
            "--storage-opt",
            f"size={int(limits.storage_limit_bytes)}",
        ]
    return args
