"""P2 #287 — Mobile simulate-track driver.

Single entry point for ``scripts/simulate.sh --type=mobile`` covering
the five P2 deliverables:

    1. iOS Simulator + Android Emulator boot (``xcrun simctl`` / ``emulator``)
    2. XCUITest (iOS) + Espresso (Android) UI-test invocation
    3. Flutter / React Native alternative runners (``flutter test
       integration_test`` / ``detox test``)
    4. Cloud device-farm delegation (Firebase Test Lab / AWS Device
       Farm / BrowserStack)
    5. Screenshot matrix (N devices × M locales) via ``fastlane snapshot``
       / ``fastlane screengrab``.

Design
------
All external CLIs (``xcrun``, ``adb``, ``gradle``, ``xcodebuild``,
``flutter``, ``detox``, ``gcloud``, ``aws``, ``browserstack-local``,
``fastlane``) are **optional**. When a binary is not on PATH (sandbox /
CI-first-run / Linux host running the iOS gate), the affected gate
degrades to a ``mock`` result that the caller can distinguish from a
real pass. Nothing here fabricates a real-device result — a mock means
"environment lacks the tooling to run this gate", not "gate passed".

The Python module owns all unit numbers, YAML parsing, and multi-step
JSON aggregation. ``scripts/simulate.sh mobile`` stays a thin shell
dispatcher that invokes this module once via ``python3 -m
backend.mobile_simulator`` and reads a single JSON summary back — the
exact same contract used by W2 ``web_simulator``.

Public API
----------
``simulate_mobile(*, profile: str, app_path: Path, ...) -> MobileSimResult``

Returns a dataclass with the flat-dict shape consumed by
``run_mobile`` in ``simulate.sh``.

UI framework autodetection
--------------------------
``resolve_ui_framework(app_path)`` inspects the app directory for the
first unambiguous marker and returns one of
``xcuitest`` / ``espresso`` / ``flutter`` / ``react-native``. When no
marker is present it falls back to the mobile platform implied by the
profile (``ios`` ⇒ xcuitest, ``android`` ⇒ espresso). The autodetect
order is deliberately specific-before-generic so a Flutter project that
contains an ``android/`` folder (every Flutter project does!) is
classified as Flutter rather than Espresso.

Cloud device farms
------------------
Farm invocation is **delegation**, not execution — the module emits the
CLI invocation (``gcloud firebase test android run …`` / ``aws
devicefarm schedule-run …`` / ``browserstack-local --key …``) and
records the delegation handle in the report. Actual execution on the
farm is out of scope for P2 (just like iOS build dispatch in P1): the
gate's job is to prove the argv is buildable from profile + app state
so downstream O6/O7 workflows can relay it.

Why not shell out everything from bash
--------------------------------------
Same reason HMI / Web tracks moved their logic here: unit parsing, YAML
traversal, framework autodetection and multi-step JSON aggregation are
miserable in bash. The shell layer remains a thin dispatcher that
invokes this module once.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants / supported farms
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SUPPORTED_FARMS: frozenset[str] = frozenset({
    "firebase",       # Firebase Test Lab      (Android + iOS)
    "aws",            # AWS Device Farm        (Android + iOS)
    "browserstack",   # BrowserStack App Live  (Android + iOS)
})
"""Recognized device-farm adapters. Other values raise
``UnknownDeviceFarmError`` at ``simulate_mobile`` time."""

_FARM_CLI: dict[str, str] = {
    "firebase": "gcloud",
    "aws": "aws",
    "browserstack": "browserstack-local",
}
"""CLI binary each farm adapter dispatches through. Used to decide
between a real delegation and a mock report."""

UI_FRAMEWORKS: frozenset[str] = frozenset({
    "xcuitest",
    "espresso",
    "flutter",
    "react-native",
})
"""Autodetected UI-test framework labels. See
``resolve_ui_framework`` for the detection order."""


SUPPORTED_ANDROID_EMULATOR_ACTIONS: frozenset[str] = frozenset({
    "create",
    "start",
})
"""P11 #351 checkbox 3 — recognized ``action`` values for
``android_emulator_command``. Maps 1:1 to Google's Android CLI
emulator sub-commands shipped 2026-04-18:

* ``create`` → ``android emulator create <avd_name>`` (replaces
  operator-side ``avdmanager create avd …`` / manual AVD config).
* ``start``  → ``android emulator start <avd_name>`` (replaces the
  legacy ``$ANDROID_HOME/emulator/emulator -avd <name>`` invocation
  baked into ``boot_android_emulator``).

Screenshots remain on ``adb shell screencap`` (see
``adb_screencap_command``) — Google's Android CLI does not yet
expose a screenshot sub-command."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error hierarchy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MobileSimError(Exception):
    """Base class for mobile-simulator failures the caller should surface."""


class UnknownDeviceFarmError(MobileSimError):
    """``--farm`` value is not in ``SUPPORTED_FARMS``."""


class UnsupportedProfileError(MobileSimError):
    """Profile's ``target_kind`` is not ``mobile`` or platform is neither
    ``ios`` nor ``android``."""


