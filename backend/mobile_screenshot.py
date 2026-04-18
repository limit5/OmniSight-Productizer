"""V6 #2 (issue #322) — Mobile screenshot capture utility.

A focused, standalone module that turns

    ``xcrun simctl io <udid> screenshot <out.png>`` (iOS)
    ``adb shell screencap -p <remote>`` + ``adb pull`` (Android)

into one deterministic ``capture(...)`` call that any agent, HTTP
endpoint, or test harness can invoke against an already-running
emulator / simulator. The V6 #1 ``mobile_sandbox.py`` module wires a
full *build + install + screenshot* lifecycle; **this** module is the
ad-hoc primitive — reusable from:

* the agent ReAct loop that needs visual context after each edit
  (``V6 #5`` row), **without** paying for a whole Gradle rebuild;
* the device-grid preview (``V6 #3`` / ``#4``) that fans out a single
  running app across 6+ device frames in parallel;
* diff-screenshot CI steps that assert "this build still renders";
* dev-box experiments where the caller already booted a simulator
  via ``xcrun simctl boot`` and just wants a PNG back.

Why a dedicated module (not a helper inside ``mobile_sandbox``)
--------------------------------------------------------------

The sandbox module's ``SubprocessAndroidExecutor.screenshot`` /
``SshMacOsIosExecutor.screenshot`` methods depend on a
``MobileSandboxConfig`` + manager lifecycle — callers that just want
"PNG of whatever device is currently booted" shouldn't have to fabricate
a sandbox. Splitting also means this primitive has a much smaller
surface — one frozen ``ScreenshotRequest`` + one ``capture()`` entry
point — which keeps agent prompts compact.

Design decisions
----------------

* **Frozen dataclasses.** :class:`ScreenshotRequest` /
  :class:`ScreenshotResult` mirror V2 ``ui_sandbox`` + V6 ``mobile_sandbox``
  so ``to_dict()`` output is JSON-round-trip-safe.
* **Dependency-injected runner.** Every capture path takes a
  ``runner=subprocess.run``-shaped callable; tests substitute a
  :class:`FakeRunner` that records argv + serves canned stdout / exit
  codes. Zero real adb / xcrun / ssh is touched under ``pytest``.
* **Deterministic argv helpers.** :func:`build_android_capture_argv`,
  :func:`build_android_pull_argv`, :func:`build_ios_capture_argv`, and
  :func:`build_ios_scp_argv` are pure — same inputs → byte-identical
  list. Deterministic argv lets the prompt-cache amortize.
* **Graceful mock fallback.** When ``adb`` / ``xcrun`` / ``ssh`` is
  missing from ``$PATH`` we return ``status="mock"`` rather than
  raising — the agent loop distinguishes "tooling missing on this
  host" from "build exists but screencap failed".
* **PNG dimension sniff from bytes.** :func:`parse_png_dimensions`
  reads the IHDR chunk (stdlib only; no Pillow dependency) so
  :class:`ScreenshotResult` carries width/height out-of-the-box. Mobile
  UIs render in very different pixel densities across simulators and
  the agent often needs the number to pick the right device frame.
* **Bytes inline by default.** :class:`ScreenshotResult` carries the
  captured PNG as ``png_bytes`` so callers can forward it directly to
  Opus 4.7's multimodal context without re-reading from disk. Callers
  that don't want the bytes can set ``attach_bytes=False`` on the
  request.
* **Security hygiene.** Session id is `[A-Za-z0-9_.-]{1,64}` only;
  remote ssh argv is ``shlex.quote``-wrapped; output paths are forced
  absolute; we never run ``subprocess.run(..., shell=True)``.

Public API
----------
* :data:`MOBILE_SCREENSHOT_SCHEMA_VERSION` — semver bump gate.
* :class:`ScreenshotRequest`, :class:`ScreenshotResult`,
  :class:`ScreenshotStatus`.
* :class:`MobileScreenshotError` (+ subclasses).
* :func:`build_android_capture_argv`,
  :func:`build_android_pull_argv`, :func:`build_ios_capture_argv`,
  :func:`build_ios_remote_capture_argv`, :func:`build_ios_scp_argv`.
* :func:`parse_png_dimensions`.
* :func:`capture_android`, :func:`capture_ios`, :func:`capture`.
* :func:`render_screenshot_result_markdown` — human-readable status
  renderer for agent HUDs.

Contract pinned by ``backend/tests/test_mobile_screenshot.py``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


__all__ = [
    "MOBILE_SCREENSHOT_SCHEMA_VERSION",
    "DEFAULT_SCREENSHOT_TIMEOUT_S",
    "DEFAULT_ANDROID_REMOTE_PATH",
    "DEFAULT_IOS_UDID",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_FORMATS",
    "PNG_MAGIC",
    "ScreenshotStatus",
    "ScreenshotRequest",
    "ScreenshotResult",
    "MobileScreenshotError",
    "MobileScreenshotConfigError",
    "MobileScreenshotToolMissing",
    "MobileScreenshotTimeout",
    "MobileScreenshotCaptureError",
    "build_android_capture_argv",
    "build_android_pull_argv",
    "build_ios_capture_argv",
    "build_ios_remote_capture_argv",
    "build_ios_scp_argv",
    "parse_png_dimensions",
    "capture_android",
    "capture_ios",
    "capture",
    "render_screenshot_result_markdown",
]


# ───────────────────────────────────────────────────────────────────
#  Constants — pinned by the contract tests
# ───────────────────────────────────────────────────────────────────

#: Bump whenever :class:`ScreenshotRequest` / :class:`ScreenshotResult`
#: ``to_dict()`` shape changes. Major = breaking.
MOBILE_SCREENSHOT_SCHEMA_VERSION = "1.0.0"

#: ``adb shell screencap`` + ``xcrun simctl io`` both return within a
#: couple of seconds on warm devices. 20 s gives headroom for cold
#: simulators without masking a truly hung call.
DEFAULT_SCREENSHOT_TIMEOUT_S = 20.0

#: Default remote tmp path for Android — `/sdcard` is writable by adb
#: on every API level we support. Overridable for devices with
#: non-standard storage layouts.
DEFAULT_ANDROID_REMOTE_PATH = "/sdcard/omnisight-screenshot.png"

#: ``booted`` is the canonical ``xcrun simctl io`` wildcard that picks
#: the currently running simulator. We default to it so callers don't
#: need to spelunk for a UDID.
DEFAULT_IOS_UDID = "booted"

#: Platforms this module dispatches. Order stable — tests pin it.
SUPPORTED_PLATFORMS: tuple[str, ...] = ("android", "ios")

#: Screenshot formats accepted by :class:`ScreenshotResult`.
SUPPORTED_FORMATS: tuple[str, ...] = ("png",)

#: PNG file magic — used by :func:`parse_png_dimensions`.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


#: Tail cap on stdout/stderr we store on the result for debugging.
#: Prevents a pathological 10 MB gradle log from blowing up the agent's
#: context budget.
_DETAIL_TAIL_CHARS = 400


_SAFE_SESSION_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

#: ADB serial grammar — `serial-id` is printable ASCII without spaces
#: or shell metacharacters. We keep it strict to avoid argv injection
#: when callers plumb through a device list.
_SAFE_SERIAL_RE = re.compile(r"[A-Za-z0-9_.:@\-]{1,128}")

#: UDID: UUID shape OR the literal "booted" sentinel. We accept a
#: slightly broader alphabet to cover physical-device UDIDs (40-char
#: hex on older iOS devices) + the simulator runtime identifiers.
_SAFE_UDID_RE = re.compile(r"[A-Za-z0-9\-]{3,64}")


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileScreenshotError(RuntimeError):
    """Base class for screenshot-capture errors. Routers can catch
    this single type to translate every failure into one structured
    HTTP / event payload."""


class MobileScreenshotConfigError(MobileScreenshotError):
    """Raised when a :class:`ScreenshotRequest` fails validation at
    construction time."""


class MobileScreenshotToolMissing(MobileScreenshotError):
    """Raised internally when the required toolchain binary isn't on
    PATH. Typically **not** raised to the caller — :func:`capture`
    degrades to ``status="mock"`` so the agent's ReAct loop can
    distinguish this from a crash."""


class MobileScreenshotTimeout(MobileScreenshotError):
    """Raised when the underlying subprocess exceeded
    :attr:`ScreenshotRequest.timeout_s`."""


class MobileScreenshotCaptureError(MobileScreenshotError):
    """Raised when the subprocess exited non-zero or returned no
    bytes. The caller can inspect the attached result for detail."""


# ───────────────────────────────────────────────────────────────────
#  Status enum + dataclasses
# ───────────────────────────────────────────────────────────────────


class ScreenshotStatus(str, Enum):
    """Terminal states for a :class:`ScreenshotResult`.

    ``pass``   → file on disk, non-zero bytes, PNG-magic valid
    ``fail``   → subprocess exited non-zero OR file empty / bad magic
    ``skip``   → caller explicitly opted out (dry-run)
    ``mock``   → tooling missing (adb / xcrun / ssh); no capture run
    """

    passed = "pass"
    fail = "fail"
    skip = "skip"
    mock = "mock"


#: The terminal statuses that mean "no real capture happened". Agents
#: can filter ``mock`` / ``skip`` out before feeding multimodal context
#: to the model.
_NON_REAL_STATUSES = frozenset({
    ScreenshotStatus.mock, ScreenshotStatus.skip,
})


@dataclass(frozen=True)
class ScreenshotRequest:
    """Inputs to :func:`capture`.

    Frozen + deterministic — two identical requests produce
    byte-identical argv from the pure helpers.

    Fields
    ------
    session_id
        Caller-facing identifier; used in logs / events. Must match
        ``[A-Za-z0-9_.-]{1,64}``.
    platform
        ``"android"`` or ``"ios"``. Case-insensitive; normalised to
        lower-case in :meth:`__post_init__`.
    output_path
        Absolute host path where the PNG will land. Parent is
        created on demand.
    timeout_s
        Upper bound per subprocess call.
    android_remote_path
        Path on the device where ``screencap -p`` will write. Must
        be absolute.
    android_serial
        Optional `adb -s <serial>` target; empty = whichever device
        adb's heuristics pick.
    ios_udid
        ``xcrun simctl io`` target; ``"booted"`` means the running
        simulator.
    ios_remote_host
        When non-empty, the capture dispatches via
        ``ssh <host> -- xcrun simctl io …`` and pulls the resulting
        PNG back with ``scp``. On Linux / CI this is the only way to
        reach a mac runner.
    ios_remote_tmp_dir
        Remote scratch directory used for the intermediate PNG file
        before ``scp`` pulls it back. Absolute; defaults to
        ``/tmp``.
    attach_bytes
        When ``True`` (default) :class:`ScreenshotResult` carries the
        PNG as ``png_bytes`` alongside the on-disk path. Agents that
        forward to multimodal context want this; CI diff steps that
        stream to a blob store may set ``False`` to keep the result
        JSON-compact.
    format
        Currently only ``"png"`` is supported — pinned by
        :data:`SUPPORTED_FORMATS`.
    env
        Optional extra env handed to the subprocess call (ANDROID_HOME
        / DEVELOPER_DIR overrides).
    """

    session_id: str
    platform: str
    output_path: str

    timeout_s: float = DEFAULT_SCREENSHOT_TIMEOUT_S

    # ── Android ─────────────────────────────────────────────
    android_remote_path: str = DEFAULT_ANDROID_REMOTE_PATH
    android_serial: str = ""

    # ── iOS ─────────────────────────────────────────────────
    ios_udid: str = DEFAULT_IOS_UDID
    ios_remote_host: str = ""
    ios_remote_tmp_dir: str = "/tmp"

    # ── Misc ────────────────────────────────────────────────
    attach_bytes: bool = True
    format: str = "png"
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileScreenshotConfigError("session_id must be a non-empty string")
        if not _SAFE_SESSION_RE.fullmatch(self.session_id):
            raise MobileScreenshotConfigError(
                "session_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.session_id!r}"
            )
        plat = (self.platform or "").strip().lower()
        if plat not in SUPPORTED_PLATFORMS:
            raise MobileScreenshotConfigError(
                f"platform must be one of {SUPPORTED_PLATFORMS!r} — got "
                f"{self.platform!r}"
            )
        object.__setattr__(self, "platform", plat)

        if not isinstance(self.output_path, str) or not self.output_path.strip():
            raise MobileScreenshotConfigError("output_path must be a non-empty string")
        if not os.path.isabs(self.output_path):
            raise MobileScreenshotConfigError(
                f"output_path must be absolute: {self.output_path!r}"
            )

        if not isinstance(self.timeout_s, (int, float)) or self.timeout_s <= 0:
            raise MobileScreenshotConfigError(
                f"timeout_s must be > 0 — got {self.timeout_s!r}"
            )
        object.__setattr__(self, "timeout_s", float(self.timeout_s))

        if not self.android_remote_path.startswith("/"):
            raise MobileScreenshotConfigError(
                "android_remote_path must be absolute — got "
                f"{self.android_remote_path!r}"
            )

        if self.android_serial and not _SAFE_SERIAL_RE.fullmatch(self.android_serial):
            raise MobileScreenshotConfigError(
                "android_serial must match [A-Za-z0-9_.:@-]{1,128} — got "
                f"{self.android_serial!r}"
            )

        udid = (self.ios_udid or "").strip()
        if not udid:
            raise MobileScreenshotConfigError("ios_udid must be non-empty")
        if udid != "booted" and not _SAFE_UDID_RE.fullmatch(udid):
            raise MobileScreenshotConfigError(
                f"ios_udid must be 'booted' or a safe token — got {self.ios_udid!r}"
            )
        object.__setattr__(self, "ios_udid", udid)

        if self.ios_remote_tmp_dir and not self.ios_remote_tmp_dir.startswith("/"):
            raise MobileScreenshotConfigError(
                f"ios_remote_tmp_dir must be absolute — got {self.ios_remote_tmp_dir!r}"
            )

        if self.format not in SUPPORTED_FORMATS:
            raise MobileScreenshotConfigError(
                f"format must be one of {SUPPORTED_FORMATS!r} — got {self.format!r}"
            )

        if not isinstance(self.env, Mapping):
            raise MobileScreenshotConfigError("env must be a Mapping[str, str]")
        env_snapshot: dict[str, str] = {}
        for k, v in self.env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise MobileScreenshotConfigError(
                    "env entries must be str→str"
                )
            env_snapshot[k] = v
        object.__setattr__(self, "env", MappingProxyType(env_snapshot))

    @property
    def is_remote_ios(self) -> bool:
        """True when iOS capture will dispatch via ssh."""
        return self.platform == "ios" and bool(self.ios_remote_host.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_SCREENSHOT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "platform": self.platform,
            "output_path": self.output_path,
            "timeout_s": float(self.timeout_s),
            "android_remote_path": self.android_remote_path,
            "android_serial": self.android_serial,
            "ios_udid": self.ios_udid,
            "ios_remote_host": self.ios_remote_host,
            "ios_remote_tmp_dir": self.ios_remote_tmp_dir,
            "attach_bytes": bool(self.attach_bytes),
            "format": self.format,
            "env": dict(self.env),
        }


@dataclass(frozen=True)
class ScreenshotResult:
    """Structured capture outcome.

    Frozen; JSON-safe via :meth:`to_dict` (PNG bytes are excluded
    from the dict — callers that want the bytes reach for
    :attr:`png_bytes` directly).
    """

    session_id: str
    platform: str
    status: ScreenshotStatus = ScreenshotStatus.skip
    path: str = ""
    format: str = "png"
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    duration_ms: int = 0
    captured_at: float = 0.0
    detail: str = ""
    png_bytes: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ScreenshotStatus):
            raise MobileScreenshotConfigError(
                f"status must be ScreenshotStatus — got {type(self.status)!r}"
            )
        if self.platform not in SUPPORTED_PLATFORMS:
            raise MobileScreenshotConfigError(
                f"platform must be one of {SUPPORTED_PLATFORMS!r}"
            )
        if self.width < 0 or self.height < 0:
            raise MobileScreenshotConfigError("dimensions must be non-negative")
        if self.size_bytes < 0:
            raise MobileScreenshotConfigError("size_bytes must be non-negative")
        if self.duration_ms < 0:
            raise MobileScreenshotConfigError("duration_ms must be non-negative")
        if self.captured_at < 0:
            raise MobileScreenshotConfigError("captured_at must be non-negative")

    @property
    def ok(self) -> bool:
        """True when a real PNG landed on disk and bytes are trustworthy."""
        return self.status is ScreenshotStatus.passed

    @property
    def has_real_capture(self) -> bool:
        """True when the result reflects a real subprocess dispatch
        (pass *or* fail). ``mock`` / ``skip`` return False."""
        return self.status not in _NON_REAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_SCREENSHOT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "platform": self.platform,
            "status": self.status.value,
            "path": self.path,
            "format": self.format,
            "width": int(self.width),
            "height": int(self.height),
            "size_bytes": int(self.size_bytes),
            "duration_ms": int(self.duration_ms),
            "captured_at": float(self.captured_at),
            "detail": self.detail,
            # Deliberately omit `png_bytes` — not JSON-safe and might be
            # multi-MB. Callers needing round-trip base64-encode it
            # themselves.
            "has_bytes": bool(self.png_bytes),
            "ok": self.ok,
        }


# ───────────────────────────────────────────────────────────────────
#  Pure argv helpers
# ───────────────────────────────────────────────────────────────────


def _adb_prefix(serial: str) -> list[str]:
    """Helper — ``adb -s <serial>`` when serial is set, else ``adb``."""
    if serial:
        return ["adb", "-s", serial]
    return ["adb"]


def build_android_capture_argv(
    remote_path: str = DEFAULT_ANDROID_REMOTE_PATH,
    *,
    serial: str = "",
) -> list[str]:
    """Return ``adb [-s <serial>] shell screencap -p <remote>`` argv.

    Pure — two identical calls return byte-identical lists. Caller
    still needs to pull the file back via
    :func:`build_android_pull_argv`."""
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise MobileScreenshotConfigError(
            f"remote_path must be absolute — got {remote_path!r}"
        )
    if serial and not _SAFE_SERIAL_RE.fullmatch(serial):
        raise MobileScreenshotConfigError(
            f"serial must match [A-Za-z0-9_.:@-]{{1,128}} — got {serial!r}"
        )
    return [*_adb_prefix(serial), "shell", "screencap", "-p", remote_path]


def build_android_pull_argv(
    remote_path: str,
    local_path: str,
    *,
    serial: str = "",
) -> list[str]:
    """Return ``adb [-s <serial>] pull <remote> <local>`` argv."""
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise MobileScreenshotConfigError(
            f"remote_path must be absolute — got {remote_path!r}"
        )
    if not isinstance(local_path, str) or not local_path:
        raise MobileScreenshotConfigError("local_path must be non-empty")
    if serial and not _SAFE_SERIAL_RE.fullmatch(serial):
        raise MobileScreenshotConfigError(
            f"serial must match [A-Za-z0-9_.:@-]{{1,128}} — got {serial!r}"
        )
    return [*_adb_prefix(serial), "pull", remote_path, local_path]


def build_ios_capture_argv(
    output_path: str,
    *,
    udid: str = DEFAULT_IOS_UDID,
) -> list[str]:
    """Return ``xcrun simctl io <udid> screenshot <out.png>`` argv.

    Shape matches the V6 #1 ``mobile_sandbox.build_ios_screenshot_argv``
    by design — agents that copy argv from logs stay interoperable."""
    if not isinstance(output_path, str) or not output_path:
        raise MobileScreenshotConfigError("output_path must be non-empty")
    target = (udid or "").strip() or DEFAULT_IOS_UDID
    if target != "booted" and not _SAFE_UDID_RE.fullmatch(target):
        raise MobileScreenshotConfigError(
            f"udid must be 'booted' or a safe token — got {udid!r}"
        )
    return ["xcrun", "simctl", "io", target, "screenshot", output_path]


def build_ios_remote_capture_argv(
    remote_host: str,
    remote_path: str,
    *,
    udid: str = DEFAULT_IOS_UDID,
    ssh_bin: str = "ssh",
) -> list[str]:
    """Return the ``ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new
    <host> -- <xcrun simctl io …>`` argv for remote iOS capture.

    The inner xcrun argv is ``shlex.quote``-wrapped per token, joined
    with spaces, and handed as one string to ssh — standard ssh shell
    traversal. Byte-identical for identical inputs."""
    if not isinstance(remote_host, str) or not remote_host.strip():
        raise MobileScreenshotConfigError("remote_host must be non-empty")
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise MobileScreenshotConfigError(
            f"remote_path must be absolute — got {remote_path!r}"
        )
    inner = build_ios_capture_argv(remote_path, udid=udid)
    remote_cmd = " ".join(shlex.quote(str(tok)) for tok in inner)
    return [
        ssh_bin,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        remote_host, "--", remote_cmd,
    ]


def build_ios_scp_argv(
    remote_host: str,
    remote_path: str,
    local_path: str,
    *,
    scp_bin: str = "scp",
) -> list[str]:
    """Return ``scp <host>:<remote> <local>`` argv for pulling the
    captured PNG back from a remote mac runner."""
    if not isinstance(remote_host, str) or not remote_host.strip():
        raise MobileScreenshotConfigError("remote_host must be non-empty")
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise MobileScreenshotConfigError(
            f"remote_path must be absolute — got {remote_path!r}"
        )
    if not isinstance(local_path, str) or not local_path:
        raise MobileScreenshotConfigError("local_path must be non-empty")
    return [scp_bin, f"{remote_host}:{remote_path}", local_path]


# ───────────────────────────────────────────────────────────────────
#  PNG inspection — stdlib only, no Pillow dependency
# ───────────────────────────────────────────────────────────────────


def parse_png_dimensions(data: bytes | None) -> tuple[int, int]:
    """Return ``(width, height)`` from PNG bytes via the IHDR chunk.

    Returns ``(0, 0)`` on any problem — never raises. That way the
    enclosing :class:`ScreenshotResult` can still report size_bytes +
    status even if the bytes turn out to be a PNG fragment or a
    non-PNG blob.

    Structure we rely on (RFC 2083):
        8 bytes   PNG magic
        4 bytes   IHDR length (always 13)
        4 bytes   "IHDR" signature
        4 bytes   width  (big-endian uint32)
        4 bytes   height (big-endian uint32)
    """
    if not data or not isinstance(data, (bytes, bytearray)):
        return (0, 0)
    if len(data) < 24:
        return (0, 0)
    if not bytes(data[:8]) == PNG_MAGIC:
        return (0, 0)
    if bytes(data[12:16]) != b"IHDR":
        return (0, 0)
    try:
        width, height = struct.unpack(">II", bytes(data[16:24]))
    except struct.error:
        return (0, 0)
    return (int(width), int(height))


# ───────────────────────────────────────────────────────────────────
#  Subprocess runner protocol + helpers
# ───────────────────────────────────────────────────────────────────


#: Runner signature mirrors :func:`subprocess.run` — keyword-only args
#: ``capture_output`` / ``text`` / ``timeout`` / ``check`` / ``env``.
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _tail(text: str | None, *, chars: int = _DETAIL_TAIL_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= chars:
        return text
    return "…" + text[-chars:]


def _tool_missing(bin_name: str) -> bool:
    """True when ``bin_name`` is not in PATH. Honors ``which``."""
    return shutil.which(bin_name) is None


def _resolve_bin(bin_name: str) -> str:
    """Return the absolute path for ``bin_name`` or the raw name if
    unresolved. Never raises; callers pre-check with
    :func:`_tool_missing` and bail to a mock result when unavailable.
    """
    return shutil.which(bin_name) or bin_name


def _ensure_parent(path: str) -> None:
    """Create the parent directory of ``path`` on demand. Used once
    per capture — callers pass the final PNG path and we guarantee
    the folder exists before adb / xcrun writes to it."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _build_subprocess_env(
    extra: Mapping[str, str], *, inherit: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Merge ``extra`` over the inherited env. Returns ``None`` when
    ``extra`` is empty — lets ``subprocess.run`` use its own PATH
    inheritance path without an explicit env snapshot."""
    if not extra:
        return None
    base: dict[str, str] = dict(inherit if inherit is not None else os.environ)
    base.update(dict(extra))
    return base


# ───────────────────────────────────────────────────────────────────
#  Capture entry points
# ───────────────────────────────────────────────────────────────────


def _read_png(path: str) -> bytes:
    """Read the captured file as bytes; empty on any I/O error so
    callers can still return a meaningful result."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        logger.debug("mobile_screenshot read %s failed: %s", path, exc)
        return b""


