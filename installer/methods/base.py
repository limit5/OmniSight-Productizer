"""BS.4.3 — install method shared interface + helpers.

Every install method file in ``installer/methods/`` exports a single
public callable::

    def install(job: dict, progress_cb: ProgressCallback) -> InstallResult: ...

The 4 implementations land in sibling modules (``noop`` / ``docker_pull``
/ ``shell_script`` / ``vendor_installer``) and are wired into
:data:`METHODS` via :mod:`installer.methods.__init__`. The dispatcher in
:func:`installer.main._handle_claimed_job` looks up by
``job["install_method"]`` and calls the matching ``install()``.

Job dict shape (caller's contract — main.py owns the resolution)
─────────────────────────────────────────────────────────────────
Every method receives a flat dict that merges install_jobs row +
catalog_entries fields. Required keys:

* ``id``                — install_jobs.id (``ij-XXXXXXXXXXXX``)
* ``entry_id``          — catalog_entries.id
* ``tenant_id``         — for audit / scratch path scoping
* ``install_method``    — one of ``noop|docker_pull|shell_script|vendor_installer``
* ``install_url``       — None for ``noop``; otherwise required
* ``sha256``            — None for ``noop``; otherwise 64-hex per
                          threat model §5.1 CHECK constraint
* ``metadata``          — dict; per-job free-form (e.g.
                          ``{"expected_image_present": true}``)

Optional keys (methods MAY consume but MUST tolerate absence):

* ``bytes_total``       — hint for progress reporting
* ``log_tail``          — accumulated previous log (rare; usually empty)

Progress callback contract
──────────────────────────
``progress_cb`` is called by the method during long-running phases. The
signature is keyword-only so BS.4.4 can extend it without breaking the
4 method files::

    progress_cb(stage="downloading", bytes_done=42, bytes_total=1000,
                eta_seconds=None, log_tail="...last 4KB...")

In BS.4.3 the dispatcher passes a logger-only stub (logs at INFO).
BS.4.4 replaces it with an HTTP POST to
``/installer/jobs/{id}/progress``.

The callback may raise :class:`InstallCancelled`; methods MUST catch
that, perform their kill-and-reap path (process-group SIGTERM → 10 s
wait → SIGKILL per threat model §4.8), and return
``InstallResult(state="cancelled", error_reason="cancelled_by_operator")``.

Module-global / cross-worker state audit (per implement_phase_step.md
Step 1)
────────────────────────────────────────────────────────────────────
This module is pure types + helpers — no module-level mutable state, no
caches, no singletons. The only module-level names are immutable
constants (paths, regex patterns, byte-size limits) and pure functions.
``METHODS`` lives in ``__init__.py`` and is a frozen mapping built once
at import time.

Read-after-write timing audit
─────────────────────────────
N/A — methods talk to the local filesystem and to subprocess. PG /
Redis ordering is owned by main.py's HTTP layer, not these methods.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("omnisight.installer.methods")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Filesystem layout (matches Dockerfile.installer + threat model §4.3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Root for installed toolchains. Bind-mounted from host
# ``/var/lib/omnisight/toolchains/`` per threat model §4.5; chown'd to
# uid 10001 by Dockerfile.installer so writes don't need root.
TOOLCHAINS_ROOT = "/var/lib/omnisight/toolchains"

# Air-gap bundle root (read-only bind mount per threat model §6.1).
# In air-gap mode every fetch path resolves under here.
AIRGAP_ROOT = "/var/lib/omnisight/airgap"

# Bytes we read at a time when streaming a download to disk while
# computing sha256. 1 MiB matches Python's default buffered I/O block.
_CHUNK_BYTES = 1024 * 1024

# Hard cap on log_tail field per threat model §4.5 (bus-payload size).
LOG_TAIL_MAX_BYTES = 4 * 1024  # 4 KiB

# Cap on a downloaded payload. Defence against catalog drift pointing
# at an attacker-controlled multi-GB tarball that fills the
# 10 GiB ``storage_opt`` cap (threat model §4.7) and DoS's the sidecar.
# Bigger artifacts (NXP MCUXpresso ~3 GB) override via
# ``OMNISIGHT_INSTALLER_MAX_PAYLOAD_BYTES``.
DEFAULT_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB

_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

InstallState = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class InstallResult:
    """Terminal-state result returned by every install method.

    Mirrors the install_jobs columns the sidecar must report when the
    method exits (BS.4.4 will POST this back via
    ``/installer/jobs/{id}/result``). Attributes:

    * ``state``         — one of ``completed`` / ``failed`` / ``cancelled``.
    * ``error_reason``  — structured token from the threat model §8 list
                          (``sha256_layer1_mismatch`` / ``airgap_violation``
                          / ``vendor_installer_exit_code_<N>`` / ...);
                          MUST be ``None`` iff state == 'completed'.
    * ``result_json``   — free-form per-method payload (e.g. for
                          docker_pull: ``{"image_digest": "sha256:..."}``).
                          Lands in ``install_jobs.result_json``.
    * ``bytes_done``    — final byte count for progress UI continuity.
    * ``log_tail``      — last 4 KiB of stdout/stderr; capped at
                          :data:`LOG_TAIL_MAX_BYTES`.
    """

    state: InstallState
    error_reason: str | None = None
    result_json: dict[str, Any] | None = None
    bytes_done: int = 0
    log_tail: str = ""


# ProgressCallback signature is keyword-only — BS.4.4 may extend it.
# The four method modules MUST always pass these args by keyword so
# adding new fields (e.g. ``rate_bytes_per_sec``) doesn't break them.
ProgressCallback = Callable[..., None]


# install(job, progress_cb) -> InstallResult — what every method exports.
InstallMethod = Callable[[dict[str, Any], ProgressCallback], InstallResult]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InstallMethodError(Exception):
    """Base class for install-method recoverable failures.

    Methods generally do NOT raise this — they return an
    ``InstallResult(state='failed', ...)`` instead so the dispatcher
    has a uniform reporting path. This exception exists so that helpers
    in this module (sha256 verify, download streaming, etc.) can signal
    structured failure conditions and the calling method translates to
    the right error_reason.
    """

    error_reason: str = "install_method_error"

    def __init__(self, message: str, *, error_reason: str | None = None):
        super().__init__(message)
        if error_reason is not None:
            self.error_reason = error_reason


class AirgapViolation(InstallMethodError):
    """Raised by :func:`fetch_url` / :func:`require_airgap_safe_url` when
    a non-``file://`` URL is reached while ``OMNISIGHT_INSTALLER_AIRGAP=1``.

    Threat model §6.2: in air-gap mode every outbound URL must hard-fail.
    """

    error_reason = "airgap_violation"


class InsecureURL(InstallMethodError):
    """Raised when a non-air-gap URL is not HTTPS.

    Threat model §5.2 Layer 1: vendor URLs MUST be HTTPS so TLS+CA
    chain protects against on-path tampering.
    """

    error_reason = "insecure_url_scheme"


class Sha256Mismatch(InstallMethodError):
    """Raised when computed sha256 != expected. Layer 1/3 of the
    verification chain (threat model §5.2)."""

    error_reason = "sha256_layer1_mismatch"


class PayloadTooLarge(InstallMethodError):
    """Raised when a download exceeds :data:`DEFAULT_MAX_PAYLOAD_BYTES`
    (or the per-process override). Defence against catalog drift / DoS
    at the sidecar's storage_opt cap (threat model §4.7)."""

    error_reason = "payload_too_large"


