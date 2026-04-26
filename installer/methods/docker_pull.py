"""BS.4.3 — ``install_method='docker_pull'`` install method.

Pulls a vendor docker image via the docker-socket-proxy ACL (threat
model §4.6: ``IMAGES=1`` enables ``GET /images/...`` + ``POST /images/create``;
``POST=0`` blocks ``run/exec/rm``). After pull we verify the image's
``RepoDigest`` matches the catalog entry's ``sha256`` — Docker's
content-addressable digest IS a sha256, so this is the same chain link
as the file-payload methods (threat model §5.2 layer 3 equivalent).

Air-gap mode hard-blocks docker_pull (threat model §6.2 table). Operators
who need a vendor image in air-gap MUST pre-load it via ``docker save / load``
on the host and use ``install_method='noop'`` with
``metadata.expected_image_present=true + metadata.image_ref``.

Job dict required fields:

* ``install_url``   — image reference (e.g.
                      ``ghcr.io/nxp/mcuxpresso:1.2.3`` or
                      ``nxp/sdk@sha256:abcd...``). The reference syntax
                      MUST NOT include a scheme (``docker pull`` rejects
                      ``https://...``).
* ``sha256``        — 64-hex content-addressable digest. The same value
                      Docker prints in ``RepoDigests`` after pull. Per
                      alembic 0051 CHECK + threat model §5.1, REQUIRED
                      for non-noop install methods.

Result JSON shape on success:

    {"image_ref": "ghcr.io/nxp/sdk:1.2.3",
     "image_id": "sha256:...",                   # local image id
     "repo_digest": "ghcr.io/nxp/sdk@sha256:...",
     "size_bytes": 1234567}                       # via docker inspect
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from .base import (
    AirgapViolation,
    InstallCancelled,
    InstallMethodError,
    InstallResult,
    ProgressCallback,
    is_airgap_mode,
    is_valid_sha256_hex,
    logger,
    require_job_fields,
)


# Docker image reference syntax — ``[registry/]repo[:tag][@digest]``.
# We allow a generous superset and let docker-cli reject malformed.
# Hard-block obvious shell-injection chars to avoid surprises if the
# operator's catalog row is corrupted.
_FORBIDDEN_REF_CHARS = re.compile(r"[\s;|&<>$`\\\"'(){}\[\]]")
_DOCKER_PULL_PROGRESS_LINE = re.compile(
    # Matches lines like ``Status: Downloaded newer image for foo:tag``
    # or ``Pulling fs layer`` / ``Downloading [=====>      ]  12.34MB/56.78MB``.
    r"^(?P<id>[0-9a-f]{12}|Status):"
)


def install(job: dict[str, Any], progress_cb: ProgressCallback) -> InstallResult:
    """``docker pull <ref>`` + sha256 digest verification."""
    require_job_fields(
        job, ["id", "entry_id", "install_method", "install_url", "sha256"],
        method="docker_pull",
    )
    if job["install_method"] != "docker_pull":
        raise InstallMethodError(
            f"docker_pull method called with install_method={job['install_method']!r}",
            error_reason="dispatch_method_mismatch",
        )

    image_ref = str(job["install_url"]).strip()
    expected_sha256 = str(job["sha256"]).strip().lower()

    if is_airgap_mode():
        return InstallResult(
            state="failed",
            error_reason="airgap_violation",
            result_json={
                "image_ref": image_ref,
                "hint": (
                    "docker_pull is hard-disabled in air-gap mode "
                    "(threat model §6.2). Pre-load the image on the host "
                    "with `docker load -i <tarball>` and switch the catalog "
                    "entry to install_method='noop' with metadata."
                    "expected_image_present=true."
                ),
            },
        )

    _validate_image_ref(image_ref)
    if not is_valid_sha256_hex(expected_sha256):
        return InstallResult(
            state="failed",
            error_reason="catalog_entry_invalid_sha256",
            result_json={
                "expected_sha256": expected_sha256,
                "expected_format": "^[a-f0-9]{64}$",
            },
        )

    docker = shutil.which("docker")
    if docker is None:
        return InstallResult(
            state="failed",
            error_reason="docker_cli_missing",
            result_json={"image_ref": image_ref},
        )

    progress_cb(
        stage="pulling",
        bytes_done=0,
        bytes_total=job.get("bytes_total"),
        eta_seconds=None,
        log_tail="",
    )

    pull_outcome = _docker_pull(
        docker, image_ref, progress_cb=progress_cb, job_id=job["id"],
    )
    if pull_outcome.cancelled:
        return InstallResult(
            state="cancelled",
            error_reason="cancelled_by_operator",
            log_tail=pull_outcome.log_tail,
        )
    if pull_outcome.returncode != 0:
        return InstallResult(
            state="failed",
            error_reason=_classify_docker_error(pull_outcome.log_tail),
            result_json={
                "image_ref": image_ref,
                "exit_code": pull_outcome.returncode,
            },
            log_tail=pull_outcome.log_tail,
        )

    inspect = _docker_inspect(docker, image_ref)
    if inspect is None:
        return InstallResult(
            state="failed",
            error_reason="docker_inspect_failed",
            result_json={"image_ref": image_ref},
            log_tail=pull_outcome.log_tail,
        )

    matched_digest = _match_digest(inspect.repo_digests, expected_sha256)
    if matched_digest is None:
        # Pull succeeded but RepoDigest is not what the catalog
        # promised. Either the registry served a different blob (MitM
        # excluded by HTTPS-to-registry, but registry compromise possible)
        # OR the catalog row is stale. Either way: hard fail per
        # threat model §5.4 ``sha256_layer1_mismatch``.
        return InstallResult(
            state="failed",
            error_reason="sha256_layer1_mismatch",
            result_json={
                "image_ref": image_ref,
                "expected_sha256": expected_sha256,
                "image_id": inspect.image_id,
                "repo_digests": list(inspect.repo_digests),
            },
            log_tail=pull_outcome.log_tail,
        )

    progress_cb(
        stage="completed",
        bytes_done=inspect.size_bytes or 0,
        bytes_total=inspect.size_bytes,
        eta_seconds=0,
        log_tail=pull_outcome.log_tail,
    )

    logger.info(
        "docker_pull install %s for entry %s: pulled %s, digest %s, %d bytes",
        job["id"], job["entry_id"], image_ref, matched_digest,
        inspect.size_bytes or 0,
    )
    return InstallResult(
        state="completed",
        bytes_done=inspect.size_bytes or 0,
        log_tail=pull_outcome.log_tail,
        result_json={
            "image_ref": image_ref,
            "image_id": inspect.image_id,
            "repo_digest": matched_digest,
            "size_bytes": inspect.size_bytes,
        },
    )


# ──────────────────────────────────────────────────────────────────
#  docker pull / inspect helpers
# ──────────────────────────────────────────────────────────────────


def _validate_image_ref(image_ref: str) -> None:
    if not image_ref:
        raise InstallMethodError(
            "docker_pull: empty install_url (image reference)",
            error_reason="malformed_job_payload",
        )
    if "://" in image_ref:
        raise InstallMethodError(
            f"docker_pull: install_url MUST be a docker image reference, "
            f"not a URL with scheme: {image_ref!r}",
            error_reason="malformed_image_ref",
        )
    if _FORBIDDEN_REF_CHARS.search(image_ref):
        raise InstallMethodError(
            f"docker_pull: image ref contains shell-meta characters: "
            f"{image_ref!r}",
            error_reason="malformed_image_ref",
        )


from dataclasses import dataclass


@dataclass
class _PullOutcome:
    returncode: int
    log_tail: str
    cancelled: bool


@dataclass
class _InspectOutcome:
    image_id: str | None
    repo_digests: tuple[str, ...]
    size_bytes: int | None


def _docker_pull(
    docker: str,
    image_ref: str,
    *,
    progress_cb: ProgressCallback,
    job_id: str,
) -> _PullOutcome:
    """Run ``docker pull`` and stream stdout to progress_cb.

    docker-cli emits one progress line per layer + a final ``Status:``
    line. We forward the last ~4 KiB to ``log_tail`` and call
    progress_cb once per parseable line so the UI sees per-layer
    motion. A genuine SIGINT to docker-cli is not graceful (image
    layers may end up half-pulled but daemon GC handles them) so we
    rely on docker-socket-proxy to make ``rm`` impossible from the
    sidecar; cancel here just stops the pull mid-flight.
    """
    log_lines: list[str] = []
    cancelled = False

    proc = subprocess.Popen(
        [docker, "pull", image_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # Same hardening as run_in_process_group in base.py — new
        # session so cancel can killpg.
        start_new_session=True,
        pass_fds=(),
        close_fds=True,
    )
    assert proc.stdout is not None
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            log_lines.append(line)
            try:
                progress_cb(
                    stage="pulling",
                    bytes_done=0,
                    bytes_total=None,
                    eta_seconds=None,
                    log_tail="\n".join(log_lines[-100:]),
                )
            except InstallCancelled:
                cancelled = True
                break
        if cancelled:
            _terminate(proc)
        rc = proc.wait()
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass

    return _PullOutcome(
        returncode=rc,
        log_tail="\n".join(log_lines)[-4096:],
        cancelled=cancelled,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort SIGTERM → SIGKILL for ``docker pull``."""
    import os
    import signal as _signal
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    try:
        os.killpg(pgid, _signal.SIGTERM)
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, _signal.SIGKILL)
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except (OSError, ProcessLookupError):
        pass