def _finalise_success(
    *,
    request: ScreenshotRequest,
    path: str,
    duration_ms: int,
    clock: Callable[[], float],
) -> ScreenshotResult:
    """Happy-path post-processing. Reads bytes, inspects dimensions,
    bundles into a :class:`ScreenshotResult`."""
    png_bytes = _read_png(path)
    size_bytes = len(png_bytes)
    width, height = parse_png_dimensions(png_bytes)
    # Bad magic → downgrade to fail but keep bytes so debugger can
    # inspect what the device actually returned.
    if size_bytes == 0:
        return ScreenshotResult(
            session_id=request.session_id,
            platform=request.platform,
            status=ScreenshotStatus.fail,
            path=path,
            format=request.format,
            duration_ms=duration_ms,
            captured_at=clock(),
            detail="capture produced empty file",
        )
    if width == 0 or height == 0:
        return ScreenshotResult(
            session_id=request.session_id,
            platform=request.platform,
            status=ScreenshotStatus.fail,
            path=path,
            format=request.format,
            size_bytes=size_bytes,
            duration_ms=duration_ms,
            captured_at=clock(),
            detail="capture produced non-PNG bytes (bad IHDR)",
            png_bytes=png_bytes if request.attach_bytes else b"",
        )
    return ScreenshotResult(
        session_id=request.session_id,
        platform=request.platform,
        status=ScreenshotStatus.passed,
        path=path,
        format=request.format,
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_ms=duration_ms,
        captured_at=clock(),
        detail=f"{width}x{height} {size_bytes}B",
        png_bytes=png_bytes if request.attach_bytes else b"",
    )


