"""BS.4.3 — ``install_method='noop'`` install method.

The simplest of the four. Used when the catalog entry models a
toolchain that's already provided by the host (e.g. ``docker`` itself,
or a pre-baked vendor image the operator pre-loaded with
``docker import`` in air-gap mode — see threat model §6.2 air-gap
table footnote).

Contract
────────
* ``install_method`` MUST be ``'noop'`` (asserted defensively).
* ``install_url``    — None expected; ignored if present.
* ``sha256``         — None expected; ignored if present (alembic 0051
                       CHECK exempts noop from the sha256 NOT NULL
                       requirement, threat model §5.1).
* ``metadata`` may carry ``expected_image_present: bool`` (threat model
  §6.2) — if True we run ``docker image inspect <metadata.image_ref>``
  to confirm the host has the image; not present → still ``completed``
  but with a ``noop_image_check_skipped`` note in result_json.

Why we don't fail-soft when ``expected_image_present`` is requested
but missing: the catalog entry was authored expecting the operator to
``docker save / docker load`` ahead of install (air-gap workflow). If
the image is absent, dependent installs (e.g. a backend service that
expects this image) will silently fail later; loud-fail here so the
operator sees the gap immediately.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from .base import (
    InstallMethodError,
    InstallResult,
    ProgressCallback,
    logger,
    require_job_fields,
)


def install(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    """Confirm the entry is logically installed; no filesystem writes."""
    require_job_fields(job, ["id", "entry_id", "install_method"], method="noop")
    if job["install_method"] != "noop":
        raise InstallMethodError(
            f"noop method called with install_method={job['install_method']!r}",
            error_reason="dispatch_method_mismatch",
        )

    progress_cb(
        stage="verifying",
        bytes_done=0,
        bytes_total=0,
        eta_seconds=0,
        log_tail="",
    )

    metadata = job.get("metadata") or {}
    expected_image_present = bool(metadata.get("expected_image_present"))
    image_ref = metadata.get("image_ref") or metadata.get("docker_image")

    if expected_image_present and image_ref:
        outcome = _check_docker_image_present(str(image_ref))
        if not outcome.present:
            return InstallResult(
                state="failed",
                error_reason="noop_expected_image_missing",
                result_json={
                    "image_ref": image_ref,
                    "checked_via": outcome.checked_via,
                    "stderr_tail": outcome.stderr_tail,
                },
                log_tail=outcome.stderr_tail,
            )
        logger.info(
            "noop install %s: image %s confirmed present (via %s)",
            job["id"], image_ref, outcome.checked_via,
        )
        return InstallResult(
            state="completed",
            result_json={
                "image_ref": image_ref,
                "checked_via": outcome.checked_via,
                "image_id": outcome.image_id,
            },
        )

    note = (
        "noop_image_check_skipped"
        if expected_image_present
        else "noop_no_check_required"
    )
    logger.info(
        "noop install %s for entry %s: %s",
        job["id"], job["entry_id"], note,
    )
    return InstallResult(
        state="completed",
        result_json={"note": note, "metadata_keys": sorted(metadata.keys())},
    )


# ──────────────────────────────────────────────────────────────────
#  Docker image presence check (only when metadata asks for it)
# ──────────────────────────────────────────────────────────────────


from dataclasses import dataclass


@dataclass
class _ImageCheckOutcome:
    present: bool
    checked_via: str
    image_id: str | None = None
    stderr_tail: str = ""


def _check_docker_image_present(image_ref: str) -> _ImageCheckOutcome:
    """``docker image inspect`` against whatever DOCKER_HOST is wired —
    in compose this hits docker-socket-proxy (IMAGES=1, threat model
    §4.6). Outside compose the call returns ``not present`` because
    docker-cli can't reach a daemon.

    We deliberately do NOT pull or modify state — noop is read-only
    by definition. If docker-cli isn't on PATH (unlikely given
    Dockerfile.installer ships ``docker-ce-cli``), we report
    ``checked_via='docker_cli_missing'`` and treat it as present so a
    misconfigured sidecar doesn't block installs that don't really
    need the check.
    """
    docker = shutil.which("docker")
    if docker is None:
        return _ImageCheckOutcome(
            present=True,  # fail-open: don't block on infrastructure gap
            checked_via="docker_cli_missing",
            stderr_tail="docker CLI not on PATH; skipping presence check",
        )
    try:
        proc = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image_ref],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _ImageCheckOutcome(
            present=False,
            checked_via="docker_inspect_error",
            stderr_tail=str(exc)[-256:],
        )
    if proc.returncode == 0:
        return _ImageCheckOutcome(
            present=True,
            checked_via="docker_inspect",
            image_id=proc.stdout.strip(),
        )
    return _ImageCheckOutcome(
        present=False,
        checked_via="docker_inspect",
        stderr_tail=(proc.stderr or "").strip()[-256:],
    )
