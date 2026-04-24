"""P1 #286 — Mobile toolchain integration tests.

Covers:
  * ``resolve_macos_builder`` — env-based dispatch + error modes.
  * ``resolve_mobile_toolchain`` — iOS requires macOS delegator,
    Android resolves locally.
  * ``gradle_wrapper_command`` / ``fastlane_gym_command`` /
    ``fastlane_supply_command`` — pure argv builders.
  * ``docker_run_android_command`` — env-name passthrough without
    values, image qualified tag resolution.
  * ``describe`` / ``safe_quote`` — log-safe pretty-printing.
  * Dockerfile itself — sanity checks to keep the image in sync
    with the P0 profile pins (NDK version, SDK level).

All tests are pure-Python; none invoke subprocess, shell out to
docker, or reach any remote host. The P2 (#287) simulate track will
add the execution-side integration tests against the actual
``ghcr.io/omnisight/mobile-build`` image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import mobile_toolchain as mt


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants / canonical names
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_mobile_build_image_is_ghcr_omnisight():
    """Operators + CI configs hard-reference this string. Pin it."""
    assert mt.MOBILE_BUILD_IMAGE == "ghcr.io/omnisight/mobile-build"


def test_supported_macos_builders_is_the_four_from_todo():
    """TODO.md P1 enumerates exactly these four values."""
    assert mt.SUPPORTED_MACOS_BUILDERS == frozenset({
        "self-hosted",
        "macstadium",
        "cirrus-ci",
        "github-macos-runner",
    })


def test_env_macos_builder_name_is_prefixed():
    """All OmniSight env vars use the OMNISIGHT_ prefix."""
    assert mt.ENV_MACOS_BUILDER.startswith("OMNISIGHT_")
    assert mt.ENV_MOBILE_IMAGE_TAG.startswith("OMNISIGHT_")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  resolve_macos_builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_resolve_macos_builder_unset_raises_required():
    with pytest.raises(mt.MacOSBuilderRequiredError):
        mt.resolve_macos_builder(env={})


def test_resolve_macos_builder_empty_string_raises_required():
    """Set-but-empty (common when CI blanks a var) must behave the
    same as unset — otherwise operators get a confusing
    UnknownMacOSBuilderError."""
    with pytest.raises(mt.MacOSBuilderRequiredError):
        mt.resolve_macos_builder(env={mt.ENV_MACOS_BUILDER: ""})
    with pytest.raises(mt.MacOSBuilderRequiredError):
        mt.resolve_macos_builder(env={mt.ENV_MACOS_BUILDER: "   "})


def test_resolve_macos_builder_unknown_value_raises():
    with pytest.raises(mt.UnknownMacOSBuilderError):
        mt.resolve_macos_builder(env={mt.ENV_MACOS_BUILDER: "windows"})


@pytest.mark.parametrize("kind", sorted(mt.SUPPORTED_MACOS_BUILDERS))
def test_resolve_macos_builder_happy_path(kind):
    builder = mt.resolve_macos_builder(env={mt.ENV_MACOS_BUILDER: kind})
    assert builder.kind == kind
    assert builder.display_name
    assert isinstance(builder.env_forward, tuple)
    assert len(builder.env_forward) >= 1
    # Fastlane Apple ID is the common-denominator credential —
    # every iOS delegator must forward it.
    assert "FASTLANE_APPLE_ID" in builder.env_forward


def test_resolve_macos_builder_case_insensitive():
    """Operators type the env value in whatever case they like.
    ``SELF-HOSTED`` / ``Self-Hosted`` / ``self-hosted`` all work."""
    for raw in ("self-hosted", "SELF-HOSTED", "Self-Hosted"):
        assert mt.resolve_macos_builder(
            env={mt.ENV_MACOS_BUILDER: raw}
        ).kind == "self-hosted"


def test_resolve_macos_builder_self_hosted_picks_up_host_env():
    """Self-hosted delegator reads OMNISIGHT_MACOS_HOST for its
    host_hint. Tests that the env → host_hint wiring is correct."""
    builder = mt.resolve_macos_builder(env={
        mt.ENV_MACOS_BUILDER: "self-hosted",
        "OMNISIGHT_MACOS_HOST": "build@mac-ci-01",
    })
    assert builder.host_hint == "build@mac-ci-01"


def test_resolve_macos_builder_describe_never_echoes_values():
    """MacOSBuilder.describe() lists env NAMES (safe) not VALUES
    (unsafe, may contain tokens). Lock that invariant here."""
    builder = mt.resolve_macos_builder(env={
        mt.ENV_MACOS_BUILDER: "macstadium",
        "MACSTADIUM_HOST_ID": "host-42",
        "MACSTADIUM_API_KEY": "super-secret-never-log-me",
    })
    desc = builder.describe()
    assert "super-secret-never-log-me" not in desc
    assert "MACSTADIUM_API_KEY" in desc  # Name is ok.
    assert "host-42" in desc  # host_hint is ok (not a secret).


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  resolve_mobile_toolchain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_resolve_mobile_toolchain_android_arm64_v8a_no_env():
    """Android resolves without any env set — the Linux build path
    is the default."""
    tc = mt.resolve_mobile_toolchain("android-arm64-v8a", env={})
    assert tc.profile_id == "android-arm64-v8a"
    assert tc.mobile_platform == "android"
    assert tc.mobile_abi == "arm64-v8a"
    assert tc.needs_macos_host is False
    assert tc.android is not None
    assert tc.macos is None
    # The android builder must surface the P0 profile pins.
    assert tc.android.image == mt.MOBILE_BUILD_IMAGE
    assert tc.android.image_tag == mt.MOBILE_BUILD_IMAGE_TAG
    assert tc.android.sdk_root == "/opt/android/sdk"
    assert "ndk/27.0.12077973" in tc.android.ndk_root
    assert "aarch64-linux-android24-clang" in tc.android.toolchain_path
    assert "bundle" in tc.android.build_cmd.lower()


def test_resolve_mobile_toolchain_android_armeabi_v7a_no_env():
    tc = mt.resolve_mobile_toolchain("android-armeabi-v7a", env={})
    assert tc.mobile_abi == "armeabi-v7a"
    assert tc.android is not None
    # v7a's toolchain is the eabi clang binary.
    assert "armv7a-linux-androideabi24-clang" in tc.android.toolchain_path


def test_resolve_mobile_toolchain_image_tag_env_override():
    """CI pipelines pin to a digest-tagged image for reproducibility
    — OMNISIGHT_MOBILE_IMAGE_TAG lets them override ``latest``."""
    tc = mt.resolve_mobile_toolchain(
        "android-arm64-v8a",
        env={mt.ENV_MOBILE_IMAGE_TAG: "2026.04.17-r1"},
    )
    assert tc.android is not None
    assert tc.android.image_tag == "2026.04.17-r1"
    assert tc.android.qualified_image == (
        f"{mt.MOBILE_BUILD_IMAGE}:2026.04.17-r1"
    )


def test_resolve_mobile_toolchain_ios_without_builder_raises():
    with pytest.raises(mt.MacOSBuilderRequiredError):
        mt.resolve_mobile_toolchain("ios-arm64", env={})


def test_resolve_mobile_toolchain_ios_with_unknown_builder_raises():
    with pytest.raises(mt.UnknownMacOSBuilderError):
        mt.resolve_mobile_toolchain(
            "ios-arm64",
            env={mt.ENV_MACOS_BUILDER: "linux-actually"},
        )


@pytest.mark.parametrize("profile_id", ["ios-arm64", "ios-simulator"])
@pytest.mark.parametrize("builder_kind", sorted(mt.SUPPORTED_MACOS_BUILDERS))
def test_resolve_mobile_toolchain_ios_happy_path(profile_id, builder_kind):
    """iOS profile × supported builder = full 4 × 2 = 8 combos work."""
    tc = mt.resolve_mobile_toolchain(
        profile_id,
        env={mt.ENV_MACOS_BUILDER: builder_kind},
    )
    assert tc.mobile_platform == "ios"
    assert tc.needs_macos_host is True
    assert tc.android is None
    assert tc.macos is not None
    assert tc.macos.kind == builder_kind
    # SDK / min-OS are surfaced from the profile.
    assert tc.sdk_version == "17.5"
    assert tc.min_os_version == "16.0"


def test_resolve_mobile_toolchain_rejects_non_mobile_profile():
    """Web / embedded / software profiles must not resolve through
    this entry point — the loader is mobile-only."""
    with pytest.raises(mt.UnsupportedPlatformError):
        mt.resolve_mobile_toolchain("aarch64", env={})
    with pytest.raises(mt.UnsupportedPlatformError):
        mt.resolve_mobile_toolchain("web-vercel", env={})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  gradle_wrapper_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_gradle_wrapper_command_basic(tmp_path):
    argv = mt.gradle_wrapper_command(tmp_path, "bundleRelease")
    assert argv == [str(tmp_path / "gradlew"), "bundleRelease"]


def test_gradle_wrapper_command_with_abi_and_extra(tmp_path):
    argv = mt.gradle_wrapper_command(
        tmp_path,
        "assembleDebug",
        extra_args=["--stacktrace", "--info"],
        abi="armeabi-v7a",
    )
    assert argv[0].endswith("gradlew")
    assert "assembleDebug" in argv
    assert "-PtargetAbi=armeabi-v7a" in argv
    assert "--stacktrace" in argv
    assert "--info" in argv


def test_gradle_wrapper_command_uses_absolute_wrapper_path(tmp_path):
    """Returned argv[0] is an absolute path — prevents ``gradlew not
    found`` when the caller's cwd differs from project_root."""
    argv = mt.gradle_wrapper_command(tmp_path, "test")
    assert Path(argv[0]).is_absolute()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  fastlane_gym_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fastlane_gym_command_defaults():
    argv = mt.fastlane_gym_command(scheme="MyApp")
    assert argv[:2] == ["fastlane", "gym"]
    assert "--scheme=MyApp" in argv
    assert "--configuration=Release" in argv
    assert "--export_method=app-store" in argv


