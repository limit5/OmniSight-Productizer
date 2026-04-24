"""P0 #285 — mobile platform profile coverage tests.

Locks in the four mobile profiles introduced in P0:
  * ios-arm64           (iOS Device ABI)
  * ios-simulator       (iOS Simulator, x86_64 + arm64)
  * android-arm64-v8a   (Android primary 64-bit ABI)
  * android-armeabi-v7a (Android legacy 32-bit ABI)

Verifies they:
  1. Are enumerated by `list_profile_ids` (sibling to the web
     profiles — same schema infrastructure, different dispatch).
  2. Declare `target_kind: mobile` so the W0 dispatcher routes them
     through `_resolve_mobile` rather than embedded / web.
  3. Carry the four P0-mandated fields per the TODO.md line item:
     `sdk_version`, `min_os_version`, `toolchain_path`, `emulator_spec`.
  4. `validate_profile()` returns no errors for any of them — mobile
     profiles are NOT forced to declare embedded-only fields like
     `kernel_arch` (the W0 invariant carried forward).
  5. Each profile's ABI/version pins match the platform-limit reasoning
     baked into the YAML — tests double as living docs so an edit that
     drifts the Android pair apart (or raises iOS min-OS past the P7
     StoreKit 2 dependency floor) trips immediately.
"""

from __future__ import annotations

import pytest

from backend.platform import (
    get_platform_config,
    list_profile_ids,
    load_raw_profile,
    validate_profile,
)

_MOBILE_PROFILES = (
    "ios-arm64",
    "ios-simulator",
    "android-arm64-v8a",
    "android-armeabi-v7a",
)


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_is_enumerated(profile_id):
    assert profile_id in list_profile_ids()


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_declares_target_kind_mobile(profile_id):
    """Every P0 profile must declare target_kind=mobile — that's the
    dispatch signal the W0 loader uses to pick `_resolve_mobile`."""
    data = load_raw_profile(profile_id)
    assert data.get("target_kind") == "mobile"


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_declares_required_fields(profile_id):
    """P0 TODO.md spec: every profile declares SDK version /
    min API level / toolchain path / emulator spec. The loader doesn't
    force these (it has defaults), but the profiles themselves must be
    explicit so operators can reason without grepping Python."""
    data = load_raw_profile(profile_id)
    for key in ("sdk_version", "min_os_version", "toolchain_path", "emulator_spec"):
        assert key in data, f"{profile_id} missing required P0 field: {key}"


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_validates_clean(profile_id):
    """Mobile profiles must not trip embedded-only validations
    (kernel_arch et al). This is the W0 → P0 carried-forward invariant."""
    data = load_raw_profile(profile_id)
    errs = validate_profile(data)
    assert errs == [], f"{profile_id}: unexpected validation errors: {errs}"


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_resolves_to_mobile_toolchain(profile_id):
    """The dispatched build_toolchain block must come from the mobile
    resolver, not embedded / web. Catches accidental copy-paste
    regressions that omit `target_kind`."""
    cfg = get_platform_config(profile_id)
    assert cfg["target_kind"] == "mobile"
    assert cfg["build_toolchain"]["kind"] == "mobile"
    # cross_prefix / arch must not leak into a mobile toolchain.
    assert "cross_prefix" not in cfg["build_toolchain"]
    assert "arch" not in cfg["build_toolchain"]
    # Mobile-specific fields must be surfaced by the resolver.
    for key in ("mobile_platform", "mobile_abi", "min_os_version",
                "sdk_version", "toolchain_path", "emulator_spec"):
        assert key in cfg["build_toolchain"], (
            f"{profile_id}: resolver dropped mobile field {key!r}"
        )


@pytest.mark.parametrize("profile_id", _MOBILE_PROFILES)
def test_mobile_profile_emulator_spec_is_mapping(profile_id):
    """`emulator_spec` is a structured mapping (kind + model + runtime
    IDs), not a free-form string — downstream P2 simulate-track relies
    on it having a stable shape."""
    data = load_raw_profile(profile_id)
    spec = data["emulator_spec"]
    assert isinstance(spec, dict), f"{profile_id}: emulator_spec must be a mapping"
    assert "kind" in spec, f"{profile_id}: emulator_spec missing 'kind' discriminator"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  iOS-specific invariants (living spec)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ios_device_is_pure_arm64():
    """iPhone 12+ are arm64-only; we do NOT ship armv7/armv7s for iOS."""
    data = load_raw_profile("ios-arm64")
    assert data["mobile_platform"] == "ios"
    assert data["mobile_abi"] == "arm64"
    assert data["emulator_spec"]["kind"] == "paired_simulator"


