#!/usr/bin/env python3
"""SBOM signing scaffold — L4-CORE-15 Security Stack.

Wraps sigstore/cosign for signing and verifying Software Bill of Materials
in SPDX or CycloneDX format.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SBOMSignResult:
    sbom_path: str
    signature_path: str = ""
    success: bool = False
    log_entry: str = ""
    error: str = ""


def generate_sbom_spdx(project_dir: str, output_path: str) -> str:
    """Generate SPDX SBOM using syft (if available)."""
    try:
        proc = subprocess.run(
            ["syft", project_dir, "-o", f"spdx-json={output_path}"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            return output_path
        return ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback: generate minimal SBOM stub
        sbom = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": Path(project_dir).name,
            "documentNamespace": f"https://spdx.org/spdxdocs/{Path(project_dir).name}",
            "packages": [],
        }
        Path(output_path).write_text(json.dumps(sbom, indent=2))
        return output_path


def sign_with_cosign(
    file_path: str,
    key_path: str = "",
    keyless: bool = False,
) -> SBOMSignResult:
    """Sign a file with cosign."""
    cmd = ["cosign", "sign-blob", file_path, "--yes"]

    if keyless:
        cmd += ["--output-signature", f"{file_path}.sig"]
    elif key_path:
        cmd += ["--key", key_path, "--output-signature", f"{file_path}.sig"]
    else:
        return SBOMSignResult(
            sbom_path=file_path,
            error="Must provide key_path or set keyless=True",
        )

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return SBOMSignResult(
            sbom_path=file_path,
            signature_path=f"{file_path}.sig",
            success=proc.returncode == 0,
            log_entry=proc.stdout.strip() if proc.stdout else "",
            error=proc.stderr.strip() if proc.returncode != 0 else "",
        )
    except FileNotFoundError:
        return SBOMSignResult(
            sbom_path=file_path,
            error="cosign not found — install via: go install github.com/sigstore/cosign/v2/cmd/cosign@latest",
        )
    except subprocess.TimeoutExpired:
        return SBOMSignResult(sbom_path=file_path, error="Signing timed out")


def verify_with_cosign(
    file_path: str,
    signature_path: str = "",
    key_path: str = "",
) -> bool:
    """Verify a cosign signature."""
    sig_path = signature_path or f"{file_path}.sig"
    cmd = ["cosign", "verify-blob", file_path, "--signature", sig_path]

    if key_path:
        cmd += ["--key", key_path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <sign|verify|generate> <path> [--key KEY]")
        sys.exit(1)

    action = sys.argv[1]
    target = sys.argv[2]
    key = ""
    if "--key" in sys.argv:
        key = sys.argv[sys.argv.index("--key") + 1]

    if action == "generate":
        out = generate_sbom_spdx(target, f"{target}/sbom.spdx.json")
        print(f"SBOM generated: {out}")
    elif action == "sign":
        result = sign_with_cosign(target, key_path=key, keyless=not key)
        print(f"Sign result: success={result.success}, sig={result.signature_path}")
    elif action == "verify":
        ok = verify_with_cosign(target, key_path=key)
        print(f"Verification: {'PASS' if ok else 'FAIL'}")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)