def test_fastlane_gym_command_custom_output():
    out = Path("/tmp/build/ios")
    argv = mt.fastlane_gym_command(
        scheme="MyApp",
        configuration="Debug",
        output_directory=out,
        export_method="ad-hoc",
        extra_flags=["--include_bitcode=false"],
    )
    assert "--configuration=Debug" in argv
    assert "--export_method=ad-hoc" in argv
    assert f"--output_directory={out}" in argv
    assert "--include_bitcode=false" in argv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  fastlane_supply_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fastlane_supply_requires_aab_or_apk():
    with pytest.raises(ValueError):
        mt.fastlane_supply_command(package_name="com.example.app")


def test_fastlane_supply_rejects_both_aab_and_apk():
    """One or the other — caller must pick. supply cannot upload
    both in a single command."""
    with pytest.raises(ValueError):
        mt.fastlane_supply_command(
            package_name="com.example.app",
            aab_path=Path("/tmp/a.aab"),
            apk_path=Path("/tmp/a.apk"),
        )


def test_fastlane_supply_aab_happy_path():
    argv = mt.fastlane_supply_command(
        package_name="com.example.app",
        track="internal",
        aab_path=Path("/tmp/a.aab"),
    )
    assert argv[:2] == ["fastlane", "supply"]
    assert "--package_name=com.example.app" in argv
    assert "--track=internal" in argv
    assert "--aab=/tmp/a.aab" in argv


