#!/usr/bin/env python3
"""OP-1859 — run host-side Android HIL launch verification."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.agents.hil_verify import HilVerifyError, run_hil_verify


def _jsonable(result) -> dict:
    payload = asdict(result)
    payload["apk_path"] = str(result.apk_path)
    payload["artifacts_dir"] = str(result.artifacts_dir)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, install, launch, and logcat-check an Android APK"
    )
    parser.add_argument("project_key", help="JIRA project key, e.g. DEMO")
    parser.add_argument("--device", dest="device_serial", help="adb device serial")
    parser.add_argument(
        "--workspace",
        default=".artifacts/hil-verify",
        help="workspace and artifact directory root",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="seconds of logcat capture after launch",
    )
    args = parser.parse_args(argv)

    try:
        result = run_hil_verify(
            args.project_key,
            device_serial=args.device_serial,
            workspace_root=Path(args.workspace),
            capture_seconds=args.seconds,
        )
    except HilVerifyError as exc:
        print(json.dumps({"pass": False, "error": str(exc)}, indent=2))
        return 1

    print(json.dumps(_jsonable(result), indent=2))
    print(f"artifacts: {result.artifacts_dir}")
    return 0 if result.pass_ else 1


if __name__ == "__main__":
    raise SystemExit(main())