def _docker_inspect(docker: str, image_ref: str) -> _InspectOutcome | None:
    try:
        proc = subprocess.run(
            [docker, "image", "inspect", image_ref],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    head = payload[0] if isinstance(payload[0], dict) else {}
    digests_raw = head.get("RepoDigests") or []
    digests = tuple(d for d in digests_raw if isinstance(d, str))
    return _InspectOutcome(
        image_id=head.get("Id"),
        repo_digests=digests,
        size_bytes=int(head["Size"]) if isinstance(head.get("Size"), int) else None,
    )


def _match_digest(
    repo_digests: tuple[str, ...], expected_sha256: str,
) -> str | None:
    """Return the first ``ref@sha256:<expected>`` from repo_digests, or
    None if the expected hex isn't present in any digest."""
    suffix = f"sha256:{expected_sha256.lower()}"
    for d in repo_digests:
        # docker stores ``<repo>@sha256:<hex>``; we compare just the
        # algorithm+hex tail so the repo-name portion of the digest
        # doesn't matter (legitimate vendor image may live under
        # multiple registries with the same content-hash).
        if d.endswith(suffix):
            return d
    return None


def _classify_docker_error(log_tail: str) -> str:
    """Pick a structured error_reason from docker-cli stderr.

    Mirrors the threat model §8 list. Falls back to
    ``docker_pull_failed`` when nothing matches."""
    blob = (log_tail or "").lower()
    if "denied: requested access" in blob or "unauthorized" in blob:
        return "docker_pull_unauthorized"
    if "manifest unknown" in blob or "not found" in blob:
        return "docker_pull_not_found"
    if "no such host" in blob or "lookup" in blob and "dial tcp" in blob:
        return "docker_pull_dns_failure"
    if "connection refused" in blob or "tls handshake" in blob:
        return "docker_pull_network_error"
    if "permission denied" in blob and "/var/run/docker.sock" in blob:
        # This shouldn't happen in compose because the proxy is on
        # tcp://, but a misconfigured local smoke might trip it.
        return "docker_socket_denied"
    return "docker_pull_failed"