def test_fastlane_supply_apk_happy_path():
    argv = mt.fastlane_supply_command(
        package_name="com.example.app",
        track="alpha",
        apk_path=Path("/tmp/a.apk"),
    )
    assert "--apk=/tmp/a.apk" in argv
    assert "--track=alpha" in argv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  docker_run_android_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_docker_run_android_command_passes_names_not_values(tmp_path):
    """Secret env passthrough uses ``-e NAME`` (Docker pulls the
    value from the parent env at exec time) — NOT ``-e NAME=VALUE``.
    Locks in the no-value-in-argv invariant."""
    tc = mt.resolve_mobile_toolchain("android-arm64-v8a", env={})
    assert tc.android is not None
    argv = mt.docker_run_android_command(
        builder=tc.android,
        project_root=tmp_path,
        inner_argv=["./gradlew", "bundleRelease"],
        extra_env={
            "ANDROID_KEYSTORE_PASSWORD": "shh-do-not-log-me",
            "ANDROID_KEY_ALIAS": "shh-also-secret",
        },
    )
    serialized = " ".join(argv)
    assert "shh-do-not-log-me" not in serialized
    assert "shh-also-secret" not in serialized
    assert "-e" in argv
    assert "ANDROID_KEYSTORE_PASSWORD" in argv
    assert "ANDROID_KEY_ALIAS" in argv


