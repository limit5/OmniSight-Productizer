"""BS.4.3 — ``install_method='shell_script'`` install method.

Downloads a vendor ``install.sh`` from ``install_url``, sha256-verifies
it against ``catalog_entries.sha256`` (threat model §5.2 layer 3), then
executes it inside a job-scoped POSIX process group via
:func:`installer.methods.base.run_in_process_group` (threat model §4.8 —
new session so cancel can ``killpg(SIGTERM)`` → 10 s grace → ``killpg(SIGKILL)``).

Why this is the most security-sensitive of the four methods
─────────────────────────────────────────────────────────────
The vendor's shell script runs arbitrary commands as uid 10001. The
sidecar container is otherwise hardened (cap_drop=ALL, read_only rootfs,
docker-socket-proxy with ``POST=0`` blocks pivot to other containers)
but a malicious or buggy script can still:

* Write into the bind-mounted ``/var/lib/omnisight/toolchains/`` path.
  Mitigation: each install gets a per-job scratch dir
  (``scratch_path_for_job``) and only on success is it ``os.replace``'d
  into the final entry-id path (threat model §4.9).
* Spawn long-lived children (e.g. background cron-like daemons).
  Mitigation: process group + killpg.
* Try to ``curl | bash`` more code from the network. Mitigation: in
  air-gap mode the sidecar is on ``--network=none`` so any network
  call from the script fails. In non-air-gap, the script's network
  egress is whatever the operator's compose network allows (out of
  scope for the threat model — it's a deliberate tradeoff).

Per ADR §4.4 step 3 sidecar must NOT auto-retry shell_script jobs that
``failed`` (vendor scripts are not assumed idempotent). The
``installer_job_retry_blocked`` flag in result_json signals BS.7.6 to
gate the retry button on operator confirmation.

Job dict required fields:

* ``install_url`` — HTTPS URL to ``install.sh`` (or ``file://`` in
                    air-gap). Never ``http://``.
* ``sha256``      — 64-hex digest of the script bytes.

Result JSON shape on success::

    {"script_url": "https://...",
     "script_sha256": "<hex>",
     "exit_code": 0,
     "elapsed_seconds": 42.5}
"""

from __future__ import annotations

import contextlib
import os
import shutil
from typing import Any

from .base import (
    InstallCancelled,
    InstallMethodError,
    InstallResult,
    ProgressCallback,
    fetch_url,
    is_valid_sha256_hex,
    logger,
    require_job_fields,
    run_in_process_group,
    scratch_path_for_job,
    verify_sha256,
)


