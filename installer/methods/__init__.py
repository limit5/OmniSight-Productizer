"""BS.4.3 — install method dispatch + shared interface re-exports.

Each ``install_method`` value in ``catalog_entries.install_method``
maps to exactly one module here::

    noop              → installer.methods.noop:install
    docker_pull       → installer.methods.docker_pull:install
    shell_script      → installer.methods.shell_script:install
    vendor_installer  → installer.methods.vendor_installer:install

The :data:`METHODS` mapping is the single source of truth. Adding a
new install method means: (1) add a sibling module with an ``install``
function, (2) add it to :data:`METHODS`, (3) update the alembic 0051
CHECK constraint to include the new value, (4) update threat model
§3 with the new vendor surface and §5.4 with the sha256 requirement
(or document why noop is the only exempt method).

Per implement_phase_step.md Step 1 module-global state audit
─────────────────────────────────────────────────────────────
:data:`METHODS` is built once at import time from the four sibling
modules. It is logically immutable; we expose it as ``MappingProxyType``
so anyone holding the reference can't mutate it. The four ``install``
functions themselves are pure (no module-level state of their own —
audited per the docstrings of each module). Each sidecar replica
imports the same module and gets the same dispatch table; cross-replica
coordination (a job goes to exactly one sidecar) is enforced by
backend's ``SELECT … FOR UPDATE SKIP LOCKED`` — answer #1 from the
SOP rubric.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from . import docker_pull as _docker_pull
from . import noop as _noop
from . import shell_script as _shell_script
from . import vendor_installer as _vendor_installer
from .base import (
    AirgapViolation,
    InsecureURL,
    InstallCancelled,
    InstallMethod,
    InstallMethodError,
    InstallResult,
    InstallState,
    PayloadTooLarge,
    ProgressCallback,
    Sha256Mismatch,
    is_airgap_mode,
)


# Single source of truth — keep in alphabetic order to make CI diffs
# obvious if a new method ever lands.
METHODS: "MappingProxyType[str, InstallMethod]" = MappingProxyType({
    "docker_pull": _docker_pull.install,
    "noop": _noop.install,
    "shell_script": _shell_script.install,
    "vendor_installer": _vendor_installer.install,
})


def dispatch(
    job: dict[str, Any], progress_cb: ProgressCallback,
) -> InstallResult:
    """Route *job* to the install method that matches
    ``job['install_method']``.

    Failures here are programmer / catalog-drift errors (unknown method,
    missing key) — they translate to ``InstallResult(state='failed')``
    so the dispatcher can report uniformly via BS.4.4's result POST."""
    method_name = job.get("install_method")
    if not method_name:
        return InstallResult(
            state="failed",
            error_reason="dispatch_missing_install_method",
            result_json={
                "job_id": job.get("id"),
                "entry_id": job.get("entry_id"),
            },
        )
    fn = METHODS.get(str(method_name))
    if fn is None:
        return InstallResult(
            state="failed",
            error_reason="dispatch_unknown_install_method",
            result_json={
                "job_id": job.get("id"),
                "install_method": method_name,
                "supported": sorted(METHODS.keys()),
            },
        )
    try:
        return fn(job, progress_cb)
    except InstallCancelled:
        # Any method that didn't already wrap progress_cb's cancel
        # raise lands here — surface as a clean cancelled result so
        # the dispatcher's reporter (BS.4.4) can POST one row.
        return InstallResult(
            state="cancelled",
            error_reason="cancelled_by_operator",
            result_json={
                "job_id": job.get("id"),
                "install_method": method_name,
            },
        )
    except InstallMethodError as exc:
        # Caught at the boundary so a bug in one method doesn't take
        # down the sidecar; the operator sees a failed row instead of
        # a vanished install.
        return InstallResult(
            state="failed",
            error_reason=exc.error_reason,
            result_json={
                "job_id": job.get("id"),
                "install_method": method_name,
                "error": str(exc)[-256:],
            },
        )


__all__ = [
    "AirgapViolation",
    "InsecureURL",
    "InstallCancelled",
    "InstallMethod",
    "InstallMethodError",
    "InstallResult",
    "InstallState",
    "METHODS",
    "PayloadTooLarge",
    "ProgressCallback",
    "Sha256Mismatch",
    "dispatch",
    "is_airgap_mode",
]
