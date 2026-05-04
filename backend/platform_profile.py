"""Platform profile loader & toolchain dispatcher (W0 #274).

This module is the single entry point for resolving a platform profile
into a normalized config dict, regardless of whether the target is an
embedded SoC, a web runtime, a mobile OS, or plain host software. It
replaces the historical contract where every consumer assumed the
`configs/platforms/*.yaml` shape meant cross-compile.

Renamed from ``backend.platform`` → ``backend.platform_profile`` in
FX.9.3 (2026-05-04) to permanently end the stdlib shadow trap: the old
name shadowed CPython's stdlib ``platform`` whenever ``backend/`` landed
on ``sys.path[0]`` (alembic CLI, pytest cwd=backend/, multiprocessing
spawn). Several earlier defensive workarounds in CI / bootstrap /
multi-worker fixtures targeted that shadow; with the rename they remain
as belt-and-suspenders but are no longer load-bearing.

Why this exists
---------------
Pre-W0 the project only supported embedded cross-compile profiles
(aarch64 / armv7 / riscv64 / vendor SoC / host_native). Priorities
W (web), P (mobile), X (software) all need to share the profile loader
but produce radically different build toolchains:
  * embedded → gcc/clang + sysroot + cmake toolchain file
  * web      → node + package manager + bundler
  * mobile   → xcodebuild / gradle / flutter / react-native CLI
  * software → python / node / jvm / system compiler

We dispatch on the `target_kind` field declared in the YAML.

Backward compatibility
----------------------
Profiles that omit `target_kind` are treated as `embedded`. All the
in-tree profiles were updated in the same Phase to declare it
explicitly, but external/legacy profile files still load unchanged.

Public API
----------
    get_platform_config(platform: str) -> dict
        Resolve a profile id to a normalized dict. Adds the dispatched
        toolchain block under `build_toolchain`.

    load_raw_profile(platform: str) -> dict
        Just parse the YAML (no resolver). Useful for consumers that
        need to peek before deciding to dispatch.

    validate_profile(data: dict) -> list[str]
        Return a list of validation errors; empty list = valid.

    resolve_build_toolchain(data: dict) -> dict
        Given a parsed profile, return the dispatched toolchain block.

    TARGET_KINDS: frozenset
        Valid enum values for `target_kind`.

The legacy `backend.agents.tools.get_platform_config` tool (LangChain
`@tool`-decorated coroutine) still exists for agent-facing usage and
continues to produce its historical text output — it is unaffected by
this refactor by design. This module is the *library* layer underneath
it; tools.get_platform_config will migrate to consume this in a
follow-up once W1/W2 land.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PLATFORMS_DIR = _PROJECT_ROOT / "configs" / "platforms"
_PLATFORM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Files under configs/platforms/ that are NOT platform profiles and must
# be skipped by any enumerator. `schema.yaml` is the schema declaration.
_NON_PROFILE_FILES: frozenset[str] = frozenset({"schema.yaml"})

TARGET_KINDS: frozenset[str] = frozenset({"embedded", "web", "mobile", "software"})
DEFAULT_TARGET_KIND = "embedded"


class PlatformProfileError(ValueError):
    """Raised when a profile id is invalid, missing, or malformed."""


def _validate_platform_name(name: str) -> bool:
    return bool(name) and bool(_PLATFORM_NAME_RE.match(name)) and ".." not in name


def _profile_path(platform: str) -> Path:
    if not _validate_platform_name(platform):
        raise PlatformProfileError(f"Invalid platform name: {platform!r}")
    candidate = (_PLATFORMS_DIR / f"{platform}.yaml").resolve(strict=False)
    try:
        candidate.relative_to(_PLATFORMS_DIR.resolve())
    except ValueError:
        raise PlatformProfileError(f"Refused path-escape: {platform!r}")
    return candidate


def load_raw_profile(platform: str) -> dict[str, Any]:
    """Parse a profile YAML into a dict. Raises on missing/invalid."""
    if platform in _NON_PROFILE_FILES or f"{platform}.yaml" in _NON_PROFILE_FILES:
        raise PlatformProfileError(f"{platform!r} is a schema file, not a profile")
    path = _profile_path(platform)
    if not path.exists():
        raise PlatformProfileError(f"Platform profile not found: {platform}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PlatformProfileError(f"Malformed YAML for {platform}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlatformProfileError(f"Profile {platform} did not parse to a mapping")
    return data


def target_kind_of(data: dict[str, Any]) -> str:
    """Return the `target_kind` for a parsed profile, defaulting to
    `embedded` when absent. Validates the value against TARGET_KINDS."""
    kind = (data.get("target_kind") or DEFAULT_TARGET_KIND)
    if kind not in TARGET_KINDS:
        raise PlatformProfileError(
            f"Invalid target_kind: {kind!r} (must be one of {sorted(TARGET_KINDS)})"
        )
    return kind


def validate_profile(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors for a parsed profile.

    An empty list means the profile is valid. Callers may choose to
    treat warnings as fatal or not; current consumers use this for
    surfacing operator-visible diagnostics, not hard-gating load.
    """
    errs: list[str] = []
    if not data.get("platform"):
        errs.append("missing required field: platform")
    if not data.get("label"):
        errs.append("missing required field: label")

    # target_kind: if present, must be a known value; if absent, we
    # tolerate it (→ embedded) but the caller is encouraged to declare.
    raw_kind = data.get("target_kind")
    if raw_kind is not None and raw_kind not in TARGET_KINDS:
        errs.append(
            f"invalid target_kind: {raw_kind!r} "
            f"(must be one of {sorted(TARGET_KINDS)})"
        )

    kind = raw_kind or DEFAULT_TARGET_KIND
    if kind == "embedded":
        # embedded profiles historically required kernel_arch for
        # ARCH= / CROSS_COMPILE= dispatch. host_native intentionally
        # declares an empty kernel_arch so we only error on missing key.
        if "kernel_arch" not in data:
            errs.append("embedded profile missing kernel_arch")
    return errs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Toolchain resolvers (dispatched by target_kind)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_embedded(data: dict[str, Any]) -> dict[str, Any]:
    """Build toolchain block for an embedded (cross-compile) target.

    Mirrors the historical contract used by `backend.agents.tools
    .get_platform_config`: ARCH / CROSS_COMPILE / TOOLCHAIN / QEMU /
    SYSROOT / CMAKE_TOOLCHAIN_FILE. We expose them in the returned
    dict unchanged so downstream consumers can keep using the same
    keys they already recognise.
    """
    return {
        "kind": "embedded",
        "toolchain": data.get("toolchain") or "gcc",
        "cross_prefix": data.get("cross_prefix", ""),
        "arch": data.get("kernel_arch", ""),
        "arch_flags": data.get("arch_flags", ""),
        "qemu": data.get("qemu", ""),
        "sysroot_path": data.get("sysroot_path", ""),
        "cmake_toolchain_file": data.get("cmake_toolchain_file", ""),
        "docker_packages": list(data.get("docker_packages") or []),
    }


