"""X4 #300 — Software compliance package.

Three independent gates that the X-series software-vertical CI must
pass before a build is allowed to ship:

    * Multi-ecosystem SPDX license allow/deny scan (``licenses.py``)
    * CVE / vulnerability scan via trivy/grype/osv-scanner (``cves.py``)
    * SBOM emit in CycloneDX or SPDX format (``sbom.py``)

Each gate returns a ``GateReport`` dataclass; the ``run_all()``
orchestrator bundles them into a single ``SoftwareComplianceBundle``
that plugs into the C8 compliance harness's audit-log pipeline as an
``external`` tool verdict so the existing evidence-bundle consumers
(DE, HMI, PR gate) pick it up for free.

Companion to ``backend/web_compliance`` (W5 #279, npm-only SPDX).
This package is the X-series equivalent across every language
ecosystem OmniSight skills can ship in.
"""

from __future__ import annotations

from backend.software_compliance.bundle import (
    GateReport,
    GateVerdict,
    SoftwareComplianceBundle,
    bundle_to_compliance_report,
    run_all,
)
from backend.software_compliance.cves import (
    CVEReport,
    DEFAULT_FAIL_ON,
    SEVERITY_ORDER,
    Vulnerability,
    scan_cves,
)
from backend.software_compliance.licenses import (
    DEFAULT_DENY_LICENSES,
    ECOSYSTEMS,
    LicenseReport,
    PackageLicense,
    detect_ecosystem,
    scan_licenses,
)
from backend.software_compliance.sbom import (
    SBOM_FORMATS,
    SBOMDocument,
    emit_sbom,
    to_cyclonedx,
    to_spdx,
)

__all__ = [
    # bundle
    "GateReport",
    "GateVerdict",
    "SoftwareComplianceBundle",
    "bundle_to_compliance_report",
    "run_all",
    # cves
    "CVEReport",
    "DEFAULT_FAIL_ON",
    "SEVERITY_ORDER",
    "Vulnerability",
    "scan_cves",
    # licenses
    "DEFAULT_DENY_LICENSES",
    "ECOSYSTEMS",
    "LicenseReport",
    "PackageLicense",
    "detect_ecosystem",
    "scan_licenses",
    # sbom
    "SBOM_FORMATS",
    "SBOMDocument",
    "emit_sbom",
    "to_cyclonedx",
    "to_spdx",
]