class InstallCancelled(Exception):
    """Raised by progress_cb when the install_jobs row was flipped to
    ``state='cancelled'`` server-side. Methods catch this and run their
    kill-and-reap path (threat model §4.8)."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Air-gap mode helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_airgap_mode() -> bool:
    """``OMNISIGHT_INSTALLER_AIRGAP=1`` — read at call time so tests
    can monkey-patch ``os.environ`` between cases. No module-level
    cache (per implement_phase_step.md Step 1)."""
    return os.environ.get("OMNISIGHT_INSTALLER_AIRGAP", "0") == "1"


def require_airgap_safe_url(url: str) -> None:
    """Hard-fail if *url* is non-``file://`` while air-gap mode is on.

    Threat model §6.2 — air-gap mode disallows any outbound URL.
    Non-airgap mode separately requires HTTPS (see :func:`require_https`).
    """
    if is_airgap_mode() and not url.startswith("file://"):
        raise AirgapViolation(
            f"non-file URL in air-gap mode: {url!r} "
            "(set OMNISIGHT_INSTALLER_AIRGAP=0 or supply a file:// URL "
            "under /var/lib/omnisight/airgap/)"
        )


def require_https(url: str) -> None:
    """Hard-fail unless *url* is ``https://`` or ``file://``.

    Threat model §5.2 Layer 1: vendor URLs MUST be HTTPS so the system
    CA bundle protects against MitM. ``file://`` is always allowed
    (covers air-gap mode + local-fixture tests)."""
    if url.startswith("file://"):
        return
    if is_airgap_mode():
        # In airgap, require_airgap_safe_url owns the (stricter) check.
        return
    if not url.startswith("https://"):
        raise InsecureURL(
            f"non-HTTPS URL not allowed: {url!r} "
            "(threat model §5.2 Layer 1 — TLS is the first gate before "
            "sha256 layers; set OMNISIGHT_INSTALLER_AIRGAP=1 to allow "
            "file:// fixtures during dev/test)"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  sha256 verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_valid_sha256_hex(value: str | None) -> bool:
    """64-char lowercase hex; mirrors the alembic 0051 CHECK constraint
    regex for ``catalog_entries.sha256``."""
    return bool(value) and bool(_SHA256_HEX_RE.match(value or ""))


def sha256_file(path: str) -> str:
    """Stream *path*, return the lowercase hex digest. Reads in 1-MiB
    chunks so even a multi-GB tarball stays bounded in RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: str, expected: str) -> None:
    """Compare ``sha256(file)`` to *expected*; raise on mismatch.

    Threat model §5.2 Layer 3 — the payload check. Layer 1 (TLS) is
    handled by :func:`require_https`; Layer 2 (vendor checksum file +
    GPG) is forward-compat (catalog schema doesn't ship sha256_url +
    sha256_url_sig fields yet — see threat model §5.5).
    """
    if not is_valid_sha256_hex(expected):
        raise Sha256Mismatch(
            f"expected sha256 has invalid format: {expected!r} "
            "(must match ^[a-f0-9]{64}$)",
            error_reason="catalog_entry_invalid_sha256",
        )
    actual = sha256_file(path)
    if actual != expected.lower():
        raise Sha256Mismatch(
            f"sha256 mismatch for {path!r}: expected {expected}, got {actual}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Download streaming
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _max_payload_bytes() -> int:
    raw = os.environ.get("OMNISIGHT_INSTALLER_MAX_PAYLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_PAYLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_PAYLOAD_BYTES
    return max(1, value)


def fetch_url(
    url: str,
    dest_path: str,
    *,
    progress_cb: ProgressCallback,
    bytes_total_hint: int | None = None,
    stage: str = "downloading",
) -> int:
    """Stream *url* to *dest_path* with progress + airgap + scheme guards.

    Returns the total bytes written. Raises :class:`AirgapViolation`,
    :class:`InsecureURL`, :class:`PayloadTooLarge`, or wraps urllib
    network errors as :class:`InstallMethodError`.

    Progress is reported every 1 MiB (one chunk) — over a 100-line
    log this works out to one update per ~1 MB which is sane for the
    UI's progress bar and avoids drowning the bus.
    """
    require_airgap_safe_url(url)
    require_https(url)

    cap = _max_payload_bytes()
    parsed = urllib.parse.urlparse(url)
    bytes_done = 0
    bytes_total = bytes_total_hint
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            content_length = resp.headers.get("Content-Length")
            if bytes_total is None and content_length is not None:
                with contextlib.suppress(ValueError):
                    bytes_total = int(content_length)
            if bytes_total is not None and bytes_total > cap:
                raise PayloadTooLarge(
                    f"declared Content-Length {bytes_total} exceeds cap "
                    f"{cap} for url {url!r}",
                )
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with open(dest_path, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    fh.write(chunk)
                    bytes_done += len(chunk)
                    if bytes_done > cap:
                        raise PayloadTooLarge(
                            f"payload exceeded cap {cap} bytes mid-stream "
                            f"for url {url!r}",
                        )
                    progress_cb(
                        stage=stage,
                        bytes_done=bytes_done,
                        bytes_total=bytes_total,
                        eta_seconds=None,
                        log_tail="",
                    )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        # ``OSError`` covers non-HTTP file:// failures (ENOENT) and
        # ``URLError`` covers TLS / DNS / connection refused.
        if parsed.scheme == "file":
            raise InstallMethodError(
                f"file:// fetch failed for {url!r}: {exc}",
                error_reason="airgap_fixture_missing"
                if is_airgap_mode()
                else "fetch_failed",
            ) from exc
        raise InstallMethodError(
            f"network fetch failed for {url!r}: {exc}",
            error_reason="fetch_failed",
        ) from exc
    return bytes_done


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Atomic install path (threat model §4.9)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def entry_install_root(entry_id: str) -> str:
    """Final install path: ``/var/lib/omnisight/toolchains/<entry-id>/``.

    Catalog UI lists entries whose final path exists; an entry whose
    only artifact is in a ``scratch-*`` sibling is treated as
    "install in progress / failed midway" (threat model §4.9)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", entry_id)
    return os.path.join(TOOLCHAINS_ROOT, safe)


def scratch_path_for_job(entry_id: str, job_id: str) -> str:
    """Per-job scratch dir, sibling of the final entry path.

    Layout:

        <TOOLCHAINS_ROOT>/
        ├── .scratch-<entry-id>-<job-id>/   ← scratch (this fn returns)
        └── <entry-id>/                     ← final

    The two paths share a parent directory so :func:`atomic_promote`
    can ``os.replace(scratch, final)`` — rename within the same
    filesystem is atomic on Linux. The ``.scratch-`` prefix keeps the
    catalog UI scanner from listing a half-installed entry.

    Threat model §4.9 wording is loose ("scratch under <entry-id>/")
    but the **invariant** it cares about is: catalog UI never lists a
    half-installed entry. Sibling-layout satisfies that invariant
    while making atomic-rename actually possible.
    """
    safe_entry = re.sub(r"[^A-Za-z0-9._-]", "-", entry_id)
    safe_job = re.sub(r"[^A-Za-z0-9._-]", "-", job_id)
    return os.path.join(TOOLCHAINS_ROOT, f".scratch-{safe_entry}-{safe_job}")


def atomic_promote(scratch: str, final: str) -> None:
    """Move *scratch* → *final* atomically (rename within same fs).

    If *final* already exists, swap it aside (``final.old-<pid>``) then
    remove on success — install upgrade case. Any error leaves both
    paths intact for forensic / retry; raise so the caller surfaces
    a clear error_reason."""
    if not os.path.isdir(scratch):
        raise InstallMethodError(
            f"scratch path missing: {scratch!r}",
            error_reason="atomic_promote_no_scratch",
        )
    parent = os.path.dirname(final)
    os.makedirs(parent, exist_ok=True)
    backup: str | None = None
    if os.path.exists(final):
        backup = f"{final}.old-{os.getpid()}"
        os.replace(final, backup)
    try:
        os.replace(scratch, final)
    except OSError:
        if backup is not None:
            with contextlib.suppress(OSError):
                os.replace(backup, final)
        raise
    if backup is not None:
        with contextlib.suppress(OSError):
            shutil.rmtree(backup)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Job-scoped subprocess + reaping (threat model §4.8)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class SubprocessOutcome:
    """Captured stdout/stderr (interleaved) + exit status. ``log_tail``
    is the last :data:`LOG_TAIL_MAX_BYTES` of combined output, ready
    to land on ``install_jobs.log_tail``."""

    returncode: int
    log_tail: str
    cancelled: bool = False
    elapsed_seconds: float = 0.0


def run_in_process_group(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    progress_cb: ProgressCallback,
    progress_stage: str = "running",
    cancel_check: Callable[[], bool] | None = None,
    poll_interval_s: float = 0.5,
    sigterm_grace_s: float = 10.0,
) -> SubprocessOutcome:
    """Spawn *argv* in a new POSIX process group (so cancel can
    SIGTERM the whole tree, threat model §4.8) and stream stdout +
    stderr.

    Cancel paths:

    * ``cancel_check()`` returns True (default: never) → killpg SIGTERM,
      wait up to *sigterm_grace_s*, then killpg SIGKILL.
    * ``progress_cb`` raises :class:`InstallCancelled` → same path.

    The function never returns an unfinished subprocess. Caller is
    responsible for wrapping in scratch-path + atomic_promote semantics.
    """
    import time

    started = time.monotonic()
    log_chunks: list[bytes] = []
    log_total_bytes = 0
    cancelled = False

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Threat model §4.8 essentials:
        start_new_session=True,  # equivalent to preexec_fn=os.setsid
        pass_fds=(),
        close_fds=True,
    )
    try:
        assert proc.stdout is not None  # subprocess.PIPE
        os.set_blocking(proc.stdout.fileno(), False)

        while True:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break

            with contextlib.suppress(BlockingIOError):
                chunk = proc.stdout.read(4096)
                if chunk:
                    log_chunks.append(chunk)
                    log_total_bytes += len(chunk)
                    while log_total_bytes > LOG_TAIL_MAX_BYTES * 4 and len(log_chunks) > 1:
                        # Trim periodically so memory stays bounded for
                        # log-spammy installers; the final ``log_tail``
                        # is anyway only the last 4 KiB.
                        log_total_bytes -= len(log_chunks.pop(0))

            try:
                progress_cb(
                    stage=progress_stage,
                    bytes_done=0,
                    bytes_total=None,
                    eta_seconds=None,
                    log_tail=_tail_text(log_chunks),
                )
            except InstallCancelled:
                cancelled = True
                break

            rc = proc.poll()
            if rc is not None:
                break
            time.sleep(poll_interval_s)

        if cancelled:
            _kill_process_group(proc, sigterm_grace_s)

        # Drain any remaining stdout the non-blocking poll missed.
        try:
            os.set_blocking(proc.stdout.fileno(), True)
        except OSError:
            pass
        with contextlib.suppress(Exception):
            tail = proc.stdout.read()
            if tail:
                log_chunks.append(tail)
                log_total_bytes += len(tail)
        rc = proc.wait()
    finally:
        with contextlib.suppress(Exception):
            if proc.stdout is not None:
                proc.stdout.close()

    return SubprocessOutcome(
        returncode=rc,
        log_tail=_tail_text(log_chunks),
        cancelled=cancelled,
        elapsed_seconds=time.monotonic() - started,
    )


def _kill_process_group(proc: subprocess.Popen, sigterm_grace_s: float) -> None:
    """SIGTERM the whole process group, wait *sigterm_grace_s*, then
    SIGKILL. Mirrors threat model §4.8's example."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # Already reaped by kernel — nothing to kill.
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=sigterm_grace_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def _tail_text(chunks: list[bytes]) -> str:
    """Join chunks, take the last :data:`LOG_TAIL_MAX_BYTES`, decode
    as utf-8 with replacement so a binary blip doesn't crash the
    progress reporter."""
    if not chunks:
        return ""
    blob = b"".join(chunks)
    if len(blob) > LOG_TAIL_MAX_BYTES:
        blob = blob[-LOG_TAIL_MAX_BYTES:]
    return blob.decode("utf-8", errors="replace")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Job-dict validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def require_job_fields(
    job: dict[str, Any], names: list[str], *, method: str,
) -> None:
    """Hard-check that every name in *names* is present + non-None on
    *job*. Methods call this at the very top so a malformed dispatch
    caller fails fast with a clear error_reason instead of crashing
    deeper in the call stack.

    A ``KeyError`` here generally indicates main.py forgot to merge
    catalog entry fields (install_url / sha256 / install_method) into
    the job dict; that is a programmer bug, not operator-recoverable.
    Still surfaced as an InstallResult so the operator UI shows
    something rather than the sidecar dying silently.
    """
    missing = [n for n in names if job.get(n) in (None, "")]
    if missing:
        raise InstallMethodError(
            f"{method}: missing required job fields: {missing}",
            error_reason="malformed_job_payload",
        )


# ``field`` re-export so caller code can ``from .base import field``
# if it ever wants to extend dataclasses without re-importing dataclasses.
__all__ = [
    "AIRGAP_ROOT",
    "AirgapViolation",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "InsecureURL",
    "InstallCancelled",
    "InstallMethod",
    "InstallMethodError",
    "InstallResult",
    "InstallState",
    "LOG_TAIL_MAX_BYTES",
    "PayloadTooLarge",
    "ProgressCallback",
    "Sha256Mismatch",
    "SubprocessOutcome",
    "TOOLCHAINS_ROOT",
    "atomic_promote",
    "entry_install_root",
    "fetch_url",
    "field",
    "is_airgap_mode",
    "is_valid_sha256_hex",
    "logger",
    "require_airgap_safe_url",
    "require_https",
    "require_job_fields",
    "run_in_process_group",
    "scratch_path_for_job",
    "sha256_file",
    "verify_sha256",
]