class UnknownAndroidEmulatorActionError(MobileSimError):
    """``android_emulator_command`` called with an action outside
    ``SUPPORTED_ANDROID_EMULATOR_ACTIONS`` (P11 #351 checkbox 3)."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class EmulatorBootReport:
    """Boot attempt result.

    status:
      * ``booted`` — xcrun/emulator reported ready
      * ``mock``   — CLI not on PATH (sandbox / wrong-OS host)
      * ``fail``   — CLI present but exit != 0 or timed out
    """
    status: str = "mock"
    kind: str = ""            # "simulator" | "avd"
    device_model: str = ""
    udid: str = ""
    runtime: str = ""         # iOS runtime or Android API level
    duration_ms: int = 0
    detail: str = ""


@dataclass
class UITestReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    framework: str = ""           # xcuitest / espresso / flutter / react-native
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration_ms: int = 0
    detail: str = ""


@dataclass
class SmokeReport:
    """App-launch smoke (install + launch + idle ping — pre-UI-test)."""
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    launched: bool = False
    detail: str = ""


@dataclass
class DeviceFarmReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock" | "delegated"
    farm: str = ""                # firebase / aws / browserstack
    argv: list[str] = field(default_factory=list)
    env_forward: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class ScreenshotMatrixReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    devices: list[str] = field(default_factory=list)
    locales: list[str] = field(default_factory=list)
    captured: int = 0             # devices * locales on success, 0 otherwise
    output_dir: str = ""
    detail: str = ""


@dataclass
class MobileSimResult:
    profile: str
    app_path: str
    mobile_platform: str = ""         # ios / android
    mobile_abi: str = ""
    ui_framework: str = ""
    emulator: EmulatorBootReport = field(default_factory=EmulatorBootReport)
    smoke: SmokeReport = field(default_factory=SmokeReport)
    ui_test: UITestReport = field(default_factory=UITestReport)
    device_farm: DeviceFarmReport = field(default_factory=DeviceFarmReport)
    screenshot_matrix: ScreenshotMatrixReport = field(default_factory=ScreenshotMatrixReport)
    gates: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def overall_pass(self) -> bool:
        return all(self.gates.values()) and not self.errors


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Android CLI emulator helpers (P11 #351 checkbox 3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def android_cli_available() -> bool:
    """True when Google's Android CLI (``android`` binary distributed
    from ``d.android.com/tools/agents``) is on PATH.

    ``boot_android_emulator`` uses this to pick between the P11 #351
    fast path (``android emulator start``) and the legacy
    ``$ANDROID_HOME/emulator/emulator -avd`` fallback. Deliberately
    **not cached** — an operator may run
    ``scripts/install_android_cli.sh`` mid-session and the boot path
    must see it on the next call.
    """
    return shutil.which("android") is not None


def android_emulator_command(
    action: str,
    avd_name: str,
    *,
    api_level: str = "",
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Emit the ``android emulator <action> …`` argv (P11 #351 checkbox 3).

    Pure command builder — does not probe the host, invoke subprocess,
    or fall back. Callers gate on ``android_cli_available()`` and route
    to the legacy ``emulator`` binary branch when ``False``.

    =====================  ===============================================
    action                 emitted argv
    =====================  ===============================================
    ``"create"``           ``android emulator create <avd_name>
                           [--api-level <N>] [extra_args]``
    ``"start"``            ``android emulator start <avd_name> [extra_args]``
    =====================  ===============================================

    Action tokens are case-insensitive and hyphen/underscore interchangeable.

    Parameters
    ----------
    action
        One of ``SUPPORTED_ANDROID_EMULATOR_ACTIONS``.
    avd_name
        AVD identifier (e.g. ``omnisight_pixel8_api34``). Required for
        both actions — ``android emulator start`` without a target AVD
        prints usage; we reject upstream so callers get a deterministic
        error rather than a silently-launched "default" AVD.
    api_level
        Optional API level (e.g. ``"34"``). Only consumed by ``create``;
        passed as ``--api-level <N>``. Ignored for ``start``.
    extra_args
        Appended verbatim after the required positional(s). Typical
        uses: ``("-no-window", "-no-audio")`` for headless ``start``,
        ``("--device", "pixel_8")`` for ``create``.

    Returns
    -------
    list[str]
        argv suitable for ``subprocess.run`` / ``subprocess.Popen``.
        Does NOT invoke.

    Raises
    ------
    UnknownAndroidEmulatorActionError
        ``action`` is not in ``SUPPORTED_ANDROID_EMULATOR_ACTIONS``.
    ValueError
        ``avd_name`` is empty.
    """
    norm = action.strip().lower().replace("_", "-")
    if norm not in SUPPORTED_ANDROID_EMULATOR_ACTIONS:
        raise UnknownAndroidEmulatorActionError(
            f"android_emulator_command: action={action!r} is not supported; "
            f"valid values: {sorted(SUPPORTED_ANDROID_EMULATOR_ACTIONS)}"
        )
    if not avd_name:
        raise ValueError(
            "android_emulator_command requires a non-empty avd_name "
            "(e.g. 'omnisight_pixel8_api34')"
        )
    argv: list[str] = ["android", "emulator", norm, avd_name]
    if norm == "create" and api_level:
        argv.extend(["--api-level", str(api_level)])
    argv.extend(extra_args)
    return argv