def capture_android(
    request: ScreenshotRequest,
    *,
    runner: SubprocessRunner = subprocess.run,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> ScreenshotResult:
    """Run ``adb shell screencap`` → ``adb pull`` → return
    :class:`ScreenshotResult`.

    Degrades to ``status="mock"`` when ``adb`` isn't on PATH — the
    agent loop can distinguish that from a real device / emulator
    failure.
    """
    if request.platform != "android":
        raise MobileScreenshotConfigError(
            f"capture_android requires platform='android' — got {request.platform!r}"
        )

    if _tool_missing("adb"):
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.mock,
            path=request.output_path,
            format=request.format, captured_at=clock(),
            detail="adb not on PATH",
        )

    _ensure_parent(request.output_path)
    env = _build_subprocess_env(request.env)
    start = monotonic()

    # Step 1 — ``adb shell screencap -p <remote>``
    capture_argv = build_android_capture_argv(
        request.android_remote_path, serial=request.android_serial,
    )
    capture_argv[0] = _resolve_bin(capture_argv[0])
    try:
        capture_proc = runner(
            capture_argv,
            capture_output=True, text=True,
            timeout=request.timeout_s, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"adb screencap timed out after {request.timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"adb screencap error: {exc}",
        )
    if capture_proc.returncode != 0:
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=(
                f"adb screencap rc={capture_proc.returncode} "
                f"{_tail(capture_proc.stderr or capture_proc.stdout)}"
            ),
        )

    # Step 2 — ``adb pull <remote> <local>``
    pull_argv = build_android_pull_argv(
        request.android_remote_path, request.output_path,
        serial=request.android_serial,
    )
    pull_argv[0] = _resolve_bin(pull_argv[0])
    try:
        pull_proc = runner(
            pull_argv,
            capture_output=True, text=True,
            timeout=request.timeout_s, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"adb pull timed out after {request.timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"adb pull error: {exc}",
        )
    if pull_proc.returncode != 0:
        return ScreenshotResult(
            session_id=request.session_id, platform="android",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=(
                f"adb pull rc={pull_proc.returncode} "
                f"{_tail(pull_proc.stderr or pull_proc.stdout)}"
            ),
        )

    duration_ms = int((monotonic() - start) * 1000)
    return _finalise_success(
        request=request, path=request.output_path,
        duration_ms=duration_ms, clock=clock,
    )


