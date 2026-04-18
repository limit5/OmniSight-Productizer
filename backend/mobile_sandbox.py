"""V6 #1 (issue #322) — Per-session mobile build server.

Gives each agent session an isolated "build + render" pipeline for
iOS / Android apps, so Opus-4.7-driven mobile codegen can be visually
verified in-the-loop the same way V2 ``ui_sandbox`` does for web.

Pipelines by platform
---------------------

* **Android** — Docker container (`gradle:jdk21` or the configured
  toolchain image) + Gradle build of the workspace → `.apk`
  (optionally `.aab`) → Android emulator boot (`emulator -avd`) +
  `adb install` + screenshot via `adb shell screencap`. All the
  heavy lifting lives inside the container so we don't pollute the
  host `ANDROID_HOME` / JDK.
* **iOS** — because `xcodebuild` requires macOS, we dispatch via a
  **remote delegate** (ssh to a mac host, a fastlane REST
  callback, or BrowserStack's Xcode Cloud API). The local module
  emits the exact argv + env envelope, wires the delegate, and
  stores the returned `.app` path + screenshot bundle. When no
  delegate is configured (dev box / Linux CI) the pipeline degrades
  to a `mock` status so the caller can distinguish "tooling missing"
  from "build failed".

Why one module for two very different pipelines
-----------------------------------------------

Because the **agent-facing contract** is identical: create session →
build → install → screenshot → inspect errors → retry. Exposing one
``MobileSandboxManager`` that takes `platform` keeps the ReAct loop
simple; under the hood we dispatch to Android or iOS executors.
This mirrors how ``mobile_simulator`` unifies xcuitest / espresso /
flutter / react-native behind one entry point.

Design decisions
----------------

* **Dependency-injected executors.** Tests use an in-memory
  :class:`FakeAndroidExecutor` / :class:`FakeIosExecutor` fixture
  without needing Docker, an AVD, or an ssh tunnel. Production wires
  :class:`SubprocessAndroidExecutor` + :class:`SshMacOsIosExecutor`.
* **Frozen dataclasses.** :class:`MobileSandboxConfig` /
  :class:`MobileSandboxInstance` / :class:`BuildReport` /
  :class:`ScreenshotReport` mirror ``ui_sandbox`` — state transitions
  go through ``replace()`` so the manager's history log is trivially
  auditable.
* **Deterministic argv builders.** :func:`build_android_build_argv`
  and :func:`build_ios_build_argv` are pure — same config → byte
  identical argv. Callers can assert the exact command without
  stubbing time/os.
* **One sandbox per session.** :meth:`MobileSandboxManager.create`
  raises :class:`MobileSandboxAlreadyExists` if the session already
  has a sandbox, matching V2 ``ui_sandbox`` semantics.
* **Graceful fallback.** Executor errors mark the sandbox ``failed``
  and capture the stderr — they do not propagate mid-agent-loop.
* **Build error → agent auto-fix hand-off.** :func:`parse_build_error`
  best-effort parses Gradle / Xcode / Swift compiler diagnostics
  into structured :class:`BuildError` records. V6 row #6 feeds these
  back into the agent loop.

Public API
----------
* :class:`MobileSandboxConfig` / :class:`MobileSandboxInstance` —
  inputs/outputs of :meth:`MobileSandboxManager.create`.
* :class:`MobileSandboxManager` — thread-safe registry keyed on
  ``session_id``.
* :class:`AndroidExecutor` / :class:`IosExecutor` — protocols.
* :class:`SubprocessAndroidExecutor` — shells out to `docker`.
* :class:`SshMacOsIosExecutor` — shells out to ssh + a remote mac
  runner wrapper (``omnisight-ios-runner``).
* :func:`build_android_build_argv` / :func:`build_ios_build_argv` —
  pure argv helpers.
* :func:`parse_build_error` — structured diagnostics extractor.

Contract pinned by ``backend/tests/test_mobile_sandbox.py``
-----------------------------------------------------------
* :data:`MOBILE_SANDBOX_SCHEMA_VERSION` is a semver string; bump on
  any change to ``*.to_dict()`` shape.
* :data:`DEFAULT_ANDROID_BUILD_IMAGE` pins the Android toolchain
  image — changing it ships as major.
* :data:`DEFAULT_GRADLE_TASK` / :data:`DEFAULT_XCODEBUILD_ACTION`
  match the canonical CI pipelines.
* :func:`build_android_build_argv` / :func:`build_ios_build_argv`
  are deterministic.
* :func:`parse_build_error` never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)


__all__ = [
    "MOBILE_SANDBOX_SCHEMA_VERSION",
    "DEFAULT_ANDROID_BUILD_IMAGE",
    "DEFAULT_GRADLE_TASK",
    "DEFAULT_XCODEBUILD_ACTION",
    "DEFAULT_XCODEBUILD_CONFIGURATION",
    "DEFAULT_ANDROID_WORKDIR",
    "DEFAULT_IOS_WORKDIR",
    "DEFAULT_BUILD_TIMEOUT_S",
    "DEFAULT_INSTALL_TIMEOUT_S",
    "DEFAULT_SCREENSHOT_TIMEOUT_S",
    "DEFAULT_IDLE_LIMIT_S",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_SCREENSHOT_FORMATS",
    "MobileSandboxStatus",
    "MobileSandboxConfig",
    "MobileSandboxInstance",
    "BuildError",
    "BuildReport",
    "InstallReport",
    "ScreenshotReport",
    "AndroidExecutor",
    "IosExecutor",
    "SubprocessAndroidExecutor",
    "SshMacOsIosExecutor",
    "MobileSandboxManager",
    "MobileSandboxError",
    "MobileSandboxAlreadyExists",
    "MobileSandboxNotFound",
    "MobileSandboxConfigError",
    "format_sandbox_name",
    "validate_workspace",
    "allocate_screenshot_port",
    "build_android_build_argv",
    "build_ios_build_argv",
    "build_android_install_argv",
    "build_android_screenshot_argv",
    "build_ios_screenshot_argv",
    "locate_android_apk",
    "locate_ios_app_bundle",
    "parse_build_error",
    "render_sandbox_status_markdown",
]


# ───────────────────────────────────────────────────────────────────
#  Constants — pinned by the contract tests
# ───────────────────────────────────────────────────────────────────

#: Bump whenever any ``to_dict()`` shape below changes.
MOBILE_SANDBOX_SCHEMA_VERSION = "1.0.0"

#: Android build happens inside Docker — pin a JDK 21 + Gradle image
#: that matches the V1 Android scaffolder's expected toolchain.
#: Callers that need a different JDK override via
#: ``MobileSandboxConfig.android_image``.
DEFAULT_ANDROID_BUILD_IMAGE = "gradle:8.10-jdk21"

#: Gradle task for a release-artifact build. ``assembleDebug`` is the
#: canonical "does it compile" gate — we deliberately don't default to
#: ``assembleRelease`` because release builds need signing config.
DEFAULT_GRADLE_TASK = "assembleDebug"

#: xcodebuild action that produces a ``.app`` bundle we can install
#: on the Simulator. ``build-for-testing`` would also work but
#: ``build`` is the minimum cycle and plays nicer when the project
#: lacks a test target.
DEFAULT_XCODEBUILD_ACTION = "build"

#: xcodebuild configuration — ``Debug`` keeps symbols + avoids the
#: provisioning profile dance ``Release`` requires.
DEFAULT_XCODEBUILD_CONFIGURATION = "Debug"

#: Where the Gradle workspace is bind-mounted inside the Android
#: build container. ``/workspace`` avoids the convention collision
#: with the project's own ``/app`` Gradle module name.
DEFAULT_ANDROID_WORKDIR = "/workspace"

#: Remote iOS runner's workspace root. Callers override via
#: ``MobileSandboxConfig.ios_workdir`` when the mac's checkout lives
#: elsewhere.
DEFAULT_IOS_WORKDIR = "/tmp/omnisight-ios"

#: Gradle cold build + assemble can take minutes on first run
#: (dependency resolution). Pin 20 min upper bound; callers shorten
#: for CI.
DEFAULT_BUILD_TIMEOUT_S = 1200.0

#: Install + launch via adb / simctl should return within a minute
#: even on cold devices.
DEFAULT_INSTALL_TIMEOUT_S = 60.0

#: Screencap / simctl screenshot are cheap — 20 s is plenty.
DEFAULT_SCREENSHOT_TIMEOUT_S = 20.0

#: Idle reaper limit — 15 minutes matches V2 row 2 policy; real
#: reaping lives in the sibling lifecycle module.
DEFAULT_IDLE_LIMIT_S = 900.0

#: Platforms the manager dispatches. Order is stable — tests pin it.
SUPPORTED_PLATFORMS: tuple[str, ...] = ("android", "ios")

#: Screenshot formats accepted by :class:`ScreenshotReport`.
SUPPORTED_SCREENSHOT_FORMATS: tuple[str, ...] = ("png",)


_SAFE_SESSION_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

# Gradle / AGP compile-error signatures. We target the `* What went
# wrong:` block Gradle prints on task failure plus the
# `e:/w: file:line:col: message` diagnostic form emitted by kotlinc /
# javac.
_GRADLE_TRIGGER_RE = re.compile(
    r"(?:FAILURE: Build failed|\* What went wrong:|BUILD FAILED|"
    r"Execution failed for task|Could not resolve)",
    re.IGNORECASE,
)
_GRADLE_DIAGNOSTIC_RE = re.compile(
    r"(?P<severity>[ew]):\s*(?P<file>[^\s:][^\s:]*):(?P<line>\d+)"
    r"(?::(?P<col>\d+))?:\s*(?P<message>.+)"
)
# Xcode/Swift compile-error signature, same shape as `file:line:col:
# error: message`.
_XCODE_DIAGNOSTIC_RE = re.compile(
    r"(?P<file>/[^\s:][^\s:]*\.(?:swift|m|mm|h|hpp)):(?P<line>\d+)"
    r"(?::(?P<col>\d+))?:\s*(?P<severity>error|warning):\s*(?P<message>.+)"
)

# Android emulator default-host port range — 5554-5585 is the
# adb-well-known span but some of those ports collide with the local
# adb server (5037). We mirror ui_sandbox's dynamic range.
DEFAULT_SCREENSHOT_PORT_RANGE: tuple[int, int] = (41000, 41999)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileSandboxError(RuntimeError):
    """Base class for mobile-sandbox manager errors."""


class MobileSandboxAlreadyExists(MobileSandboxError):
    """Raised by :meth:`MobileSandboxManager.create` when the session
    already has a live sandbox."""


class MobileSandboxNotFound(MobileSandboxError):
    """Raised when the caller references an unknown ``session_id``."""


class MobileSandboxConfigError(MobileSandboxError):
    """Raised when a :class:`MobileSandboxConfig` fails validation at
    construction time."""


# ───────────────────────────────────────────────────────────────────
#  Enum + dataclasses
# ───────────────────────────────────────────────────────────────────


class MobileSandboxStatus(str, Enum):
    """Lifecycle states of a mobile sandbox.

    ``pending``     → created, executor not yet invoked
    ``building``    → build issued, artifact not yet produced
    ``built``       → artifact on disk / remote, not yet installed
    ``installing``  → install requested (adb install / simctl install)
    ``running``     → app launched, ready to screenshot
    ``stopping``    → stop requested, artifacts still winding down
    ``stopped``     → build/install container stopped
    ``failed``      → unrecoverable error; ``error`` field holds detail
    """

    pending = "pending"
    building = "building"
    built = "built"
    installing = "installing"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"


_TERMINAL_STATUSES = frozenset({MobileSandboxStatus.stopped, MobileSandboxStatus.failed})


@dataclass(frozen=True)
class MobileSandboxConfig:
    """Inputs to :meth:`MobileSandboxManager.create`.

    Frozen + deterministic — two configs with the same field values
    produce byte-identical argv output from
    :func:`build_android_build_argv` / :func:`build_ios_build_argv`.
    """

    session_id: str
    platform: str
    workspace_path: str

    # ── Android ─────────────────────────────────────────────
    android_image: str = DEFAULT_ANDROID_BUILD_IMAGE
    gradle_task: str = DEFAULT_GRADLE_TASK
    android_workdir: str = DEFAULT_ANDROID_WORKDIR
    android_module: str = "app"
    android_emulator_avd: str = ""
    android_package_name: str = ""
    android_launch_activity: str = ""

    # ── iOS ─────────────────────────────────────────────────
    ios_workdir: str = DEFAULT_IOS_WORKDIR
    xcode_scheme: str = ""
    xcode_configuration: str = DEFAULT_XCODEBUILD_CONFIGURATION
    xcode_action: str = DEFAULT_XCODEBUILD_ACTION
    xcode_project: str = ""           # *.xcworkspace | *.xcodeproj
    ios_simulator_udid: str = ""
    ios_simulator_device: str = "iPhone 15 Pro"
    ios_simulator_runtime: str = ""
    ios_bundle_id: str = ""
    ios_remote_host: str = ""         # ssh target e.g. "builder@mac-runner"

    # ── Timeouts ────────────────────────────────────────────
    build_timeout_s: float = DEFAULT_BUILD_TIMEOUT_S
    install_timeout_s: float = DEFAULT_INSTALL_TIMEOUT_S
    screenshot_timeout_s: float = DEFAULT_SCREENSHOT_TIMEOUT_S
    stop_timeout_s: float = 10.0

    # ── Misc ────────────────────────────────────────────────
    env: Mapping[str, str] = field(default_factory=dict)
    build_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileSandboxConfigError("session_id must be a non-empty string")
        if not _SAFE_SESSION_RE.fullmatch(self.session_id):
            raise MobileSandboxConfigError(
                "session_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.session_id!r}"
            )
        plat = (self.platform or "").strip().lower()
        if plat not in SUPPORTED_PLATFORMS:
            raise MobileSandboxConfigError(
                f"platform must be one of {SUPPORTED_PLATFORMS!r} — got "
                f"{self.platform!r}"
            )
        object.__setattr__(self, "platform", plat)
        if not isinstance(self.workspace_path, str) or not self.workspace_path.strip():
            raise MobileSandboxConfigError("workspace_path must be a non-empty string")
        if not isinstance(self.android_image, str) or not self.android_image.strip():
            raise MobileSandboxConfigError("android_image must be non-empty")
        if not isinstance(self.gradle_task, str) or not self.gradle_task.strip():
            raise MobileSandboxConfigError("gradle_task must be non-empty")
        if not isinstance(self.android_workdir, str) or not self.android_workdir.startswith("/"):
            raise MobileSandboxConfigError("android_workdir must be an absolute path")
        if not isinstance(self.ios_workdir, str) or not self.ios_workdir.startswith("/"):
            raise MobileSandboxConfigError("ios_workdir must be an absolute path")
        if self.xcode_action not in ("build", "build-for-testing"):
            raise MobileSandboxConfigError(
                f"xcode_action must be 'build' or 'build-for-testing' — "
                f"got {self.xcode_action!r}"
            )
        if self.xcode_configuration not in ("Debug", "Release"):
            raise MobileSandboxConfigError(
                f"xcode_configuration must be 'Debug' or 'Release' — "
                f"got {self.xcode_configuration!r}"
            )
        for key, value in (
            ("build_timeout_s", self.build_timeout_s),
            ("install_timeout_s", self.install_timeout_s),
            ("screenshot_timeout_s", self.screenshot_timeout_s),
            ("stop_timeout_s", self.stop_timeout_s),
        ):
            if not isinstance(value, (int, float)) or value <= 0:
                raise MobileSandboxConfigError(f"{key} must be positive")
        # iOS requires a remote delegate for xcodebuild — on Linux
        # hosts, the caller may leave it empty and the executor will
        # degrade to a mock build. That contract is enforced by the
        # executor, not here (a blank host is a legitimate degraded
        # mode).
        if not isinstance(self.ios_remote_host, str):
            raise MobileSandboxConfigError("ios_remote_host must be string")
        # Normalise env / build_args
        env_src = dict(self.env) if self.env else {}
        for k, v in env_src.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise MobileSandboxConfigError("env keys and values must be strings")
        object.__setattr__(self, "env", MappingProxyType(env_src))
        if isinstance(self.build_args, (list, tuple)):
            args = tuple(str(part) for part in self.build_args)
        else:
            raise MobileSandboxConfigError("build_args must be a sequence of strings")
        object.__setattr__(self, "build_args", args)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_SANDBOX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "platform": self.platform,
            "workspace_path": self.workspace_path,
            "android_image": self.android_image,
            "gradle_task": self.gradle_task,
            "android_workdir": self.android_workdir,
            "android_module": self.android_module,
            "android_emulator_avd": self.android_emulator_avd,
            "android_package_name": self.android_package_name,
            "android_launch_activity": self.android_launch_activity,
            "ios_workdir": self.ios_workdir,
            "xcode_scheme": self.xcode_scheme,
            "xcode_configuration": self.xcode_configuration,
            "xcode_action": self.xcode_action,
            "xcode_project": self.xcode_project,
            "ios_simulator_udid": self.ios_simulator_udid,
            "ios_simulator_device": self.ios_simulator_device,
            "ios_simulator_runtime": self.ios_simulator_runtime,
            "ios_bundle_id": self.ios_bundle_id,
            "ios_remote_host": self.ios_remote_host,
            "build_timeout_s": float(self.build_timeout_s),
            "install_timeout_s": float(self.install_timeout_s),
            "screenshot_timeout_s": float(self.screenshot_timeout_s),
            "stop_timeout_s": float(self.stop_timeout_s),
            "env": dict(self.env),
            "build_args": list(self.build_args),
        }


@dataclass(frozen=True)
class BuildError:
    """One structured build diagnostic parsed from Gradle / xcodebuild
    stderr by :func:`parse_build_error`.

    Used by V6 row #6 (``build error → agent auto-fix``) to decide
    which file + line the agent should patch.
    """

    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    severity: str = "error"           # "error" | "warning"
    tool: str = "gradle"              # "gradle" | "xcodebuild"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class BuildReport:
    """Result of the build phase.

    status:
      * ``pass``   — artifact produced (apk / aab / app)
      * ``fail``   — build tool exited non-zero; see ``errors``
      * ``mock``   — tooling absent (Docker not on PATH / no ssh
        target); agent should not interpret as real success
      * ``skip``   — platform config requested no build
    """
    status: str = "skip"
    artifact_path: str = ""           # absolute on host or remote
    tool: str = ""                    # gradle | xcodebuild
    duration_ms: int = 0
    exit_code: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    errors: tuple[BuildError, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "artifact_path": self.artifact_path,
            "tool": self.tool,
            "duration_ms": int(self.duration_ms),
            "exit_code": int(self.exit_code),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "errors": [e.to_dict() for e in self.errors],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InstallReport:
    """Result of the install phase (adb install / simctl install)."""
    status: str = "skip"              # "pass" | "fail" | "skip" | "mock"
    launched: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "launched": self.launched,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScreenshotReport:
    """Result of the screenshot phase."""
    status: str = "skip"              # "pass" | "fail" | "skip" | "mock"
    path: str = ""                    # absolute on host
    format: str = "png"
    width: int = 0
    height: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "format": self.format,
            "width": int(self.width),
            "height": int(self.height),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MobileSandboxInstance:
    """Snapshot of a mobile sandbox's state.

    Frozen — state transitions happen by :func:`dataclasses.replace`.
    """

    session_id: str
    sandbox_name: str
    config: MobileSandboxConfig
    status: MobileSandboxStatus = MobileSandboxStatus.pending
    container_id: str | None = None       # Android: docker container id
    delegate_handle: str | None = None    # iOS: ssh/fastlane handle
    screenshot_port: int | None = None
    build: BuildReport = field(default_factory=BuildReport)
    install: InstallReport = field(default_factory=InstallReport)
    screenshot: ScreenshotReport = field(default_factory=ScreenshotReport)
    created_at: float = 0.0
    started_at: float | None = None
    built_at: float | None = None
    ready_at: float | None = None
    stopped_at: float | None = None
    last_active_at: float = 0.0
    error: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileSandboxConfigError("session_id must be non-empty")
        if not isinstance(self.sandbox_name, str) or not self.sandbox_name.strip():
            raise MobileSandboxConfigError("sandbox_name must be non-empty")
        if not isinstance(self.status, MobileSandboxStatus):
            raise MobileSandboxConfigError(
                f"status must be MobileSandboxStatus, got {type(self.status)!r}"
            )
        if self.created_at < 0 or self.last_active_at < 0:
            raise MobileSandboxConfigError("timestamps must be non-negative")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def is_running(self) -> bool:
        return self.status is MobileSandboxStatus.running

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def idle_seconds(self, now: float | None = None) -> float:
        if self.last_active_at <= 0:
            return 0.0
        ref = time.time() if now is None else now
        return max(0.0, ref - self.last_active_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_SANDBOX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "sandbox_name": self.sandbox_name,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "container_id": self.container_id,
            "delegate_handle": self.delegate_handle,
            "screenshot_port": self.screenshot_port,
            "build": self.build.to_dict(),
            "install": self.install.to_dict(),
            "screenshot": self.screenshot.to_dict(),
            "created_at": float(self.created_at),
            "started_at": None if self.started_at is None else float(self.started_at),
            "built_at": None if self.built_at is None else float(self.built_at),
            "ready_at": None if self.ready_at is None else float(self.ready_at),
            "stopped_at": None if self.stopped_at is None else float(self.stopped_at),
            "last_active_at": float(self.last_active_at),
            "error": self.error,
            "warnings": list(self.warnings),
        }


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def format_sandbox_name(
    session_id: str, platform: str, *, prefix: str = "omnisight-mobile"
) -> str:
    """Produce a Docker / ssh-safe sandbox name for ``session_id``.

    Matches the Docker container-name grammar
    ``[a-zA-Z0-9][a-zA-Z0-9_.-]*`` — we lowercase, replace illegal
    chars, and cap at 63 chars. The platform is embedded to make
    ``docker ps`` / ``ps`` output identifiable without tagging.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise MobileSandboxConfigError("session_id must be non-empty")
    plat = (platform or "").strip().lower()
    if plat not in SUPPORTED_PLATFORMS:
        raise MobileSandboxConfigError(
            f"platform must be one of {SUPPORTED_PLATFORMS!r}"
        )
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "-", session_id.strip().lower())
    safe = safe.strip("-_.") or "sess"
    full = f"{prefix}-{plat}-{safe}"
    return full[:63]