def adb_screencap_command(
    *,
    remote_path: str = "/sdcard/screencap.png",
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Emit the ``adb shell screencap -p <remote_path>`` argv.

    Preserved alongside the Android CLI emulator fast path (P11 #351
    checkbox 3) — Google's Android CLI does not yet expose a
    screenshot sub-command, so captures continue to flow through adb
    regardless of whether the emulator was launched by
    ``android emulator start`` or the legacy ``emulator -avd`` binary.

    Pure command builder. The caller still needs a follow-up
    ``adb pull <remote_path> <local_path>`` to retrieve the PNG.
    """
    argv: list[str] = ["adb", "shell", "screencap", "-p", remote_path]
    argv.extend(extra_args)
    return argv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI framework autodetect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def resolve_ui_framework(
    app_path: Path,
    *,
    mobile_platform: str = "",
) -> str:
    """Return the UI-test framework label for an app directory.

    Detection order (specific → generic):
      1. ``pubspec.yaml``       → ``flutter``
      2. ``app.json`` + ``metro.config.*`` or ``package.json`` with
         ``react-native`` dep → ``react-native``
      3. Any ``.xcworkspace`` / ``.xcodeproj`` + ``*UITests`` target dir
         → ``xcuitest``
      4. ``build.gradle`` with ``androidTest/`` dir → ``espresso``
      5. Fallback to the profile's ``mobile_platform`` (``ios`` ⇒
         xcuitest, ``android`` ⇒ espresso). Returns ``""`` when neither
         markers nor platform hint are usable — caller treats that as
         "skip UI stage".
    """
    root = Path(app_path)
    if not root.is_dir():
        return ""

    # Flutter first — a Flutter project also contains android/ + ios/,
    # so a deeper marker would misclassify it as native. pubspec.yaml
    # is the canonical Flutter marker.
    if (root / "pubspec.yaml").is_file():
        return "flutter"

    # React Native — package.json listing "react-native" is the
    # canonical marker. Check Metro config too for tighter evidence.
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            if any(name == "react-native" or name.startswith("react-native-") for name in deps):
                return "react-native"
        except (OSError, json.JSONDecodeError):
            # tolerate malformed manifests; fall through to native checks
            pass

    # Native iOS — .xcworkspace or .xcodeproj + UI-test target
    for child in root.iterdir():
        if child.is_dir() and (child.name.endswith(".xcworkspace") or child.name.endswith(".xcodeproj")):
            return "xcuitest"

    # Native Android — top-level build.gradle (kts or groovy) OR a
    # nested android/ folder with gradle metadata
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "espresso"
    if (root / "android").is_dir() and (
        (root / "android" / "build.gradle").is_file()
        or (root / "android" / "build.gradle.kts").is_file()
    ):
        return "espresso"

    # Fallback via platform hint
    plat = (mobile_platform or "").strip().lower()
    if plat == "ios":
        return "xcuitest"
    if plat == "android":
        return "espresso"
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Emulator / simulator boot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def boot_ios_simulator(
    *,
    device_model: str = "",
    runtime: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> EmulatorBootReport:
    """``xcrun simctl boot`` wrapper.

    Requires macOS + Xcode — on any other host the report is flagged
    ``mock`` so P2 CI runs on Linux do not falsely report a boot.
    """
    src = env if env is not None else os.environ
    udid = (src.get("OMNISIGHT_IOS_SIM_UDID") or "").strip()
    model = (src.get("OMNISIGHT_IOS_SIM_DEVICE") or "").strip() or device_model or "iPhone 15 Pro"
    rt = (src.get("OMNISIGHT_IOS_SIM_RUNTIME") or "").strip() or runtime or "com.apple.CoreSimulator.SimRuntime.iOS-17-5"

    xcrun = shutil.which("xcrun")
    if not xcrun:
        return EmulatorBootReport(
            status="mock",
            kind="simulator",
            device_model=model,
            udid=udid or "mock-udid",
            runtime=rt,
            detail="xcrun not on PATH (non-macOS host)",
        )
    try:
        proc = subprocess.run(
            [xcrun, "simctl", "boot", udid or model],
            capture_output=True, text=True, timeout=90,
        )
        return EmulatorBootReport(
            status="booted" if proc.returncode == 0 else "fail",
            kind="simulator",
            device_model=model,
            udid=udid or model,
            runtime=rt,
            duration_ms=0,
            detail=(proc.stderr or proc.stdout or "")[:200],
        )
    except Exception as exc:  # noqa: BLE001
        return EmulatorBootReport(
            status="fail", kind="simulator",
            device_model=model, udid=udid, runtime=rt,
            detail=f"boot error: {exc}",
        )


def boot_android_emulator(
    *,
    avd_name: str = "",
    api_level: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> EmulatorBootReport:
    """Android emulator boot wrapper with P11 #351 Android-CLI fast path.

    Launch strategy:
      1. If Google's Android CLI is on PATH → ``android emulator start
         <avd_name> -no-window -no-audio -no-snapshot`` (P11 #351
         checkbox 3 fast path — 3× faster / 70% less token per Google
         2026-04-18 benchmark).
      2. Else if legacy ``$ANDROID_HOME/emulator/emulator`` is on PATH
         → ``emulator -avd <name> -no-window -no-audio -no-snapshot``
         (pre-P11 path — retained so operator-incomplete hosts keep
         building).
      3. Else → ``status="mock"`` so a sandboxed / Linux-sans-SDK
         runner still produces a structured report rather than hanging.

    Either launcher feeds back into the same ``adb shell getprop
    sys.boot_completed`` probe — adb is always required because boot
    completion detection is intrinsic to Android's boot protocol, not
    the emulator wrapper. The detail string carries
    ``android-cli: …`` / ``legacy-emulator: …`` so operators can
    tell which path ran without re-reading the source.
    """
    src = env if env is not None else os.environ
    name = (src.get("OMNISIGHT_ANDROID_AVD_NAME") or "").strip() or avd_name or "omnisight_pixel8_api34"
    level = (src.get("OMNISIGHT_ANDROID_API_LEVEL") or "").strip() or api_level or "34"

    cli_path = shutil.which("android")
    emu = shutil.which("emulator")
    adb = shutil.which("adb")
    if not adb or (not cli_path and not emu):
        return EmulatorBootReport(
            status="mock",
            kind="avd",
            device_model=name,
            runtime=f"API {level}",
            detail="android CLI / emulator / adb not on PATH",
        )

    if cli_path:
        launch_argv = android_emulator_command(
            "start",
            avd_name=name,
            extra_args=("-no-window", "-no-audio", "-no-snapshot"),
        )
        launch_argv[0] = cli_path
        launcher_kind = "android-cli"
    else:
        launch_argv = [emu, "-avd", name, "-no-window", "-no-audio", "-no-snapshot"]
        launcher_kind = "legacy-emulator"

    try:
        # Start the emulator in the background — a real runner would
        # wait on boot_completed; we keep P2 synchronous by just
        # attempting a boot check with a tight timeout.
        subprocess.Popen(
            launch_argv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        probe = subprocess.run(
            [adb, "shell", "getprop", "sys.boot_completed"],
            capture_output=True, text=True, timeout=30,
        )
        booted = probe.returncode == 0 and probe.stdout.strip() == "1"
        body = (probe.stdout or probe.stderr or "").strip()
        return EmulatorBootReport(
            status="booted" if booted else "fail",
            kind="avd",
            device_model=name,
            runtime=f"API {level}",
            detail=f"{launcher_kind}: {body}"[:200],
        )
    except Exception as exc:  # noqa: BLE001
        return EmulatorBootReport(
            status="fail", kind="avd",
            device_model=name, runtime=f"API {level}",
            detail=f"{launcher_kind} boot error: {exc}",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Smoke (install + launch)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def run_smoke(
    *,
    mobile_platform: str,
    app_path: Path,
    emulator: EmulatorBootReport,
) -> SmokeReport:
    """Minimum viable "did the app install + launch" check.

    On Linux/sandbox hosts (emulator status=mock) this degrades to a
    mock result. On real hosts it checks for a built artifact — an
    ``.app`` bundle for iOS or an ``.apk`` / ``.aab`` under
    ``app/build/outputs`` for Android. We intentionally don't run
    ``xcrun simctl install`` / ``adb install`` here — the real install
    happens as part of the UI-test framework invocation that follows.
    """
    if emulator.status == "mock":
        return SmokeReport(
            status="mock",
            launched=False,
            detail=f"emulator mock ({emulator.detail})",
        )
    root = Path(app_path)
    if mobile_platform == "ios":
        # .app bundles live under build/ios/*.app after xcodebuild
        found = any(root.rglob("*.app"))
        return SmokeReport(
            status="pass" if found else "skip",
            launched=found,
            detail=".app bundle found" if found else "no .app bundle",
        )
    if mobile_platform == "android":
        apk = any(root.rglob("*.apk"))
        aab = any(root.rglob("*.aab"))
        found = apk or aab
        return SmokeReport(
            status="pass" if found else "skip",
            launched=found,
            detail=("apk found" if apk else "aab found") if found else "no apk/aab",
        )
    return SmokeReport(status="skip", detail=f"unknown platform {mobile_platform!r}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI-test runners
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def run_xcuitest(
    app_path: Path,
    *,
    scheme: str = "",
    destination: str = "",
    timeout: int = 900,
) -> UITestReport:
    """``xcodebuild test`` — requires macOS + Xcode.

    We rely on xcodebuild's ``-resultBundlePath`` JSON output, but when
    xcodebuild is absent (Linux CI / sandbox) we return a ``mock``
    report so the shell doesn't fail the track just for missing a tool.
    """
    xcbuild = shutil.which("xcodebuild")
    if not xcbuild:
        return UITestReport(status="mock", framework="xcuitest", detail="xcodebuild not on PATH")
    argv = [xcbuild, "test"]
    if scheme:
        argv.extend(["-scheme", scheme])
    if destination:
        argv.extend(["-destination", destination])
    else:
        argv.extend(["-destination", "generic/platform=iOS Simulator"])
    try:
        proc = subprocess.run(
            argv, cwd=str(app_path), capture_output=True, text=True, timeout=timeout,
        )
        passed, failed = _parse_xcodebuild_counts(proc.stdout + proc.stderr)
        status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
        return UITestReport(
            status=status,
            framework="xcuitest",
            total=passed + failed,
            passed=passed,
            failed=failed,
            detail=f"xcodebuild rc={proc.returncode}",
        )
    except subprocess.TimeoutExpired:
        return UITestReport(status="fail", framework="xcuitest", detail="xcodebuild timed out")
    except Exception as exc:  # noqa: BLE001
        return UITestReport(status="fail", framework="xcuitest", detail=f"xcodebuild error: {exc}")


def _parse_xcodebuild_counts(output: str) -> tuple[int, int]:
    """Extract (passed, failed) counts from xcodebuild stdout.

    xcodebuild emits lines like ``Test Case '-[MyUITests testFoo]'
    passed (0.123 seconds).`` Counting those is cheaper than depending
    on ``xcpretty`` / ``xcresultparser``.
    """
    passed = sum(1 for line in output.splitlines() if "' passed " in line and "Test Case" in line)
    failed = sum(1 for line in output.splitlines() if "' failed " in line and "Test Case" in line)
    return passed, failed


def run_espresso(
    project_root: Path,
    *,
    timeout: int = 900,
) -> UITestReport:
    """``./gradlew connectedAndroidTest`` — delegates to the Espresso
    instrumentation test runner on the connected emulator/device."""
    gradle = None
    wrapper = Path(project_root) / "gradlew"
    if wrapper.is_file() and os.access(wrapper, os.X_OK):
        gradle = str(wrapper)
    elif shutil.which("gradle"):
        gradle = shutil.which("gradle")
    if not gradle:
        return UITestReport(status="mock", framework="espresso", detail="gradlew/gradle not found")
    argv = [gradle, "connectedAndroidTest", "--console=plain"]
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
        )
        passed, failed = _parse_gradle_test_counts(proc.stdout + proc.stderr)
        status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
        return UITestReport(
            status=status, framework="espresso",
            total=passed + failed, passed=passed, failed=failed,
            detail=f"gradle rc={proc.returncode}",
        )
    except subprocess.TimeoutExpired:
        return UITestReport(status="fail", framework="espresso", detail="gradle timed out")
    except Exception as exc:  # noqa: BLE001
        return UITestReport(status="fail", framework="espresso", detail=f"gradle error: {exc}")


def _parse_gradle_test_counts(output: str) -> tuple[int, int]:
    """Extract (passed, failed) from gradle connectedAndroidTest output.

    Gradle emits a summary line like ``Tests: 42, Failures: 1, Errors:
    0, Skipped: 0`` — or in the xUnit XML. We parse the summary line
    conservatively; callers with richer needs should read the
    ``app/build/outputs/androidTest-results/`` XML directly.
    """
    passed = 0
    failed = 0
    for line in output.splitlines():
        ls = line.strip()
        if ls.startswith("Tests: ") or ls.startswith("Tests run: "):
            # "Tests: 42, Failures: 1, Errors: 0, Skipped: 2"
            parts = [p.strip() for p in ls.split(",")]
            for p in parts:
                if p.startswith("Tests: "):
                    try:
                        passed = int(p.split()[1])
                    except (IndexError, ValueError):
                        pass
                elif p.startswith("Failures: ") or p.startswith("Errors: "):
                    try:
                        failed += int(p.split()[1])
                    except (IndexError, ValueError):
                        pass
            if passed >= failed:
                passed -= failed
            break
    return passed, failed


def run_flutter_tests(project_root: Path, *, timeout: int = 600) -> UITestReport:
    """``flutter test integration_test/`` — Flutter's cross-platform
    runner. When flutter is absent we degrade to mock."""
    flutter = shutil.which("flutter")
    if not flutter:
        return UITestReport(status="mock", framework="flutter", detail="flutter not on PATH")
    integration = Path(project_root) / "integration_test"
    target = "integration_test" if integration.is_dir() else "test"
    argv = [flutter, "test", target, "--reporter=json"]
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
        )
        passed, failed = _parse_flutter_test_json(proc.stdout)
        status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
        return UITestReport(
            status=status, framework="flutter",
            total=passed + failed, passed=passed, failed=failed,
            detail=f"flutter rc={proc.returncode}",
        )
    except subprocess.TimeoutExpired:
        return UITestReport(status="fail", framework="flutter", detail="flutter timed out")
    except Exception as exc:  # noqa: BLE001
        return UITestReport(status="fail", framework="flutter", detail=f"flutter error: {exc}")


def _parse_flutter_test_json(stdout: str) -> tuple[int, int]:
    """Count testDone events with result != 'success' from flutter's
    newline-delimited JSON reporter."""
    passed = 0
    failed = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "testDone":
            # hidden == True means setUp/tearDown boilerplate; skip
            if evt.get("hidden"):
                continue
            if evt.get("result") == "success":
                passed += 1
            else:
                failed += 1
    return passed, failed


def run_rn_tests(project_root: Path, *, timeout: int = 600) -> UITestReport:
    """React Native UI tests via Detox (preferred) falling back to
    ``npm test`` when Detox isn't configured."""
    npx = shutil.which("npx")
    npm = shutil.which("npm")
    if not npx and not npm:
        return UITestReport(status="mock", framework="react-native", detail="npm/npx not on PATH")
    root = Path(project_root)
    detoxrc = any((root / name).is_file() for name in (".detoxrc.js", ".detoxrc.json", ".detoxrc"))
    if detoxrc and npx:
        argv = [npx, "detox", "test", "--reuse"]
        try:
            proc = subprocess.run(
                argv, cwd=str(root), capture_output=True, text=True, timeout=timeout,
            )
            status = "pass" if proc.returncode == 0 else "fail"
            return UITestReport(
                status=status, framework="react-native",
                detail=f"detox rc={proc.returncode}",
            )
        except Exception as exc:  # noqa: BLE001
            return UITestReport(status="fail", framework="react-native",
                                detail=f"detox error: {exc}")
    # No Detox — best-effort jest run
    if npm:
        try:
            proc = subprocess.run(
                [npm, "test", "--", "--silent"],
                cwd=str(root), capture_output=True, text=True, timeout=timeout,
            )
            status = "pass" if proc.returncode == 0 else "fail"
            return UITestReport(
                status=status, framework="react-native",
                detail=f"npm test rc={proc.returncode}",
            )
        except Exception as exc:  # noqa: BLE001
            return UITestReport(status="fail", framework="react-native",
                                detail=f"npm test error: {exc}")
    return UITestReport(status="mock", framework="react-native", detail="no Detox and no npm")


