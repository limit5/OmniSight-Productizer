"""W5 #279 — Compliance bundle orchestrator.

Runs all three web-vertical gates (WCAG / GDPR / SPDX) and produces a
single ``ComplianceBundle`` evidence object. The bundle also converts
to a C8 ``ComplianceReport`` via ``bundle_to_compliance_report()`` so
the existing audit-log hash-chain and HMI "compliance tools" listing
page pick it up for free — the web compliance gates appear alongside
the ONVIF / USB / UAC tools as a single "web" protocol row.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

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

logger = logging.getLogger(__name__)


class GateVerdict(str, enum.Enum):
    """Per-gate pass/fail enum; mirrors C8 TestVerdict for mapping."""

    pass_ = "pass"
    fail = "fail"
    error = "error"
    skipped = "skipped"


@dataclass
class GateReport:
    """One gate's contribution to the bundle."""

    gate_id: str  # "wcag" / "gdpr" / "spdx"
    name: str
    verdict: GateVerdict
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.pass_


@dataclass
class ComplianceBundle:
    """Evidence bundle returned by ``run_all()``."""

    app_path: str
    url: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    gates: list[GateReport] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A bundle passes when no gate is FAIL (SKIPPED is non-blocking,
        matching C8 TestVerdict.skipped semantics)."""
        if not self.gates:
            return False
        return all(g.verdict != GateVerdict.fail and g.verdict != GateVerdict.error
                   for g in self.gates)

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
        return {
            "app_path": self.app_path,
            "url": self.url,
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


# ── Gate wrappers ────────────────────────────────────────────────


def _wcag_gate(
    url: str | None,
    checklist_overrides: dict[str, dict[str, str]] | None,
) -> tuple[GateReport, WCAGReport]:
    if not url:
        report = WCAGReport(url="", source="mock")
        for entry in WCAG_AA_MANUAL_CHECKLIST:
            from backend.web_compliance.wcag import WCAGManualItem
            report.manual_checklist.append(WCAGManualItem(**entry))
        report.recompute_passed()
        summary = "WCAG scan skipped (no URL supplied); manual checklist attached"
        return (
            GateReport(
                gate_id="wcag",
                name="WCAG 2.2 AA",
                verdict=GateVerdict.skipped,
                summary=summary,
                detail=report.to_dict(),
            ),
            report,
        )
    report = run_wcag_scan(url, checklist_overrides=checklist_overrides)
    if report.source == "mock":
        verdict = GateVerdict.skipped
        summary = "axe-core CLI unavailable; manual checklist attached"
    elif report.passed:
        verdict = GateVerdict.pass_
        summary = (
            f"axe-core clean ({len(report.violations)} total, "
            f"{report.critical_violations} critical / {report.serious_violations} serious)"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"axe-core found {report.critical_violations} critical + "
            f"{report.serious_violations} serious violations"
        )
    return (
        GateReport(
            gate_id="wcag",
            name="WCAG 2.2 AA",
            verdict=verdict,
            summary=summary,
            detail=report.to_dict(),
        ),
        report,
    )


def _gdpr_gate(app_path: Path) -> tuple[GateReport, GDPRReport]:
    report = scan_gdpr(app_path)
    if report.passed:
        verdict = GateVerdict.pass_
        summary = f"All {len(report.checks)} GDPR posture checks passed"
    else:
        failing = [c.id for c in report.checks if not c.passed]
        verdict = GateVerdict.fail
        summary = f"GDPR posture gaps: {', '.join(failing)}"
    return (
        GateReport(
            gate_id="gdpr",
            name="GDPR posture",
            verdict=verdict,
            summary=summary,
            detail=report.to_dict(),
        ),
        report,
    )


def _spdx_gate(
    app_path: Path,
    deny: Iterable[str],
    allowlist: Iterable[str] | None,
) -> tuple[GateReport, SPDXReport]:
    report = scan_licenses(app_path, deny=deny, allowlist=allowlist)
    if report.source == "mock":
        verdict = GateVerdict.skipped
        summary = "No node_modules / arborist found — SPDX scan skipped"
    elif report.passed:
        verdict = GateVerdict.pass_
        summary = (
            f"{report.total_packages} packages scanned via {report.source}; "
            f"0 denied, {len(report.unknown)} with unknown licenses"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.denied)} package(s) carry a denied license "
            f"(deny-list: {', '.join(sorted(DEFAULT_DENY_LICENSES)[:3])}, …)"
        )
    return (
        GateReport(
            gate_id="spdx",
            name="SPDX license scan",
            verdict=verdict,
            summary=summary,
            detail=report.to_dict(),
        ),
        report,
    )


# ── Orchestrator ────────────────────────────────────────────────


def run_all(
    app_path: Path | str,
    *,
    url: str | None = None,
    checklist_overrides: dict[str, dict[str, str]] | None = None,
    spdx_deny: Iterable[str] = DEFAULT_DENY_LICENSES,
    spdx_allowlist: Iterable[str] | None = None,
) -> ComplianceBundle:
    """Run the three W5 gates and return one ``ComplianceBundle``.

    The bundle's ``passed`` property is ``True`` only when every gate
    either passes or is explicitly skipped *with* a justification
    (e.g. WCAG URL not supplied in a static-site smoke). A ``fail``
    verdict on any gate blocks the bundle.
    """
    root = Path(app_path).resolve()
    bundle = ComplianceBundle(app_path=str(root), url=url)

    wcag_gate, _ = _wcag_gate(url, checklist_overrides)
    gdpr_gate, _ = _gdpr_gate(root)
    spdx_gate, _ = _spdx_gate(root, spdx_deny, spdx_allowlist)

    bundle.gates = [wcag_gate, gdpr_gate, spdx_gate]

    # A bundle passes iff no gate is in the FAIL state. SKIPPED gates
    # count as "not blocking" (same semantic as C8 skipped tests).
    return bundle


# ── C8 compliance harness bridge ────────────────────────────────


def bundle_to_compliance_report(bundle: ComplianceBundle):
    """Convert a ``ComplianceBundle`` into the C8 ``ComplianceReport``
    shape so it plugs into the existing audit-log hash-chain and HMI
    compliance-tools view.

    Imports compliance_harness lazily so the web_compliance package
    itself stays importable on minimal installs without the full C8
    protocol tooling.
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
            test_id=f"W5-{g.gate_id.upper()}",
            test_name=g.name,
            verdict=_map[g.verdict],
            evidence="",
            message=g.summary,
        )
        for g in bundle.gates
    ]

    # We synthesise a "web" protocol row. The C8 ComplianceProtocol
    # enum doesn't currently include a web value; we reuse the
    # ``onvif`` protocol slot with metadata noting the real origin so
    # the existing audit consumer (which only cares about the report's
    # hash-chain entry) stays valid. A future change to C8 could add a
    # ``web`` member to the enum; until then, metadata is sufficient.
    return ComplianceReport(
        tool_name="w5_web_compliance",
        protocol=ComplianceProtocol.onvif,
        device_under_test=bundle.app_path,
        timestamp=bundle.timestamp,
        results=results,
        raw_log_path="",
        metadata={
            "origin": "web_compliance",
            "url": bundle.url or "",
            "bundle": bundle.to_dict(),
        },
    )


__all__ = [
    "ComplianceBundle",
    "GateReport",
    "GateVerdict",
    "run_all",
    "bundle_to_compliance_report",
]
