"""X4 #300 — Software compliance bundle orchestrator.

Runs the three X4 gates (license allow/deny, CVE scan, SBOM emit) and
returns a single ``SoftwareComplianceBundle`` that plugs into the C8
compliance-harness audit log (same contract as ``web_compliance``).

    license  — multi-ecosystem SPDX deny/allow scan (``licenses.py``)
    cve      — vulnerability scan via trivy/grype/osv (``cves.py``)
    sbom     — SBOM emit in CycloneDX or SPDX (``sbom.py``)

The sbom "gate" is advisory: emitting an SBOM is always technically
possible (we ship our own emitter), so it returns ``pass`` when the
file is written and ``error`` when IO fails. It never blocks a ship.
License and CVE are the ones that can FAIL a build.

Exit-code mapping (mirrors W5 ``web_compliance``):
    pass    → all gates pass or are skipped (no fails)
    fail    → at least one gate failed
    error   → a gate crashed
    skipped → tool missing on host
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.software_compliance.cves import (
    CVEReport,
    DEFAULT_FAIL_ON,
    scan_cves,
)
from backend.software_compliance.licenses import (
    DEFAULT_DENY_LICENSES,
    LicenseReport,
    detect_ecosystem,
    scan_licenses,
)
from backend.software_compliance.sbom import (
    SBOM_FORMATS,
    SBOMDocument,
    emit_sbom,
)

logger = logging.getLogger(__name__)


class GateVerdict(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"
    error = "error"
    skipped = "skipped"


@dataclass
class GateReport:
    gate_id: str  # "license" / "cve" / "sbom"
    name: str
    verdict: GateVerdict
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.pass_


@dataclass
class SoftwareComplianceBundle:
    app_path: str
    ecosystem: str = ""
    timestamp: float = field(default_factory=time.time)
    gates: list[GateReport] = field(default_factory=list)
    sbom: Optional[SBOMDocument] = None

    @property
    def passed(self) -> bool:
        if not self.gates:
            return False
        return all(
            g.verdict != GateVerdict.fail and g.verdict != GateVerdict.error
            for g in self.gates
        )

    @property
    def passed_count(self) -> int:
        return sum(1 for g in self.gates if g.passed)

    @property
    def skipped_count(self) -> int:
        return sum(1 for g in self.gates if g.verdict == GateVerdict.skipped)

    @property
    def failed_count(self) -> int:
        return sum(1 for g in self.gates if g.verdict == GateVerdict.fail)

    def get(self, gate_id: str) -> Optional[GateReport]:
        for g in self.gates:
            if g.gate_id == gate_id:
                return g
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "app_path": self.app_path,
            "ecosystem": self.ecosystem,
            "timestamp": self.timestamp,
            "passed": self.passed,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "total_gates": len(self.gates),
            "gates": [
                {
                    "gate_id": g.gate_id,
                    "name": g.name,
                    "verdict": g.verdict.value,
                    "summary": g.summary,
                    "detail": g.detail,
                }
                for g in self.gates
            ],
        }
        if self.sbom is not None:
            out["sbom"] = {
                "format": self.sbom.format,
                "component_name": self.sbom.component_name,
                "component_version": self.sbom.component_version,
                "bytes": len(self.sbom.content),
            }
        return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Individual gate wrappers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _license_gate(
    app_path: Path,
    ecosystem: Optional[str],
    deny: Iterable[str],
    allowlist: Iterable[str] | None,
) -> tuple[GateReport, LicenseReport]:
    report = scan_licenses(app_path, ecosystem=ecosystem, deny=deny, allowlist=allowlist)
    if report.error:
        verdict = GateVerdict.error
        summary = f"license scan error: {report.error}"
    elif report.source == "mock":
        verdict = GateVerdict.skipped
        summary = (
            f"no dependency scanner available for ecosystem={report.ecosystem!r}; "
            f"install cargo-license / go-licenses / pip-licenses / license-checker"
        )
    elif report.passed:
        verdict = GateVerdict.pass_
        summary = (
            f"{report.total_packages} {report.ecosystem} packages scanned via "
            f"{report.source}; 0 denied, {len(report.unknown)} unknown"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.denied)} {report.ecosystem} package(s) carry a "
            f"denied license"
        )
    return (
        GateReport(
            gate_id="license",
            name="SPDX license allow/deny",
            verdict=verdict,
            summary=summary,
            detail=report.to_dict(),
        ),
        report,
    )


def _cve_gate(
    app_path: Path,
    scanner: Optional[str],
    fail_on: Iterable[str],
) -> tuple[GateReport, CVEReport]:
    report = scan_cves(app_path, scanner=scanner, fail_on=fail_on)
    if report.error:
        verdict = GateVerdict.error
        summary = f"CVE scan error: {report.error}"
    elif report.source == "mock":
        verdict = GateVerdict.skipped
        summary = "no CVE scanner on PATH (install trivy / grype / osv-scanner)"
    elif report.passed:
        sev_str = ", ".join(
            f"{k}={v}" for k, v in sorted(report.severity_counts.items())
        ) or "no findings"
        verdict = GateVerdict.pass_
        summary = (
            f"{report.source} clean: {report.total_findings} total "
            f"({sev_str}); blocking thresholds {report.fail_on}"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.blocking_findings)} {report.fail_on} finding(s) "
            f"from {report.source}"
        )
    return (
        GateReport(
            gate_id="cve",
            name="CVE / vulnerability scan",
            verdict=verdict,
            summary=summary,
            detail=report.to_dict(),
        ),
        report,
    )


def _sbom_gate(
    license_report: LicenseReport,
    *,
    fmt: str,
    out_path: Optional[Path],
    component_name: str,
    component_version: str,
) -> tuple[GateReport, Optional[SBOMDocument]]:
    try:
        doc = emit_sbom(
            license_report,
            fmt=fmt,
            component_name=component_name,
            component_version=component_version,
        )
    except Exception as exc:
        return (
            GateReport(
                gate_id="sbom",
                name="SBOM emit",
                verdict=GateVerdict.error,
                summary=f"SBOM emit failed: {exc}",
                detail={"format": fmt, "error": str(exc)},
            ),
            None,
        )

    wrote_to = ""
    if out_path is not None:
        try:
            doc.write(out_path)
            wrote_to = str(out_path)
        except OSError as exc:
            return (
                GateReport(
                    gate_id="sbom",
                    name="SBOM emit",
                    verdict=GateVerdict.error,
                    summary=f"SBOM write failed: {exc}",
                    detail={"format": fmt, "error": str(exc)},
                ),
                doc,
            )

    component_count = len(license_report.allowed) + len(license_report.denied) + len(license_report.unknown)
    return (
        GateReport(
            gate_id="sbom",
            name=f"SBOM emit ({fmt})",
            verdict=GateVerdict.pass_,
            summary=(
                f"{fmt} SBOM with {component_count} components"
                + (f" written to {wrote_to}" if wrote_to else " (in-memory)")
            ),
            detail={
                "format": fmt,
                "component_count": component_count,
                "bytes": len(doc.content),
                "written_to": wrote_to,
            },
        ),
        doc,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_all(
    app_path: Path | str,
    *,
    ecosystem: Optional[str] = None,
    deny: Iterable[str] = DEFAULT_DENY_LICENSES,
    allowlist: Iterable[str] | None = None,
    cve_scanner: Optional[str] = None,
    cve_fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    sbom_format: str = "cyclonedx",
    sbom_out: Optional[Path | str] = None,
    component_name: str = "",
    component_version: str = "",
) -> SoftwareComplianceBundle:
    """Run license + CVE + SBOM emit and return one bundle.

    ``ecosystem`` forces a specific license scanner; otherwise
    auto-detected from marker files. ``sbom_out`` writes the SBOM to
    disk when provided; otherwise the document lives in-memory on the
    bundle and the gate still reports ``pass``.
    """
    root = Path(app_path).resolve()
    bundle = SoftwareComplianceBundle(app_path=str(root))

    eco = ecosystem or detect_ecosystem(root) or ""
    bundle.ecosystem = eco

    license_gate, license_report = _license_gate(root, ecosystem, deny, allowlist)
    cve_gate, _cve_report = _cve_gate(root, cve_scanner, cve_fail_on)

    sbom_path = Path(sbom_out).resolve() if sbom_out else None
    if sbom_format not in SBOM_FORMATS:
        sbom_gate = GateReport(
            gate_id="sbom",
            name="SBOM emit",
            verdict=GateVerdict.error,
            summary=f"unknown SBOM format {sbom_format!r} (supported: {SBOM_FORMATS})",
            detail={"format": sbom_format},
        )
        sbom_doc: Optional[SBOMDocument] = None
    else:
        sbom_gate, sbom_doc = _sbom_gate(
            license_report,
            fmt=sbom_format,
            out_path=sbom_path,
            component_name=component_name,
            component_version=component_version,
        )

    bundle.gates = [license_gate, cve_gate, sbom_gate]
    bundle.sbom = sbom_doc
    return bundle


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C8 compliance-harness bridge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def bundle_to_compliance_report(bundle: SoftwareComplianceBundle):
    """Convert a bundle into a C8 ``ComplianceReport`` for the audit
    hash-chain. Mirrors ``web_compliance.bundle_to_compliance_report``.
    """
    from backend.compliance_harness import (
        ComplianceProtocol,
        ComplianceReport,
        TestCaseResult,
        TestVerdict,
    )

    _map = {
        GateVerdict.pass_: TestVerdict.pass_,
        GateVerdict.fail: TestVerdict.fail,
        GateVerdict.error: TestVerdict.error,
        GateVerdict.skipped: TestVerdict.skipped,
    }
    results = [
        TestCaseResult(
            test_id=f"X4-{g.gate_id.upper()}",
            test_name=g.name,
            verdict=_map[g.verdict],
            evidence="",
            message=g.summary,
        )
        for g in bundle.gates
    ]

    # C8 ComplianceProtocol doesn't have a ``software`` slot — reuse
    # ``onvif`` with origin metadata so existing audit consumers keep
    # working. Same compromise as web_compliance.
    return ComplianceReport(
        tool_name="x4_software_compliance",
        protocol=ComplianceProtocol.onvif,
        device_under_test=bundle.app_path,
        timestamp=bundle.timestamp,
        results=results,
        raw_log_path="",
        metadata={
            "origin": "software_compliance",
            "ecosystem": bundle.ecosystem,
            "bundle": bundle.to_dict(),
        },
    )


__all__ = [
    "GateReport",
    "GateVerdict",
    "SoftwareComplianceBundle",
    "bundle_to_compliance_report",
    "run_all",
]