def capture_ios(
    request: ScreenshotRequest,
    *,
    runner: SubprocessRunner = subprocess.run,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> ScreenshotResult:
    """Run ``xcrun simctl io <udid> screenshot`` → return
    :class:`ScreenshotResult`.

    Two modes:

    * **Local macOS** — dispatches the xcrun argv directly to the
      host. Requires ``xcrun`` on PATH.
    * **Remote macOS via ssh** — when
      :attr:`ScreenshotRequest.ios_remote_host` is set, dispatches
      over ``ssh <host> -- xcrun …`` and pulls the PNG back with
      ``scp``. Requires ``ssh`` + ``scp`` on the local PATH; no
      assumption about toolchain versions on the remote side.

    Degrades to ``status="mock"`` when the required tool is missing
    (``xcrun`` / ``ssh``)."""
    if request.platform != "ios":
        raise MobileScreenshotConfigError(
            f"capture_ios requires platform='ios' — got {request.platform!r}"
        )

    if request.is_remote_ios:
        return _capture_ios_remote(
            request, runner=runner, clock=clock, monotonic=monotonic,
        )
    return _capture_ios_local(
        request, runner=runner, clock=clock, monotonic=monotonic,
    )


def _capture_ios_local(
    request: ScreenshotRequest,
    *,
    runner: SubprocessRunner,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
) -> ScreenshotResult:
    if _tool_missing("xcrun"):
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.mock,
            path=request.output_path,
            format=request.format, captured_at=clock(),
            detail="xcrun not on PATH (no local macOS Xcode)",
        )

    _ensure_parent(request.output_path)
    env = _build_subprocess_env(request.env)
    argv = build_ios_capture_argv(request.output_path, udid=request.ios_udid)
    argv[0] = _resolve_bin(argv[0])
    start = monotonic()
    try:
        proc = runner(
            argv,
            capture_output=True, text=True,
            timeout=request.timeout_s, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"xcrun simctl io timed out after {request.timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"xcrun simctl io error: {exc}",
        )
    if proc.returncode != 0:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=(
                f"xcrun simctl io rc={proc.returncode} "
                f"{_tail(proc.stderr or proc.stdout)}"
            ),
        )
    duration_ms = int((monotonic() - start) * 1000)
    return _finalise_success(
        request=request, path=request.output_path,
        duration_ms=duration_ms, clock=clock,
    )