def validate_workspace(workspace_path: str) -> Path:
    """Return the resolved Path of ``workspace_path`` if it exists and
    is a directory — raise otherwise.

    Identical contract to ``ui_sandbox.validate_workspace`` for
    consistent error messages across the two sandboxes.
    """
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        raise MobileSandboxConfigError("workspace_path must be non-empty")
    path = Path(workspace_path)
    if not path.is_absolute():
        raise MobileSandboxConfigError(
            f"workspace_path must be absolute: {workspace_path!r}"
        )
    if not path.exists():
        raise MobileSandboxConfigError(
            f"workspace_path does not exist: {workspace_path!r}"
        )
    if not path.is_dir():
        raise MobileSandboxConfigError(
            f"workspace_path is not a directory: {workspace_path!r}"
        )
    return path


def allocate_screenshot_port(
    session_id: str,
    *,
    in_use: Iterable[int] = (),
    port_range: tuple[int, int] = DEFAULT_SCREENSHOT_PORT_RANGE,
) -> int:
    """Deterministically pick a screenshot-delivery port in
    ``port_range``. Used by the Android executor to expose the
    emulator's ADB screenshot endpoint when running headlessly.

    Hash-based + linear probe, same pattern as
    ``ui_sandbox.allocate_host_port`` so operators can debug with
    "session X always lives on 41137".
    """
    lo, hi = port_range
    if not (1 <= lo <= hi <= 65535):
        raise MobileSandboxConfigError(f"port_range invalid: {port_range!r}")
    span = hi - lo + 1
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % span
    taken = {int(p) for p in in_use}
    for offset in range(span):
        candidate = lo + (start + offset) % span
        if candidate not in taken:
            return candidate
    raise MobileSandboxError(
        f"no screenshot port available in range {port_range!r}"
    )


