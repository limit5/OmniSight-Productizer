"""P6 #291 — CLI entry for the mobile compliance bundle.

Usage:
    python3 -m backend.mobile_compliance --app-path=./ios-app \
        [--platform=ios] [--min-target-sdk=35] \
        [--catalogue=configs/privacy_label_sdks.yaml] \
        [--json-out=/tmp/p6.json] \
        [--label-ios-out=/tmp/nutrition.json] \
        [--data-safety-out=/tmp/data_safety.yaml]

Exits 0 iff the bundle passes; 1 otherwise. The JSON bundle is always
written (stdout by default) so CI can persist the evidence.

The ``--label-ios-out`` and ``--data-safety-out`` flags are convenience
shortcuts that extract just the nutrition-label JSON / data-safety YAML
from the privacy_labels gate — for direct upload to ASC or Play Console.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.mobile_compliance.bundle import MIN_TARGET_SDK, run_all


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3 -m backend.mobile_compliance",
        description="Run the P6 mobile compliance bundle (ASC / Play / Privacy Label).",
    )
    p.add_argument("--app-path", required=True, help="Mobile project root")
    p.add_argument(
        "--platform",
        choices=["ios", "android", "both"],
        default="both",
        help="Restrict which gates run (default: both)",
    )
    p.add_argument(
        "--min-target-sdk",
        type=int,
        default=MIN_TARGET_SDK,
        help=f"Play targetSdk floor (default: {MIN_TARGET_SDK})",
    )
    p.add_argument(
        "--catalogue",
        default=None,
        help="Override path to the SDK → privacy-category YAML catalogue.",
    )
    p.add_argument(
        "--json-out",
        default=None,
        help="Write bundle JSON to this path (default: stdout)",
    )
    p.add_argument(
        "--label-ios-out",
        default=None,
        help="Write iOS nutrition-label JSON to this path (optional)",
    )
    p.add_argument(
        "--data-safety-out",
        default=None,
        help="Write Play data-safety YAML to this path (optional)",
    )
    return p.parse_args(argv)


def _write_data_safety_yaml(data: dict, path: Path) -> None:
    try:
        import yaml
    except ImportError:  # pragma: no cover — yaml is a base dep
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
        return
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    bundle = run_all(
        Path(args.app_path),
        platform=args.platform,
        min_target_sdk=args.min_target_sdk,
        catalogue_path=Path(args.catalogue) if args.catalogue else None,
    )
    payload = json.dumps(bundle.to_dict(), indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(payload)
    else:
        sys.stdout.write(payload + "\n")

    # Extract privacy-label side outputs on demand.
    privacy_gate = bundle.get("privacy_labels")
    if privacy_gate is not None:
        detail = privacy_gate.detail
        if args.label_ios_out and detail.get("nutrition_label_ios"):
            Path(args.label_ios_out).write_text(
                json.dumps(
                    detail["nutrition_label_ios"], indent=2, sort_keys=True,
                )
            )
        if args.data_safety_out and detail.get("data_safety_form"):
            _write_data_safety_yaml(
                detail["data_safety_form"], Path(args.data_safety_out),
            )

    return 0 if bundle.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
