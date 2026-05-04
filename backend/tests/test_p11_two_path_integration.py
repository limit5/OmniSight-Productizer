"""P11 #351 checkbox 6 — two-path build + deploy + emulator-boot integration.

Closes the P11 epic by proving the **end-to-end chain** works on both
host configurations the dispatcher supports:

    A. Google's Android CLI (``android`` binary, d.android.com/tools/agents,
       2026-04-18) is on PATH ⇒ ``resolve_android_invocation`` routes to
       ``android run/create/sdk-install`` AND ``boot_android_emulator``
       routes to ``android emulator start``. The fast path Google
       benchmarked at 3× faster / ~70% less token.

    B. Android CLI is **absent** ⇒ ``resolve_android_invocation`` falls
       back to ``./gradlew installDebug`` AND ``boot_android_emulator``
       falls back to the legacy ``$ANDROID_HOME/emulator/emulator -avd``
       binary (or ``mock`` if even that is missing).

Why a separate integration file (not just more rows in
``test_mobile_toolchain.py`` / ``test_mobile_simulator.py``)
-----------------------------------------------------------
The unit test files cover the **dispatcher** and the **boot helper**
in isolation. P11 checkbox 6 is explicitly the *cross-module chain*:
toolchain dispatcher → boot dispatcher → smoke deploy → gate roll-up,
both branches end-to-end. Putting the chain assertions here keeps
the unit files single-responsibility and gives operators one file
to point at when answering "did anyone verify both paths actually
build + deploy + boot end-to-end?".

How the two paths are exercised without real Android tooling
------------------------------------------------------------
Every external CLI (``android`` / ``emulator`` / ``adb`` /
``gradlew``) is stubbed via ``shutil.which`` monkeypatching; the
``subprocess.Popen`` / ``subprocess.run`` calls inside
``boot_android_emulator`` are intercepted so the tests are
deterministic on Linux CI without an Android SDK installed. Each
path's gate roll-up is asserted on top of the same
``simulate_mobile`` orchestrator that ``scripts/simulate.sh`` calls,
so a regression in either branch fails CI before it can reach a host
with the real binaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import mobile_simulator as ms
from backend import mobile_toolchain as mt
from backend.platform import get_platform_config


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture helpers — synthesise host configurations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_ANDROID_BIN = "/usr/local/bin/android"
_LEGACY_EMU_BIN = "/opt/android/sdk/emulator/emulator"
_ADB_BIN = "/usr/bin/adb"


def _patch_path(monkeypatch, *, android: bool, legacy_emulator: bool, adb: bool) -> None:
    """Stub ``shutil.which`` against both ``mt`` and ``ms`` simultaneously.

    The two modules carry independent ``shutil`` references — the
    integration chain crosses both, so a single patch on one module
    would let the other escape into the host PATH. Patching both keeps
    the test deterministic regardless of which module the caller
    happens to enter first.
    """
    mapping: dict[str, str | None] = {
        "android": _ANDROID_BIN if android else None,
        "emulator": _LEGACY_EMU_BIN if legacy_emulator else None,
        "adb": _ADB_BIN if adb else None,
    }

    def fake_which(name: str):
        return mapping.get(name)

    monkeypatch.setattr(mt.shutil, "which", fake_which)
    monkeypatch.setattr(ms.shutil, "which", fake_which)


def _make_android_project(tmp_path: Path, *, with_gradlew: bool = True) -> Path:
    """Synthesise the minimum Android project tree the dispatcher and
    smoke-deploy gate need: a ``gradlew`` wrapper at the root, an
    ``app/build/outputs/`` directory with a fake ``.apk`` so
    ``run_smoke`` reports ``pass`` (rather than ``skip``)."""
    root = tmp_path / "android-project"
    root.mkdir(parents=True)
    if with_gradlew:
        wrapper = root / "gradlew"
        wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
        wrapper.chmod(0o755)
    (root / "build.gradle").write_text("// android project\n")
    outputs = root / "app" / "build" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "app-debug.apk").write_bytes(b"PK\x03\x04")
    return root


class _StubBootProc:
    """Stand-in for the ``adb shell getprop sys.boot_completed`` probe.

    Returns ``returncode=0`` + ``stdout="1\\n"`` so
    ``boot_android_emulator`` records ``status="booted"``.
    """

    returncode = 0
    stdout = "1\n"
    stderr = ""


def _record_subprocess(monkeypatch) -> dict[str, list[list[str]]]:
    """Capture every ``Popen`` (emulator launch) and ``run`` (boot probe).

    Returns a recorder dict the test asserts against to prove which
    binary the boot dispatcher actually shelled out to.
    """
    recorded: dict[str, list[list[str]]] = {"popen": [], "run": []}

    class _FakePopen:
        def __init__(self, argv, **_kwargs):
            recorded["popen"].append(list(argv))

    def _fake_run(argv, *_args, **_kwargs):
        recorded["run"].append(list(argv))
        return _StubBootProc()

    monkeypatch.setattr(ms.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ms.subprocess, "run", _fake_run)
    return recorded


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATH A — Android CLI present (Google fast path)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAndroidCliPathBuildDeployBoot:
    """End-to-end on a host where the Android CLI is available."""

    def test_build_invocation_uses_android_cli(self, tmp_path, monkeypatch):
        """Build phase: ``resolve_android_invocation("run", root)`` ⇒
        ``android run <root>``. Pinning the argv shape catches any
        regression that quietly downgrades to ``./gradlew`` even when
        ``android`` is on PATH (the regression Google's 3× / 70% benchmark
        gain would silently disappear under)."""
        _patch_path(monkeypatch, android=True, legacy_emulator=False, adb=True)
        root = _make_android_project(tmp_path)

        inv = mt.resolve_android_invocation("run", root)

        assert inv.path_kind == "android-cli"
        assert inv.argv == ["android", "run", str(root)]
        assert "android CLI on PATH" in inv.detail

    def test_deploy_invocation_uses_android_cli_create_for_scaffold(
        self, tmp_path, monkeypatch,
    ):
        """Deploy/scaffold phase: ``android create`` replaces the hand-
        rolled template scaffolding from P1. Must succeed on the CLI
        path (no ``NoGradleFallbackError`` here — that error is only
        for the absent-CLI path)."""
        _patch_path(monkeypatch, android=True, legacy_emulator=False, adb=True)
        root = _make_android_project(tmp_path)

        inv = mt.resolve_android_invocation(
            "create", root, extra_args=["--template", "kotlin"],
        )

        assert inv.path_kind == "android-cli"
        assert inv.argv == [
            "android", "create", str(root), "--template", "kotlin",
        ]

    def test_emulator_boot_uses_android_emulator_start(
        self, tmp_path, monkeypatch,
    ):
        """Boot phase: ``boot_android_emulator`` shells out to
        ``android emulator start <avd>`` (the resolved ``android``
        absolute path is substituted into argv[0] so PATH changes
        mid-run can't leak through)."""
        _patch_path(monkeypatch, android=True, legacy_emulator=False, adb=True)
        recorded = _record_subprocess(monkeypatch)

        report = ms.boot_android_emulator(avd_name="p11_avd", api_level="34")

        assert report.status == "booted"
        assert report.detail.startswith("android-cli:")
        assert len(recorded["popen"]) == 1
        launch_argv = recorded["popen"][0]
        assert launch_argv[0] == _ANDROID_BIN
        assert launch_argv[1:4] == ["emulator", "start", "p11_avd"]
        # Headless flags propagate so CI runners don't get a graphical window.
        assert "-no-window" in launch_argv

    def test_full_chain_orchestrator_passes_with_cli(self, tmp_path, monkeypatch):
        """Full chain: ``simulate_mobile`` runs build-resolve + emulator
        boot + smoke + UI-test stubs + gate roll-up; with the CLI on
        PATH every gate must roll up to ``overall_pass=True`` on a
        synthesised Android project. A regression that breaks the CLI
        path mid-chain (e.g. dispatcher emits an arg the boot helper
        can't consume) lights this red before the ``[D]`` gate."""
        _patch_path(monkeypatch, android=True, legacy_emulator=False, adb=True)
        _record_subprocess(monkeypatch)
        root = _make_android_project(tmp_path)

        result = ms.simulate_mobile(
            profile="android-arm64-v8a",
            app_path=root,
        )

        assert result.mobile_platform == "android"
        assert result.mobile_abi == "arm64-v8a"
        assert result.emulator.status == "booted"
        assert result.emulator.detail.startswith("android-cli:")
        # Smoke discovers the synthesised ``app-debug.apk`` ⇒ pass, not skip.
        assert result.smoke.status == "pass"
        assert result.smoke.launched is True
        assert result.overall_pass() is True

    def test_profile_flag_agrees_with_cli_path_choice(self, tmp_path, monkeypatch):
        """Drift guard tying P11 checkbox 5 to checkbox 6: the
        profile-level ``android_cli_available`` flag and the runtime
        decision must agree on the happy path. If a future operator
        flips the profile to ``False`` while leaving the CLI on PATH
        they'll get a fast-path runtime + an opt-out config — caller
        wiring (out of scope here) is expected to honour the config and
        force gradle. This test pins the *current* contract: profile
        ``True`` + CLI present = fast path picked."""
        _patch_path(monkeypatch, android=True, legacy_emulator=False, adb=True)
        cfg = get_platform_config("android-arm64-v8a")
        assert cfg["android_cli_available"] is True

        inv = mt.resolve_android_invocation("run", tmp_path)
        assert inv.path_kind == "android-cli"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATH B — Android CLI absent (Gradle wrapper + legacy emulator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGradleFallbackPathBuildDeployBoot:
    """End-to-end on a host where the Android CLI was never installed —
    the pre-P11 baseline that must keep working forever (the fallback
    contract documented in ``backend/docker/Dockerfile.mobile-build``
    and ``scripts/install_android_cli.sh``)."""

    def test_build_invocation_falls_back_to_gradle_install_debug(
        self, tmp_path, monkeypatch,
    ):
        """Build phase: ``resolve_android_invocation("run", root)`` ⇒
        ``./gradlew installDebug``. Pin the task name (``installDebug``,
        NOT ``assembleDebug`` — the latter only builds, doesn't install)
        so a refactor doesn't silently break the install step on
        fallback hosts."""
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        root = _make_android_project(tmp_path)

        inv = mt.resolve_android_invocation("run", root)

        assert inv.path_kind == "gradle-wrapper"
        assert inv.argv[0] == str(root / "gradlew")
        assert "installDebug" in inv.argv
        assert "android CLI absent" in inv.detail

    def test_build_invocation_forwards_abi_to_gradle_fallback(
        self, tmp_path, monkeypatch,
    ):
        """The ABI filter must reach the Gradle invocation as
        ``-PtargetAbi=<abi>`` even on the fallback path. Otherwise the
        dispatcher silently produces a fat APK instead of the per-ABI
        build the profile asked for — a divergence the integration test
        is here to catch."""
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        root = _make_android_project(tmp_path)

        inv = mt.resolve_android_invocation(
            "run", root, abi="arm64-v8a", extra_args=["--stacktrace"],
        )

        assert inv.path_kind == "gradle-wrapper"
        assert "-PtargetAbi=arm64-v8a" in inv.argv
        assert "--stacktrace" in inv.argv

    def test_deploy_phase_create_raises_no_fallback_on_host(
        self, tmp_path, monkeypatch,
    ):
        """Deploy/scaffold phase on the absent-CLI host: ``create`` /
        ``sdk-install`` have no host Gradle equivalent (template
        scaffolding + sdkmanager live in the Docker image only). The
        dispatcher must raise ``NoGradleFallbackError`` with an
        operator hint pointing at the install script + Docker image so
        the next step is actionable from the error message alone."""
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        root = _make_android_project(tmp_path)

        with pytest.raises(mt.NoGradleFallbackError) as exc_info:
            mt.resolve_android_invocation("create", root)

        msg = str(exc_info.value)
        assert "install_android_cli.sh" in msg
        assert mt.MOBILE_BUILD_IMAGE in msg

    def test_emulator_boot_uses_legacy_emulator_binary(
        self, tmp_path, monkeypatch,
    ):
        """Boot phase on the absent-CLI host: ``boot_android_emulator``
        must shell out to the legacy ``$ANDROID_HOME/emulator/emulator
        -avd <name>`` binary (the pre-P11 path) so operator-incomplete
        hosts keep building."""
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        recorded = _record_subprocess(monkeypatch)

        report = ms.boot_android_emulator(avd_name="legacy_avd")

        assert report.status == "booted"
        assert report.detail.startswith("legacy-emulator:")
        assert len(recorded["popen"]) == 1
        launch_argv = recorded["popen"][0]
        assert launch_argv[0] == _LEGACY_EMU_BIN
        assert "-avd" in launch_argv
        assert "legacy_avd" in launch_argv

    def test_emulator_boot_mocks_when_neither_launcher_present(
        self, tmp_path, monkeypatch,
    ):
        """Boot phase on a sandbox/CI runner with neither ``android``
        nor ``emulator``: must degrade to ``status="mock"`` rather than
        raising. The orchestrator gate roll-up still passes because
        mocks are non-blocking — that's the contract that lets the
        whole P2 simulate-track green up on a Linux CI runner without
        SDK + NDK + AVD images."""
        _patch_path(monkeypatch, android=False, legacy_emulator=False, adb=False)
        report = ms.boot_android_emulator()
        assert report.status == "mock"
        assert "android CLI / emulator / adb" in report.detail

    def test_full_chain_orchestrator_passes_on_legacy_path(
        self, tmp_path, monkeypatch,
    ):
        """Full chain on the absent-CLI host: ``simulate_mobile`` rolls
        up to ``overall_pass=True`` via the legacy emulator + Gradle
        path. The roll-up must match the CLI path's structure (same
        gate keys, same envelope shape) — operators reading the JSON
        envelope shouldn't be able to tell which path ran without
        looking at ``emulator_detail``."""
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        _record_subprocess(monkeypatch)
        root = _make_android_project(tmp_path)

        result = ms.simulate_mobile(
            profile="android-arm64-v8a",
            app_path=root,
        )

        assert result.mobile_platform == "android"
        assert result.mobile_abi == "arm64-v8a"
        assert result.emulator.status == "booted"
        assert result.emulator.detail.startswith("legacy-emulator:")
        assert result.smoke.status == "pass"
        assert result.overall_pass() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-path equivalence — two paths, same envelope shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrossPathEquivalence:
    """Asserts the two paths' externally-visible envelope shapes match —
    the artifact, gate roll-up, and JSON keys are identical; only the
    ``*_detail`` fields and the resolved argv differ. This is what
    lets operator dashboards bind on stable keys without branching on
    "did the CLI run or not"."""

    def _run(self, tmp_path, monkeypatch, *, cli_present: bool) -> ms.MobileSimResult:
        _patch_path(
            monkeypatch,
            android=cli_present,
            legacy_emulator=not cli_present,
            adb=True,
        )
        _record_subprocess(monkeypatch)
        root = _make_android_project(tmp_path)
        return ms.simulate_mobile(profile="android-arm64-v8a", app_path=root)

    def test_both_paths_produce_same_gate_keys(self, tmp_path, monkeypatch):
        cli = self._run(tmp_path / "cli", monkeypatch, cli_present=True)
        leg = self._run(tmp_path / "leg", monkeypatch, cli_present=False)
        assert set(cli.gates.keys()) == set(leg.gates.keys())

    def test_both_paths_pass_overall(self, tmp_path, monkeypatch):
        cli = self._run(tmp_path / "cli", monkeypatch, cli_present=True)
        leg = self._run(tmp_path / "leg", monkeypatch, cli_present=False)
        assert cli.overall_pass() is True
        assert leg.overall_pass() is True

    def test_envelope_distinguishes_paths_via_detail_only(
        self, tmp_path, monkeypatch,
    ):
        """The path is observable in ``emulator.detail`` (audit trail
        for postmortems) but the structured fields downstream tools key
        on (``status``, ``kind``, ``device_model``) are identical
        between paths."""
        cli = self._run(tmp_path / "cli", monkeypatch, cli_present=True)
        leg = self._run(tmp_path / "leg", monkeypatch, cli_present=False)
        assert cli.emulator.kind == leg.emulator.kind == "avd"
        assert cli.emulator.status == leg.emulator.status == "booted"
        assert cli.emulator.detail.startswith("android-cli:")
        assert leg.emulator.detail.startswith("legacy-emulator:")

    def test_result_to_json_shape_identical_across_paths(
        self, tmp_path, monkeypatch,
    ):
        """The ``simulate.sh`` consumer reads keys from the JSON
        envelope; the two paths must emit the same key set so the shell
        layer doesn't need to branch on the underlying toolchain."""
        cli = self._run(tmp_path / "cli", monkeypatch, cli_present=True)
        leg = self._run(tmp_path / "leg", monkeypatch, cli_present=False)
        assert set(ms.result_to_json(cli).keys()) == set(ms.result_to_json(leg).keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mid-session install — fallback contract regression gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMidSessionInstall:
    """Operator runs ``scripts/install_android_cli.sh`` mid-session;
    the very next dispatcher / boot call must pick up the new fast
    path. Counter-example: a process that cached ``shutil.which``
    would keep running gradle-wrapper / legacy-emulator until restart,
    silently negating the install."""

    def test_dispatcher_switches_path_on_next_call(self, tmp_path, monkeypatch):
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        first = mt.resolve_android_invocation("run", tmp_path)
        assert first.path_kind == "gradle-wrapper"

        # Operator just ran the install script — `android` now on PATH.
        _patch_path(monkeypatch, android=True, legacy_emulator=True, adb=True)
        second = mt.resolve_android_invocation("run", tmp_path)
        assert second.path_kind == "android-cli"

    def test_boot_helper_switches_path_on_next_call(self, tmp_path, monkeypatch):
        _patch_path(monkeypatch, android=False, legacy_emulator=True, adb=True)
        recorded = _record_subprocess(monkeypatch)
        first = ms.boot_android_emulator(avd_name="x")
        assert first.detail.startswith("legacy-emulator:")
        assert recorded["popen"][-1][0] == _LEGACY_EMU_BIN

        _patch_path(monkeypatch, android=True, legacy_emulator=True, adb=True)
        second = ms.boot_android_emulator(avd_name="x")
        assert second.detail.startswith("android-cli:")
        assert recorded["popen"][-1][0] == _ANDROID_BIN