def install(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    """Download → verify → execute vendor install.sh in a PG-scoped subprocess."""
    try:
        return _install_inner(job, progress_cb)
    except InstallCancelled:
        # progress_cb raised cancel before/between phases — there's no
        # subprocess to kill yet (or it's already been reaped by
        # run_in_process_group). Surface as cancelled cleanly.
        return InstallResult(
            state="cancelled",
            error_reason="cancelled_by_operator",
        )


def _install_inner(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    require_job_fields(
        job, ["id", "entry_id", "install_method", "install_url", "sha256"],
        method="shell_script",
    )
    if job["install_method"] != "shell_script":
        raise InstallMethodError(
            f"shell_script method called with install_method={job['install_method']!r}",
            error_reason="dispatch_method_mismatch",
        )

    expected_sha256 = str(job["sha256"]).strip().lower()
    if not is_valid_sha256_hex(expected_sha256):
        return InstallResult(
            state="failed",
            error_reason="catalog_entry_invalid_sha256",
            result_json={"expected_sha256": expected_sha256},
        )

    install_url = str(job["install_url"]).strip()
    scratch = scratch_path_for_job(job["entry_id"], job["id"])
    script_path = os.path.join(scratch, "install.sh")

    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as exc:
        return InstallResult(
            state="failed",
            error_reason="scratch_mkdir_failed",
            result_json={"scratch": scratch, "error": str(exc)[-256:]},
        )

    progress_cb(
        stage="downloading",
        bytes_done=0,
        bytes_total=job.get("bytes_total"),
        eta_seconds=None,
        log_tail="",
    )

    try:
        bytes_done = fetch_url(
            install_url, script_path,
            progress_cb=progress_cb,
            bytes_total_hint=job.get("bytes_total"),
            stage="downloading",
        )
    except InstallMethodError as exc:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason=exc.error_reason,
            result_json={"install_url": install_url, "error": str(exc)[-256:]},
        )

    progress_cb(
        stage="verifying",
        bytes_done=bytes_done,
        bytes_total=bytes_done,
        eta_seconds=0,
        log_tail="",
    )
    try:
        verify_sha256(script_path, expected_sha256)
    except InstallMethodError as exc:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason=exc.error_reason,
            result_json={
                "install_url": install_url,
                "expected_sha256": expected_sha256,
            },
        )

    try:
        os.chmod(script_path, 0o700)
    except OSError as exc:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason="scratch_chmod_failed",
            result_json={"path": script_path, "error": str(exc)[-256:]},
        )

    progress_cb(
        stage="running",
        bytes_done=bytes_done,
        bytes_total=bytes_done,
        eta_seconds=None,
        log_tail="",
    )

    env = _scrubbed_env()
    outcome = run_in_process_group(
        ["bash", script_path],
        cwd=scratch,
        env=env,
        progress_cb=progress_cb,
        progress_stage="running",
    )

    if outcome.cancelled:
        _cleanup(scratch)
        return InstallResult(
            state="cancelled",
            error_reason="cancelled_by_operator",
            log_tail=outcome.log_tail,
            bytes_done=bytes_done,
        )

    if outcome.returncode != 0:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason=f"shell_script_exit_code_{outcome.returncode}",
            log_tail=outcome.log_tail,
            bytes_done=bytes_done,
            result_json={
                "install_url": install_url,
                "exit_code": outcome.returncode,
                "elapsed_seconds": round(outcome.elapsed_seconds, 2),
                # Per ADR §4.4 step 3 — sidecar will NOT auto-retry
                # shell_script failures. BS.7.6 gates retry on operator
                # confirmation; this flag tells the UI to show the
                # warning banner.
                "auto_retry_blocked": True,
            },
        )

    # Success — drop scratch on the floor; the script is responsible
    # for placing artifacts under its own ``/var/lib/omnisight/...``
    # path. Per threat model §4.9 the atomic-promote pattern applies
    # to vendor_installer's tar/extract flow; shell scripts vary
    # widely and a forced rename would break vendors that install
    # to /opt/<vendor>/...
    _cleanup(scratch)
    logger.info(
        "shell_script install %s for entry %s: exit 0 in %.2fs",
        job["id"], job["entry_id"], outcome.elapsed_seconds,
    )
    return InstallResult(
        state="completed",
        log_tail=outcome.log_tail,
        bytes_done=bytes_done,
        result_json={
            "install_url": install_url,
            "script_sha256": expected_sha256,
            "exit_code": 0,
            "elapsed_seconds": round(outcome.elapsed_seconds, 2),
        },
    )


def _cleanup(scratch: str) -> None:
    """Best-effort scratch removal. Suppress errors so a stuck mount /
    busy-fd doesn't mask the real install_method outcome."""
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch, ignore_errors=True)


def _scrubbed_env() -> dict[str, str]:
    """Build a minimal env for the vendor script.

    Drops anything starting with ``OMNISIGHT_`` so the vendor script
    can't read sidecar tokens (``OMNISIGHT_INSTALLER_TOKEN``) or other
    config; preserves ``PATH`` / ``HOME`` / ``LANG`` / ``LC_*`` / ``TZ``
    so common build chains work; injects a couple of helpers the
    vendor script may want to read."""
    keep_prefixes = ("LC_",)
    keep_keys = {"PATH", "HOME", "LANG", "TZ", "TERM", "USER", "LOGNAME"}
    base = {
        k: v
        for k, v in os.environ.items()
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes)
    }
    base.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return base