def test_ios_simulator_declares_fat_binary_slices():
    """Simulator must emit a universal binary covering both x86_64
    (Intel CI hosts) and arm64 (Apple Silicon). Dropping either slice
    silently breaks part of the CI fleet."""
    data = load_raw_profile("ios-simulator")
    assert data["mobile_abi"] == "arm64_simulator"
    spec = data["emulator_spec"]
    assert spec["kind"] == "simulator"
    assert set(spec["slices"]) == {"x86_64", "arm64"}


def test_ios_profiles_share_sdk_baseline():
    """Device + simulator profiles MUST pin the same SDK / min-OS.
    Drift causes "works on simulator, crashes on device" bugs — trip
    loud at profile-load time, not at App Store rejection time."""
    device = load_raw_profile("ios-arm64")
    sim = load_raw_profile("ios-simulator")
    assert device["sdk_version"] == sim["sdk_version"]
    assert device["target_os_version"] == sim["target_os_version"]
    assert device["min_os_version"] == sim["min_os_version"]


def test_ios_min_os_covers_storekit2_floor():
    """StoreKit 2 (the P7 #292 pilot dependency) requires iOS 15+.
    We pin min_os_version to 16.0 in 2026 — bumping to 17+ is a
    breaking change for paying customers, lowering to 15 deadens
    StoreKit 2 support. Flip this and explain why in the PR."""
    for profile in ("ios-arm64", "ios-simulator"):
        data = load_raw_profile(profile)
        major = int(str(data["min_os_version"]).split(".")[0])
        assert major >= 15, (
            f"{profile}: iOS min_os_version must be >= 15 for StoreKit 2"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Android-specific invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_android_profiles_pin_primary_and_legacy_abi():
    """The two Android profiles MUST be the Play-sanctioned pair:
    arm64-v8a (mandatory) + armeabi-v7a (legacy 32-bit). Anything
    else (armeabi / x86 / mips) is not a supported submission target."""
    v8a = load_raw_profile("android-arm64-v8a")
    v7a = load_raw_profile("android-armeabi-v7a")
    assert v8a["mobile_platform"] == "android"
    assert v7a["mobile_platform"] == "android"
    assert v8a["mobile_abi"] == "arm64-v8a"
    assert v7a["mobile_abi"] == "armeabi-v7a"


def test_android_profiles_share_sdk_and_min_api_level():
    """Both Android ABI profiles must pin the same compile SDK / target
    SDK / minSdkVersion. Drift here causes one ABI slice to silently
    fail Play upload because its targetSdk is behind policy."""
    v8a = load_raw_profile("android-arm64-v8a")
    v7a = load_raw_profile("android-armeabi-v7a")
    assert v8a["sdk_version"] == v7a["sdk_version"]
    assert v8a["target_os_version"] == v7a["target_os_version"]
    assert v8a["min_os_version"] == v7a["min_os_version"]


def test_android_target_sdk_matches_compile_sdk():
    """Google Play policy: targetSdk must be current-minus-one at most.
    Our invariant: targetSdk == compileSdk. Violation means Play will
    reject the upload — catch it here instead of at P5 upload time."""
    for profile in ("android-arm64-v8a", "android-armeabi-v7a"):
        data = load_raw_profile(profile)
        assert data["sdk_version"] == data["target_os_version"], (
            f"{profile}: sdk_version / target_os_version must match"
        )


def test_android_min_api_level_matches_ndk_toolchain_suffix():
    """The NDK clang binary name encodes the API level (e.g.
    `aarch64-linux-android24-clang`); it MUST agree with
    `min_os_version`. If someone bumps minSdk without re-picking the
    NDK toolchain, gradle produces binaries with the wrong API floor."""
    for profile in ("android-arm64-v8a", "android-armeabi-v7a"):
        data = load_raw_profile(profile)
        min_api = str(data["min_os_version"])
        toolchain = data["toolchain_path"]
        assert f"android{min_api}-clang" in toolchain or \
               f"androideabi{min_api}-clang" in toolchain, (
            f"{profile}: toolchain_path {toolchain!r} does not agree with "
            f"min_os_version {min_api!r}"
        )


def test_android_v8a_build_command_produces_aab():
    """Play Store requires .aab since 2021-08. Primary ABI's default
    build_cmd must emit a bundle, not a bare APK."""
    data = load_raw_profile("android-arm64-v8a")
    assert "bundle" in data["build_cmd"].lower(), (
        "android-arm64-v8a build_cmd must produce an Android App Bundle"
    )


def test_android_profiles_include_emulator_spec():
    """Both Android profiles carry an AVD spec so the P2 simulate-track
    can boot a matching emulator without per-project config."""
    for profile in ("android-arm64-v8a", "android-armeabi-v7a"):
        spec = load_raw_profile(profile)["emulator_spec"]
        assert spec["kind"] == "avd"
        assert spec["avd_name"]
        assert spec["api_level"]
        assert "system-images;" in spec["system_image"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P11 #351 checkbox 5 — android_cli_available profile flag
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ANDROID_PROFILES = ("android-arm64-v8a", "android-armeabi-v7a")


@pytest.mark.parametrize("profile_id", _ANDROID_PROFILES)
def test_android_profile_declares_android_cli_available(profile_id):
    """P11 #351 checkbox 5 — every Android profile must declare
    ``android_cli_available`` so downstream resolvers can distinguish
    "CLI not wanted here" from "profile was written pre-P11 and didn't
    know to declare". Missing key on an Android profile trips loud."""
    data = load_raw_profile(profile_id)
    assert "android_cli_available" in data, (
        f"{profile_id}: Android profiles must declare android_cli_available "
        "(P11 #351 checkbox 5)"
    )
    assert isinstance(data["android_cli_available"], bool), (
        f"{profile_id}: android_cli_available must be a YAML bool, got "
        f"{type(data['android_cli_available']).__name__}"
    )


def test_android_profiles_agree_on_cli_availability():
    """Drift guard: both ABI profiles must flip the flag together.
    Splitting them would mean one ABI slice builds via CLI fast path
    while the other silently falls back to ``./gradlew`` — a subtle
    class of bug where your arm64 APK is 3× faster to produce than
    your v7a slice with no explanation in the build log."""
    v8a = load_raw_profile("android-arm64-v8a")
    v7a = load_raw_profile("android-armeabi-v7a")
    assert v8a["android_cli_available"] == v7a["android_cli_available"], (
        "android-arm64-v8a and android-armeabi-v7a must agree on "
        "android_cli_available — the `android` binary is a single "
        "install that covers both ABIs (per-ABI selection is per-call, "
        "not per-install). Drift here means the two slices take "
        "different build paths for no ABI-specific reason."
    )


@pytest.mark.parametrize("profile_id", _ANDROID_PROFILES)
def test_android_profile_cli_available_flag_is_true_post_p11(profile_id):
    """P11 #351 ships with CLI fast path enabled for both profiles
    (Google's `android` CLI, released 2026-04-18, covers both ABIs).
    If a future operator needs to flip this to ``false`` (air-gap host
    / policy lock), this assertion is the place to document the why."""
    data = load_raw_profile(profile_id)
    assert data["android_cli_available"] is True, (
        f"{profile_id}: post-P11 default is android_cli_available=true; "
        "if flipping to false, record the reason in the profile comment."
    )


@pytest.mark.parametrize("profile_id", _ANDROID_PROFILES)
def test_android_profile_cli_flag_surfaces_in_build_toolchain(profile_id):
    """The resolver must echo ``android_cli_available`` into the
    ``build_toolchain`` block so consumers can read it without
    re-parsing the raw profile YAML."""
    cfg = get_platform_config(profile_id)
    assert "android_cli_available" in cfg["build_toolchain"]
    assert cfg["build_toolchain"]["android_cli_available"] is True


@pytest.mark.parametrize("profile_id", ("ios-arm64", "ios-simulator"))
def test_ios_profiles_omit_android_cli_flag(profile_id):
    """iOS profiles deliberately omit ``android_cli_available``. The
    resolver surfaces it as ``None`` (not-declared sentinel) so a
    consumer that mistakenly reads the flag on an iOS profile gets a
    three-state signal (True / False / None) instead of silently
    assuming False."""
    data = load_raw_profile(profile_id)
    assert "android_cli_available" not in data, (
        f"{profile_id}: iOS profile should not carry android_cli_available"
    )
    cfg = get_platform_config(profile_id)
    assert cfg["build_toolchain"]["android_cli_available"] is None
