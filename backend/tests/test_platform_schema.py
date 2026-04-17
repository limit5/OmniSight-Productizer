"""W0 #274 — platform profile schema generalization tests.

Locks in the behavior that:
  1. `target_kind` enum = {embedded, web, mobile, software}
  2. Profiles that omit `target_kind` fall back to `embedded` (zero
     regression for pre-W0 profiles that never declared the field).
  3. Each kind routes to its own build-toolchain resolver.
  4. The embedded resolver produces the same fields the legacy
     `agents.tools.get_platform_config` text tool emits — parity check.
  5. `schema.yaml` is treated as a schema declaration, not a profile.
  6. Malformed `target_kind` values raise, not silently coerce.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.platform import (
    DEFAULT_TARGET_KIND,
    TARGET_KINDS,
    PlatformProfileError,
    get_platform_config,
    list_profile_ids,
    load_raw_profile,
    resolve_build_toolchain,
    target_kind_of,
    validate_profile,
)

_REPO = Path(__file__).resolve().parent.parent.parent
_PLATFORMS = _REPO / "configs" / "platforms"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema declaration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_target_kinds_enum_is_the_documented_set():
    """The loader's TARGET_KINDS must match the W0 spec exactly."""
    assert TARGET_KINDS == frozenset({"embedded", "web", "mobile", "software"})


def test_default_target_kind_is_embedded():
    """Pre-W0 profiles had no target_kind; they must still load as
    embedded so the first Priority-W landing doesn't break them."""
    assert DEFAULT_TARGET_KIND == "embedded"


def test_schema_yaml_exists_and_declares_enum():
    """schema.yaml must be checked in and must declare the same enum
    the Python loader enforces."""
    schema_path = _PLATFORMS / "schema.yaml"
    assert schema_path.exists(), "configs/platforms/schema.yaml must exist"
    data = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    assert set(data["target_kinds"]) == TARGET_KINDS


def test_schema_yaml_is_not_enumerated_as_a_profile():
    """list_profile_ids must skip schema.yaml — it's a schema
    declaration, not a target."""
    ids = list_profile_ids()
    assert "schema" not in ids
    assert ids == sorted(ids)


def test_loading_schema_as_profile_errors():
    """Defensive: if someone calls load_raw_profile('schema'), we must
    refuse rather than happily return the schema file as if it were a
    target."""
    with pytest.raises(PlatformProfileError):
        load_raw_profile("schema")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Existing embedded profiles: zero-regression
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize(
    "profile_id",
    ["aarch64", "armv7", "riscv64", "vendor-example", "host_native"],
)
def test_existing_profiles_declare_embedded_target_kind(profile_id):
    """Every in-tree profile must explicitly declare target_kind=embedded
    after W0, so operators get a uniform story and linters don't have to
    guess."""
    data = load_raw_profile(profile_id)
    assert data.get("target_kind") == "embedded", (
        f"{profile_id} must declare target_kind: embedded"
    )


@pytest.mark.parametrize(
    "profile_id,expected_arch",
    [
        ("aarch64", "arm64"),
        ("armv7", "arm"),
        ("riscv64", "riscv"),
    ],
)
def test_embedded_parity_cross_compile_fields(profile_id, expected_arch):
    """Parity check: for the three canonical cross-compile profiles,
    the W0 loader must emit the same ARCH / CROSS_COMPILE / TOOLCHAIN
    that the legacy text tool produced. If this test breaks, we broke
    existing simulate/build pipelines."""
    cfg = get_platform_config(profile_id)
    build = cfg["build_toolchain"]
    assert cfg["target_kind"] == "embedded"
    assert build["kind"] == "embedded"
    assert build["arch"] == expected_arch
    assert build["cross_prefix"].startswith(
        {"arm64": "aarch64-", "arm": "arm-", "riscv": "riscv64-"}[expected_arch]
    )
    assert build["toolchain"], "embedded toolchain must be non-empty"


def test_host_native_parity_empty_cross_prefix():
    """host_native must resolve with an empty cross_prefix — that's the
    signal consumers use to take the fast-path (no QEMU, no cross)."""
    build = get_platform_config("host_native")["build_toolchain"]
    assert build["kind"] == "embedded"
    assert build["cross_prefix"] == ""
    assert build["arch"] == ""  # host_native leaves kernel_arch empty by design