def test_docker_run_android_command_wraps_correctly(tmp_path):
    tc = mt.resolve_mobile_toolchain("android-arm64-v8a", env={})
    assert tc.android is not None
    argv = mt.docker_run_android_command(
        builder=tc.android,
        project_root=tmp_path,
        inner_argv=["./gradlew", "test"],
    )
    assert argv[0] == "docker"
    assert argv[1] == "run"
    assert "--rm" in argv
    assert "-v" in argv
    # Mount syntax: host_path:/workspace
    mount_arg = argv[argv.index("-v") + 1]
    assert mount_arg.endswith(":/workspace")
    assert str(tmp_path) in mount_arg
    # The qualified image must appear before inner argv.
    image_idx = argv.index(tc.android.qualified_image)
    gradle_idx = argv.index("./gradlew")
    assert image_idx < gradle_idx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  describe / safe_quote
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_describe_android_one_liner_has_image_and_docker_flag():
    tc = mt.resolve_mobile_toolchain("android-arm64-v8a", env={})
    line = mt.describe(tc)
    assert "android/arm64-v8a" in line
    assert mt.MOBILE_BUILD_IMAGE in line
    assert "docker=" in line


def test_describe_ios_one_liner_mentions_macos_builder():
    tc = mt.resolve_mobile_toolchain(
        "ios-arm64",
        env={mt.ENV_MACOS_BUILDER: "github-macos-runner"},
    )
    line = mt.describe(tc)
    assert "ios/arm64" in line
    assert "GitHub" in line  # display_name


def test_safe_quote_handles_space_and_shell_meta():
    argv = ["fastlane", "gym", "--scheme=My App", "-e", "FOO;rm -rf /"]
    out = mt.safe_quote(argv)
    # The space-containing arg must be wrapped in quotes so shell
    # re-parse preserves it as a single word.
    assert "'--scheme=My App'" in out or '"--scheme=My App"' in out
    # Shell meta in the -e value is quoted — copy-pasting the log
    # line into a shell won't execute `rm -rf /`.
    assert "'FOO;rm -rf /'" in out or '"FOO;rm -rf /"' in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dockerfile sanity — image pins track profile pins
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DOCKERFILE = (
    Path(__file__).resolve().parent.parent
    / "docker" / "Dockerfile.mobile-build"
)


def test_dockerfile_exists():
    assert _DOCKERFILE.is_file(), (
        "P1 Dockerfile missing at backend/docker/Dockerfile.mobile-build"
    )


def test_dockerfile_pins_match_p0_profile_values():
    """The Docker image's NDK / SDK pins MUST agree with the values
    baked into configs/platforms/android-arm64-v8a.yaml — drift
    would make the profile's toolchain_path point at a binary that
    isn't in the image. Cross-check both sides here."""
    txt = _DOCKERFILE.read_text()
    assert "ANDROID_NDK_VERSION=27.0.12077973" in txt
    assert "ANDROID_COMPILE_SDK=35" in txt
    # The canonical image name.
    assert 'LABEL org.opencontainers.image.title="omnisight/mobile-build"' in txt


def test_dockerfile_installs_fastlane_and_cocoapods():
    """Both are part of the P1 deliverable. Even though CocoaPods'
    real invocation needs Xcode (macOS), the gem must be installed
    so Fastfiles that reference it don't fail the load phase."""
    txt = _DOCKERFILE.read_text()
    assert "COCOAPODS_VERSION=1.15" in txt
    assert "FASTLANE_VERSION=2." in txt
    assert "gem install" in txt


def test_dockerfile_calls_out_ios_macos_restriction():
    """Operators reading the Dockerfile MUST see the 'iOS requires
    macOS' warning up front. Makes the delegation story explicit
    rather than lurking in the Python module."""
    txt = _DOCKERFILE.read_text()
    head = txt[:2000]
    assert "iOS" in head and "macOS" in head
    assert "OMNISIGHT_MACOS_BUILDER" in head


def test_dockerfile_uses_non_root_builder_user():
    """Gradle caches / Android SDK chown must map to a non-root
    uid so bind-mounted workspaces don't write root-owned files
    back to the host."""
    txt = _DOCKERFILE.read_text()
    assert "useradd" in txt or "USER " in txt
    assert "USER builder" in txt


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P11 #351 — Android CLI install drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_INSTALL_ANDROID_CLI = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "install_android_cli.sh"
)


