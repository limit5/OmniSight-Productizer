"""P1 #286 — Mobile toolchain integration.

Library-layer wrapper that turns a P0 (#285) mobile platform profile
into an executable build plan. Three responsibilities:

1. **Local Linux builds (Android / Flutter-Android / RN-Android)** —
   resolve the gradle-wrapper / fastlane invocation, shell out either
   directly (when the caller already runs inside the
   ``ghcr.io/omnisight/mobile-build`` image) or via ``docker run``
   against the image.

2. **Remote macOS delegation (iOS)** — Linux cannot build iOS. The
   module reads ``OMNISIGHT_MACOS_BUILDER`` and returns a delegator
   (self-hosted SSH runner / MacStadium / Cirrus CI / GitHub Actions
   macos-N runner). The delegator object is an opaque handle the
   caller invokes; concrete build execution on the remote host is out
   of scope for P1 — P1 proves the delegation contract, P2 (#287)
   ``scripts/simulate.sh`` wires it into the dev loop, and
   downstream P5 (#290) App Store upload consumes the artifact
   returned.

3. **Fastlane / gym / gradle wrapper helpers** — command-builder
   utilities that emit the correct CLI invocation for both iOS
   (fastlane gym on macOS) and Android (fastlane supply / ./gradlew).
   Kept as pure functions so they can be unit-tested without any
   subprocess invocation.

Design constraints
------------------
* **No credential logging** — signing material (keystore passwords,
  Apple ID, App Store Connect API key) travels through the env via
  P3 (#288) secret_store; this module never echoes any env var whose
  name matches ``*PASSWORD``/``*TOKEN``/``*KEY``. Helpers scrub
  command strings before logging.

* **Delegator objects, not driver scripts** — we return a dataclass
  describing WHERE the macOS build should run, WHAT command to run,
  and WHICH env vars to forward. The concrete dispatch (SSH / API
  POST / push to git remote) is the caller's responsibility. This
  keeps P1 testable offline and lets P2 / P5 pick their own
  transport.

* **Graceful without Docker** — ``resolve_android_runner`` succeeds
  even when the docker CLI isn't present; the resulting
  ``AndroidBuilder`` flags ``local_docker_available=False`` so the
  caller (P2 simulate track) can decide between ``run-in-host`` and
  ``error out``.

Public API
----------
``resolve_mobile_toolchain(profile_id)``
    Single entry point. Returns a ``MobileToolchain`` pointing at
    the correct builder (Android local / iOS remote).
``resolve_macos_builder()``
    Inspect env and return a ``MacOSBuilder`` delegator.
``AndroidBuilder``
    Dataclass describing the Linux-local (Docker or host) gradle
    invocation.
``fastlane_gym_command()`` / ``gradle_wrapper_command()``
    Pure command-builder helpers.
``MOBILE_BUILD_IMAGE``
    Canonical name of the P1 Docker image
    (``ghcr.io/omnisight/mobile-build``).
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from backend import platform_profile as _platform

logger = logging.getLogger(__name__)


# ── Canonical image / env var names ────────────────────────────────
MOBILE_BUILD_IMAGE: str = "ghcr.io/omnisight/mobile-build"
"""P1 Docker image name. Consumers should reference this constant
rather than hard-coding the string — when the image moves to a new
registry or tag, one-line change here propagates."""

MOBILE_BUILD_IMAGE_TAG: str = "latest"
"""Default tag; overridable per project via ``OMNISIGHT_MOBILE_IMAGE_TAG``."""

ENV_MACOS_BUILDER: str = "OMNISIGHT_MACOS_BUILDER"
"""Env var selecting the remote macOS build provider."""

ENV_MOBILE_IMAGE_TAG: str = "OMNISIGHT_MOBILE_IMAGE_TAG"
"""Env var overriding ``MOBILE_BUILD_IMAGE_TAG`` (CI pipelines pin
to a sha-digested tag for reproducibility)."""

SUPPORTED_MACOS_BUILDERS: frozenset[str] = frozenset({
    "self-hosted",
    "macstadium",
    "cirrus-ci",
    "github-macos-runner",
})
"""Recognized values for ``OMNISIGHT_MACOS_BUILDER``. Anything else
raises ``UnknownMacOSBuilderError``."""


SUPPORTED_ANDROID_CLI_ACTIONS: frozenset[str] = frozenset({
    "create",
    "run",
    "sdk-install",
})
"""P11 #351 — recognized ``action`` values for
``android_cli_command``. Maps 1:1 to Google's Android CLI
sub-commands shipped 2026-04-18:

* ``create``      → ``android create <project_path>`` (replaces hand-rolled
  template scaffolding from P1).
* ``run``         → ``android run <project_path>`` (replaces
  ``gradle_wrapper_command("installDebug")`` + follow-up
  ``adb shell am start``).
* ``sdk-install`` → ``android sdk install <package>`` (replaces the
  manual ``sdkmanager`` invocations baked into
  ``backend/docker/Dockerfile.mobile-build``).

``sdk-install`` uses a hyphen so the action token is a single,
unambiguous identifier — the emitted argv still splits it into
``android sdk install`` at the CLI boundary."""


# ── Error hierarchy ────────────────────────────────────────────────

class MobileToolchainError(Exception):
    """Base class for P1 toolchain resolution / invocation failures."""


class MacOSBuilderRequiredError(MobileToolchainError):
    """iOS build requested but ``OMNISIGHT_MACOS_BUILDER`` is unset.

    iOS binaries can only be produced on a macOS host. Operators must
    wire one of the supported remote builders (self-hosted runner,
    MacStadium dedicated host, Cirrus CI, or GitHub-hosted
    ``macos-14`` / ``macos-15`` runner) before any iOS job can run.
    """


class UnknownMacOSBuilderError(MobileToolchainError):
    """``OMNISIGHT_MACOS_BUILDER`` set to an unrecognized value."""


class MissingDockerImageError(MobileToolchainError):
    """Docker CLI or the mobile-build image is unavailable locally."""


class UnsupportedPlatformError(MobileToolchainError):
    """Profile's ``mobile_platform`` is neither ``ios`` nor ``android``."""


class UnknownAndroidCliActionError(MobileToolchainError):
    """``android_cli_command`` called with an action outside
    ``SUPPORTED_ANDROID_CLI_ACTIONS`` (P11 #351)."""


class NoGradleFallbackError(MobileToolchainError):
    """``resolve_android_invocation`` was asked for an action that has
    no Gradle wrapper equivalent (``create`` / ``sdk-install``) on a
    host where Google's Android CLI is not on PATH (P11 #351 checkbox 4).

    The two actions that surface this error don't have a Gradle helper
    by design — template scaffolding and SDK manager invocations are
    baked into ``ghcr.io/omnisight/mobile-build`` and only become
    callable from the host once an operator runs
    ``scripts/install_android_cli.sh`` (or the build moves into the
    Docker image, where the CLI ships pre-installed). Surfacing this
    as a typed error rather than silently emitting ``./gradlew create``
    keeps the failure mode honest for the caller."""


# ── Delegator dataclasses ──────────────────────────────────────────

@dataclass(frozen=True)
class MacOSBuilder:
    """Handle describing where + how to run an iOS build.

    Attributes
    ----------
    kind
        One of ``SUPPORTED_MACOS_BUILDERS``.
    display_name
        Human-readable label used in logs / HMI.
    host_hint
        Transport-specific identifier — for ``self-hosted`` an SSH
        ``user@host`` or a hostname; for ``macstadium`` the ORG
        dedicated-host id; for ``cirrus-ci`` / ``github-macos-runner``
        the macOS image label (``macos-14`` / ``macos-15``). None
        implies the caller must supply it.
    env_forward
        Env var names the caller MUST forward to the macOS job (e.g.
        Apple ID, App Store Connect API key). Kept as a tuple — not
        values — so this object is safe to log.
    """

    kind: str
    display_name: str
    host_hint: Optional[str] = None
    env_forward: tuple[str, ...] = ()

    def describe(self) -> str:
        """One-line human summary. Never echoes env-var values."""
        host = f" @ {self.host_hint}" if self.host_hint else ""
        fwd = (
            f" (forwards env: {', '.join(self.env_forward)})"
            if self.env_forward
            else ""
        )
        return f"macOS builder: {self.display_name}{host}{fwd}"


