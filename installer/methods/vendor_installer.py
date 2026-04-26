"""BS.4.3 — ``install_method='vendor_installer'`` install method.

Downloads a vendor binary installer (NXP MCUXpresso ``.run``,
Qualcomm ``.bin``, Qt online installer in ``-headless`` mode, …),
sha256-verifies it, then executes inside a job-scoped POSIX process
group with ``--target=<scratch>`` (or whatever the entry's
``metadata.installer_args`` provides) so the artifact lands in a
scratch dir; on success ``os.replace`` it onto the final entry path
(threat model §4.9 atomic install).

Difference vs ``shell_script``
──────────────────────────────
* Binary, not a script — but invocation pattern is identical
  (subprocess.Popen + setsid + killpg).
* Atomic-promote enforced: vendors typically ``--target=<dir>`` and
  the dir contents become the toolchain. We force a scratch path,
  pass it via ``metadata.installer_args`` substitution
  (``"{scratch}"`` placeholder), and ``os.replace`` to final on
  success.
* Vendor exit codes 0 + 1 (Qt-style "already installed") are both
  treated as success when ``metadata.success_exit_codes`` lists 1
  — defaults to ``[0]`` only.

Job dict shape (in addition to base required fields):

* ``install_url`` — HTTPS URL to the binary installer.
* ``sha256``      — payload digest.
* ``metadata``    — may include:

    * ``installer_args``     — list[str] passed after the binary;
                                ``"{scratch}"`` substituted with the
                                scratch path (default
                                ``["--silent", "--target", "{scratch}"]``).
    * ``success_exit_codes`` — list[int] (default ``[0]``).
    * ``timeout_s``          — int (default 3600 = 1h).
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
    atomic_promote,
    entry_install_root,
    fetch_url,
    is_valid_sha256_hex,
    logger,
    require_job_fields,
    run_in_process_group,
    scratch_path_for_job,
    verify_sha256,
)

_DEFAULT_ARGS = ("--silent", "--target", "{scratch}")
_DEFAULT_SUCCESS_EXITS = (0,)
_DEFAULT_TIMEOUT_S = 3600


def install(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    """Download → verify → execute vendor installer → atomic-promote scratch."""
    try:
        return _install_inner(job, progress_cb)
    except InstallCancelled:
        return InstallResult(
            state="cancelled",
            error_reason="cancelled_by_operator",
        )


def _install_inner(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    require_job_fields(
        job, ["id", "entry_id", "install_method", "install_url", "sha256"],
        method="vendor_installer",
    )
    if job["install_method"] != "vendor_installer":
        raise InstallMethodError(
            f"vendor_installer method called with install_method={job['install_method']!r}",
            error_reason="dispatch_method_mismatch",
        )

    expected_sha256 = str(job["sha256"]).strip().lower()
    if not is_valid_sha256_hex(expected_sha256):
        return InstallResult(
            state="failed",
            error_reason="catalog_entry_invalid_sha256",
            result_json={"expected_sha256": expected_sha256},
        )

    metadata = job.get("metadata") or {}
    success_exit_codes = _coerce_int_set(
        metadata.get("success_exit_codes"), _DEFAULT_SUCCESS_EXITS,
    )
    args_template = _coerce_arg_list(
        metadata.get("installer_args"), _DEFAULT_ARGS,
    )

    install_url = str(job["install_url"]).strip()
    scratch = scratch_path_for_job(job["entry_id"], job["id"])
    binary_path = os.path.join(scratch, "vendor_installer.bin")
    payload_dir = os.path.join(scratch, "payload")

    try:
        os.makedirs(payload_dir, exist_ok=True)
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
            install_url, binary_path,
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
        verify_sha256(binary_path, expected_sha256)
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
        os.chmod(binary_path, 0o700)
    except OSError as exc:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason="scratch_chmod_failed",
            result_json={"path": binary_path, "error": str(exc)[-256:]},
        )

    progress_cb(
        stage="running",
        bytes_done=bytes_done,
        bytes_total=bytes_done,
        eta_seconds=None,
        log_tail="",
    )

    final_args = [
        a.replace("{scratch}", payload_dir) for a in args_template
    ]
    outcome = run_in_process_group(
        [binary_path, *final_args],
        cwd=scratch,
        env=_scrubbed_env(),
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

    if outcome.returncode not in success_exit_codes:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason=f"vendor_installer_exit_code_{outcome.returncode}",
            log_tail=outcome.log_tail,
            bytes_done=bytes_done,
            result_json={
                "install_url": install_url,
                "exit_code": outcome.returncode,
                "elapsed_seconds": round(outcome.elapsed_seconds, 2),
                "success_exit_codes": list(success_exit_codes),
            },
        )

    # Success — promote scratch payload dir to final entry path. We
    # remove the binary from scratch first (no need to keep it around;
    # it's a one-shot archive).
    with contextlib.suppress(OSError):
        os.remove(binary_path)
    final = entry_install_root(job["entry_id"])
    try:
        atomic_promote(payload_dir, final)
    except (InstallMethodError, OSError) as exc:
        _cleanup(scratch)
        return InstallResult(
            state="failed",
            error_reason=getattr(exc, "error_reason", "atomic_promote_failed"),
            log_tail=outcome.log_tail,
            bytes_done=bytes_done,
            result_json={"scratch": scratch, "final": final, "error": str(exc)[-256:]},
        )

    # Tidy up the now-empty scratch parent.
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch, ignore_errors=True)

    logger.info(
        "vendor_installer install %s for entry %s: exit %d in %.2fs, "
        "promoted to %s",
        job["id"], job["entry_id"], outcome.returncode,
        outcome.elapsed_seconds, final,
    )
    return InstallResult(
        state="completed",
        log_tail=outcome.log_tail,
        bytes_done=bytes_done,
        result_json={
            "install_url": install_url,
            "binary_sha256": expected_sha256,
            "exit_code": outcome.returncode,
            "final_path": final,
            "elapsed_seconds": round(outcome.elapsed_seconds, 2),
        },
    )


# ──────────────────────────────────────────────────────────────────


def _cleanup(scratch: str) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch, ignore_errors=True)


def _coerce_int_set(raw: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list):
        return default
    out: list[int] = []
    for v in raw:
        if isinstance(v, bool):  # bool is a subclass of int — exclude
            continue
        if isinstance(v, int):
            out.append(v)
    return tuple(out) if out else default


def _coerce_arg_list(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list):
        return default
    out: list[str] = []
    for v in raw:
        if isinstance(v, str):
            out.append(v)
    return tuple(out) if out else default


def _scrubbed_env() -> dict[str, str]:
    """Same scrub logic as shell_script — drop ``OMNISIGHT_*`` to keep
    sidecar config from leaking into the vendor binary's environment."""
    keep_prefixes = ("LC_",)
    keep_keys = {"PATH", "HOME", "LANG", "TZ", "TERM", "USER", "LOGNAME"}
    base = {
        k: v
        for k, v in os.environ.items()
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes)
    }
    base.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return base
