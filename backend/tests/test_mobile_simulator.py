"""P2 #287 — unit tests for `backend.mobile_simulator`.

Exercises the Python library independently of the shell layer. Integration
with `scripts/simulate.sh --type=mobile` is covered by
`test_mobile_simulate.py`. Everything here runs pure pytest with temp
dirs — no network, no external binaries required. External CLIs
(`xcrun`, `adb`, `gradle`, `fastlane`, …) are stubbed via monkeypatch
against `shutil.which` so the tests are deterministic regardless of
host OS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import mobile_simulator as ms


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _flutter_fixture(tmp_path: Path) -> Path:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    return tmp_path


def _rn_fixture(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "rn", "dependencies": {"react-native": "0.76.0"}})
    )
    return tmp_path


def _ios_native_fixture(tmp_path: Path) -> Path:
    (tmp_path / "MyApp.xcodeproj").mkdir()
    return tmp_path


def _android_native_fixture(tmp_path: Path) -> Path:
    (tmp_path / "build.gradle").write_text("// android project\n")
    return tmp_path


def _stub_which(monkeypatch, mapping: dict[str, str | None]) -> None:
    """Replace shutil.which with a lookup over ``mapping``.

    ``shutil.which(name) → mapping.get(name)``; missing keys return
    ``None`` so callers can flip individual CLIs on/off deterministically.
    """
    monkeypatch.setattr(ms.shutil, "which", lambda name: mapping.get(name))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  resolve_ui_framework
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveUIFramework:
    def test_flutter_wins_over_native_subdirs(self, tmp_path):
        root = _flutter_fixture(tmp_path)
        # Also drop a native android shell — flutter still wins.
        (root / "android").mkdir()
        (root / "android" / "build.gradle").write_text("")
        assert ms.resolve_ui_framework(root) == "flutter"

    def test_react_native_detected_via_package_json(self, tmp_path):
        assert ms.resolve_ui_framework(_rn_fixture(tmp_path)) == "react-native"

    def test_ios_xcodeproj_detected(self, tmp_path):
        assert ms.resolve_ui_framework(_ios_native_fixture(tmp_path)) == "xcuitest"

    def test_android_build_gradle_detected(self, tmp_path):
        assert ms.resolve_ui_framework(_android_native_fixture(tmp_path)) == "espresso"

    def test_platform_hint_fallback_ios(self, tmp_path):
        assert ms.resolve_ui_framework(tmp_path, mobile_platform="ios") == "xcuitest"

    def test_platform_hint_fallback_android(self, tmp_path):
        assert ms.resolve_ui_framework(tmp_path, mobile_platform="android") == "espresso"

    def test_empty_dir_and_no_hint_returns_empty(self, tmp_path):
        assert ms.resolve_ui_framework(tmp_path) == ""

    def test_missing_dir_returns_empty(self, tmp_path):
        assert ms.resolve_ui_framework(tmp_path / "nope") == ""

    def test_malformed_package_json_does_not_raise(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json}")
        (tmp_path / "build.gradle").write_text("")
        assert ms.resolve_ui_framework(tmp_path) == "espresso"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Emulator / simulator boot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmulatorBoot:
    def test_ios_mock_when_xcrun_absent(self, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.boot_ios_simulator(device_model="iPhone 15 Pro")
        assert report.status == "mock"
        assert report.kind == "simulator"
        assert report.device_model == "iPhone 15 Pro"

    def test_ios_env_overrides_defaults(self, monkeypatch):
        _stub_which(monkeypatch, {})
        env = {
            "OMNISIGHT_IOS_SIM_UDID": "udid-123",
            "OMNISIGHT_IOS_SIM_DEVICE": "iPhone SE",
            "OMNISIGHT_IOS_SIM_RUNTIME": "com.apple.CoreSimulator.SimRuntime.iOS-16-0",
        }
        report = ms.boot_ios_simulator(env=env)
        assert report.udid == "udid-123"
        assert report.device_model == "iPhone SE"
        assert "iOS-16-0" in report.runtime

    def test_android_mock_when_emulator_absent(self, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.boot_android_emulator()
        assert report.status == "mock"
        assert report.kind == "avd"

    def test_android_env_overrides(self, monkeypatch):
        _stub_which(monkeypatch, {})
        env = {
            "OMNISIGHT_ANDROID_AVD_NAME": "myavd",
            "OMNISIGHT_ANDROID_API_LEVEL": "33",
        }
        report = ms.boot_android_emulator(env=env)
        assert report.device_model == "myavd"
        assert "33" in report.runtime


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Smoke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSmoke:
    def test_mock_when_emulator_mocked(self, tmp_path):
        emu = ms.EmulatorBootReport(status="mock", detail="no xcrun")
        report = ms.run_smoke(mobile_platform="ios", app_path=tmp_path, emulator=emu)
        assert report.status == "mock"
        assert report.launched is False

    def test_ios_pass_when_app_bundle_present(self, tmp_path):
        (tmp_path / "build" / "ios").mkdir(parents=True)
        (tmp_path / "build" / "ios" / "MyApp.app").mkdir()
        emu = ms.EmulatorBootReport(status="booted", kind="simulator")
        report = ms.run_smoke(mobile_platform="ios", app_path=tmp_path, emulator=emu)
        assert report.status == "pass"
        assert report.launched is True

    def test_android_pass_when_apk_present(self, tmp_path):
        (tmp_path / "app.apk").write_bytes(b"PK\x03\x04")
        emu = ms.EmulatorBootReport(status="booted", kind="avd")
        report = ms.run_smoke(mobile_platform="android", app_path=tmp_path, emulator=emu)
        assert report.status == "pass"
        assert report.launched is True

    def test_android_pass_when_aab_present(self, tmp_path):
        (tmp_path / "app-release.aab").write_bytes(b"PK")
        emu = ms.EmulatorBootReport(status="booted", kind="avd")
        report = ms.run_smoke(mobile_platform="android", app_path=tmp_path, emulator=emu)
        assert report.status == "pass"

    def test_android_skip_when_no_artifact(self, tmp_path):
        emu = ms.EmulatorBootReport(status="booted", kind="avd")
        report = ms.run_smoke(mobile_platform="android", app_path=tmp_path, emulator=emu)
        assert report.status == "skip"

    def test_unknown_platform_skip(self, tmp_path):
        emu = ms.EmulatorBootReport(status="booted")
        report = ms.run_smoke(mobile_platform="windows", app_path=tmp_path, emulator=emu)
        assert report.status == "skip"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI test runners (mock path only — real CLI invocation out of scope)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUIRunnersMockPath:
    def test_xcuitest_mock_when_xcodebuild_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_xcuitest(tmp_path)
        assert report.status == "mock"
        assert report.framework == "xcuitest"

    def test_espresso_mock_when_gradle_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_espresso(tmp_path)
        assert report.status == "mock"
        assert report.framework == "espresso"

    def test_flutter_mock_when_flutter_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_flutter_tests(tmp_path)
        assert report.status == "mock"
        assert report.framework == "flutter"

    def test_rn_mock_when_npm_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_rn_tests(tmp_path)
        assert report.status == "mock"
        assert report.framework == "react-native"


class TestXcodeBuildCountParser:
    def test_counts_passed_and_failed(self):
        output = (
            "Test Case '-[FooUITests testA]' passed (0.123 seconds).\n"
            "Test Case '-[FooUITests testB]' passed (0.111 seconds).\n"
            "Test Case '-[FooUITests testC]' failed (0.045 seconds).\n"
        )
        passed, failed = ms._parse_xcodebuild_counts(output)
        assert passed == 2
        assert failed == 1

    def test_empty_output_zero(self):
        assert ms._parse_xcodebuild_counts("") == (0, 0)


class TestGradleCountParser:
    def test_summary_line_parsed(self):
        output = "some junk\nTests: 10, Failures: 2, Errors: 1, Skipped: 0\nend\n"
        passed, failed = ms._parse_gradle_test_counts(output)
        assert failed == 3
        assert passed == 7  # 10 - 3

    def test_absent_summary_zero(self):
        assert ms._parse_gradle_test_counts("no tests here") == (0, 0)


class TestFlutterJsonParser:
    def test_counts_testDone_events(self):
        lines = [
            json.dumps({"type": "start"}),
            json.dumps({"type": "testDone", "result": "success"}),
            json.dumps({"type": "testDone", "result": "success", "hidden": True}),
            json.dumps({"type": "testDone", "result": "failure"}),
        ]
        passed, failed = ms._parse_flutter_test_json("\n".join(lines))
        assert passed == 1
        assert failed == 1

    def test_malformed_lines_skipped(self):
        passed, failed = ms._parse_flutter_test_json("not-json\n" + json.dumps(
            {"type": "testDone", "result": "success"}
        ))
        assert passed == 1
        assert failed == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Device farms
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeviceFarm:
    def test_skip_when_no_farm(self, tmp_path):
        report = ms.run_device_farm("", app_path=tmp_path, mobile_platform="android")
        assert report.status == "skip"
        assert report.argv == []

    def test_unknown_farm_raises(self, tmp_path):
        with pytest.raises(ms.UnknownDeviceFarmError):
            ms.run_device_farm("saucelabs", app_path=tmp_path, mobile_platform="android")

    def test_firebase_argv_has_no_secret_values(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {"gcloud": "/usr/bin/gcloud"})
        report = ms.run_device_farm("firebase", app_path=tmp_path, mobile_platform="android")
        assert report.status == "delegated"
        assert report.argv[0] == "gcloud"
        assert "GOOGLE_CLOUD_PROJECT" in report.env_forward
        # No secret values should leak into argv
        joined = " ".join(report.argv)
        for secret in ("AIza", "-----BEGIN", "Bearer "):
            assert secret not in joined

    def test_firebase_mock_when_cli_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_device_farm("firebase", app_path=tmp_path, mobile_platform="ios")
        assert report.status == "mock"
        assert report.farm == "firebase"
        assert "gcloud" in report.detail

    def test_aws_delegates_when_cli_present(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {"aws": "/usr/local/bin/aws"})
        report = ms.run_device_farm("aws", app_path=tmp_path, mobile_platform="android")
        assert report.status == "delegated"
        assert report.argv[0] == "aws"
        assert "AWS_ACCESS_KEY_ID" in report.env_forward

    def test_browserstack_delegates_when_cli_present(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {"browserstack-local": "/usr/local/bin/browserstack-local"})
        report = ms.run_device_farm("browserstack", app_path=tmp_path, mobile_platform="ios")
        assert report.status == "delegated"
        assert report.argv[0] == "browserstack-local"
        assert "BROWSERSTACK_ACCESS_KEY" in report.env_forward

    def test_ios_xctest_type_for_firebase(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {"gcloud": "/usr/bin/gcloud"})
        report = ms.run_device_farm("firebase", app_path=tmp_path, mobile_platform="ios")
        assert any("xctest" in a for a in report.argv)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Screenshot matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScreenshotMatrix:
    def test_skip_empty_matrix(self, tmp_path):
        report = ms.run_screenshot_matrix(
            app_path=tmp_path, mobile_platform="ios", devices=[], locales=[],
        )
        assert report.status == "skip"
        assert report.captured == 0

    def test_skip_when_locales_empty(self, tmp_path):
        report = ms.run_screenshot_matrix(
            app_path=tmp_path, mobile_platform="ios",
            devices=["iPhone 15 Pro"], locales=[],
        )
        assert report.status == "skip"

    def test_mock_when_fastlane_absent(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        report = ms.run_screenshot_matrix(
            app_path=tmp_path, mobile_platform="ios",
            devices=["iPhone 15 Pro", "iPhone 14"],
            locales=["en-US", "zh-TW"],
        )
        assert report.status == "mock"
        # Matrix recorded even though not executed
        assert report.devices == ["iPhone 15 Pro", "iPhone 14"]
        assert report.locales == ["en-US", "zh-TW"]
        assert report.captured == 0  # mock never captures

    def test_parse_csv_list(self):
        assert ms._parse_csv_list("a, b,c ,, d") == ["a", "b", "c", "d"]
        assert ms._parse_csv_list("") == []
        assert ms._parse_csv_list("   ") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulateMobileOrchestrator:
    def test_android_profile_mock_sandbox(self, tmp_path, monkeypatch):
        """End-to-end on a Linux sandbox: every external tool mocked out,
        result must be a valid envelope with overall_pass=True because
        mocks count as non-blocking."""
        _stub_which(monkeypatch, {})
        result = ms.simulate_mobile(
            profile="android-arm64-v8a", app_path=tmp_path,
        )
        assert result.mobile_platform == "android"
        assert result.mobile_abi == "arm64-v8a"
        assert result.ui_framework in ("espresso", "")  # empty dir → platform fallback
        assert result.emulator.status == "mock"
        assert result.device_farm.status == "skip"  # no --farm
        assert result.overall_pass() is True

    def test_ios_profile_with_farm_and_matrix(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {"gcloud": "/usr/bin/gcloud"})
        result = ms.simulate_mobile(
            profile="ios-simulator", app_path=tmp_path,
            farm="firebase",
            devices=["iPhone 15 Pro"], locales=["en-US", "zh-TW"],
        )
        assert result.mobile_platform == "ios"
        assert result.ui_framework == "xcuitest"
        assert result.device_farm.farm == "firebase"
        assert result.device_farm.status == "delegated"
        assert result.screenshot_matrix.devices == ["iPhone 15 Pro"]
        assert result.screenshot_matrix.locales == ["en-US", "zh-TW"]

    def test_unknown_farm_records_error(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        result = ms.simulate_mobile(
            profile="android-arm64-v8a", app_path=tmp_path, farm="saucelabs",
        )
        # Orchestrator catches UnknownDeviceFarmError and records it
        assert any("saucelabs" in e for e in result.errors)
        assert result.device_farm.status == "fail"
        assert result.overall_pass() is False

    def test_result_to_json_shape(self, tmp_path, monkeypatch):
        _stub_which(monkeypatch, {})
        result = ms.simulate_mobile(
            profile="android-arm64-v8a", app_path=tmp_path,
        )
        payload = ms.result_to_json(result)
        required_keys = {
            "profile", "app_path", "mobile_platform", "mobile_abi",
            "ui_framework", "emulator_status", "smoke_status",
            "ui_test_status", "device_farm_status",
            "screenshot_matrix_status", "gates", "overall_pass", "errors",
        }
        assert required_keys.issubset(payload.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCli:
    def test_cli_emits_single_json_line(self, tmp_path, monkeypatch, capsys):
        _stub_which(monkeypatch, {})
        monkeypatch.setattr("sys.argv", [
            "backend.mobile_simulator",
            "--profile", "android-arm64-v8a",
            "--app-path", str(tmp_path),
        ])
        rc = ms._cli_main()
        assert rc == 0
        out = capsys.readouterr().out.strip()
        # Single JSON line, parseable
        data = json.loads(out)
        assert data["profile"] == "android-arm64-v8a"
        assert data["mobile_platform"] == "android"
        assert "gates" in data