def build_android_build_argv(config: MobileSandboxConfig) -> list[str]:
    """Return the deterministic ``docker run`` argv for an Android
    Gradle build.

    Pure — same config in, byte-identical list out. The command is
    ``docker run --rm -v <workspace>:<workdir> -w <workdir> <image>
    ./gradlew <task> <build_args…>``. We bind the workspace read-write
    because Gradle writes ``.gradle/`` + ``build/`` caches back.
    """
    if not isinstance(config, MobileSandboxConfig):
        raise TypeError("config must be MobileSandboxConfig")
    if config.platform != "android":
        raise MobileSandboxConfigError(
            "build_android_build_argv requires platform='android'"
        )
    sandbox_name = format_sandbox_name(config.session_id, "android")
    argv: list[str] = [
        "docker", "run", "--rm",
        "--name", sandbox_name,
        "-v", f"{config.workspace_path}:{config.android_workdir}",
        "-w", config.android_workdir,
    ]
    for key in sorted(config.env):
        argv += ["-e", f"{key}={config.env[key]}"]
    argv.append(config.android_image)
    # Prefer the wrapper — every modern Android project ships one.
    argv.append("./gradlew")
    if config.android_module and config.gradle_task:
        argv.append(f":{config.android_module}:{config.gradle_task}")
    else:
        argv.append(config.gradle_task)
    argv.append("--no-daemon")
    argv.append("--console=plain")
    for extra in config.build_args:
        argv.append(str(extra))
    return argv


