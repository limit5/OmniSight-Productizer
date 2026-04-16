"""W5 #279 — Web compliance gates package.

Three independent gates that the web-vertical CI must pass before a build
is allowed to ship:

    * WCAG 2.2 AA accessibility scan (``wcag.py``)
    * GDPR posture scan (``gdpr.py``)
    * SPDX license allow/deny scan (``spdx.py``)

Each gate returns a ``GateReport`` dataclass; the ``run_all()`` orchestrator
bundles them into a single ``ComplianceBundle`` that plugs into the C8
compliance harness's audit-log pipeline as an ``external`` tool verdict so
the existing evidence-bundle consumers (DE, HMI, PR gate) pick it up for
free.

Design mirrors ``backend/web_simulator.py``: every external CLI (axe-core,
``@npmcli/arborist``, etc.) is optional; the gate degrades to a ``mock``
source in sandbox / first-run environments so unit tests don't require
``npm`` on the runner. Real CI runs install the tool and get the actual
scan.
"""

from __future__ import annotations

from backend.web_compliance.bundle import (
    ComplianceBundle,
    GateReport,
    GateVerdict,
    run_all,
    bundle_to_compliance_report,
)
from backend.web_compliance.gdpr import GDPRReport, scan_gdpr
from backend.web_compliance.spdx import (
    DEFAULT_DENY_LICENSES,
    SPDXReport,
    scan_licenses,
)
from backend.web_compliance.wcag import (
    WCAG_AA_MANUAL_CHECKLIST,
    WCAGReport,
    run_wcag_scan,
)

__all__ = [
    "ComplianceBundle",
    "DEFAULT_DENY_LICENSES",
    "GDPRReport",
    "GateReport",
    "GateVerdict",
    "SPDXReport",
    "WCAGReport",
    "WCAG_AA_MANUAL_CHECKLIST",
    "bundle_to_compliance_report",
    "run_all",
    "scan_gdpr",
    "scan_licenses",
    "run_wcag_scan",
]