def _capture_ios_remote(
    request: ScreenshotRequest,
    *,
    runner: SubprocessRunner,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
) -> ScreenshotResult:
    if _tool_missing("ssh"):
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.mock,
            path=request.output_path,
            format=request.format, captured_at=clock(),
            detail="ssh not on PATH — cannot reach remote macOS runner",
        )
    if _tool_missing("scp"):
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.mock,
            path=request.output_path,
            format=request.format, captured_at=clock(),
            detail="scp not on PATH — cannot pull remote PNG back",
        )

    _ensure_parent(request.output_path)
    env = _build_subprocess_env(request.env)
    # Remote PNG path uses the session id to avoid two concurrent
    # captures on the same runner fighting over the same file.
    remote_tmp = request.ios_remote_tmp_dir.rstrip("/") or "/tmp"
    remote_path = f"{remote_tmp}/omnisight-screenshot-{request.session_id}.png"

    capture_argv = build_ios_remote_capture_argv(
        request.ios_remote_host, remote_path, udid=request.ios_udid,
    )
    capture_argv[0] = _resolve_bin(capture_argv[0])
    start = monotonic()
    try:
        capture_proc = runner(
            capture_argv,
            capture_output=True, text=True,
            timeout=request.timeout_s, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"ssh xcrun simctl io timed out after {request.timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"ssh xcrun simctl io error: {exc}",
        )
    if capture_proc.returncode != 0:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=(
                f"ssh xcrun simctl io rc={capture_proc.returncode} "
                f"{_tail(capture_proc.stderr or capture_proc.stdout)}"
            ),
        )

    scp_argv = build_ios_scp_argv(
        request.ios_remote_host, remote_path, request.output_path,
    )
    scp_argv[0] = _resolve_bin(scp_argv[0])
    try:
        scp_proc = runner(
            scp_argv,
            capture_output=True, text=True,
            timeout=request.timeout_s, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"scp pull timed out after {request.timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=f"scp pull error: {exc}",
        )
    if scp_proc.returncode != 0:
        return ScreenshotResult(
            session_id=request.session_id, platform="ios",
            status=ScreenshotStatus.fail,
            path=request.output_path,
            format=request.format,
            duration_ms=int((monotonic() - start) * 1000),
            captured_at=clock(),
            detail=(
                f"scp rc={scp_proc.returncode} "
                f"{_tail(scp_proc.stderr or scp_proc.stdout)}"
            ),
        )
    duration_ms = int((monotonic() - start) * 1000)
    return _finalise_success(
        request=request, path=request.output_path,
        duration_ms=duration_ms, clock=clock,
    )