def build_ios_build_argv(config: MobileSandboxConfig) -> list[str]:
    """Return the deterministic ``xcodebuild`` argv for an iOS build.

    Pure. When the caller has a remote mac delegate the caller wraps
    this argv with ``ssh <host> -- …`` themselves; keeping this
    function free of remote transport concerns means tests can assert
    the xcodebuild call and the ssh harness independently.
    """
    if not isinstance(config, MobileSandboxConfig):
        raise TypeError("config must be MobileSandboxConfig")
    if config.platform != "ios":
        raise MobileSandboxConfigError(
            "build_ios_build_argv requires platform='ios'"
        )
    argv: list[str] = ["xcodebuild", config.xcode_action]
    if config.xcode_project:
        # xcodebuild wants -workspace for .xcworkspace, -project for
        # .xcodeproj. We sniff the suffix.
        proj = config.xcode_project
        if proj.endswith(".xcworkspace"):
            argv += ["-workspace", proj]
        else:
            argv += ["-project", proj]
    if config.xcode_scheme:
        argv += ["-scheme", config.xcode_scheme]
    argv += ["-configuration", config.xcode_configuration]
    if config.ios_simulator_udid:
        dest = f"platform=iOS Simulator,id={config.ios_simulator_udid}"
    else:
        dest = f"platform=iOS Simulator,name={config.ios_simulator_device}"
        if config.ios_simulator_runtime:
            dest += f",OS={config.ios_simulator_runtime}"
    argv += ["-destination", dest]
    argv += ["-derivedDataPath", str(Path(config.ios_workdir) / "DerivedData")]
    argv += ["CODE_SIGN_IDENTITY=", "CODE_SIGNING_REQUIRED=NO",
             "CODE_SIGNING_ALLOWED=NO"]
    for extra in config.build_args:
        argv.append(str(extra))
    return argv


def build_android_install_argv(
    config: MobileSandboxConfig, apk_path: str
) -> list[str]:
    """Return ``adb install -r -g <apk>`` argv. ``-r`` reinstalls in
    place (keeps data), ``-g`` auto-grants runtime perms so the agent
    doesn't have to drive a permission-prompt dance."""
    if not apk_path or not isinstance(apk_path, str):
        raise MobileSandboxConfigError("apk_path must be a non-empty string")
    if config.platform != "android":
        raise MobileSandboxConfigError(
            "build_android_install_argv requires platform='android'"
        )
    return ["adb", "install", "-r", "-g", apk_path]


def build_android_screenshot_argv(
    remote_path: str = "/sdcard/omnisight-screenshot.png",
) -> list[str]:
    """Return ``adb shell screencap -p <remote>`` argv. Caller pulls
    the file back with ``adb pull <remote> <local>`` in a separate
    step."""
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        raise MobileSandboxConfigError("remote_path must be absolute")
    return ["adb", "shell", "screencap", "-p", remote_path]


def build_ios_screenshot_argv(output_path: str, *, udid: str = "booted") -> list[str]:
    """Return ``xcrun simctl io <udid> screenshot <out.png>`` argv."""
    if not isinstance(output_path, str) or not output_path:
        raise MobileSandboxConfigError("output_path must be non-empty")
    target = (udid or "booted").strip() or "booted"
    return ["xcrun", "simctl", "io", target, "screenshot", output_path]