def test_dockerfile_installs_android_cli_from_google_agents_url():
    """P11 #351 checkbox 1 — the mobile-build image must pull the
    Android CLI tarball from the Google agents distribution URL so
    backend/mobile_toolchain.py can take the `android` fast path
    instead of falling back to `./gradlew`. Operators reading the
    Dockerfile should also see the fallback contract called out
    inline so it doesn't look like an accidental `|| true`."""
    txt = _DOCKERFILE.read_text()
    assert "ANDROID_CLI_VERSION=" in txt, (
        "Android CLI version pin missing from Dockerfile — P11 #351 "
        "requires a versioned install so image rebuilds are reproducible"
    )
    assert "d.android.com/tools/agents" in txt, (
        "Android CLI tarball URL drift — P11 #351 pins to Google's "
        "official distribution at d.android.com/tools/agents"
    )
    assert "/opt/android-cli" in txt and "/usr/local/bin/android" in txt, (
        "Android CLI install path drift — binary must land at "
        "/opt/android-cli and symlink into /usr/local/bin/android"
    )
    assert "shutil.which" in txt or "fall back" in txt or "Gradle" in txt, (
        "Dockerfile must document the shutil.which('android') fallback "
        "contract inline so the `|| true`-style tolerance isn't silent"
    )


def test_host_install_android_cli_script_exists_and_is_executable():
    """P11 #351 checkbox 1 — host install script operator can run
    with ``sudo scripts/install_android_cli.sh``. Must be executable
    so the command line doesn't need to be prefixed with ``bash``."""
    assert _INSTALL_ANDROID_CLI.is_file(), (
        "scripts/install_android_cli.sh missing — P11 #351 requires a "
        "host install path mirroring the Docker image install"
    )
    import os
    mode = _INSTALL_ANDROID_CLI.stat().st_mode
    assert mode & 0o111, (
        "scripts/install_android_cli.sh must be chmod +x so operators "
        "can run it directly"
    )


