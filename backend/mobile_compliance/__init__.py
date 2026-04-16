"""P6 #291 — Mobile store compliance gates package.

Three static, offline gates the mobile-vertical pipeline runs before
a build is allowed into the P5 store_submission path:

    * App Store Review Guidelines — ``app_store_guidelines.py``
    * Google Play Policy          — ``play_policy.py``
    * Privacy label generator     — ``privacy_labels.py``

The ``run_all()`` orchestrator bundles their verdicts into a
``MobileComplianceBundle``, which ``bundle_to_compliance_report()``
maps into the C8 ``ComplianceReport`` shape so it plugs into the
existing audit-log hash-chain and HMI compliance-tools view.

Mirrors the structure of ``backend.web_compliance`` (W5 #279):
each gate degrades to a ``skipped`` verdict when its target manifest
is absent, so unit tests work offline in sandbox.
"""

from __future__ import annotations

from backend.mobile_compliance.app_store_guidelines import (
    ASCFinding,
    ASCGuidelinesReport,
    scan_app_store_guidelines,
)
from backend.mobile_compliance.bundle import (
    GateReport,
    GateVerdict,
    MobileComplianceBundle,
    bundle_to_compliance_report,
    run_all,
)
from backend.mobile_compliance.play_policy import (
    MIN_TARGET_SDK,
    PlayFinding,
    PlayPolicyReport,
    scan_play_policy,
)
from backend.mobile_compliance.privacy_labels import (
    APPLE_CATEGORY_TAXONOMY,
    APPLE_PURPOSE_TAXONOMY,
    PLAY_CATEGORY_TAXONOMY,
    PrivacyLabelReport,
    generate_privacy_label,
)

__all__ = [
    "APPLE_CATEGORY_TAXONOMY",
    "APPLE_PURPOSE_TAXONOMY",
    "ASCFinding",
    "ASCGuidelinesReport",
    "GateReport",
    "GateVerdict",
    "MIN_TARGET_SDK",
    "MobileComplianceBundle",
    "PLAY_CATEGORY_TAXONOMY",
    "PlayFinding",
    "PlayPolicyReport",
    "PrivacyLabelReport",
    "bundle_to_compliance_report",
    "generate_privacy_label",
    "run_all",
    "scan_app_store_guidelines",
    "scan_play_policy",
]
