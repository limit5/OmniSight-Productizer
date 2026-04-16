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