@dataclass(frozen=True)
class AndroidBuilder:
    """Handle describing the Linux-local Android build environment.

    ``local_docker_available`` reflects whether ``docker`` is on PATH
    at resolve time. When False the caller either runs gradle on the
    host (if the host has the SDK + NDK installed) or refuses to
    proceed. We don't force-require Docker because a lean CI runner
    may have the SDK baked in directly.
    """

    image: str
    image_tag: str
    sdk_root: str
    ndk_root: str
    toolchain_path: str
    build_cmd: str
    local_docker_available: bool

    @property
    def qualified_image(self) -> str:
        return f"{self.image}:{self.image_tag}"


@dataclass(frozen=True)
class MobileToolchain:
    """Top-level result of ``resolve_mobile_toolchain``.

    Exactly one of ``android`` / ``macos`` is set, never both,
    corresponding to the profile's ``mobile_platform``.
    """

    profile_id: str
    mobile_platform: str
    mobile_abi: str
    min_os_version: str
    sdk_version: str
    build_cmd: str
    android: Optional[AndroidBuilder] = None
    macos: Optional[MacOSBuilder] = None

    @property
    def needs_macos_host(self) -> bool:
        return self.mobile_platform == "ios"


# ── macOS builder resolution ───────────────────────────────────────

_MACOS_BUILDER_METADATA: dict[str, dict[str, Any]] = {
    "self-hosted": {
        "display_name": "Self-hosted macOS runner",
        "env_forward": (
            "OMNISIGHT_MACOS_HOST",
            "OMNISIGHT_MACOS_SSH_USER",
            "FASTLANE_APPLE_ID",
            "FASTLANE_APP_SPECIFIC_PASSWORD",
            "APP_STORE_CONNECT_API_KEY_PATH",
        ),
        "host_env": "OMNISIGHT_MACOS_HOST",
    },
    "macstadium": {
        "display_name": "MacStadium dedicated host",
        "env_forward": (
            "MACSTADIUM_API_KEY",
            "MACSTADIUM_HOST_ID",
            "FASTLANE_APPLE_ID",
            "APP_STORE_CONNECT_API_KEY_PATH",
        ),
        "host_env": "MACSTADIUM_HOST_ID",
    },
    "cirrus-ci": {
        "display_name": "Cirrus CI macOS task",
        "env_forward": (
            "CIRRUS_API_TOKEN",
            "FASTLANE_APPLE_ID",
            "APP_STORE_CONNECT_API_KEY_PATH",
        ),
        # Cirrus macOS image name is picked in .cirrus.yml, not env.
        "host_env": None,
    },
    "github-macos-runner": {
        "display_name": "GitHub Actions macOS runner",
        "env_forward": (
            "GITHUB_TOKEN",
            "FASTLANE_APPLE_ID",
            "APP_STORE_CONNECT_API_KEY_PATH",
        ),
        "host_env": "OMNISIGHT_GITHUB_MACOS_LABEL",
    },
}