def test_host_install_android_cli_uses_same_url_as_dockerfile():
    """Drift guard — if the Dockerfile URL changes, the host install
    script must follow. Otherwise hosts get one version and Docker
    images another, and the Python backend's fast-path vs fallback
    decision differs depending on whether it runs in a container."""
    dockerfile_txt = _DOCKERFILE.read_text()
    script_txt = _INSTALL_ANDROID_CLI.read_text()
    assert "d.android.com/tools/agents" in dockerfile_txt
    assert "d.android.com/tools/agents" in script_txt, (
        "install_android_cli.sh must pull from the same Google agents "
        "URL as the Dockerfile — otherwise host / Docker installs "
        "diverge"
    )
    assert "shutil.which" in script_txt or "mobile_toolchain" in script_txt, (
        "install_android_cli.sh should reference the P11 fallback "
        "contract so operators know why the script is optional"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P11 #351 — android_cli_command argv builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_supported_android_cli_actions_are_the_three_from_todo():
    """TODO.md P11 checkbox 2 enumerates exactly these three
    actions — create / run / sdk install. Lock the set so a typo
    like ``sdk_install`` vs ``sdk-install`` at a call site is caught
    by the action allowlist instead of silently producing a bad argv."""
    assert mt.SUPPORTED_ANDROID_CLI_ACTIONS == frozenset({
        "create",
        "run",
        "sdk-install",
    })


def test_android_cli_available_proxies_shutil_which(monkeypatch):
    """``android_cli_available`` must reflect PATH at call time —
    operators may install the CLI via ``scripts/install_android_cli.sh``
    mid-session, so caching the value would give a stale answer."""
    monkeypatch.setattr(mt.shutil, "which", lambda name: None)
    assert mt.android_cli_available() is False
    monkeypatch.setattr(
        mt.shutil,
        "which",
        lambda name: "/usr/local/bin/android" if name == "android" else None,
    )
    assert mt.android_cli_available() is True


def test_android_cli_command_create_emits_expected_argv(tmp_path):
    """P11 #351 checkbox 2 — ``create`` action replaces the hand-rolled
    template scaffolding. Argv must start with ``android create`` and
    carry the project_path as the positional."""
    argv = mt.android_cli_command("create", tmp_path)
    assert argv == ["android", "create", str(tmp_path)]


def test_android_cli_command_run_emits_expected_argv(tmp_path):
    """P11 #351 checkbox 2 — ``run`` action replaces
    ``gradle_wrapper_command('installDebug')``. Argv must be
    ``android run <project_path>``; anything else breaks the
    fallback contract at the call site."""
    argv = mt.android_cli_command("run", tmp_path)
    assert argv == ["android", "run", str(tmp_path)]


def test_android_cli_command_sdk_install_requires_package(tmp_path):
    """``sdk-install`` without a package is a call-site bug — surface
    it as ValueError rather than emitting ``android sdk install``
    with a missing positional (which would print CLI usage and the
    caller would see a cryptic exit code)."""
    with pytest.raises(ValueError):
        mt.android_cli_command("sdk-install", tmp_path)


def test_android_cli_command_sdk_install_emits_package(tmp_path):
    """P11 #351 checkbox 2 — ``sdk install`` replaces the manual
    sdkmanager invocations baked into the Docker image. Argv is
    ``android sdk install <package>``; project_path is accepted for
    call-site symmetry but not embedded in the argv because
    ``android sdk install`` operates on the shared SDK root."""
    argv = mt.android_cli_command(
        "sdk-install",
        tmp_path,
        sdk_package="platforms;android-35",
    )
    assert argv == ["android", "sdk", "install", "platforms;android-35"]


def test_android_cli_command_action_is_case_and_separator_insensitive(tmp_path):
    """Operators / agents may type ``SDK_Install`` / ``Sdk-Install``
    / ``sdk install`` interchangeably. Normalise all three to the
    canonical ``sdk-install`` — a typo-rejecting allowlist is more
    annoying than helpful when the surface is this small."""
    for raw in ("SDK-INSTALL", "Sdk_Install", "sdk_install"):
        argv = mt.android_cli_command(
            raw, tmp_path, sdk_package="platform-tools"
        )
        assert argv == ["android", "sdk", "install", "platform-tools"]


def test_android_cli_command_unknown_action_raises(tmp_path):
    """Reject typos / future-actions-we-dont-support loudly so the
    caller can pick up an alternative path rather than emitting
    ``android emulator`` (handled by mobile_simulator, not here)."""
    with pytest.raises(mt.UnknownAndroidCliActionError):
        mt.android_cli_command("emulator", tmp_path)
    with pytest.raises(mt.UnknownAndroidCliActionError):
        mt.android_cli_command("deploy", tmp_path)


def test_android_cli_command_appends_extra_args(tmp_path):
    """``extra_args`` is forwarded verbatim after the action's
    required positional(s), mirroring the gradle_wrapper_command
    convention. Supports ``--template kotlin``, ``--device emu-5554``,
    ``--channel beta`` etc. without each needing a kwarg."""
    argv = mt.android_cli_command(
        "create",
        tmp_path,
        extra_args=["--template", "kotlin"],
    )
    assert argv == [
        "android", "create", str(tmp_path), "--template", "kotlin",
    ]
    argv = mt.android_cli_command(
        "run",
        tmp_path,
        extra_args=["--device", "emu-5554"],
    )
    assert argv[-2:] == ["--device", "emu-5554"]


def test_android_cli_command_is_pure_and_does_not_probe_host(tmp_path, monkeypatch):
    """``android_cli_command`` is a pure argv builder — it must NOT
    probe PATH, spawn subprocess, or fall back. Callers compose
    ``android_cli_available()`` + this helper + ``gradle_wrapper_command``
    at their own layer, which keeps this function unit-testable
    without any environment setup. Lock the invariant by asserting
    that shutil.which is never called during argv emission."""
    calls: list[str] = []

    def spy(name: str):
        calls.append(name)
        return None

    monkeypatch.setattr(mt.shutil, "which", spy)
    mt.android_cli_command("create", tmp_path)
    mt.android_cli_command("run", tmp_path)
    mt.android_cli_command(
        "sdk-install", tmp_path, sdk_package="platform-tools"
    )
    assert calls == [], (
        "android_cli_command must be a pure argv builder; it called "
        f"shutil.which({calls!r}) — move the detection to the caller"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P11 #351 checkbox 4 — resolve_android_invocation fallback dispatcher
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _patch_android_cli_present(monkeypatch, present: bool) -> None:
    """Make ``shutil.which`` answer as if the Android CLI is / isn't on
    PATH. We patch the symbol the toolchain module imported (``mt.shutil``)
    so the override is local to the module under test."""
    fake_path = "/usr/local/bin/android"

    def fake_which(name: str):
        if name == "android" and present:
            return fake_path
        return None

    monkeypatch.setattr(mt.shutil, "which", fake_which)


def test_resolve_android_invocation_run_picks_cli_when_present(tmp_path, monkeypatch):
    """P11 #351 checkbox 4 — ``run`` action with ``android`` on PATH
    routes through ``android run <project_root>`` (the CLI fast path)
    rather than the ``./gradlew installDebug`` fallback."""
    _patch_android_cli_present(monkeypatch, True)
    inv = mt.resolve_android_invocation("run", tmp_path)
    assert inv.path_kind == "android-cli"
    assert inv.argv == ["android", "run", str(tmp_path)]
    assert "android CLI on PATH" in inv.detail


def test_resolve_android_invocation_run_falls_back_to_gradle_when_cli_absent(
    tmp_path, monkeypatch,
):
    """P11 #351 checkbox 4 — ``run`` without the CLI must fall back
    to ``./gradlew installDebug`` so existing P1/P2 hosts that haven't
    yet run ``scripts/install_android_cli.sh`` keep building. The
    Gradle task name is the load-bearing pin: anything other than
    ``installDebug`` would break the install + launch contract that
    ``android run`` replaced."""
    _patch_android_cli_present(monkeypatch, False)
    inv = mt.resolve_android_invocation("run", tmp_path)
    assert inv.path_kind == "gradle-wrapper"
    assert inv.argv[0] == str(tmp_path / "gradlew")
    assert "installDebug" in inv.argv
    assert "android CLI absent" in inv.detail
    assert "installDebug" in inv.detail


def test_resolve_android_invocation_run_fallback_forwards_abi_and_extra_args(
    tmp_path, monkeypatch,
):
    """The Gradle fallback must accept the same ``abi`` /
    ``extra_args`` ergonomics as a direct ``gradle_wrapper_command``
    call — otherwise the dispatcher silently drops the ABI filter on
    fallback hosts and produces a fat APK instead of a per-ABI build."""
    _patch_android_cli_present(monkeypatch, False)
    inv = mt.resolve_android_invocation(
        "run",
        tmp_path,
        abi="arm64-v8a",
        extra_args=["--stacktrace"],
    )
    assert inv.path_kind == "gradle-wrapper"
    assert "-PtargetAbi=arm64-v8a" in inv.argv
    assert "--stacktrace" in inv.argv


def test_resolve_android_invocation_create_picks_cli_when_present(tmp_path, monkeypatch):
    """``create`` with the CLI present emits ``android create
    <project_root>`` — the hand-rolled template scaffolding code path
    from P1 is gone."""
    _patch_android_cli_present(monkeypatch, True)
    inv = mt.resolve_android_invocation(
        "create", tmp_path, extra_args=["--template", "kotlin"],
    )
    assert inv.path_kind == "android-cli"
    assert inv.argv == [
        "android", "create", str(tmp_path), "--template", "kotlin",
    ]


def test_resolve_android_invocation_create_without_cli_raises_no_fallback(
    tmp_path, monkeypatch,
):
    """``create`` has no ``./gradlew`` equivalent — template
    scaffolding lives in the Docker image. Surface this as a typed
    error rather than silently emitting ``./gradlew create``, which
    would fail with a confusing "Task 'create' not found" instead."""
    _patch_android_cli_present(monkeypatch, False)
    with pytest.raises(mt.NoGradleFallbackError) as exc_info:
        mt.resolve_android_invocation("create", tmp_path)
    msg = str(exc_info.value)
    assert "create" in msg
    # Operator hint must point at the install script + Docker image so
    # the next step is actionable from the error message alone.
    assert "install_android_cli.sh" in msg
    assert mt.MOBILE_BUILD_IMAGE in msg


def test_resolve_android_invocation_sdk_install_picks_cli_when_present(
    tmp_path, monkeypatch,
):
    """``sdk-install`` with the CLI present emits ``android sdk install
    <package>`` — replaces the manual ``sdkmanager`` invocations baked
    into the Dockerfile."""
    _patch_android_cli_present(monkeypatch, True)
    inv = mt.resolve_android_invocation(
        "sdk-install", tmp_path, sdk_package="platforms;android-35",
    )
    assert inv.path_kind == "android-cli"
    assert inv.argv == [
        "android", "sdk", "install", "platforms;android-35",
    ]


def test_resolve_android_invocation_sdk_install_without_cli_raises_no_fallback(
    tmp_path, monkeypatch,
):
    """``sdk-install`` has no ``./gradlew`` equivalent — the SDK
    manager is baked into the Docker image, not the host helpers.
    Same NoGradleFallbackError treatment as ``create``."""
    _patch_android_cli_present(monkeypatch, False)
    with pytest.raises(mt.NoGradleFallbackError):
        mt.resolve_android_invocation(
            "sdk-install", tmp_path, sdk_package="platform-tools",
        )


def test_resolve_android_invocation_unknown_action_raises(tmp_path, monkeypatch):
    """Unknown action surfaces as ``UnknownAndroidCliActionError`` —
    the same error type ``android_cli_command`` raises so callers can
    catch a single exception type for both layers."""
    _patch_android_cli_present(monkeypatch, True)
    with pytest.raises(mt.UnknownAndroidCliActionError):
        mt.resolve_android_invocation("emulator", tmp_path)
    _patch_android_cli_present(monkeypatch, False)
    with pytest.raises(mt.UnknownAndroidCliActionError):
        mt.resolve_android_invocation("emulator", tmp_path)


def test_resolve_android_invocation_action_normalisation(tmp_path, monkeypatch):
    """Same case- / separator-insensitivity contract as the underlying
    ``android_cli_command`` — locking it at the dispatcher layer too
    so a typo in P1/P2 call sites can't sneak past."""
    _patch_android_cli_present(monkeypatch, True)
    for raw in ("RUN", "Run", "run"):
        inv = mt.resolve_android_invocation(raw, tmp_path)
        assert inv.path_kind == "android-cli"
        assert inv.argv == ["android", "run", str(tmp_path)]
    for raw in ("SDK_Install", "sdk-INSTALL", "Sdk_Install"):
        inv = mt.resolve_android_invocation(
            raw, tmp_path, sdk_package="platform-tools",
        )
        assert inv.argv == ["android", "sdk", "install", "platform-tools"]


def test_resolve_android_invocation_probes_path_at_call_time(tmp_path, monkeypatch):
    """Decision must be made at call time, not memoised — operators
    may run ``scripts/install_android_cli.sh`` mid-session and the
    next ``resolve_android_invocation("run", …)`` should immediately
    pick up the new fast path. Counter-example: a cached probe would
    keep returning the gradle fallback until the process restarts.

    We assert by toggling the fake PATH between two calls and checking
    each call's ``path_kind`` reflects the toggle."""
    _patch_android_cli_present(monkeypatch, False)
    first = mt.resolve_android_invocation("run", tmp_path)
    assert first.path_kind == "gradle-wrapper"

    _patch_android_cli_present(monkeypatch, True)
    second = mt.resolve_android_invocation("run", tmp_path)
    assert second.path_kind == "android-cli"


def test_resolve_android_invocation_fallback_table_covers_every_action():
    """Drift guard — every action in ``SUPPORTED_ANDROID_CLI_ACTIONS``
    must have an entry in ``_ANDROID_CLI_GRADLE_FALLBACK`` so a future
    new action (e.g. ``test``) doesn't silently route to KeyError when
    the CLI is absent."""
    assert (
        set(mt._ANDROID_CLI_GRADLE_FALLBACK.keys())
        == set(mt.SUPPORTED_ANDROID_CLI_ACTIONS)
    ), (
        "_ANDROID_CLI_GRADLE_FALLBACK and SUPPORTED_ANDROID_CLI_ACTIONS "
        "drifted — adding a new action without an explicit fallback "
        "decision would KeyError on hosts without the Android CLI"
    )


def test_resolve_android_invocation_fallback_table_pins_run_to_install_debug():
    """``android run`` ≈ build + install + launch on a connected
    device. The Gradle equivalent is ``installDebug`` (NOT
    ``assembleDebug`` — that only builds, doesn't install). Pin the
    mapping so a refactor doesn't silently break the install step at
    fallback hosts."""
    assert mt._ANDROID_CLI_GRADLE_FALLBACK["run"] == "installDebug"