def run_ui_tests(
    *,
    framework: str,
    app_path: Path,
    scheme: str = "",
    destination: str = "",
) -> UITestReport:
    """Dispatch to the framework-specific runner."""
    if framework == "xcuitest":
        return run_xcuitest(app_path, scheme=scheme, destination=destination)
    if framework == "espresso":
        return run_espresso(app_path)
    if framework == "flutter":
        return run_flutter_tests(app_path)
    if framework == "react-native":
        return run_rn_tests(app_path)
    return UITestReport(status="skip", framework=framework or "",
                        detail="no UI framework detected")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Device-farm delegation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _firebase_argv(
    *,
    app_path: Path,
    mobile_platform: str,
) -> tuple[list[str], list[str]]:
    """Build the ``gcloud firebase test <platform> run`` argv + env-forward
    list. Values are intentionally absent — the actual project ID
    travels via ``GOOGLE_CLOUD_PROJECT`` the runner forwards."""
    subcmd = "android" if mobile_platform == "android" else "ios"
    artifact_hint = "--app=app-release.apk" if mobile_platform == "android" else "--test=ui-tests.zip"
    argv = [
        "gcloud", "firebase", "test", subcmd, "run",
        f"--type={'instrumentation' if mobile_platform == 'android' else 'xctest'}",
        artifact_hint,
        "--results-dir=gs://omnisight-p2-results",
    ]
    env_forward = ["GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"]
    return argv, env_forward