def resolve_macos_builder(
    env: Optional[Mapping[str, str]] = None,
) -> MacOSBuilder:
    """Inspect ``OMNISIGHT_MACOS_BUILDER`` and return the matching
    delegator handle.

    Parameters
    ----------
    env
        Mapping used in place of ``os.environ``. Tests pass a dict so
        resolution is deterministic without mutating process env.

    Raises
    ------
    MacOSBuilderRequiredError
        ``OMNISIGHT_MACOS_BUILDER`` unset or empty.
    UnknownMacOSBuilderError
        Value is not in ``SUPPORTED_MACOS_BUILDERS``.
    """
    source = env if env is not None else os.environ
    raw = (source.get(ENV_MACOS_BUILDER) or "").strip().lower()
    if not raw:
        raise MacOSBuilderRequiredError(
            f"iOS build requires {ENV_MACOS_BUILDER} to be set; "
            f"valid values: {sorted(SUPPORTED_MACOS_BUILDERS)}"
        )
    if raw not in SUPPORTED_MACOS_BUILDERS:
        raise UnknownMacOSBuilderError(
            f"{ENV_MACOS_BUILDER}={raw!r} is not supported; "
            f"valid values: {sorted(SUPPORTED_MACOS_BUILDERS)}"
        )
    meta = _MACOS_BUILDER_METADATA[raw]
    host_env = meta["host_env"]
    host_hint = source.get(host_env) if host_env else None
    return MacOSBuilder(
        kind=raw,
        display_name=meta["display_name"],
        host_hint=host_hint or None,
        env_forward=tuple(meta["env_forward"]),
    )


# ── Android builder resolution ─────────────────────────────────────

def _docker_available() -> bool:
    """``True`` if the docker CLI is on PATH. Doesn't pull images."""
    return shutil.which("docker") is not None


def _resolve_android_builder(
    profile_data: Mapping[str, Any],
    env: Optional[Mapping[str, str]] = None,
) -> AndroidBuilder:
    """Produce an ``AndroidBuilder`` from the P0 mobile profile's
    `build_toolchain` block."""
    source = env if env is not None else os.environ
    tag = source.get(ENV_MOBILE_IMAGE_TAG, "").strip() or MOBILE_BUILD_IMAGE_TAG
    toolchain = profile_data.get("build_toolchain", {})
    return AndroidBuilder(
        image=MOBILE_BUILD_IMAGE,
        image_tag=tag,
        sdk_root=toolchain.get("sdk_root", ""),
        ndk_root=toolchain.get("ndk_root", ""),
        toolchain_path=toolchain.get("toolchain_path", ""),
        build_cmd=toolchain.get("build_cmd", "./gradlew bundleRelease"),
        local_docker_available=_docker_available(),
    )


# ── Public entry point ─────────────────────────────────────────────

def resolve_mobile_toolchain(
    profile_id: str,
    env: Optional[Mapping[str, str]] = None,
) -> MobileToolchain:
    """Resolve a P0 mobile profile to an executable P1 toolchain.

    * iOS profiles ⇒ always require a macOS delegator; the delegator
      is resolved eagerly here so misconfiguration fails fast at
      profile-load time rather than mid-build.
    * Android profiles ⇒ locally resolvable; ``local_docker_available``
      reflects whether the host has docker.

    Raises
    ------
    UnsupportedPlatformError
        Profile's ``mobile_platform`` is neither ``ios`` nor ``android``
        (e.g. a react-native meta-profile that P4 hasn't defined yet).
    MacOSBuilderRequiredError / UnknownMacOSBuilderError
        iOS profile with missing / invalid ``OMNISIGHT_MACOS_BUILDER``.
    """
    cfg = _platform.get_platform_config(profile_id)
    if cfg.get("target_kind") != "mobile":
        raise UnsupportedPlatformError(
            f"profile {profile_id!r} target_kind is "
            f"{cfg.get('target_kind')!r}, not 'mobile'"
        )
    mobile_platform = str(cfg.get("mobile_platform") or "").strip().lower()
    mobile_abi = str(cfg.get("mobile_abi") or "").strip()
    min_os = str(cfg.get("min_os_version") or "").strip()
    sdk_ver = str(cfg.get("sdk_version") or "").strip()
    build_cmd = str(cfg.get("build_toolchain", {}).get("build_cmd") or "").strip()

    if mobile_platform == "ios":
        mac = resolve_macos_builder(env=env)
        return MobileToolchain(
            profile_id=profile_id,
            mobile_platform=mobile_platform,
            mobile_abi=mobile_abi,
            min_os_version=min_os,
            sdk_version=sdk_ver,
            build_cmd=build_cmd,
            macos=mac,
        )
    if mobile_platform == "android":
        android = _resolve_android_builder(cfg, env=env)
        return MobileToolchain(
            profile_id=profile_id,
            mobile_platform=mobile_platform,
            mobile_abi=mobile_abi,
            min_os_version=min_os,
            sdk_version=sdk_ver,
            build_cmd=build_cmd,
            android=android,
        )
    raise UnsupportedPlatformError(
        f"profile {profile_id!r} has mobile_platform={mobile_platform!r}; "
        "only 'ios' and 'android' are supported in P1"
    )