def _resolve_web(data: dict[str, Any]) -> dict[str, Any]:
    """Build toolchain block for a web target (W1 #275 consumer).

    No cross-compile — the "toolchain" here is the JS runtime plus the
    build command. We pick sensible defaults so a partially-declared
    profile still loads without exploding.
    """
    return {
        "kind": "web",
        "runtime": data.get("runtime") or "node20",
        "runtime_version": data.get("runtime_version", ""),
        "build_cmd": data.get("build_cmd") or "npm run build",
        "bundle_size_budget": data.get("bundle_size_budget", ""),
        "memory_limit_mb": data.get("memory_limit_mb", 0),
        "deploy_provider": data.get("deploy_provider", ""),
    }


def _resolve_mobile(data: dict[str, Any]) -> dict[str, Any]:
    """Build toolchain block for a mobile target (P-series consumer, P0 #285).

    Surfaces iOS / Android build knobs that the P1 toolchain integration
    (Docker image + fastlane + gradle wrapper) will consume. None of
    these are required — partial profiles still resolve so the P2
    simulate-track gate can run against a profile that hasn't finished
    plumbing signing yet.

    ``android_cli_available`` (P11 #351) is surfaced as ``Optional[bool]``
    — ``None`` means the profile did not declare one (iOS + legacy
    Android profiles pre-P11 checkbox 5). Callers that want to enforce
    the CLI fast path should treat ``None`` as "don't know, fall back to
    PATH probe"; ``False`` is a positive opt-out signal that overrides
    the PATH probe.
    """
    raw_cli_flag = data.get("android_cli_available")
    android_cli_available: bool | None = (
        bool(raw_cli_flag) if raw_cli_flag is not None else None
    )
    return {
        "kind": "mobile",
        "mobile_platform": data.get("mobile_platform", ""),
        "mobile_abi": data.get("mobile_abi", ""),
        "min_os_version": data.get("min_os_version", ""),
        "target_os_version": data.get("target_os_version", ""),
        "signing_identity": data.get("signing_identity", ""),
        "sdk_version": data.get("sdk_version", ""),
        "sdk_root": data.get("sdk_root", ""),
        "ndk_root": data.get("ndk_root", ""),
        "toolchain_path": data.get("toolchain_path", ""),
        "emulator_spec": dict(data.get("emulator_spec") or {}),
        "build_cmd": data.get("build_cmd", ""),
        "android_cli_available": android_cli_available,
    }


def _resolve_software(data: dict[str, Any]) -> dict[str, Any]:
    """Build toolchain block for pure software targets (X-series)."""
    return {
        "kind": "software",
        "software_runtime": data.get("software_runtime", ""),
        "packaging": data.get("packaging", ""),
        "build_cmd": data.get("build_cmd", ""),
    }


_RESOLVERS = {
    "embedded": _resolve_embedded,
    "web": _resolve_web,
    "mobile": _resolve_mobile,
    "software": _resolve_software,
}


def resolve_build_toolchain(data: dict[str, Any]) -> dict[str, Any]:
    """Return the dispatched toolchain block for a parsed profile."""
    return _RESOLVERS[target_kind_of(data)](data)


def get_platform_config(platform: str) -> dict[str, Any]:
    """Load a profile and return `{...raw fields, "target_kind", "build_toolchain"}`.

    This is the library-layer entry point. It is intentionally
    synchronous and returns a dict rather than text — the agent-facing
    coroutine tool in `backend.agents.tools` remains the text-returning
    surface.
    """
    data = load_raw_profile(platform)
    kind = target_kind_of(data)
    build = resolve_build_toolchain(data)
    return {
        **data,
        "target_kind": kind,
        "build_toolchain": build,
    }


def list_profile_ids() -> list[str]:
    """Enumerate every profile id under configs/platforms/ (excluding
    the schema declaration). Sorted for stable output."""
    ids: list[str] = []
    if not _PLATFORMS_DIR.is_dir():
        return ids
    for entry in _PLATFORMS_DIR.iterdir():
        if not entry.is_file() or entry.suffix != ".yaml":
            continue
        if entry.name in _NON_PROFILE_FILES:
            continue
        ids.append(entry.stem)
    return sorted(ids)