def _aws_argv(
    *,
    app_path: Path,
    mobile_platform: str,
) -> tuple[list[str], list[str]]:
    """Build the ``aws devicefarm schedule-run`` argv. Project ARN +
    device pool ARN come from env so the argv is safe to log."""
    argv = [
        "aws", "devicefarm", "schedule-run",
        "--project-arn", "$AWS_DEVICEFARM_PROJECT_ARN",
        "--device-pool-arn", "$AWS_DEVICEFARM_POOL_ARN",
        "--name", "omnisight-p2-run",
        "--app-arn", "$AWS_DEVICEFARM_APP_ARN",
        "--test", "type=APPIUM_PYTHON,testPackageArn=$AWS_DEVICEFARM_TEST_ARN"
        if mobile_platform == "android"
        else "type=XCTEST_UI,testPackageArn=$AWS_DEVICEFARM_TEST_ARN",
    ]
    env_forward = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_DEVICEFARM_PROJECT_ARN",
        "AWS_DEVICEFARM_POOL_ARN",
        "AWS_DEVICEFARM_APP_ARN",
        "AWS_DEVICEFARM_TEST_ARN",
    ]
    return argv, env_forward


def _browserstack_argv(
    *,
    app_path: Path,
    mobile_platform: str,
) -> tuple[list[str], list[str]]:
    """Build the ``browserstack-local`` argv for a tunneled session.

    BrowserStack App Automate's real entry point is the REST API; we
    emit the local-tunnel invocation here since it's the only CLI
    required from the runner box.
    """
    argv = [
        "browserstack-local",
        "--key", "$BROWSERSTACK_ACCESS_KEY",
        "--local-identifier", "omnisight-p2",
        "--daemon", "start",
    ]
    env_forward = [
        "BROWSERSTACK_USERNAME",
        "BROWSERSTACK_ACCESS_KEY",
    ]
    return argv, env_forward


