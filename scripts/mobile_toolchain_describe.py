#!/usr/bin/env python3
"""P1 #286 — Describe a mobile profile's resolved toolchain.

Human-readable CLI that prints what `backend.mobile_toolchain.
resolve_mobile_toolchain()` returns for a given profile id, so
operators can sanity-check their `OMNISIGHT_MACOS_BUILDER` /
`OMNISIGHT_MOBILE_IMAGE_TAG` env before kicking off a real build.

Usage:
    python3 scripts/mobile_toolchain_describe.py ios-arm64
    python3 scripts/mobile_toolchain_describe.py android-arm64-v8a

Exit codes:
    0 — profile resolved
    2 — configuration error (missing env, unknown builder, etc.)
    3 — profile not found / not mobile
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import mobile_toolchain  # noqa: E402
from backend.platform import PlatformProfileError  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <profile_id>", file=sys.stderr)
        return 2
    profile_id = argv[1]
    try:
        tc = mobile_toolchain.resolve_mobile_toolchain(profile_id)
    except mobile_toolchain.MacOSBuilderRequiredError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            f"\nSet {mobile_toolchain.ENV_MACOS_BUILDER}= to one of:",
            file=sys.stderr,
        )
        for name in sorted(mobile_toolchain.SUPPORTED_MACOS_BUILDERS):
            print(f"    - {name}", file=sys.stderr)
        return 2
    except mobile_toolchain.UnknownMacOSBuilderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except mobile_toolchain.UnsupportedPlatformError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except PlatformProfileError as e:
        print(f"ERROR: unknown profile {profile_id!r}: {e}", file=sys.stderr)
        return 3

    print(mobile_toolchain.describe(tc))
    if tc.android is not None:
        print(f"  sdk_root        : {tc.android.sdk_root}")
        print(f"  ndk_root        : {tc.android.ndk_root}")
        print(f"  toolchain_path  : {tc.android.toolchain_path}")
        print(f"  build_cmd       : {tc.android.build_cmd}")
        print(
            f"  local_docker    : "
            f"{'available' if tc.android.local_docker_available else 'missing'}"
        )
    if tc.macos is not None:
        print(f"  kind            : {tc.macos.kind}")
        print(f"  host_hint       : {tc.macos.host_hint or '(unset)'}")
        if tc.macos.env_forward:
            print("  env_forward     :")
            for name in tc.macos.env_forward:
                print(f"      - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
