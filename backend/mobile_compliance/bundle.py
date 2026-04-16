"""P6 #291 — Mobile store compliance bundle orchestrator.

Runs the three P6 gates (ASC Review Guidelines, Play Policy,
Privacy Label Generator) over a mobile project and produces one
``MobileComplianceBundle`` evidence object. A ``bundle_to_compliance_report``
converter maps the bundle into the C8 ``ComplianceReport`` shape so the
existing audit-log hash-chain + HMI compliance-tools listing pick it
up for free — same pattern as W5 ``web_compliance`` (#279).

Integration with P5 store submission:

    1. P5's ``StoreSubmissionContext`` is created *after* a bundle
       passes. P6 gates block P5 when blockers remain.
    2. The bundle's hash is persisted into the P5 audit entry
       (``store_submission_audit_log``) so a future Play/ASC reviewer
       can trace exactly which gate version cleared the build.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.mobile_compliance.app_store_guidelines import (
    ASCGuidelinesReport,
    scan_app_store_guidelines,
)
from backend.mobile_compliance.play_policy import (
    MIN_TARGET_SDK,
    PlayPolicyReport,
    scan_play_policy,
)
from backend.mobile_compliance.privacy_labels import (
    PrivacyLabelReport,
    generate_privacy_label,
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
    gate_id: str   # "app_store_guidelines" / "play_policy" / "privacy_labels"
    name: str
    verdict: GateVerdict
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.pass_


@dataclass
class MobileComplianceBundle:
    app_path: str
    platform: str            # "ios" / "android" / "both"
    timestamp: float = field(default_factory=time.time)
    gates: list[GateReport] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Bundle passes when no gate is FAIL or ERROR. SKIPPED is OK
        — it just means "gate not applicable to this platform" (e.g.
        Play gate on an iOS-only repo).
        """
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
        return {
            "app_path": self.app_path,
            "platform": self.platform,
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


# ── Gate wrappers ────────────────────────────────────────────────────


def _asc_gate(
    app_path: Path,
    platform: str,
) -> GateReport:
    if platform == "android":
        return GateReport(
            gate_id="app_store_guidelines",
            name="App Store Review Guidelines",
            verdict=GateVerdict.skipped,
            summary="Android-only scan — ASC gate skipped.",
            detail={},
        )
    report = scan_app_store_guidelines(app_path)
    if report.files_scanned == 0 and not report.findings:
        verdict = GateVerdict.skipped
        summary = "No iOS source detected — ASC gate skipped."
    elif report.passed:
        verdict = GateVerdict.pass_
        summary = (
            f"ASC clean ({report.files_scanned} files scanned, "
            f"{len(report.warnings)} warnings)"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.blockers)} ASC blocker(s) "
            f"(+{len(report.warnings)} warnings)"
        )
    return GateReport(
        gate_id="app_store_guidelines",
        name="App Store Review Guidelines",
        verdict=verdict,
        summary=summary,
        detail=report.to_dict(),
    )


def _play_gate(
    app_path: Path,
    platform: str,
    min_target_sdk: int,
) -> GateReport:
    if platform == "ios":
        return GateReport(
            gate_id="play_policy",
            name="Google Play Policy",
            verdict=GateVerdict.skipped,
            summary="iOS-only scan — Play gate skipped.",
            detail={},
        )
    report = scan_play_policy(app_path, min_target_sdk=min_target_sdk)
    is_android_project = (
        report.target_sdk is not None
        or report.declares_background_location
        or report.dependencies
        or report.data_safety_form_path is not None
    )
    if not is_android_project and not report.findings:
        verdict = GateVerdict.skipped
        summary = "No Android project detected — Play gate skipped."
    elif report.passed:
        verdict = GateVerdict.pass_
        summary = (
            f"Play clean (targetSdk={report.target_sdk}, "
            f"{len(report.dependencies)} deps, "
            f"{len(report.warnings)} warnings)"
        )
    else:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.blockers)} Play blocker(s) "
            f"(+{len(report.warnings)} warnings)"
        )
    return GateReport(
        gate_id="play_policy",
        name="Google Play Policy",
        verdict=verdict,
        summary=summary,
        detail=report.to_dict(),
    )


