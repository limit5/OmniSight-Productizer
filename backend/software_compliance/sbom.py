"""X4 #300 — SBOM emitter (CycloneDX 1.5 JSON and SPDX 2.3 tag-value).

Consumes the ``LicenseReport`` produced by ``licenses.scan_licenses``
and renders it as one of two industry-standard SBOM formats:

    cyclonedx — CycloneDX 1.5 JSON (OWASP, auditable by ``cyclonedx-cli``)
    spdx      — SPDX 2.3 tag-value plain text (ISO/IEC 5962)

We implement both formats natively — no external tool required.
``syft``/``trivy sbom``/``cyclonedx-cli`` can produce richer output
when available, but OmniSight ships its own emitter so a fresh clone
can export an auditable SBOM without any install step.

Why no external syft dep
------------------------
syft's richer SBOM includes file-level hashes and supplier URLs that
we don't actually have from the license-scan inputs. Shipping a minimal
but *valid* SBOM is worth more than gating the whole emit on a 200MB
Go binary being installed. Callers that want the richer output can
call ``syft`` themselves and merge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.software_compliance.licenses import LicenseReport, PackageLicense

logger = logging.getLogger(__name__)


SBOM_FORMATS: tuple[str, ...] = ("cyclonedx", "spdx")


# Map our ecosystem ids to PURL type prefixes (pkg:<type>/<name>@<ver>).
# CycloneDX + SPDX both reference PURLs so the same mapping works.
_PURL_TYPE: dict[str, str] = {
    "cargo": "cargo",
    "go": "golang",
    "pip": "pypi",
    "npm": "npm",
}


def _package_purl(pkg: PackageLicense) -> str:
    typ = _PURL_TYPE.get(pkg.ecosystem, pkg.ecosystem or "generic")
    name = pkg.name
    ver = pkg.version
    # For golang, PURLs use lowercase and keep slashes; pypi lowercases.
    if typ == "pypi":
        name = name.lower()
    if ver:
        return f"pkg:{typ}/{name}@{ver}"
    return f"pkg:{typ}/{name}"


def _package_spdx_id(pkg: PackageLicense, idx: int) -> str:
    """SPDX IDs must match ``SPDXRef-[A-Za-z0-9.-]+`` — sanitise the
    name and append the row index so collisions are impossible."""
    safe = re.sub(r"[^A-Za-z0-9.\-]", "-", f"{pkg.name}-{pkg.version}").strip("-")
    if not safe:
        safe = "pkg"
    return f"SPDXRef-Pkg-{idx}-{safe}"[:255]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CycloneDX 1.5 JSON
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def to_cyclonedx(report: LicenseReport, *, component_name: str = "", component_version: str = "") -> dict[str, Any]:
    """Render the license report as a CycloneDX 1.5 JSON document.

    ``component_name`` / ``component_version`` identify the root
    application. When omitted we synthesise from the app_path basename.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    root_name = component_name or Path(report.app_path).name or "omnisight-software"
    root_ver = component_version or "0.0.0"
    all_pkgs = [*report.allowed, *report.denied, *report.unknown]

    components: list[dict[str, Any]] = []
    for pkg in all_pkgs:
        comp: dict[str, Any] = {
            "type": "library",
            "name": pkg.name,
            "purl": _package_purl(pkg),
        }
        if pkg.version:
            comp["version"] = pkg.version
        if pkg.license and pkg.license != "UNKNOWN":
            # CycloneDX allows either a structured list of SPDX ids or
            # a free-form expression. We emit the expression form which
            # preserves compound expressions like ``(MIT OR Apache-2.0)``.
            comp["licenses"] = [{"expression": pkg.license}]
        components.append(comp)

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": ts,
            "tools": [
                {
                    "vendor": "OmniSight",
                    "name": "software_compliance",
                    "version": "1.0.0",
                }
            ],
            "component": {
                "type": "application",
                "name": root_name,
                "version": root_ver,
            },
        },
        "components": components,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SPDX 2.3 tag-value
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def to_spdx(report: LicenseReport, *, component_name: str = "", component_version: str = "") -> str:
    """Render the license report as SPDX 2.3 tag-value text."""
    root_name = component_name or Path(report.app_path).name or "omnisight-software"
    root_ver = component_version or "0.0.0"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc_ns_hash = hashlib.sha256(
        f"{root_name}:{root_ver}:{report.app_path}".encode()
    ).hexdigest()[:16]

    lines: list[str] = [
        "SPDXVersion: SPDX-2.3",
        "DataLicense: CC0-1.0",
        "SPDXID: SPDXRef-DOCUMENT",
        f"DocumentName: {root_name}",
        f"DocumentNamespace: https://omnisight.local/spdx/{root_name}-{doc_ns_hash}",
        "Creator: Tool: omnisight-software-compliance-1.0.0",
        f"Created: {ts}",
        "",
        f"PackageName: {root_name}",
        "SPDXID: SPDXRef-ROOT",
        f"PackageVersion: {root_ver}",
        "PackageDownloadLocation: NOASSERTION",
        "FilesAnalyzed: false",
        "PackageLicenseConcluded: NOASSERTION",
        "PackageLicenseDeclared: NOASSERTION",
        "PackageCopyrightText: NOASSERTION",
        "",
    ]

    all_pkgs = [*report.allowed, *report.denied, *report.unknown]
    for idx, pkg in enumerate(all_pkgs):
        spdx_id = _package_spdx_id(pkg, idx)
        license_concluded = pkg.license if pkg.license and pkg.license != "UNKNOWN" else "NOASSERTION"
        # SPDX license expressions are stricter than npm/rust freeform —
        # anything with a space that isn't an operator becomes NOASSERTION.
        if license_concluded != "NOASSERTION" and " " in license_concluded:
            if not any(op in license_concluded for op in (" OR ", " AND ", " WITH ")):
                license_concluded = "NOASSERTION"
        lines += [
            f"PackageName: {pkg.name}",
            f"SPDXID: {spdx_id}",
            f"PackageVersion: {pkg.version or 'NOASSERTION'}",
            "PackageDownloadLocation: NOASSERTION",
            "FilesAnalyzed: false",
            f"PackageLicenseConcluded: {license_concluded}",
            f"PackageLicenseDeclared: {license_concluded}",
            "PackageCopyrightText: NOASSERTION",
            f"ExternalRef: PACKAGE-MANAGER purl {_package_purl(pkg)}",
            "",
            f"Relationship: SPDXRef-ROOT DEPENDS_ON {spdx_id}",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SBOMDocument:
    format: str               # "cyclonedx" / "spdx"
    content: str              # serialised document (JSON for cdx, text for spdx)
    component_name: str = ""
    component_version: str = ""

    def write(self, out_path: Path | str) -> Path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.content, encoding="utf-8")
        return path


def emit_sbom(
    report: LicenseReport,
    *,
    fmt: str = "cyclonedx",
    component_name: str = "",
    component_version: str = "",
) -> SBOMDocument:
    """Serialise ``report`` to an SBOM document in the requested format."""
    fmt = fmt.strip().lower()
    if fmt not in SBOM_FORMATS:
        raise ValueError(f"unknown SBOM format {fmt!r} (supported: {SBOM_FORMATS})")
    if fmt == "cyclonedx":
        payload = to_cyclonedx(report, component_name=component_name, component_version=component_version)
        content = json.dumps(payload, indent=2, sort_keys=True)
    else:
        content = to_spdx(report, component_name=component_name, component_version=component_version)
    return SBOMDocument(
        format=fmt,
        content=content,
        component_name=component_name,
        component_version=component_version,
    )


__all__ = [
    "SBOMDocument",
    "SBOM_FORMATS",
    "emit_sbom",
    "to_cyclonedx",
    "to_spdx",
]