def locate_android_apk(
    workspace_path: str, *, module: str = "app", variant: str = "debug",
) -> str | None:
    """Find the first APK Gradle produces for the canonical
    ``<module>/build/outputs/apk/<variant>/*.apk`` path.

    Returns ``None`` when no artifact exists — callers decide whether
    that's a build failure or a "skip screenshot" signal.
    """
    try:
        root = Path(workspace_path)
    except TypeError:
        return None
    if not root.is_dir():
        return None
    expected = root / module / "build" / "outputs" / "apk" / variant
    if expected.is_dir():
        apks = sorted(expected.glob("*.apk"))
        if apks:
            return str(apks[0])
    # Fallback — any .apk under <module>/build/outputs
    outputs = root / module / "build" / "outputs"
    if outputs.is_dir():
        apks = sorted(outputs.rglob("*.apk"))
        if apks:
            return str(apks[0])
    return None


def locate_ios_app_bundle(
    derived_data_path: str,
    *,
    configuration: str = "Debug",
    platform: str = "iphonesimulator",
) -> str | None:
    """Find the first ``.app`` bundle produced by xcodebuild at
    ``<derived>/Build/Products/<Configuration>-<platform>/*.app``."""
    try:
        root = Path(derived_data_path)
    except TypeError:
        return None
    if not root.is_dir():
        return None
    expected = root / "Build" / "Products" / f"{configuration}-{platform}"
    if expected.is_dir():
        apps = sorted(expected.glob("*.app"))
        if apps:
            return str(apps[0])
    products = root / "Build" / "Products"
    if products.is_dir():
        apps = sorted(products.rglob("*.app"))
        if apps:
            return str(apps[0])
    return None


def parse_build_error(
    stderr_text: str, *, tool: str = "gradle",
) -> tuple[BuildError, ...]:
    """Best-effort parse of Gradle or xcodebuild diagnostics.

    Returns an empty tuple when nothing matches — never raises. V6
    row #6 hands the result back to the agent so it can patch the
    offending file directly.
    """
    if not stderr_text or not isinstance(stderr_text, str):
        return ()
    tool_norm = (tool or "").strip().lower() or "gradle"
    out: list[BuildError] = []
    seen: set[tuple[str, str | None, int | None, str]] = set()

    if tool_norm == "gradle":
        lines = stderr_text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            diag = _GRADLE_DIAGNOSTIC_RE.search(line)
            if diag:
                raw_sev = diag.group("severity").lower()
                severity = "error" if raw_sev == "e" else "warning"
                raw_line = diag.group("line")
                raw_col = diag.group("col")
                rec = (
                    diag.group("message").strip(),
                    diag.group("file"),
                    int(raw_line) if raw_line else None,
                    severity,
                )
                if rec not in seen:
                    seen.add(rec)
                    out.append(BuildError(
                        message=rec[0],
                        file=rec[1],
                        line=rec[2],
                        column=int(raw_col) if raw_col else None,
                        severity=severity,
                        tool="gradle",
                    ))
                i += 1
                continue
            trig = _GRADLE_TRIGGER_RE.search(line)
            if trig:
                # Gradle's "* What went wrong" block: collect the next
                # non-blank line as the message.
                message = line.strip()
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    message = f"{message} {lines[j].strip()}".strip()
                rec = (message, None, None, "error")
                if rec not in seen:
                    seen.add(rec)
                    out.append(BuildError(
                        message=message, severity="error", tool="gradle",
                    ))
                i = max(i + 1, j + 1)
                continue
            i += 1
        return tuple(out)

    if tool_norm in ("xcodebuild", "xcode", "swift"):
        for line in stderr_text.splitlines():
            diag = _XCODE_DIAGNOSTIC_RE.search(line)
            if not diag:
                continue
            severity = diag.group("severity").lower()
            raw_line = diag.group("line")
            raw_col = diag.group("col")
            rec = (
                diag.group("message").strip(),
                diag.group("file"),
                int(raw_line) if raw_line else None,
                severity,
            )
            if rec in seen:
                continue
            seen.add(rec)
            out.append(BuildError(
                message=rec[0],
                file=rec[1],
                line=rec[2],
                column=int(raw_col) if raw_col else None,
                severity=severity,
                tool="xcodebuild",
            ))
        return tuple(out)

    return ()


def render_sandbox_status_markdown(instance: MobileSandboxInstance) -> str:
    """Deterministic markdown summary for operator logs / SSE bodies.

    Mirrors ``ui_sandbox.render_sandbox_status_markdown`` shape so the
    UI can render both sandbox families with the same component.
    """
    if not isinstance(instance, MobileSandboxInstance):
        raise TypeError("instance must be MobileSandboxInstance")
    lines: list[str] = [
        f"### Mobile Sandbox `{instance.session_id}`",
        "",
        f"- status: **{instance.status.value}**",
        f"- platform: `{instance.config.platform}`",
        f"- sandbox: `{instance.sandbox_name}`",
        f"- container_id: `{instance.container_id or '(none)'}`",
        f"- delegate: `{instance.delegate_handle or '(none)'}`",
        f"- workspace: `{instance.config.workspace_path}`",
        f"- build: `{instance.build.status}` artifact=`{instance.build.artifact_path or '(none)'}`",
        f"- install: `{instance.install.status}` launched=`{instance.install.launched}`",
        f"- screenshot: `{instance.screenshot.status}` path=`{instance.screenshot.path or '(none)'}`",
    ]
    if instance.error:
        lines.append(f"- error: {instance.error}")
    if instance.warnings:
        lines.append("- warnings: " + ", ".join(instance.warnings))
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────
#  Executor protocols
# ───────────────────────────────────────────────────────────────────


class AndroidExecutor(Protocol):
    """Minimal shim the manager speaks to for Android builds + render."""

    def build(self, config: MobileSandboxConfig) -> BuildReport: ...
    def install(self, config: MobileSandboxConfig, artifact_path: str) -> InstallReport: ...
    def screenshot(
        self, config: MobileSandboxConfig, *, output_dir: str,
    ) -> ScreenshotReport: ...
    def stop(self, sandbox_name: str, *, timeout_s: float) -> None: ...


class IosExecutor(Protocol):
    """Minimal shim the manager speaks to for iOS builds + render."""

    def build(self, config: MobileSandboxConfig) -> BuildReport: ...
    def install(self, config: MobileSandboxConfig, artifact_path: str) -> InstallReport: ...
    def screenshot(
        self, config: MobileSandboxConfig, *, output_dir: str,
    ) -> ScreenshotReport: ...
    def stop(self, delegate_handle: str, *, timeout_s: float) -> None: ...


# ───────────────────────────────────────────────────────────────────
#  Subprocess executors (production glue)
# ───────────────────────────────────────────────────────────────────


def _tail(text: str | None, *, chars: int = 4000) -> str:
    if not text:
        return ""
    if len(text) <= chars:
        return text
    return text[-chars:]


