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
