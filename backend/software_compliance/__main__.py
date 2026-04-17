"""X4 #300 — CLI entry for the software compliance bundle.

Usage:
    python3 -m backend.software_compliance --app-path=./service \\
        [--ecosystem=cargo] [--allowlist=openssl,readline@GPL-3.0] \\
        [--cve-scanner=trivy] [--cve-fail-on=CRITICAL] \\
        [--sbom-format=cyclonedx] [--sbom-out=./sbom.cdx.json] \\
        [--component-name=foo --component-version=1.2.3] \\
        [--json-out=/tmp/x4.json]

Exit codes:
    0  bundle passed (all gates pass or skipped)
    1  at least one gate failed
    2  caller-side error (bad args, missing path)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.software_compliance.bundle import run_all
from backend.software_compliance.licenses import ECOSYSTEMS
from backend.software_compliance.sbom import SBOM_FORMATS


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3 -m backend.software_compliance",
        description="Run the X4 software compliance bundle (licenses / CVE / SBOM).",
    )
    p.add_argument("--app-path", required=True, help="Project root to scan")
    p.add_argument(
        "--ecosystem",
        default=None,
        choices=list(ECOSYSTEMS) + [None],
        help=f"Force ecosystem (default: auto-detect from marker files). Choices: {ECOSYSTEMS}",
    )
    p.add_argument(
        "--allowlist",
        default="",
        help="Comma-separated SPDX license allowlist (name or name@license)",
    )
    p.add_argument(
        "--deny",
        default="",
        help="Comma-separated SPDX denylist override (empty → use defaults)",
    )
    p.add_argument(
        "--cve-scanner",
        default=None,
        choices=["trivy", "grype", "osv-scanner", None],
        help="Force CVE scanner (default: first on PATH)",
    )
    p.add_argument(
        "--cve-fail-on",
        default="CRITICAL,HIGH",
        help="Comma-separated severities that fail the gate",
    )
    p.add_argument(
        "--sbom-format",
        default="cyclonedx",
        choices=list(SBOM_FORMATS),
        help="SBOM output format",
    )
    p.add_argument("--sbom-out", default=None, help="Write SBOM to this path")
    p.add_argument("--component-name", default="", help="Root component name in SBOM")
    p.add_argument("--component-version", default="", help="Root component version in SBOM")
    p.add_argument("--json-out", default=None, help="Write bundle JSON to this path (default: stdout)")
    return p.parse_args(argv)


def _split_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    app_path = Path(args.app_path)
    if not app_path.exists() or not app_path.is_dir():
        sys.stderr.write(f"app-path {app_path} is not a directory\n")
        return 2

    allowlist = _split_csv(args.allowlist)
    deny_override = _split_csv(args.deny)
    fail_on = _split_csv(args.cve_fail_on) or ["CRITICAL", "HIGH"]

    deny_kwargs: dict = {}
    if deny_override:
        from backend.software_compliance.licenses import DEFAULT_DENY_LICENSES
        deny_kwargs["deny"] = deny_override or list(DEFAULT_DENY_LICENSES)

    bundle = run_all(
        app_path,
        ecosystem=args.ecosystem,
        allowlist=allowlist,
        cve_scanner=args.cve_scanner,
        cve_fail_on=fail_on,
        sbom_format=args.sbom_format,
        sbom_out=args.sbom_out,
        component_name=args.component_name,
        component_version=args.component_version,
        **deny_kwargs,
    )
    payload = json.dumps(bundle.to_dict(), indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(payload)
    else:
        sys.stdout.write(payload + "\n")
    return 0 if bundle.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