def capture(
    request: ScreenshotRequest,
    *,
    runner: SubprocessRunner = subprocess.run,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> ScreenshotResult:
    """Platform-dispatching capture entry point.

    Callers hand over one :class:`ScreenshotRequest`; this function
    picks :func:`capture_android` or :func:`capture_ios`. Errors from
    the underlying subprocess are captured on the returned
    :class:`ScreenshotResult` rather than raised — matches V2
    ``ui_sandbox`` + V6 ``mobile_sandbox`` semantics so the agent's
    ReAct loop never has to wrap this in a try/except.
    """
    if not isinstance(request, ScreenshotRequest):
        raise TypeError("request must be ScreenshotRequest")
    if request.platform == "android":
        return capture_android(
            request, runner=runner, clock=clock, monotonic=monotonic,
        )
    if request.platform == "ios":
        return capture_ios(
            request, runner=runner, clock=clock, monotonic=monotonic,
        )
    # unreachable — ScreenshotRequest.__post_init__ rejects unknown
    # platforms, but be explicit for readers. pragma: no cover
    raise MobileScreenshotConfigError(
        f"unsupported platform {request.platform!r}"
    )


# ───────────────────────────────────────────────────────────────────
#  Human-readable status renderer
# ───────────────────────────────────────────────────────────────────


def render_screenshot_result_markdown(
    result: ScreenshotResult,
) -> str:
    """Render a short markdown block for agent HUDs / CI logs."""
    status = result.status.value
    lines = [
        f"### Mobile screenshot — {result.platform}",
        f"- session: `{result.session_id}`",
        f"- status: `{status}`",
    ]
    if result.ok:
        lines.append(
            f"- geometry: {result.width}x{result.height} "
            f"({result.size_bytes} B)"
        )
    if result.path:
        lines.append(f"- path: `{result.path}`")
    if result.duration_ms:
        lines.append(f"- duration: {result.duration_ms} ms")
    if result.detail:
        lines.append(f"- detail: {result.detail}")
    return "\n".join(lines) + "\n"
