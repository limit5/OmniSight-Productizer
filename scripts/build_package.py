#!/usr/bin/env python3
"""X3 #299 — Build & package CLI driver.

Thin shell over ``backend.build_adapters.build_artifact``. Mirrors the
``scripts/simulate.sh --type=software`` contract: one invocation, one
JSON summary on stdout, exit status reflects pass / fail / skip.

Usage
-----
    scripts/build_package.py --target=docker \
        --app-path=. --name=omnisight-backend --version=1.2.3 \
        --registry=ghcr --registry-arg=namespace=anthropic --push

    scripts/build_package.py --list-targets
    scripts/build_package.py --role=backend-rust --app-path=./service \
        --name=foo --version=0.4.0    # runs every default target

Exit codes
----------
    0  every requested target either passed or was skipped (tool absent
       on host) AND at least one passed
    1  at least one target failed (tool ran, exit non-zero)
    2  caller-side error (unknown target / invalid version / bad source)
    3  every target was skipped (no tool available on host) — caller can
       distinguish "not applicable here" from "actually broken"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import build_adapters  # noqa: E402


def _parse_kv_list(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        if "=" not in v:
            raise SystemExit(f"--registry-arg / --extra must be key=value, got {v!r}")
        k, val = v.split("=", 1)
        out[k.strip()] = val.strip()
    return out


def _emit(result: build_adapters.BuildResult | dict, *, pretty: bool) -> None:
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if pretty:
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        json.dump(payload, sys.stdout, default=str)
        sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build & package adapter CLI (X3 #299)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--target", help="adapter id (docker / helm / deb / rpm / msi / nsis / dmg / pkg / cargo-dist / goreleaser / pyinstaller / electron-builder)")
    p.add_argument("--role", help="X2 software role id — runs every default target for that role")
    p.add_argument("--list-targets", action="store_true", help="print every registered target id and exit")
    p.add_argument("--app-path", default=".", help="path to source tree (default: cwd)")
    p.add_argument("--name", help="artifact / image / chart name")
    p.add_argument("--version", help="version string (semver-shaped)")
    p.add_argument("--arch", default="noarch", help="architecture tag (default: noarch)")
    p.add_argument("--output-dir", default=".artifacts/builds", help="where to drop produced artifacts")
    p.add_argument("--manifest", help="explicit path to Dockerfile / Chart.yaml / .wxs / .nsi / spec")
    p.add_argument("--push", action="store_true", help="docker push / goreleaser release after build")
    p.add_argument("--registry", help="ghcr / dockerhub / ecr / gcr / acr / private")
    p.add_argument("--registry-arg", action="append", default=[], help="key=value (repeatable) — e.g. namespace=foo, account=123456789012, region=us-east-1")
    p.add_argument("--extra", action="append", default=[], help="key=value (repeatable) — adapter-specific extras (entrypoint, identifier, install_location, ext, platform)")
    p.add_argument("--pretty", action="store_true", help="indented JSON output")
    p.add_argument("--ignore-host-mismatch", action="store_true", help="do not raise when host can't run target — emit skip instead")

    args = p.parse_args(argv)

    if args.list_targets:
        json.dump({"targets": build_adapters.list_targets()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not args.role and not args.target:
        p.error("one of --target or --role is required")
    if not args.name or not args.version:
        p.error("--name and --version are required (unless --list-targets)")

    registry_args = _parse_kv_list(args.registry_arg)
    extra = _parse_kv_list(args.extra)

    targets: list[str]
    if args.role:
        targets = list(build_adapters.default_targets_for_role(args.role))
        if not targets:
            p.error(f"no default targets configured for role {args.role!r}")
    else:
        targets = [args.target]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Path(args.manifest) if args.manifest else None

    results: list[build_adapters.BuildResult] = []
    for tgt in targets:
        try:
            r = build_adapters.build_artifact(
                target=tgt,
                app_path=Path(args.app_path),
                name=args.name,
                version=args.version,
                arch=args.arch,
                output_dir=out_dir,
                push=args.push,
                registry=args.registry,
                registry_args=registry_args,
                manifest=manifest,
                extra=extra,
            )
        except build_adapters.HostMismatchError as exc:
            if not args.ignore_host_mismatch:
                print(json.dumps({"error": "host_mismatch", "target": tgt, "detail": str(exc)}), file=sys.stderr)
                return 2
            r = build_adapters.BuildResult(
                target=tgt, name=args.name, version=args.version, arch=args.arch,
                available=False, ok=False,
                notes=[f"host mismatch: {exc}"],
            )
        except (build_adapters.UnknownTargetError, build_adapters.InvalidVersionError, build_adapters.ArtifactSourceError, build_adapters.BuildAdapterError) as exc:
            print(json.dumps({"error": type(exc).__name__, "target": tgt, "detail": str(exc)}), file=sys.stderr)
            return 2
        results.append(r)

    summary = {
        "ticket": "X3 #299",
        "results": [r.to_dict() for r in results],
        "counts": {
            "pass": sum(1 for r in results if r.status() == "pass"),
            "fail": sum(1 for r in results if r.status() == "fail"),
            "skip": sum(1 for r in results if r.status() == "skip"),
        },
    }
    if args.pretty:
        json.dump(summary, sys.stdout, indent=2, default=str)
    else:
        json.dump(summary, sys.stdout, default=str)
    sys.stdout.write("\n")

    counts = summary["counts"]
    if counts["fail"]:
        return 1
    if counts["pass"] == 0 and counts["skip"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
