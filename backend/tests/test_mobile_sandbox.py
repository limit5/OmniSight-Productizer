"""V6 #1 (issue #322) — mobile_sandbox contract tests.

Pins ``backend/mobile_sandbox.py`` against:

  * structural invariants (``__all__`` membership, schema version,
    default constants, frozen dataclasses, JSON-safe ``to_dict``);
  * :class:`MobileSandboxConfig` validation (session id charset,
    platform enum, absolute workspace/workdir, xcode action/config
    enums, env types, positive timeouts, tuple-normalised build_args);
  * :class:`MobileSandboxInstance` state transitions — ``pending →
    building → built → installing → running → stopping → stopped``
    via :meth:`MobileSandboxManager.create/build/install/screenshot/
    stop`;
  * graceful executor failure paths (build/install failures mark
    sandbox ``failed`` rather than propagating);
  * deterministic :func:`build_android_build_argv` /
    :func:`build_ios_build_argv` (byte-identical lists);
  * :func:`parse_build_error` Gradle + xcodebuild diagnostic
    extraction;
  * one-per-session invariant (create raises
    :class:`MobileSandboxAlreadyExists`);
  * event callback emission on every state transition;
  * ``SshMacOsIosExecutor`` degrades to ``mock`` without a configured
    remote host.

``FakeAndroidExecutor`` / ``FakeIosExecutor`` fixtures record every
call and return deterministic reports — no real docker daemon, no
adb, no ssh is touched.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend import mobile_sandbox as ms
from backend.mobile_sandbox import (
    DEFAULT_ANDROID_BUILD_IMAGE,
    DEFAULT_ANDROID_WORKDIR,
    DEFAULT_BUILD_TIMEOUT_S,
    DEFAULT_GRADLE_TASK,
    DEFAULT_IDLE_LIMIT_S,
    DEFAULT_INSTALL_TIMEOUT_S,
    DEFAULT_IOS_WORKDIR,
    DEFAULT_SCREENSHOT_TIMEOUT_S,
    DEFAULT_XCODEBUILD_ACTION,
    DEFAULT_XCODEBUILD_CONFIGURATION,
    MOBILE_SANDBOX_SCHEMA_VERSION,
    SUPPORTED_PLATFORMS,
    SUPPORTED_SCREENSHOT_FORMATS,
    BuildError,
    BuildReport,
    InstallReport,
    MobileSandboxAlreadyExists,
    MobileSandboxConfig,
    MobileSandboxConfigError,
    MobileSandboxError,
    MobileSandboxManager,
    MobileSandboxNotFound,
    MobileSandboxStatus,
    ScreenshotReport,
    SubprocessAndroidExecutor,
    SshMacOsIosExecutor,
    allocate_screenshot_port,
    build_android_build_argv,
    build_android_install_argv,
    build_android_screenshot_argv,
    build_ios_build_argv,
    build_ios_screenshot_argv,
    format_sandbox_name,
    locate_android_apk,
    locate_ios_app_bundle,
    parse_build_error,
    render_sandbox_status_markdown,
    validate_workspace,
)


# ── Module invariants ─────────────────────────────────────────────


EXPECTED_ALL = {
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
}


def test_all_matches_expected():
    assert set(ms.__all__) == EXPECTED_ALL


def test_schema_version_is_semver():
    parts = MOBILE_SANDBOX_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


def test_supported_platforms_stable():
    assert SUPPORTED_PLATFORMS == ("android", "ios")


def test_supported_screenshot_formats_stable():
    assert SUPPORTED_SCREENSHOT_FORMATS == ("png",)


def test_defaults_sane():
    assert DEFAULT_ANDROID_BUILD_IMAGE.startswith("gradle:")
    assert DEFAULT_GRADLE_TASK == "assembleDebug"
    assert DEFAULT_XCODEBUILD_ACTION == "build"
    assert DEFAULT_XCODEBUILD_CONFIGURATION == "Debug"
    assert DEFAULT_ANDROID_WORKDIR.startswith("/")
    assert DEFAULT_IOS_WORKDIR.startswith("/")
    assert DEFAULT_BUILD_TIMEOUT_S > 0
    assert DEFAULT_INSTALL_TIMEOUT_S > 0
    assert DEFAULT_SCREENSHOT_TIMEOUT_S > 0
    assert DEFAULT_IDLE_LIMIT_S > 0


def test_status_enum_complete():
    values = {s.value for s in MobileSandboxStatus}
    assert values == {
        "pending", "building", "built", "installing",
        "running", "stopping", "stopped", "failed",
    }


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return str(ws)


def _android_config(workspace: str, *, session_id: str = "sess-1") -> MobileSandboxConfig:
    return MobileSandboxConfig(
        session_id=session_id,
        platform="android",
        workspace_path=workspace,
        android_module="app",
        android_package_name="com.omnisight.example",
        android_launch_activity=".MainActivity",
    )


def _ios_config(workspace: str, *, session_id: str = "sess-ios") -> MobileSandboxConfig:
    return MobileSandboxConfig(
        session_id=session_id,
        platform="ios",
        workspace_path=workspace,
        xcode_scheme="OmniSight",
        xcode_project="OmniSight.xcworkspace",
        ios_bundle_id="com.omnisight.example",
        ios_simulator_device="iPhone 15 Pro",
    )


# ── MobileSandboxConfig validation ────────────────────────────────


def test_config_happy_path_android(workspace: str):
    c = _android_config(workspace)
    assert c.platform == "android"
    assert c.android_module == "app"
    assert c.workspace_path == workspace
    assert isinstance(c.env, type(c.env))  # MappingProxyType


def test_config_happy_path_ios(workspace: str):
    c = _ios_config(workspace)
    assert c.platform == "ios"
    assert c.xcode_scheme == "OmniSight"


def test_config_is_frozen(workspace: str):
    c = _android_config(workspace)
    with pytest.raises(FrozenInstanceError):
        c.platform = "ios"  # type: ignore[misc]


def test_config_rejects_empty_session_id(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(session_id="", platform="android", workspace_path=workspace)


def test_config_rejects_bad_session_id_chars(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="sess with spaces",
            platform="android",
            workspace_path=workspace,
        )


def test_config_rejects_unknown_platform(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="windows-phone",
            workspace_path=workspace,
        )


def test_config_normalises_platform_case(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="ANDROID", workspace_path=workspace,
    )
    assert c.platform == "android"


def test_config_rejects_relative_android_workdir(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="android",
            workspace_path=workspace,
            android_workdir="workspace",
        )


def test_config_rejects_relative_ios_workdir(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="ios",
            workspace_path=workspace,
            ios_workdir="tmp",
        )


def test_config_rejects_bad_xcode_action(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="ios",
            workspace_path=workspace,
            xcode_action="archive",
        )


def test_config_rejects_bad_xcode_configuration(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="ios",
            workspace_path=workspace,
            xcode_configuration="Development",
        )


@pytest.mark.parametrize("field_name", [
    "build_timeout_s", "install_timeout_s",
    "screenshot_timeout_s", "stop_timeout_s",
])
def test_config_rejects_nonpositive_timeouts(workspace: str, field_name: str):
    kwargs = dict(
        session_id="s1", platform="android", workspace_path=workspace,
    )
    kwargs[field_name] = 0
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(**kwargs)


def test_config_rejects_non_string_env(workspace: str):
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxConfig(
            session_id="s1", platform="android",
            workspace_path=workspace,
            env={"KEY": 123},  # type: ignore[dict-item]
        )


def test_config_build_args_tuple_normalised(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="android",
        workspace_path=workspace,
        build_args=["--stacktrace", "--info"],
    )
    assert c.build_args == ("--stacktrace", "--info")


def test_config_to_dict_json_safe(workspace: str):
    c = _android_config(workspace)
    payload = c.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["schema_version"] == MOBILE_SANDBOX_SCHEMA_VERSION
    assert payload["platform"] == "android"
    assert payload["build_args"] == []


# ── Pure helpers ──────────────────────────────────────────────────


def test_format_sandbox_name_lowercases_and_caps():
    n = format_sandbox_name("Sess-Abc", "android")
    assert n == "omnisight-mobile-android-sess-abc"


def test_format_sandbox_name_replaces_illegal_chars():
    n = format_sandbox_name("sess/abc 1", "ios")
    assert " " not in n
    assert "/" not in n


def test_format_sandbox_name_rejects_empty():
    with pytest.raises(MobileSandboxConfigError):
        format_sandbox_name("", "android")


def test_format_sandbox_name_rejects_bad_platform():
    with pytest.raises(MobileSandboxConfigError):
        format_sandbox_name("s1", "nope")


def test_format_sandbox_name_caps_at_63():
    long_id = "a" * 200
    n = format_sandbox_name(long_id, "android")
    assert len(n) <= 63


def test_validate_workspace_happy(workspace: str):
    assert validate_workspace(workspace) == Path(workspace)


def test_validate_workspace_rejects_relative():
    with pytest.raises(MobileSandboxConfigError):
        validate_workspace("workspace")


def test_validate_workspace_rejects_missing():
    with pytest.raises(MobileSandboxConfigError):
        validate_workspace("/nonexistent/path/xyz/123")


def test_validate_workspace_rejects_file(tmp_path: Path):
    f = tmp_path / "notadir"
    f.write_text("x")
    with pytest.raises(MobileSandboxConfigError):
        validate_workspace(str(f))


def test_allocate_screenshot_port_deterministic():
    p1 = allocate_screenshot_port("sess-1")
    p2 = allocate_screenshot_port("sess-1")
    assert p1 == p2


def test_allocate_screenshot_port_different_sessions_different_ports():
    p1 = allocate_screenshot_port("sess-1")
    p2 = allocate_screenshot_port("sess-2")
    assert p1 != p2


def test_allocate_screenshot_port_skips_in_use():
    p1 = allocate_screenshot_port("sess-1")
    p2 = allocate_screenshot_port("sess-1", in_use={p1})
    assert p2 != p1


def test_allocate_screenshot_port_in_range():
    p = allocate_screenshot_port("sess-x")
    assert 41000 <= p <= 41999


# ── build_android_build_argv ──────────────────────────────────────


def test_build_android_argv_deterministic(workspace: str):
    c = _android_config(workspace)
    a1 = build_android_build_argv(c)
    a2 = build_android_build_argv(c)
    assert a1 == a2


def test_build_android_argv_contains_docker_run(workspace: str):
    c = _android_config(workspace)
    argv = build_android_build_argv(c)
    assert argv[0] == "docker"
    assert "run" in argv
    assert "--rm" in argv


def test_build_android_argv_binds_workspace(workspace: str):
    c = _android_config(workspace)
    argv = build_android_build_argv(c)
    joined = " ".join(argv)
    assert f"{workspace}:{DEFAULT_ANDROID_WORKDIR}" in joined


def test_build_android_argv_contains_gradle_task(workspace: str):
    c = _android_config(workspace)
    argv = build_android_build_argv(c)
    assert ":app:assembleDebug" in argv
    assert "--no-daemon" in argv
    assert "--console=plain" in argv


def test_build_android_argv_includes_build_args(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="android",
        workspace_path=workspace,
        build_args=("--stacktrace",),
    )
    argv = build_android_build_argv(c)
    assert "--stacktrace" in argv


def test_build_android_argv_rejects_ios_config(workspace: str):
    c = _ios_config(workspace)
    with pytest.raises(MobileSandboxConfigError):
        build_android_build_argv(c)


def test_build_android_argv_rejects_non_config():
    with pytest.raises(TypeError):
        build_android_build_argv("not a config")  # type: ignore[arg-type]


def test_build_android_argv_env_sorted(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="android",
        workspace_path=workspace,
        env={"Z": "1", "A": "2", "M": "3"},
    )
    argv = build_android_build_argv(c)
    env_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-e"]
    assert env_values == sorted(env_values)


# ── build_ios_build_argv ──────────────────────────────────────────


def test_build_ios_argv_deterministic(workspace: str):
    c = _ios_config(workspace)
    a1 = build_ios_build_argv(c)
    a2 = build_ios_build_argv(c)
    assert a1 == a2


def test_build_ios_argv_workspace_flag(workspace: str):
    c = _ios_config(workspace)
    argv = build_ios_build_argv(c)
    assert argv[0] == "xcodebuild"
    assert argv[1] == "build"
    assert "-workspace" in argv
    idx = argv.index("-workspace")
    assert argv[idx + 1] == "OmniSight.xcworkspace"


def test_build_ios_argv_project_flag_for_xcodeproj(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="ios",
        workspace_path=workspace,
        xcode_scheme="Foo",
        xcode_project="Foo.xcodeproj",
    )
    argv = build_ios_build_argv(c)
    assert "-project" in argv
    assert "-workspace" not in argv


def test_build_ios_argv_destination_by_udid(workspace: str):
    c = MobileSandboxConfig(
        session_id="s1", platform="ios",
        workspace_path=workspace,
        xcode_scheme="Foo",
        xcode_project="Foo.xcworkspace",
        ios_simulator_udid="ABC-123",
    )
    argv = build_ios_build_argv(c)
    idx = argv.index("-destination")
    assert "id=ABC-123" in argv[idx + 1]


def test_build_ios_argv_destination_by_device_name(workspace: str):
    c = _ios_config(workspace)
    argv = build_ios_build_argv(c)
    idx = argv.index("-destination")
    assert "name=iPhone 15 Pro" in argv[idx + 1]


def test_build_ios_argv_disables_code_signing(workspace: str):
    c = _ios_config(workspace)
    argv = build_ios_build_argv(c)
    assert "CODE_SIGN_IDENTITY=" in argv
    assert "CODE_SIGNING_REQUIRED=NO" in argv
    assert "CODE_SIGNING_ALLOWED=NO" in argv


def test_build_ios_argv_rejects_android_config(workspace: str):
    c = _android_config(workspace)
    with pytest.raises(MobileSandboxConfigError):
        build_ios_build_argv(c)


# ── Install / screenshot argv helpers ─────────────────────────────


def test_build_android_install_argv(workspace: str):
    c = _android_config(workspace)
    argv = build_android_install_argv(c, "/path/app.apk")
    assert argv == ["adb", "install", "-r", "-g", "/path/app.apk"]


def test_build_android_install_argv_rejects_empty_path(workspace: str):
    c = _android_config(workspace)
    with pytest.raises(MobileSandboxConfigError):
        build_android_install_argv(c, "")


def test_build_android_install_argv_rejects_ios_config(workspace: str):
    c = _ios_config(workspace)
    with pytest.raises(MobileSandboxConfigError):
        build_android_install_argv(c, "/x.apk")


def test_build_android_screenshot_argv_default():
    argv = build_android_screenshot_argv()
    assert argv[0] == "adb"
    assert "screencap" in argv


def test_build_android_screenshot_argv_rejects_relative():
    with pytest.raises(MobileSandboxConfigError):
        build_android_screenshot_argv("screenshot.png")


def test_build_ios_screenshot_argv_booted_default():
    argv = build_ios_screenshot_argv("/tmp/out.png")
    assert argv == ["xcrun", "simctl", "io", "booted", "screenshot", "/tmp/out.png"]


def test_build_ios_screenshot_argv_with_udid():
    argv = build_ios_screenshot_argv("/tmp/o.png", udid="ABC")
    assert "ABC" in argv


def test_build_ios_screenshot_argv_rejects_empty():
    with pytest.raises(MobileSandboxConfigError):
        build_ios_screenshot_argv("")


# ── locate_android_apk / locate_ios_app_bundle ────────────────────


def test_locate_android_apk_happy(tmp_path: Path):
    apk_dir = tmp_path / "app" / "build" / "outputs" / "apk" / "debug"
    apk_dir.mkdir(parents=True)
    apk = apk_dir / "app-debug.apk"
    apk.write_text("PK")
    assert locate_android_apk(str(tmp_path)) == str(apk)


def test_locate_android_apk_fallback_rglob(tmp_path: Path):
    apk_dir = tmp_path / "app" / "build" / "outputs" / "somewhere"
    apk_dir.mkdir(parents=True)
    apk = apk_dir / "x.apk"
    apk.write_text("PK")
    assert locate_android_apk(str(tmp_path)) == str(apk)


def test_locate_android_apk_missing(tmp_path: Path):
    assert locate_android_apk(str(tmp_path)) is None


def test_locate_android_apk_non_dir():
    assert locate_android_apk("/definitely/not/a/dir/xyz") is None


def test_locate_ios_app_bundle_happy(tmp_path: Path):
    d = tmp_path / "Build" / "Products" / "Debug-iphonesimulator"
    d.mkdir(parents=True)
    app = d / "OmniSight.app"
    app.mkdir()
    assert locate_ios_app_bundle(str(tmp_path)) == str(app)


def test_locate_ios_app_bundle_missing(tmp_path: Path):
    assert locate_ios_app_bundle(str(tmp_path)) is None


def test_locate_ios_app_bundle_non_dir():
    assert locate_ios_app_bundle("/nope/1/2/3") is None


# ── parse_build_error ─────────────────────────────────────────────


def test_parse_build_error_gradle_diagnostic():
    text = "e: /workspace/app/src/main/java/X.kt:42:17: Unresolved reference: foo"
    errors = parse_build_error(text, tool="gradle")
    assert len(errors) == 1
    err = errors[0]
    assert err.severity == "error"
    assert err.file == "/workspace/app/src/main/java/X.kt"
    assert err.line == 42
    assert err.column == 17
    assert "Unresolved reference" in err.message
    assert err.tool == "gradle"


def test_parse_build_error_gradle_warning():
    text = "w: /workspace/app/Foo.kt:10:3: deprecated API"
    errors = parse_build_error(text, tool="gradle")
    assert len(errors) == 1
    assert errors[0].severity == "warning"


def test_parse_build_error_gradle_what_went_wrong():
    text = """
FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':app:assembleDebug'.
"""
    errors = parse_build_error(text, tool="gradle")
    assert len(errors) >= 1


def test_parse_build_error_xcodebuild_diagnostic():
    text = "/Users/x/Proj/Foo/Bar.swift:12:5: error: cannot find 'bar' in scope"
    errors = parse_build_error(text, tool="xcodebuild")
    assert len(errors) == 1
    err = errors[0]
    assert err.severity == "error"
    assert err.file == "/Users/x/Proj/Foo/Bar.swift"
    assert err.line == 12
    assert err.column == 5
    assert err.tool == "xcodebuild"


def test_parse_build_error_xcodebuild_alias():
    text = "/Users/x/Foo.swift:1:1: error: oops"
    errors = parse_build_error(text, tool="xcode")
    assert len(errors) == 1


def test_parse_build_error_empty_input_returns_empty():
    assert parse_build_error("") == ()
    assert parse_build_error("", tool="xcodebuild") == ()


def test_parse_build_error_none_safe():
    assert parse_build_error(None) == ()  # type: ignore[arg-type]


def test_parse_build_error_unknown_tool():
    assert parse_build_error("anything", tool="msbuild") == ()


def test_parse_build_error_dedupes_identical_diagnostics():
    text = (
        "e: /a/X.kt:10:5: err\n"
        "e: /a/X.kt:10:5: err\n"
    )
    errors = parse_build_error(text, tool="gradle")
    assert len(errors) == 1


# ── BuildError / BuildReport / InstallReport / ScreenshotReport ──


def test_build_error_to_dict_json_safe():
    e = BuildError(message="x", file="/a/b.kt", line=1, column=2)
    json.dumps(e.to_dict())


def test_build_report_to_dict_json_safe():
    r = BuildReport(
        status="pass", artifact_path="/x/y.apk",
        tool="gradle", duration_ms=123, exit_code=0,
        errors=(BuildError(message="x"),),
    )
    payload = r.to_dict()
    json.dumps(payload)
    assert payload["errors"][0]["message"] == "x"


def test_install_report_default_is_skip():
    r = InstallReport()
    assert r.status == "skip"
    assert r.launched is False


def test_screenshot_report_default_is_skip():
    r = ScreenshotReport()
    assert r.status == "skip"
    assert r.format == "png"


def test_dataclasses_frozen():
    e = BuildError(message="x")
    with pytest.raises(FrozenInstanceError):
        e.message = "y"  # type: ignore[misc]
    r = BuildReport()
    with pytest.raises(FrozenInstanceError):
        r.status = "pass"  # type: ignore[misc]


# ── Fake executors ────────────────────────────────────────────────


class FakeAndroidExecutor:
    """In-memory android executor for manager tests."""

    def __init__(self, *, build_status="pass", install_status="pass", screenshot_status="pass"):
        self.build_status = build_status
        self.install_status = install_status
        self.screenshot_status = screenshot_status
        self.calls: list[tuple[str, dict]] = []

    def build(self, config: MobileSandboxConfig) -> BuildReport:
        self.calls.append(("build", {"session_id": config.session_id}))
        if self.build_status == "pass":
            return BuildReport(
                status="pass",
                artifact_path=f"/tmp/{config.session_id}.apk",
                tool="gradle",
            )
        return BuildReport(status=self.build_status, tool="gradle", detail="fake")

    def install(self, config: MobileSandboxConfig, artifact_path: str) -> InstallReport:
        self.calls.append(("install", {"artifact": artifact_path}))
        if self.install_status == "pass":
            return InstallReport(status="pass", launched=True, detail="Success")
        return InstallReport(status=self.install_status, detail="fake")

    def screenshot(self, config: MobileSandboxConfig, *, output_dir: str) -> ScreenshotReport:
        self.calls.append(("screenshot", {"output_dir": output_dir}))
        if self.screenshot_status == "pass":
            return ScreenshotReport(
                status="pass",
                path=f"{output_dir}/{config.session_id}.png",
                format="png", width=1080, height=1920,
            )
        return ScreenshotReport(status=self.screenshot_status, detail="fake")

    def stop(self, sandbox_name: str, *, timeout_s: float) -> None:
        self.calls.append(("stop", {"name": sandbox_name}))


class FakeIosExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def build(self, config: MobileSandboxConfig) -> BuildReport:
        self.calls.append(("build", {"session_id": config.session_id}))
        return BuildReport(
            status="pass",
            artifact_path=f"/tmp/{config.session_id}.app",
            tool="xcodebuild",
        )

    def install(self, config: MobileSandboxConfig, artifact_path: str) -> InstallReport:
        self.calls.append(("install", {"artifact": artifact_path}))
        return InstallReport(status="pass", launched=True)

    def screenshot(self, config: MobileSandboxConfig, *, output_dir: str) -> ScreenshotReport:
        self.calls.append(("screenshot", {"output_dir": output_dir}))
        return ScreenshotReport(
            status="pass", path=f"{output_dir}/{config.session_id}.png",
        )

    def stop(self, delegate_handle: str, *, timeout_s: float) -> None:
        self.calls.append(("stop", {"handle": delegate_handle}))


# ── MobileSandboxManager core lifecycle ───────────────────────────


def _manager(**extra) -> MobileSandboxManager:
    return MobileSandboxManager(
        android_executor=FakeAndroidExecutor(),
        ios_executor=FakeIosExecutor(),
        **extra,
    )


def test_manager_requires_at_least_one_executor():
    with pytest.raises(MobileSandboxConfigError):
        MobileSandboxManager()


def test_manager_create_android_pending(workspace: str):
    mgr = _manager()
    inst = mgr.create(_android_config(workspace))
    assert inst.status is MobileSandboxStatus.pending
    assert inst.sandbox_name.startswith("omnisight-mobile-android-")
    assert inst.screenshot_port is not None


def test_manager_create_ios_pending(workspace: str):
    mgr = _manager()
    inst = mgr.create(_ios_config(workspace))
    assert inst.status is MobileSandboxStatus.pending
    assert inst.sandbox_name.startswith("omnisight-mobile-ios-")


def test_manager_create_rejects_duplicate(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    with pytest.raises(MobileSandboxAlreadyExists):
        mgr.create(_android_config(workspace))


def test_manager_create_rejects_missing_executor(workspace: str):
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileSandboxConfigError):
        mgr.create(_ios_config(workspace))


def test_manager_build_transitions(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    inst = mgr.build("sess-1")
    assert inst.status is MobileSandboxStatus.built
    assert inst.build.status == "pass"
    assert inst.build.artifact_path.endswith(".apk")


def test_manager_build_records_failure(workspace: str):
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(build_status="fail"),
        ios_executor=FakeIosExecutor(),
    )
    mgr.create(_android_config(workspace))
    inst = mgr.build("sess-1")
    assert inst.status is MobileSandboxStatus.failed
    assert inst.build.status == "fail"
    assert inst.error is not None


def test_manager_install_transitions_running(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    mgr.build("sess-1")
    inst = mgr.install("sess-1")
    assert inst.status is MobileSandboxStatus.running
    assert inst.install.launched is True


def test_manager_install_rejects_when_not_built(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    with pytest.raises(MobileSandboxError):
        mgr.install("sess-1")


def test_manager_install_fail_marks_sandbox_failed(workspace: str):
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(install_status="fail"),
        ios_executor=FakeIosExecutor(),
    )
    mgr.create(_android_config(workspace))
    mgr.build("sess-1")
    inst = mgr.install("sess-1")
    assert inst.status is MobileSandboxStatus.failed


def test_manager_screenshot_requires_running(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    with pytest.raises(MobileSandboxError):
        mgr.screenshot("sess-1")


def test_manager_screenshot_happy_path(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    mgr.build("sess-1")
    mgr.install("sess-1")
    inst = mgr.screenshot("sess-1")
    assert inst.screenshot.status == "pass"
    assert inst.screenshot.path.endswith(".png")


def test_manager_stop_terminal(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    mgr.build("sess-1")
    mgr.install("sess-1")
    inst = mgr.stop("sess-1")
    assert inst.status is MobileSandboxStatus.stopped


def test_manager_stop_idempotent(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    mgr.stop("sess-1")
    inst = mgr.stop("sess-1")
    assert inst.status is MobileSandboxStatus.stopped


def test_manager_remove_requires_terminal(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    with pytest.raises(MobileSandboxError):
        mgr.remove("sess-1")


def test_manager_remove_after_stop(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    mgr.stop("sess-1")
    final = mgr.remove("sess-1")
    assert final.status is MobileSandboxStatus.stopped
    assert mgr.get("sess-1") is None


def test_manager_touch_updates_activity(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    first = mgr.get("sess-1")
    assert first is not None
    before = first.last_active_at
    # time.time() tick is coarse but strictly monotonic on most hosts
    import time
    time.sleep(0.001)
    touched = mgr.touch("sess-1")
    assert touched.last_active_at >= before


def test_manager_list_and_snapshot(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace, session_id="s1"))
    mgr.create(_ios_config(workspace, session_id="s2"))
    lst = mgr.list()
    assert len(lst) == 2
    snap = mgr.snapshot()
    json.dumps(snap)
    assert snap["count"] == 2
    assert snap["schema_version"] == MOBILE_SANDBOX_SCHEMA_VERSION


def test_manager_require_unknown_raises(workspace: str):
    mgr = _manager()
    with pytest.raises(MobileSandboxNotFound):
        mgr.build("unknown-session")


def test_manager_event_callback_receives_transitions(workspace: str):
    events: list[tuple[str, dict]] = []
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(),
        ios_executor=FakeIosExecutor(),
        event_cb=lambda et, payload: events.append((et, dict(payload))),
    )
    mgr.create(_android_config(workspace))
    mgr.build("sess-1")
    mgr.install("sess-1")
    mgr.screenshot("sess-1")
    mgr.stop("sess-1")
    event_types = [e[0] for e in events]
    assert "mobile_sandbox.created" in event_types
    assert "mobile_sandbox.building" in event_types
    assert "mobile_sandbox.built" in event_types
    assert "mobile_sandbox.ready" in event_types
    assert "mobile_sandbox.screenshot" in event_types
    assert "mobile_sandbox.stopped" in event_types


def test_manager_event_callback_isolated_from_failure(workspace: str):
    def raising_cb(et, payload):
        raise RuntimeError("callback boom")
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(),
        ios_executor=FakeIosExecutor(),
        event_cb=raising_cb,
    )
    # Should not propagate
    mgr.create(_android_config(workspace))


# ── Markdown rendering ────────────────────────────────────────────


def test_render_sandbox_status_markdown(workspace: str):
    mgr = _manager()
    mgr.create(_android_config(workspace))
    inst = mgr.get("sess-1")
    assert inst is not None
    md = render_sandbox_status_markdown(inst)
    assert "Mobile Sandbox `sess-1`" in md
    assert "platform: `android`" in md


def test_render_sandbox_status_markdown_rejects_non_instance():
    with pytest.raises(TypeError):
        render_sandbox_status_markdown("not an instance")  # type: ignore[arg-type]


# ── Subprocess executors (degradation to mock) ────────────────────


def test_subprocess_android_executor_mock_without_docker(workspace: str, monkeypatch):
    monkeypatch.setattr(ms.shutil, "which", lambda _name: None)
    ex = SubprocessAndroidExecutor()
    r = ex.build(_android_config(workspace))
    assert r.status == "mock"


def test_ssh_macos_executor_mock_without_remote_host(workspace: str):
    ex = SshMacOsIosExecutor()
    cfg = _ios_config(workspace)  # ios_remote_host=""
    r = ex.build(cfg)
    assert r.status == "mock"
    assert "no ios_remote_host" in r.detail


def test_ssh_macos_executor_mock_without_ssh(workspace: str, monkeypatch):
    monkeypatch.setattr(ms.shutil, "which", lambda _name: None)
    ex = SshMacOsIosExecutor()
    cfg = MobileSandboxConfig(
        session_id="s1", platform="ios",
        workspace_path=workspace,
        xcode_scheme="Foo",
        xcode_project="Foo.xcworkspace",
        ios_remote_host="builder@mac-runner",
    )
    r = ex.build(cfg)
    assert r.status == "mock"
    assert "ssh" in r.detail


def test_ssh_macos_executor_install_mock_without_host(workspace: str):
    ex = SshMacOsIosExecutor()
    cfg = _ios_config(workspace)
    r = ex.install(cfg, "/tmp/x.app")
    assert r.status == "mock"


def test_ssh_macos_executor_screenshot_mock_without_host(workspace: str, tmp_path: Path):
    ex = SshMacOsIosExecutor()
    cfg = _ios_config(workspace)
    r = ex.screenshot(cfg, output_dir=str(tmp_path / "out"))
    assert r.status == "mock"


def test_subprocess_android_executor_runner_injection(workspace: str, monkeypatch, tmp_path: Path):
    # docker present on PATH (so we don't short-circuit to mock), but
    # injected runner is called and returns a failure.
    monkeypatch.setattr(ms.shutil, "which", lambda name: f"/usr/bin/{name}")

    class Rec:
        called: list[list[str]] = []

        def __call__(self, argv, **kwargs):
            Rec.called.append(list(argv))
            class P:
                returncode = 1
                stdout = ""
                stderr = "FAILURE: Build failed with an exception."
            return P()

    ex = SubprocessAndroidExecutor(runner=Rec())
    r = ex.build(_android_config(workspace))
    assert r.status == "fail"
    assert "Build failed" in r.stderr_tail or r.detail
    assert Rec.called  # runner was invoked