# ── Command-builder helpers (pure functions) ──────────────────────

def gradle_wrapper_command(
    project_root: Path,
    task: str,
    *,
    extra_args: Sequence[str] = (),
    abi: Optional[str] = None,
) -> list[str]:
    """Emit the ``./gradlew <task>`` argv for a project.

    Parameters
    ----------
    project_root
        Android project root (must contain ``gradlew``). Callers are
        expected to have run ``gradle wrapper --gradle-version X``
        once during scaffold — we do not auto-bootstrap here.
    task
        Gradle task — ``bundleRelease`` / ``assembleDebug`` /
        ``test`` / ``lint`` / ``connectedAndroidTest``.
    extra_args
        Extra CLI args appended verbatim after the task.
    abi
        Optional ABI filter (e.g. ``armeabi-v7a``) — translated to
        ``-PtargetAbi=<abi>`` per our P0 convention.

    Returns
    -------
    list[str]
        argv suitable for ``subprocess.run``. Does NOT invoke.
    """
    wrapper = project_root / "gradlew"
    argv: list[str] = [str(wrapper), task]
    if abi:
        argv.append(f"-PtargetAbi={abi}")
    argv.extend(extra_args)
    return argv


def android_cli_available() -> bool:
    """True when Google's Android CLI (P11 #351, ``android`` binary
    distributed from ``d.android.com/tools/agents``) is on PATH.

    Callers use this to choose between the fast path
    (``android_cli_command(...)``) and the legacy Gradle-wrapper
    fallback (``gradle_wrapper_command(...)``). The detection is the
    P11 fallback contract documented in
    ``backend/docker/Dockerfile.mobile-build`` and
    ``scripts/install_android_cli.sh`` — if either install step was
    skipped, ``False`` here keeps P1/P2 running on the existing
    ``./gradlew`` path without further code changes."""
    return shutil.which("android") is not None