class SubprocessAndroidExecutor:
    """Default :class:`AndroidExecutor` — shells out to ``docker`` +
    ``adb``. When ``docker`` / ``adb`` are absent it returns ``mock``
    reports so Linux CI without emulators can still exercise the
    manager's control-plane.
    """

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        adb_bin: str = "adb",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._docker_bin = docker_bin
        self._adb_bin = adb_bin
        self._runner = runner or subprocess.run

    def _run(
        self, argv: list[str], *, timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            argv, capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )

    def build(self, config: MobileSandboxConfig) -> BuildReport:
        if shutil.which(self._docker_bin) is None:
            return BuildReport(
                status="mock", tool="gradle",
                detail=f"{self._docker_bin} not on PATH",
            )
        argv = build_android_build_argv(config)
        # Replace the first token with the absolute docker path so
        # tests can assert both forms independently.
        argv[0] = shutil.which(self._docker_bin) or self._docker_bin
        start = time.monotonic()
        try:
            proc = self._run(argv, timeout_s=config.build_timeout_s)
        except subprocess.TimeoutExpired:
            return BuildReport(
                status="fail", tool="gradle",
                detail=f"docker gradle timed out after {config.build_timeout_s}s",
            )
        except Exception as exc:  # noqa: BLE001
            return BuildReport(
                status="fail", tool="gradle",
                detail=f"docker gradle error: {exc}",
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = _tail(proc.stdout)
        stderr = _tail(proc.stderr)
        errors = parse_build_error(stderr or stdout, tool="gradle")
        if proc.returncode != 0:
            return BuildReport(
                status="fail", tool="gradle",
                duration_ms=duration_ms, exit_code=proc.returncode,
                stdout_tail=stdout, stderr_tail=stderr, errors=errors,
                detail=f"gradle exited {proc.returncode}",
            )
        apk = locate_android_apk(
            config.workspace_path, module=config.android_module,
        )
        if apk is None:
            return BuildReport(
                status="fail", tool="gradle",
                duration_ms=duration_ms, exit_code=0,
                stdout_tail=stdout, stderr_tail=stderr,
                errors=errors, detail="gradle ok but no apk artifact",
            )
        return BuildReport(
            status="pass", tool="gradle", artifact_path=apk,
            duration_ms=duration_ms, exit_code=0,
            stdout_tail=stdout, stderr_tail=stderr, errors=errors,
        )

    def install(
        self, config: MobileSandboxConfig, artifact_path: str,
    ) -> InstallReport:
        if shutil.which(self._adb_bin) is None:
            return InstallReport(
                status="mock", detail=f"{self._adb_bin} not on PATH",
            )
        argv = build_android_install_argv(config, artifact_path)
        argv[0] = shutil.which(self._adb_bin) or self._adb_bin
        try:
            proc = self._run(argv, timeout_s=config.install_timeout_s)
        except subprocess.TimeoutExpired:
            return InstallReport(
                status="fail", detail=f"adb install timed out",
            )
        except Exception as exc:  # noqa: BLE001
            return InstallReport(status="fail", detail=f"adb error: {exc}")
        launched = proc.returncode == 0 and "Success" in (proc.stdout or "")
        return InstallReport(
            status="pass" if launched else "fail",
            launched=launched,
            detail=_tail(proc.stderr or proc.stdout, chars=400),
        )

    def screenshot(
        self, config: MobileSandboxConfig, *, output_dir: str,
    ) -> ScreenshotReport:
        if shutil.which(self._adb_bin) is None:
            return ScreenshotReport(
                status="mock", detail=f"{self._adb_bin} not on PATH",
            )
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        remote = "/sdcard/omnisight-screenshot.png"
        local = out_dir / f"{config.session_id}.png"
        capture_argv = [
            shutil.which(self._adb_bin) or self._adb_bin,
            *build_android_screenshot_argv(remote)[1:],
        ]
        try:
            capture = self._run(capture_argv, timeout_s=config.screenshot_timeout_s)
        except subprocess.TimeoutExpired:
            return ScreenshotReport(status="fail", detail="screencap timed out")
        except Exception as exc:  # noqa: BLE001
            return ScreenshotReport(status="fail", detail=f"screencap error: {exc}")
        if capture.returncode != 0:
            return ScreenshotReport(
                status="fail",
                detail=f"screencap rc={capture.returncode}",
            )
        pull_argv = [
            shutil.which(self._adb_bin) or self._adb_bin,
            "pull", remote, str(local),
        ]
        try:
            pull = self._run(pull_argv, timeout_s=config.screenshot_timeout_s)
        except subprocess.TimeoutExpired:
            return ScreenshotReport(status="fail", detail="adb pull timed out")
        except Exception as exc:  # noqa: BLE001
            return ScreenshotReport(status="fail", detail=f"adb pull error: {exc}")
        if pull.returncode != 0 or not local.is_file():
            return ScreenshotReport(
                status="fail", detail=f"adb pull rc={pull.returncode}",
            )
        return ScreenshotReport(
            status="pass", path=str(local), format="png",
            detail=f"bytes={local.stat().st_size}",
        )

    def stop(self, sandbox_name: str, *, timeout_s: float) -> None:
        if shutil.which(self._docker_bin) is None:
            return
        argv = [
            shutil.which(self._docker_bin) or self._docker_bin,
            "stop", "-t", str(int(max(0, timeout_s))), sandbox_name,
        ]
        try:
            self._run(argv, timeout_s=max(timeout_s + 5, 15))
        except Exception as exc:  # noqa: BLE001
            logger.warning("docker stop %s failed: %s", sandbox_name, exc)


class SshMacOsIosExecutor:
    """iOS build via ssh → macOS runner.

    On Linux / CI without an ``ios_remote_host`` configured, every
    operation degrades to a ``mock`` report. On a real mac runner
    the argv is shipped via ``ssh <host> -- <argv…>`` after proper
    shell quoting.

    The remote side is expected to expose ``xcodebuild``,
    ``xcrun simctl``, and a writable ``ios_workdir``. No other
    assumptions: no fastlane, no mas, no unusual PATH — just a stock
    Xcode install.
    """

    def __init__(
        self,
        *,
        ssh_bin: str = "ssh",
        scp_bin: str = "scp",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._ssh_bin = ssh_bin
        self._scp_bin = scp_bin
        self._runner = runner or subprocess.run

    def _can_dispatch(self, config: MobileSandboxConfig) -> str | None:
        if not config.ios_remote_host.strip():
            return "no ios_remote_host configured"
        if shutil.which(self._ssh_bin) is None:
            return f"{self._ssh_bin} not on PATH"
        return None

    def _ssh_argv(self, host: str, remote_argv: Sequence[str]) -> list[str]:
        ssh = shutil.which(self._ssh_bin) or self._ssh_bin
        # shell-quote the remote argv so spaces / quotes survive
        # traversal through the outer ssh shell.
        remote_cmd = " ".join(shlex.quote(str(a)) for a in remote_argv)
        return [ssh, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                host, "--", remote_cmd]

    def _run(
        self, argv: list[str], *, timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            argv, capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )

    def build(self, config: MobileSandboxConfig) -> BuildReport:
        mock = self._can_dispatch(config)
        if mock is not None:
            return BuildReport(status="mock", tool="xcodebuild", detail=mock)
        remote_argv = build_ios_build_argv(config)
        argv = self._ssh_argv(config.ios_remote_host, remote_argv)
        start = time.monotonic()
        try:
            proc = self._run(argv, timeout_s=config.build_timeout_s)
        except subprocess.TimeoutExpired:
            return BuildReport(
                status="fail", tool="xcodebuild",
                detail=f"xcodebuild timed out after {config.build_timeout_s}s",
            )
        except Exception as exc:  # noqa: BLE001
            return BuildReport(
                status="fail", tool="xcodebuild",
                detail=f"ssh xcodebuild error: {exc}",
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = _tail(proc.stdout)
        stderr = _tail(proc.stderr)
        errors = parse_build_error(stderr or stdout, tool="xcodebuild")
        if proc.returncode != 0:
            return BuildReport(
                status="fail", tool="xcodebuild",
                duration_ms=duration_ms, exit_code=proc.returncode,
                stdout_tail=stdout, stderr_tail=stderr, errors=errors,
                detail=f"xcodebuild exited {proc.returncode}",
            )
        derived = str(Path(config.ios_workdir) / "DerivedData")
        app = locate_ios_app_bundle(
            derived, configuration=config.xcode_configuration,
        )
        if app is None:
            # On the local host we may not see the remote FS; record
            # the expected path so the agent can inspect it via ssh.
            return BuildReport(
                status="pass", tool="xcodebuild",
                artifact_path=f"{config.ios_remote_host}:{derived}",
                duration_ms=duration_ms, exit_code=0,
                stdout_tail=stdout, stderr_tail=stderr, errors=errors,
                detail="build ok; artifact lives on remote host",
            )
        return BuildReport(
            status="pass", tool="xcodebuild", artifact_path=app,
            duration_ms=duration_ms, exit_code=0,
            stdout_tail=stdout, stderr_tail=stderr, errors=errors,
        )

    def install(
        self, config: MobileSandboxConfig, artifact_path: str,
    ) -> InstallReport:
        mock = self._can_dispatch(config)
        if mock is not None:
            return InstallReport(status="mock", detail=mock)
        udid = config.ios_simulator_udid or "booted"
        remote_argv = ["xcrun", "simctl", "install", udid, artifact_path]
        argv = self._ssh_argv(config.ios_remote_host, remote_argv)
        try:
            proc = self._run(argv, timeout_s=config.install_timeout_s)
        except Exception as exc:  # noqa: BLE001
            return InstallReport(status="fail", detail=f"simctl install: {exc}")
        if proc.returncode != 0:
            return InstallReport(
                status="fail",
                detail=f"simctl install rc={proc.returncode}",
            )
        if not config.ios_bundle_id:
            return InstallReport(
                status="pass", launched=False,
                detail="installed; no bundle_id — skipping launch",
            )
        launch_argv = [
            "xcrun", "simctl", "launch", udid, config.ios_bundle_id,
        ]
        launch = self._run(
            self._ssh_argv(config.ios_remote_host, launch_argv),
            timeout_s=config.install_timeout_s,
        )
        return InstallReport(
            status="pass" if launch.returncode == 0 else "fail",
            launched=launch.returncode == 0,
            detail=_tail(launch.stderr or launch.stdout, chars=400),
        )

    def screenshot(
        self, config: MobileSandboxConfig, *, output_dir: str,
    ) -> ScreenshotReport:
        mock = self._can_dispatch(config)
        if mock is not None:
            return ScreenshotReport(status="mock", detail=mock)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        remote_path = f"{config.ios_workdir}/{config.session_id}.png"
        capture = self._ssh_argv(
            config.ios_remote_host,
            build_ios_screenshot_argv(
                remote_path, udid=config.ios_simulator_udid or "booted",
            ),
        )
        try:
            proc = self._run(capture, timeout_s=config.screenshot_timeout_s)
        except Exception as exc:  # noqa: BLE001
            return ScreenshotReport(status="fail", detail=f"simctl io: {exc}")
        if proc.returncode != 0:
            return ScreenshotReport(
                status="fail",
                detail=f"simctl io rc={proc.returncode}",
            )
        # scp the screenshot back to local
        scp = shutil.which(self._scp_bin) or self._scp_bin
        local = out_dir / f"{config.session_id}.png"
        pull_argv = [scp, f"{config.ios_remote_host}:{remote_path}", str(local)]
        try:
            pull = self._run(pull_argv, timeout_s=config.screenshot_timeout_s)
        except Exception as exc:  # noqa: BLE001
            return ScreenshotReport(status="fail", detail=f"scp pull: {exc}")
        if pull.returncode != 0 or not local.is_file():
            return ScreenshotReport(
                status="fail", detail=f"scp rc={pull.returncode}",
            )
        return ScreenshotReport(
            status="pass", path=str(local), format="png",
            detail=f"bytes={local.stat().st_size}",
        )

    def stop(self, delegate_handle: str, *, timeout_s: float) -> None:
        # xcodebuild is a foreground process; ``stop`` is only
        # meaningful if the caller stashed a session marker. We leave
        # this a no-op by default — real mac runners ship a
        # ``omnisight-ios-runner stop <handle>`` wrapper.
        logger.debug(
            "ios stop requested for handle=%s timeout=%.1f (no-op)",
            delegate_handle, timeout_s,
        )
        return


# ───────────────────────────────────────────────────────────────────
#  Manager
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class MobileSandboxManager:
    """Thread-safe registry of live mobile sandboxes, keyed on
    ``session_id``.

    One sandbox per session. Callers: create → build → install →
    screenshot → stop. Errors from executors are captured onto the
    instance rather than raised, matching the V2 ``ui_sandbox``
    semantics so the agent's ReAct loop keeps spinning.
    """

    def __init__(
        self,
        *,
        android_executor: AndroidExecutor | None = None,
        ios_executor: IosExecutor | None = None,
        screenshot_dir: str = "/tmp/omnisight-mobile-screenshots",
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        port_range: tuple[int, int] = DEFAULT_SCREENSHOT_PORT_RANGE,
    ) -> None:
        if android_executor is None and ios_executor is None:
            raise MobileSandboxConfigError(
                "at least one of android_executor / ios_executor required"
            )
        self._android = android_executor
        self._ios = ios_executor
        self._screenshot_dir = screenshot_dir
        self._clock = clock
        self._event_cb = event_cb
        self._port_range = port_range
        self._lock = threading.RLock()
        self._instances: dict[str, MobileSandboxInstance] = {}

    # ────────────── Public API ──────────────

    def create(self, config: MobileSandboxConfig) -> MobileSandboxInstance:
        """Register a new sandbox. Does **not** build — that's
        :meth:`build`. Raises :class:`MobileSandboxAlreadyExists` if
        the session already has an entry (even terminal ones)."""
        if not isinstance(config, MobileSandboxConfig):
            raise TypeError("config must be MobileSandboxConfig")
        validate_workspace(config.workspace_path)
        executor = self._executor_for(config.platform)
        if executor is None:
            raise MobileSandboxConfigError(
                f"no executor configured for platform={config.platform!r}"
            )
        with self._lock:
            if config.session_id in self._instances:
                raise MobileSandboxAlreadyExists(
                    f"session_id {config.session_id!r} already has a sandbox"
                )
            name = format_sandbox_name(config.session_id, config.platform)
            port = allocate_screenshot_port(
                config.session_id,
                in_use={
                    i.screenshot_port
                    for i in self._instances.values()
                    if i.screenshot_port is not None
                },
                port_range=self._port_range,
            )
            instance = MobileSandboxInstance(
                session_id=config.session_id,
                sandbox_name=name,
                config=config,
                status=MobileSandboxStatus.pending,
                screenshot_port=port,
                created_at=self._clock(),
                last_active_at=self._clock(),
            )
            self._instances[config.session_id] = instance
        self._emit("mobile_sandbox.created", instance)
        return instance

    def build(self, session_id: str) -> MobileSandboxInstance:
        """Run the build phase. Transitions ``pending → building →
        built`` (or ``failed``)."""
        with self._lock:
            instance = self._require(session_id)
            if instance.status is MobileSandboxStatus.built:
                return instance
            if instance.status not in {
                MobileSandboxStatus.pending,
                MobileSandboxStatus.stopped,
            }:
                raise MobileSandboxError(
                    f"cannot build from status {instance.status.value!r}"
                )
            building = replace(
                instance,
                status=MobileSandboxStatus.building,
                started_at=self._clock(),
                last_active_at=self._clock(),
            )
            self._instances[session_id] = building
            executor = self._executor_for(instance.config.platform)
        self._emit("mobile_sandbox.building", building)
        assert executor is not None  # create() already checked
        report = executor.build(instance.config)
        with self._lock:
            current = self._instances.get(session_id, building)
            new_status = (
                MobileSandboxStatus.built
                if report.status in ("pass", "mock")
                else MobileSandboxStatus.failed
            )
            updated = replace(
                current,
                status=new_status,
                build=report,
                built_at=self._clock() if new_status is MobileSandboxStatus.built else None,
                last_active_at=self._clock(),
                error=None if new_status is MobileSandboxStatus.built else report.detail,
            )
            self._instances[session_id] = updated
        self._emit(
            f"mobile_sandbox.{'built' if new_status is MobileSandboxStatus.built else 'failed'}",
            updated,
        )
        return updated

    def install(self, session_id: str) -> MobileSandboxInstance:
        """Run the install phase on the built artifact. Transitions
        ``built → installing → running`` (or ``failed``)."""
        with self._lock:
            instance = self._require(session_id)
            if instance.status is not MobileSandboxStatus.built:
                raise MobileSandboxError(
                    f"cannot install from status {instance.status.value!r}"
                )
            installing = replace(
                instance,
                status=MobileSandboxStatus.installing,
                last_active_at=self._clock(),
            )
            self._instances[session_id] = installing
            executor = self._executor_for(instance.config.platform)
        self._emit("mobile_sandbox.installing", installing)
        assert executor is not None
        report = executor.install(instance.config, instance.build.artifact_path)
        with self._lock:
            current = self._instances.get(session_id, installing)
            new_status = (
                MobileSandboxStatus.running
                if report.status in ("pass", "mock")
                else MobileSandboxStatus.failed
            )
            updated = replace(
                current,
                status=new_status,
                install=report,
                ready_at=self._clock() if new_status is MobileSandboxStatus.running else None,
                last_active_at=self._clock(),
                error=None if new_status is MobileSandboxStatus.running else report.detail,
            )
            self._instances[session_id] = updated
        self._emit("mobile_sandbox.ready", updated)
        return updated

    def screenshot(self, session_id: str) -> MobileSandboxInstance:
        """Capture a screenshot from the running emulator / simulator.
        Requires ``status == running`` — raises otherwise."""
        with self._lock:
            instance = self._require(session_id)
            if instance.status is not MobileSandboxStatus.running:
                raise MobileSandboxError(
                    f"cannot screenshot from status {instance.status.value!r}"
                )
            executor = self._executor_for(instance.config.platform)
            out_dir = str(Path(self._screenshot_dir) / instance.sandbox_name)
        assert executor is not None
        report = executor.screenshot(instance.config, output_dir=out_dir)
        with self._lock:
            current = self._instances.get(session_id, instance)
            updated = replace(
                current,
                screenshot=report,
                last_active_at=self._clock(),
            )
            self._instances[session_id] = updated
        self._emit("mobile_sandbox.screenshot", updated)
        return updated

    def touch(self, session_id: str) -> MobileSandboxInstance:
        """Update ``last_active_at`` — keeps the idle reaper away."""
        with self._lock:
            instance = self._require(session_id)
            if instance.is_terminal:
                return instance
            touched = replace(instance, last_active_at=self._clock())
            self._instances[session_id] = touched
            return touched

    def stop(self, session_id: str) -> MobileSandboxInstance:
        """Stop the build container (Android) or terminate the remote
        delegate (iOS). Errors are captured as warnings — do not
        raise."""
        with self._lock:
            instance = self._require(session_id)
            if instance.is_terminal:
                return instance
            stopping = replace(
                instance,
                status=MobileSandboxStatus.stopping,
                last_active_at=self._clock(),
            )
            self._instances[session_id] = stopping
            executor = self._executor_for(instance.config.platform)
            warnings: list[str] = list(instance.warnings)
        try:
            if executor is not None:
                if instance.config.platform == "android":
                    executor.stop(  # type: ignore[attr-defined]
                        instance.sandbox_name,
                        timeout_s=instance.config.stop_timeout_s,
                    )
                else:
                    executor.stop(  # type: ignore[attr-defined]
                        instance.delegate_handle or "",
                        timeout_s=instance.config.stop_timeout_s,
                    )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"stop_failed: {exc}")
        with self._lock:
            current = self._instances.get(session_id, stopping)
            stopped = replace(
                current,
                status=MobileSandboxStatus.stopped,
                stopped_at=self._clock(),
                last_active_at=self._clock(),
                warnings=tuple(warnings),
            )
            self._instances[session_id] = stopped
        self._emit("mobile_sandbox.stopped", stopped)
        return stopped

    def remove(self, session_id: str) -> MobileSandboxInstance:
        """Forget a session. Must be terminal first."""
        with self._lock:
            instance = self._require(session_id)
            if not instance.is_terminal:
                raise MobileSandboxError(
                    f"cannot remove sandbox in status {instance.status.value!r} "
                    "— call stop() first"
                )
            del self._instances[session_id]
        return instance

    def get(self, session_id: str) -> MobileSandboxInstance | None:
        with self._lock:
            return self._instances.get(session_id)

    def list(self) -> tuple[MobileSandboxInstance, ...]:
        with self._lock:
            return tuple(self._instances.values())

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe summary of every live sandbox."""
        with self._lock:
            return {
                "schema_version": MOBILE_SANDBOX_SCHEMA_VERSION,
                "sandboxes": [i.to_dict() for i in self._instances.values()],
                "count": len(self._instances),
            }

    # ────────────── Internal ──────────────

    def _require(self, session_id: str) -> MobileSandboxInstance:
        inst = self._instances.get(session_id)
        if inst is None:
            raise MobileSandboxNotFound(f"no sandbox for session_id={session_id!r}")
        return inst

    def _executor_for(
        self, platform: str,
    ) -> AndroidExecutor | IosExecutor | None:
        if platform == "android":
            return self._android
        if platform == "ios":
            return self._ios
        return None

    def _emit(
        self, event_type: str, instance: MobileSandboxInstance,
    ) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, instance.to_dict())
        except Exception as exc:  # pragma: no cover
            logger.warning("mobile_sandbox event callback raised: %s", exc)