def test_vendor_example_carries_vendor_fields():
    """vendor-example historically exposed sysroot_path/cmake_toolchain
    via the loader. Keep that contract."""
    data = get_platform_config("vendor-example")
    build = data["build_toolchain"]
    assert build["sysroot_path"] == "/opt/example-vendor/sysroot"
    assert build["cmake_toolchain_file"] == "/opt/example-vendor/toolchain.cmake"
    assert "gcc-aarch64-linux-gnu" in build["docker_packages"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  target_kind dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _synthetic(kind: str, **extra) -> dict:
    base = {"platform": f"synthetic-{kind}", "label": f"Synthetic {kind}"}
    if kind is not None:
        base["target_kind"] = kind
    # embedded needs kernel_arch for the validator to pass, but the
    # resolver itself tolerates absence — keep it present when
    # requested.
    base.update(extra)
    return base


def test_dispatch_embedded():
    data = _synthetic("embedded", kernel_arch="arm64", toolchain="aarch64-linux-gnu-gcc")
    assert resolve_build_toolchain(data)["kind"] == "embedded"


def test_dispatch_web():
    data = _synthetic(
        "web",
        runtime="node20",
        build_cmd="pnpm run build",
        bundle_size_budget="5MiB",
    )
    tc = resolve_build_toolchain(data)
    assert tc["kind"] == "web"
    assert tc["runtime"] == "node20"
    assert tc["build_cmd"] == "pnpm run build"
    # Web profiles must not require cross_prefix / arch / qemu — those
    # are embedded-only concerns.
    assert "cross_prefix" not in tc


def test_dispatch_mobile():
    data = _synthetic("mobile", mobile_platform="ios", min_os_version="16.0")
    tc = resolve_build_toolchain(data)
    assert tc["kind"] == "mobile"
    assert tc["mobile_platform"] == "ios"


def test_dispatch_software():
    data = _synthetic("software", software_runtime="python", packaging="pip")
    tc = resolve_build_toolchain(data)
    assert tc["kind"] == "software"
    assert tc["software_runtime"] == "python"


def test_dispatch_defaults_to_embedded_when_field_missing():
    """Backward compat: a profile with NO target_kind must resolve as
    embedded. This is the anchor point for the pre-W0 compatibility
    promise."""
    data = {"platform": "legacy", "label": "Legacy", "kernel_arch": "arm64",
            "toolchain": "aarch64-linux-gnu-gcc", "cross_prefix": "aarch64-linux-gnu-"}
    assert target_kind_of(data) == "embedded"
    assert resolve_build_toolchain(data)["kind"] == "embedded"


def test_invalid_target_kind_raises():
    """Typos like `target_kind: embeded` must fail loudly, not
    silently fall through to one of the valid kinds."""
    bad = _synthetic("web")
    bad["target_kind"] = "webz"
    with pytest.raises(PlatformProfileError):
        target_kind_of(bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  validate_profile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_validate_flags_missing_platform_and_label():
    errs = validate_profile({})
    assert any("platform" in e for e in errs)
    assert any("label" in e for e in errs)


def test_validate_flags_invalid_target_kind_without_raising():
    """validate_profile is advisory — it accumulates errors rather than
    raising, so UIs can surface multiple issues in one shot."""
    errs = validate_profile({"platform": "p", "label": "P", "target_kind": "bogus",
                             "kernel_arch": "arm64"})
    assert any("target_kind" in e for e in errs)


def test_validate_embedded_without_kernel_arch_warns():
    errs = validate_profile({"platform": "p", "label": "P", "target_kind": "embedded"})
    assert any("kernel_arch" in e for e in errs)


def test_validate_web_does_not_require_kernel_arch():
    """The whole point of W0: web profiles must not be forced to
    declare embedded-only fields."""
    errs = validate_profile({
        "platform": "web-x", "label": "Web X", "target_kind": "web",
        "runtime": "node20", "build_cmd": "npm run build",
    })
    assert errs == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Path-traversal hardening
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("bad_name", ["../etc/passwd", "", "foo/bar", "a" * 100])
def test_rejects_unsafe_platform_names(bad_name):
    with pytest.raises(PlatformProfileError):
        load_raw_profile(bad_name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X0 #296 — Software platform profiles (5 targets)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These lock in the five X0 profiles that X1 simulate-track (#297),
# X3 package adapters (#299), X4 license scan (#300), and the X5-X9
# skill pilots dispatch on. Regressions on any of these fields will
# break downstream X-series work, so we pin shape, kind, and the
# discriminating fields (host_arch / host_os / packaging).

X0_PROFILES = (
    "linux-x86_64-native",
    "linux-arm64-native",
    "windows-msvc-x64",
    "macos-arm64-native",
    "macos-x64-native",
)


@pytest.mark.parametrize("profile_id", X0_PROFILES)
def test_x0_profile_is_enumerated(profile_id):
    """All five X0 profiles must be discoverable via list_profile_ids —
    this is what X1's simulate-track module selector iterates over."""
    assert profile_id in list_profile_ids()


@pytest.mark.parametrize("profile_id", X0_PROFILES)
def test_x0_profile_declares_software_kind(profile_id):
    """Every X0 profile must dispatch to _resolve_software. If one
    accidentally lands as `embedded`, X1's software track skips it."""
    data = load_raw_profile(profile_id)
    assert data.get("target_kind") == "software"
    cfg = get_platform_config(profile_id)
    assert cfg["build_toolchain"]["kind"] == "software"


@pytest.mark.parametrize("profile_id", X0_PROFILES)
def test_x0_profile_validates_clean(profile_id):
    """X0 profiles must carry no validation errors — they're the
    reference examples operators copy when adding a new software
    target, so they have to be a clean baseline."""
    data = load_raw_profile(profile_id)
    assert validate_profile(data) == []


@pytest.mark.parametrize(
    "profile_id,expected_arch,expected_os,expected_pkg",
    [
        ("linux-x86_64-native", "x86_64", "linux", "deb"),
        ("linux-arm64-native", "arm64", "linux", "deb"),
        ("windows-msvc-x64", "x64", "windows", "msi"),
        ("macos-arm64-native", "arm64", "darwin", "dmg"),
        ("macos-x64-native", "x86_64", "darwin", "dmg"),
    ],
)
def test_x0_profile_host_shape(profile_id, expected_arch, expected_os, expected_pkg):
    """Discriminating fields — X3 package adapter and X1 language
    dispatcher read these to pick the right toolchain + installer
    generator."""
    data = load_raw_profile(profile_id)
    assert data["host_arch"] == expected_arch
    assert data["host_os"] == expected_os
    assert data["packaging"] == expected_pkg


@pytest.mark.parametrize("profile_id", X0_PROFILES)
def test_x0_profile_software_runtime_is_native(profile_id):
    """X0 profiles describe a HOST shape, not a language runtime.
    `software_runtime: native` is the agreed default; X2 role skills
    override to python/node/jvm at project level, not here."""
    data = load_raw_profile(profile_id)
    assert data["software_runtime"] == "native"


@pytest.mark.parametrize("profile_id", X0_PROFILES)
def test_x0_profile_build_cmd_is_non_empty_fallback(profile_id):
    """X1 simulate-track uses build_cmd as the diagnostic fallback when
    language autodetection fails. Empty string would silently skip the
    fallback — keep it non-empty."""
    data = load_raw_profile(profile_id)
    assert data["build_cmd"].strip()


def test_x0_does_not_duplicate_host_native_or_aarch64():
    """Regression guard: X0 linux profiles must NOT collide with
    pre-existing embedded profiles (host_native / aarch64). Each must
    remain in its own target_kind silo so resolvers dispatch cleanly."""
    linux_x64 = load_raw_profile("linux-x86_64-native")
    linux_arm = load_raw_profile("linux-arm64-native")
    host_native = load_raw_profile("host_native")
    aarch64 = load_raw_profile("aarch64")

    assert linux_x64["target_kind"] == "software"
    assert linux_arm["target_kind"] == "software"
    assert host_native["target_kind"] == "embedded"
    assert aarch64["target_kind"] == "embedded"


def test_x0_macos_profiles_preserve_signing_shape_but_no_material():
    """macOS software targets require Developer ID signing, but profile
    must encode SHAPE only — signing_identity stays empty; P3 HSM
    injects the material at build time. Pin the empty contract so a
    well-meaning edit can't paste a cert fingerprint into the repo."""
    for pid in ("macos-arm64-native", "macos-x64-native"):
        data = load_raw_profile(pid)
        assert "signing_identity" in data
        assert data["signing_identity"] == ""
        assert data["sdk_root"].endswith("Developer")
        assert data["toolchain_path"].endswith("clang")


def test_x0_windows_profile_declares_msvc_pins():
    """Windows MSVC profile must pin VS 2022 + Windows 10 SDK — X1
    sandbox installs this exact pair and X3 MSI adapter links against
    it. Drifting either is a breaking change."""
    data = load_raw_profile("windows-msvc-x64")
    assert data["msvc_version"] == "17.0"
    assert data["windows_sdk_version"].startswith("10.0.")
    assert any(p.startswith("visualstudio2022") for p in data["choco_packages"])


def test_x0_linux_profiles_share_docker_base_packages():
    """linux-x86_64-native and linux-arm64-native must carry the same
    minimal base package set — the X1 sandbox image dispatches on
    host_arch, not on an arch-specific package list. Any divergence
    here should be intentional and pinned separately."""
    x64 = load_raw_profile("linux-x86_64-native")["docker_packages"]
    arm = load_raw_profile("linux-arm64-native")["docker_packages"]
    assert x64 == arm
    assert "build-essential" in x64
    assert "pkg-config" in x64