def android_cli_command(
    action: str,
    project_path: Path,
    *,
    sdk_package: Optional[str] = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Emit the ``android <action> …`` argv for P11 #351 replacement
    of the hand-rolled Gradle-wrapper + sdkmanager invocations.

    Google's Android CLI (released 2026-04-18, see
    ``d.android.com/tools/agents``) collapses the three P1/P2 code
    paths below into one binary:

    =====================  ==============================================
    action                 emitted argv
    =====================  ==============================================
    ``"create"``           ``android create <project_path> [extra_args]``
    ``"run"``              ``android run <project_path> [extra_args]``
    ``"sdk-install"``      ``android sdk install <sdk_package> [extra_args]``
    =====================  ==============================================

    Action tokens are case-insensitive; ``"sdk_install"`` and
    ``"SDK-Install"`` are normalised to the canonical ``sdk-install``.

    This is a **pure command builder** — it does not probe the host,
    invoke subprocess, or fall back. Callers are expected to check
    ``android_cli_available()`` first and, when ``False``, route to
    the legacy helper (``gradle_wrapper_command`` for ``run``;
    scaffolding / sdkmanager have no P1 helper because the Docker
    image already bakes them in).

    Parameters
    ----------
    action
        One of ``SUPPORTED_ANDROID_CLI_ACTIONS``. Case-insensitive;
        hyphens and underscores are interchangeable.
    project_path
        Android project root. For ``sdk-install`` this argument is
        accepted for call-site symmetry but not placed in the emitted
        argv — ``android sdk install`` operates on the shared SDK
        root, not a specific project.
    sdk_package
        Required when ``action == "sdk-install"``. Example values:
        ``"platform-tools"``, ``"platforms;android-35"``,
        ``"ndk;27.0.12077973"``. Passed verbatim to the CLI.
    extra_args
        Extra CLI args appended verbatim after the action's required
        positional(s). Useful for ``--template`` (create),
        ``--device <id>`` (run), ``--channel beta`` (sdk install).

    Returns
    -------
    list[str]
        argv suitable for ``subprocess.run``. Does NOT invoke.

    Raises
    ------
    UnknownAndroidCliActionError
        ``action`` is not in ``SUPPORTED_ANDROID_CLI_ACTIONS``.
    ValueError
        ``action == "sdk-install"`` without ``sdk_package``.
    """
    norm = action.strip().lower().replace("_", "-")
    if norm not in SUPPORTED_ANDROID_CLI_ACTIONS:
        raise UnknownAndroidCliActionError(
            f"android_cli_command: action={action!r} is not supported; "
            f"valid values: {sorted(SUPPORTED_ANDROID_CLI_ACTIONS)}"
        )
    if norm == "create":
        argv: list[str] = ["android", "create", str(project_path)]
    elif norm == "run":
        argv = ["android", "run", str(project_path)]
    else:  # "sdk-install"
        if not sdk_package:
            raise ValueError(
                "android_cli_command(action='sdk-install', ...) requires "
                "sdk_package (e.g. 'platform-tools', "
                "'platforms;android-35', 'ndk;27.0.12077973')"
            )
        argv = ["android", "sdk", "install", sdk_package]
    argv.extend(extra_args)
    return argv


@dataclass(frozen=True)
class AndroidInvocation:
    """Result of ``resolve_android_invocation`` (P11 #351 checkbox 4).

    Attributes
    ----------
    argv
        Concrete argv ready for ``subprocess.run``. Whichever path the
        dispatcher picked, the shape is uniform so callers don't need
        a second branch around the result.
    path_kind
        ``"android-cli"`` when Google's Android CLI was on PATH and the
        fast path was chosen; ``"gradle-wrapper"`` when the dispatcher
        fell back to ``./gradlew``. Surface this in logs / debug
        findings so operators can tell from the audit trail which
        toolchain actually ran a build — the two paths produce
        identical artifacts but differ in latency (Google's 2026-04-18
        benchmark: 3× faster, 70% less token through the CLI).
    detail
        Human-readable single line describing the resolution decision —
        "android CLI on PATH" / "android CLI absent → ./gradlew installDebug".
        Safe to log; never echoes secret material.
    """

    argv: list[str]
    path_kind: str
    detail: str = ""


# Action → ``./gradlew`` task fallback mapping. Only ``run`` has a
# meaningful Gradle equivalent at the toolchain layer — ``create``
# (template scaffolding) and ``sdk-install`` (SDK manager) live in the
# Docker image, not in the host helper, so their fallback is "use the
# Docker image" rather than a different argv.
_ANDROID_CLI_GRADLE_FALLBACK: dict[str, Optional[str]] = {
    "create": None,         # No host Gradle equivalent — Docker image only.
    "run": "installDebug",  # ``android run`` ≈ ``./gradlew installDebug`` + adb am start.
    "sdk-install": None,    # No host Gradle equivalent — sdkmanager in Docker.
}


def resolve_android_invocation(
    action: str,
    project_root: Path,
    *,
    sdk_package: Optional[str] = None,
    abi: Optional[str] = None,
    extra_args: Sequence[str] = (),
) -> AndroidInvocation:
    """Pick Android CLI fast path if available, else fall back to ``./gradlew``.

    This is the **fallback dispatcher** P11 #351 checkbox 4 calls for —
    the single helper that ties ``android_cli_available()`` +
    ``android_cli_command()`` + ``gradle_wrapper_command()`` together so
    P1/P2 call sites don't each repeat the if/else themselves. Callers
    pass the abstract action; the dispatcher decides which physical
    toolchain to invoke based on PATH at call time.

    Resolution order:

    1. ``android_cli_available()`` ⇒ emit ``android <action> …`` argv
       (delegates to ``android_cli_command``).
    2. Else, look up ``_ANDROID_CLI_GRADLE_FALLBACK[action]``:

       * Non-``None`` Gradle task ⇒ emit ``./gradlew <task> …`` argv
         (delegates to ``gradle_wrapper_command``).
       * ``None`` (action has no host-side Gradle equivalent) ⇒ raise
         ``NoGradleFallbackError`` so the caller can either gate on the
         Docker image being available or fail fast.

    Parameters
    ----------
    action
        One of ``SUPPORTED_ANDROID_CLI_ACTIONS``. Case- and
        separator-insensitive — the same normalisation rules
        ``android_cli_command`` uses apply here.
    project_root
        Android project root. Forwarded to both branches:
        ``android <action> <project_root>`` for the CLI path,
        ``<project_root>/gradlew`` for the Gradle path.
    sdk_package
        Required when ``action == "sdk-install"`` and the CLI is on
        PATH (forwarded to ``android sdk install <package>``). Ignored
        otherwise; the Gradle fallback for ``sdk-install`` is
        ``NoGradleFallbackError`` regardless.
    abi
        Optional ABI filter (``armeabi-v7a`` / ``arm64-v8a`` etc.).
        Only consumed by the Gradle fallback (it becomes
        ``-PtargetAbi=<abi>``); ``android run`` picks the ABI from the
        target device, so the kwarg is ignored on the CLI path.
    extra_args
        Forwarded verbatim to whichever underlying argv builder runs.

    Raises
    ------
    UnknownAndroidCliActionError
        ``action`` is not in ``SUPPORTED_ANDROID_CLI_ACTIONS``.
    ValueError
        ``action == "sdk-install"`` with the CLI on PATH but no
        ``sdk_package`` (re-raised from ``android_cli_command``).
    NoGradleFallbackError
        CLI absent and the action has no Gradle wrapper equivalent
        (``create`` / ``sdk-install``).
    """
    norm = action.strip().lower().replace("_", "-")
    if norm not in SUPPORTED_ANDROID_CLI_ACTIONS:
        raise UnknownAndroidCliActionError(
            f"resolve_android_invocation: action={action!r} is not supported; "
            f"valid values: {sorted(SUPPORTED_ANDROID_CLI_ACTIONS)}"
        )
    if android_cli_available():
        argv = android_cli_command(
            norm,
            project_root,
            sdk_package=sdk_package,
            extra_args=extra_args,
        )
        return AndroidInvocation(
            argv=argv,
            path_kind="android-cli",
            detail=f"android CLI on PATH → android {norm}",
        )
    gradle_task = _ANDROID_CLI_GRADLE_FALLBACK[norm]
    if gradle_task is None:
        raise NoGradleFallbackError(
            f"action={norm!r} has no ./gradlew equivalent and the "
            "`android` CLI is not on PATH; install Google's Android CLI "
            "(scripts/install_android_cli.sh) or run inside the "
            f"{MOBILE_BUILD_IMAGE} image where the CLI ships baked in"
        )
    argv = gradle_wrapper_command(
        project_root,
        gradle_task,
        extra_args=extra_args,
        abi=abi,
    )
    return AndroidInvocation(
        argv=argv,
        path_kind="gradle-wrapper",
        detail=f"android CLI absent → ./gradlew {gradle_task}",
    )


def fastlane_gym_command(
    *,
    scheme: str,
    configuration: str = "Release",
    output_directory: Optional[Path] = None,
    export_method: str = "app-store",
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Emit the ``fastlane gym`` argv for an iOS project.

    ``gym`` is the fastlane action that wraps ``xcodebuild archive``
    + ``xcodebuild -exportArchive``. Produces a .ipa.

    MUST run on macOS — Linux invocation of this command fails
    because gym dispatches to ``xcodebuild``. We emit the argv here
    so the caller can ship it to the macOS delegator.

    Parameters
    ----------
    scheme
        Xcode scheme (e.g. ``MyApp``).
    configuration
        Build configuration — ``Release`` (App Store) / ``Debug``.
    output_directory
        Where to place the ``.ipa`` + dSYMs. Defaults to
        ``./build/ios``.
    export_method
        App Store Connect export method — one of ``app-store`` /
        ``ad-hoc`` / ``enterprise`` / ``development``.
    extra_flags
        Forwarded verbatim (allows ``--include_bitcode false`` etc.).
    """
    argv: list[str] = [
        "fastlane",
        "gym",
        f"--scheme={scheme}",
        f"--configuration={configuration}",
        f"--export_method={export_method}",
    ]
    if output_directory is not None:
        argv.append(f"--output_directory={output_directory}")
    argv.extend(extra_flags)
    return argv


def fastlane_supply_command(
    *,
    package_name: str,
    track: str = "internal",
    aab_path: Optional[Path] = None,
    apk_path: Optional[Path] = None,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Emit the ``fastlane supply`` argv for uploading to Play.

    ``supply`` runs on Linux (pure HTTPS to Google Play Developer
    API). Used by P5 (#290) Play Store upload adapter.

    Exactly one of ``aab_path`` / ``apk_path`` must be set.
    """
    if not (bool(aab_path) ^ bool(apk_path)):
        raise ValueError(
            "fastlane_supply_command: provide exactly one of "
            "aab_path / apk_path"
        )
    argv: list[str] = [
        "fastlane",
        "supply",
        f"--package_name={package_name}",
        f"--track={track}",
    ]
    if aab_path is not None:
        argv.append(f"--aab={aab_path}")
    if apk_path is not None:
        argv.append(f"--apk={apk_path}")
    argv.extend(extra_flags)
    return argv


def docker_run_android_command(
    *,
    builder: AndroidBuilder,
    project_root: Path,
    inner_argv: Sequence[str],
    extra_env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Wrap an ``inner_argv`` gradle invocation in a ``docker run``
    against ``ghcr.io/omnisight/mobile-build``.

    The generated command mounts ``project_root`` at ``/workspace``
    and forwards ``extra_env`` (names only — values stay in the
    host env and reach the container via ``-e NAME`` passthrough).

    Never includes secret VALUES in the argv — only names. This
    keeps the command safe to log and reproducible across runs that
    use different credentials.
    """
    argv: list[str] = [
        "docker", "run", "--rm",
        "-v", f"{project_root}:/workspace",
        "-w", "/workspace",
    ]
    if extra_env:
        for name in sorted(extra_env):
            argv.extend(["-e", name])
    argv.append(builder.qualified_image)
    argv.extend(inner_argv)
    return argv


# ── Pretty-printers (for HMI / CLI logs) ──────────────────────────

def describe(toolchain: MobileToolchain) -> str:
    """One-liner safe to log. Never includes secret material.

    Example::

        android/arm64-v8a sdk=35 min=24 -> ghcr.io/omnisight/mobile-build:latest (docker=yes)
        ios/arm64 sdk=17.5 min=16.0 -> macOS builder: Self-hosted macOS runner
    """
    head = (
        f"{toolchain.mobile_platform}/{toolchain.mobile_abi} "
        f"sdk={toolchain.sdk_version} min={toolchain.min_os_version}"
    )
    if toolchain.android is not None:
        dock = "yes" if toolchain.android.local_docker_available else "no"
        return f"{head} -> {toolchain.android.qualified_image} (docker={dock})"
    if toolchain.macos is not None:
        return f"{head} -> {toolchain.macos.describe()}"
    return head


def safe_quote(argv: Sequence[str]) -> str:
    """Shell-quote an argv for log display. Does not redact env
    names — callers should never pass secret VALUES in argv."""
    return " ".join(shlex.quote(a) for a in argv)