def run_device_farm(
    farm: str,
    *,
    app_path: Path,
    mobile_platform: str,
) -> DeviceFarmReport:
    """Return a delegation report for the requested device-farm adapter.

    Never executes — we produce the argv (values via env refs only)
    and flag the report as ``delegated`` if the farm's CLI is on PATH,
    else ``mock``. Concrete dispatch happens in the downstream pipeline
    (O6 worker pool / cloud runner) that owns the credentials.
    """
    farm = (farm or "").strip().lower()
    if farm == "":
        return DeviceFarmReport(status="skip")
    if farm not in SUPPORTED_FARMS:
        raise UnknownDeviceFarmError(
            f"unsupported farm {farm!r}; valid: {sorted(SUPPORTED_FARMS)}"
        )
    builders = {
        "firebase": _firebase_argv,
        "aws": _aws_argv,
        "browserstack": _browserstack_argv,
    }
    argv, env_forward = builders[farm](app_path=app_path, mobile_platform=mobile_platform)
    cli = _FARM_CLI[farm]
    status = "delegated" if shutil.which(cli) else "mock"
    detail = (
        f"{farm} CLI ({cli}) on PATH — argv emitted"
        if status == "delegated"
        else f"{farm} CLI ({cli}) absent — returning mock delegation"
    )
    return DeviceFarmReport(
        status=status, farm=farm, argv=argv, env_forward=list(env_forward), detail=detail,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Screenshot matrix (devices × locales)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _parse_csv_list(value: str) -> list[str]:
    """Split a comma-separated CLI value and drop empties/whitespace."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def run_screenshot_matrix(
    *,
    app_path: Path,
    mobile_platform: str,
    devices: Sequence[str],
    locales: Sequence[str],
    output_dir: Optional[Path] = None,
) -> ScreenshotMatrixReport:
    """Drive ``fastlane snapshot`` / ``fastlane screengrab`` across the
    cross-product of device models × locales.

    When fastlane is absent (or either list empty) we return a mock
    report so the shell can still aggregate a sane JSON envelope.
    """
    devs = [d for d in devices if d]
    locs = [loc for loc in locales if loc]
    out = Path(output_dir or Path(app_path) / "screenshots")

    if not devs or not locs:
        return ScreenshotMatrixReport(
            status="skip",
            devices=list(devs),
            locales=list(locs),
            captured=0,
            output_dir=str(out),
            detail="empty matrix (devices or locales missing)",
        )

    fastlane = shutil.which("fastlane")
    if not fastlane:
        return ScreenshotMatrixReport(
            status="mock",
            devices=list(devs),
            locales=list(locs),
            captured=0,
            output_dir=str(out),
            detail="fastlane not on PATH",
        )
    lane = "snapshot" if mobile_platform == "ios" else "screengrab"
    argv = [
        fastlane, lane,
        "--devices", ",".join(devs),
        "--languages", ",".join(locs),
        "--output_directory", str(out),
    ]
    try:
        proc = subprocess.run(
            argv, cwd=str(app_path), capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode == 0:
            return ScreenshotMatrixReport(
                status="pass",
                devices=list(devs),
                locales=list(locs),
                captured=len(devs) * len(locs),
                output_dir=str(out),
                detail=f"fastlane {lane} ok",
            )
        return ScreenshotMatrixReport(
            status="fail",
            devices=list(devs),
            locales=list(locs),
            output_dir=str(out),
            detail=f"fastlane {lane} rc={proc.returncode}",
        )
    except Exception as exc:  # noqa: BLE001
        return ScreenshotMatrixReport(
            status="fail",
            devices=list(devs),
            locales=list(locs),
            output_dir=str(out),
            detail=f"fastlane error: {exc}",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def simulate_mobile(
    *,
    profile: str,
    app_path: Path,
    farm: str = "",
    devices: Sequence[str] = (),
    locales: Sequence[str] = (),
    env: Optional[Mapping[str, str]] = None,
) -> MobileSimResult:
    """Run every P2 gate and aggregate the result."""
    app = Path(app_path).resolve()
    result = MobileSimResult(profile=profile, app_path=str(app))

    # ── Profile resolution ────────────────────────────────────────
    profile_cfg: dict[str, Any] = {}
    emu_spec: dict[str, Any] = {}
    try:
        from backend.platform_profile import get_platform_config
        profile_cfg = get_platform_config(profile)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"profile resolve failed: {exc}")
    if profile_cfg.get("target_kind") and profile_cfg.get("target_kind") != "mobile":
        raise UnsupportedProfileError(
            f"profile {profile!r} target_kind is {profile_cfg.get('target_kind')!r}, "
            "not 'mobile'"
        )
    mobile_platform = str(profile_cfg.get("mobile_platform") or "").strip().lower()
    mobile_abi = str(profile_cfg.get("mobile_abi") or "").strip()
    emu_spec = dict(profile_cfg.get("emulator_spec") or {})
    result.mobile_platform = mobile_platform
    result.mobile_abi = mobile_abi

    # ── UI framework autodetect ───────────────────────────────────
    result.ui_framework = resolve_ui_framework(app, mobile_platform=mobile_platform)

    # ── Emulator boot ─────────────────────────────────────────────
    if mobile_platform == "ios":
        result.emulator = boot_ios_simulator(
            device_model=str(emu_spec.get("device_model") or ""),
            runtime=str(emu_spec.get("runtime") or ""),
            env=env,
        )
    elif mobile_platform == "android":
        result.emulator = boot_android_emulator(
            avd_name=str(emu_spec.get("avd_name") or ""),
            api_level=str(emu_spec.get("api_level") or ""),
            env=env,
        )
    else:
        result.emulator = EmulatorBootReport(
            status="skip", detail=f"unknown mobile_platform {mobile_platform!r}",
        )

    # ── Smoke (app bundle existence) ──────────────────────────────
    result.smoke = run_smoke(
        mobile_platform=mobile_platform, app_path=app, emulator=result.emulator,
    )

    # ── UI tests ──────────────────────────────────────────────────
    result.ui_test = run_ui_tests(
        framework=result.ui_framework, app_path=app,
    )

    # ── Device-farm delegation ────────────────────────────────────
    try:
        result.device_farm = run_device_farm(
            farm, app_path=app, mobile_platform=mobile_platform,
        )
    except UnknownDeviceFarmError as exc:
        result.errors.append(str(exc))
        result.device_farm = DeviceFarmReport(status="fail", farm=farm, detail=str(exc))

    # ── Screenshot matrix ─────────────────────────────────────────
    result.screenshot_matrix = run_screenshot_matrix(
        app_path=app,
        mobile_platform=mobile_platform,
        devices=devices,
        locales=locales,
    )

    # ── Gate rollup ───────────────────────────────────────────────
    result.gates = {
        "emulator_ready": result.emulator.status in ("booted", "mock"),
        "smoke_ok": result.smoke.status in ("pass", "mock", "skip"),
        "ui_tests_ok": result.ui_test.status in ("pass", "mock", "skip"),
        "device_farm_ok": result.device_farm.status in ("pass", "mock", "skip", "delegated"),
        "screenshot_matrix_ok": result.screenshot_matrix.status in ("pass", "mock", "skip"),
    }
    return result


def result_to_json(result: MobileSimResult) -> dict[str, Any]:
    """Flatten ``MobileSimResult`` into the dict shape consumed by
    ``run_mobile`` in ``simulate.sh``."""
    return {
        "profile": result.profile,
        "app_path": result.app_path,
        "mobile_platform": result.mobile_platform,
        "mobile_abi": result.mobile_abi,
        "ui_framework": result.ui_framework,
        "emulator_status": result.emulator.status,
        "emulator_kind": result.emulator.kind,
        "emulator_device": result.emulator.device_model,
        "emulator_udid": result.emulator.udid,
        "emulator_runtime": result.emulator.runtime,
        "emulator_detail": result.emulator.detail,
        "smoke_status": result.smoke.status,
        "smoke_launched": result.smoke.launched,
        "ui_test_status": result.ui_test.status,
        "ui_test_framework": result.ui_test.framework,
        "ui_test_total": result.ui_test.total,
        "ui_test_passed": result.ui_test.passed,
        "ui_test_failed": result.ui_test.failed,
        "ui_test_detail": result.ui_test.detail,
        "device_farm_status": result.device_farm.status,
        "device_farm_name": result.device_farm.farm,
        "device_farm_argv": result.device_farm.argv,
        "device_farm_env_forward": result.device_farm.env_forward,
        "screenshot_matrix_status": result.screenshot_matrix.status,
        "screenshot_matrix_devices": result.screenshot_matrix.devices,
        "screenshot_matrix_locales": result.screenshot_matrix.locales,
        "screenshot_matrix_captured": result.screenshot_matrix.captured,
        "screenshot_matrix_output_dir": result.screenshot_matrix.output_dir,
        "gates": result.gates,
        "overall_pass": result.overall_pass(),
        "errors": result.errors,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI — invoked from simulate.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cli_main() -> int:
    """CLI entrypoint (``python3 -m backend.mobile_simulator``).

    Contract with simulate.sh: single JSON object on stdout, exit 0.
    Non-zero would cause ``set -euo pipefail`` in the shell to abort
    the whole track before it can aggregate its own envelope, so we
    always print JSON and let the shell decide gating.
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--app-path", required=True)
    parser.add_argument("--farm", default="")
    parser.add_argument("--devices", default="",
                        help="comma-separated device models for screenshot matrix")
    parser.add_argument("--locales", default="",
                        help="comma-separated locales for screenshot matrix")
    args = parser.parse_args()

    try:
        result = simulate_mobile(
            profile=args.profile,
            app_path=Path(args.app_path),
            farm=args.farm,
            devices=_parse_csv_list(args.devices),
            locales=_parse_csv_list(args.locales),
        )
    except MobileSimError as exc:
        # Emit a minimal but valid JSON envelope so simulate.sh can
        # still parse the summary and surface a structured failure.
        fail = MobileSimResult(profile=args.profile, app_path=args.app_path)
        fail.errors.append(str(exc))
        print(json.dumps(result_to_json(fail)))
        return 0

    print(json.dumps(result_to_json(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