def _privacy_gate(
    app_path: Path,
    platform: str,
    catalogue_path: Path | None,
) -> GateReport:
    report = generate_privacy_label(
        app_path, platform=platform, catalogue_path=catalogue_path,
    )
    if report.status == "no_manifests":
        verdict = GateVerdict.skipped
        summary = "No iOS / Android manifest present — privacy label skipped."
    elif report.status == "no_catalogue":
        verdict = GateVerdict.error
        summary = "SDK → category catalogue missing (configs/privacy_label_sdks.yaml)."
    elif not report.detected_sdks and report.unknown_dependencies:
        verdict = GateVerdict.fail
        summary = (
            f"{len(report.unknown_dependencies)} dependencies detected but "
            "none match the SDK catalogue — privacy label cannot be generated."
        )
    else:
        verdict = GateVerdict.pass_
        summary = (
            f"Privacy label generated: {len(report.detected_sdks)} SDK(s) "
            f"({len(report.unknown_dependencies)} unknown deps ignored)."
        )
    return GateReport(
        gate_id="privacy_labels",
        name="Privacy label generator",
        verdict=verdict,
        summary=summary,
        detail=report.to_dict(),
    )


# ── Orchestrator ────────────────────────────────────────────────────


def run_all(
    app_path: Path | str,
    *,
    platform: str = "both",
    min_target_sdk: int = MIN_TARGET_SDK,
    catalogue_path: Path | None = None,
) -> MobileComplianceBundle:
    """Run all three P6 gates and return one ``MobileComplianceBundle``.

    ``platform`` selects which gates run:
      * ``"ios"``      → ASC + privacy (Play skipped)
      * ``"android"``  → Play + privacy (ASC skipped)
      * ``"both"``     → all three (default)
    """
    if platform not in ("ios", "android", "both"):
        raise ValueError(f"Invalid platform: {platform!r}")

    root = Path(app_path).resolve()
    bundle = MobileComplianceBundle(app_path=str(root), platform=platform)

    bundle.gates.append(_asc_gate(root, platform))
    bundle.gates.append(_play_gate(root, platform, min_target_sdk))
    bundle.gates.append(_privacy_gate(root, platform, catalogue_path))

    return bundle


# ── C8 compliance harness bridge ────────────────────────────────────


def bundle_to_compliance_report(bundle: MobileComplianceBundle):
    """Convert a ``MobileComplianceBundle`` into a C8 ``ComplianceReport``
    so the existing audit-log hash-chain + HMI compliance-tools view
    list the mobile bundle alongside ONVIF / USB / UAC / Web tools.

    Lazy-imports ``compliance_harness`` so a minimal install without
    the full C8 tooling can still import this module.
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
            test_id=f"P6-{g.gate_id.upper()}",
            test_name=g.name,
            verdict=_map[g.verdict],
            evidence="",
            message=g.summary,
        )
        for g in bundle.gates
    ]

    return ComplianceReport(
        tool_name="p6_mobile_compliance",
        # C8 enum has no "mobile" member yet; reuse onvif slot with
        # metadata noting the real origin (same trick as W5).
        protocol=ComplianceProtocol.onvif,
        device_under_test=bundle.app_path,
        timestamp=bundle.timestamp,
        results=results,
        raw_log_path="",
        metadata={
            "origin": "mobile_compliance",
            "platform": bundle.platform,
            "bundle": bundle.to_dict(),
        },
    )


__all__ = [
    "GateReport",
    "GateVerdict",
    "MobileComplianceBundle",
    "bundle_to_compliance_report",
    "run_all",
]
