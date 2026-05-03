"""SC — Security scanning adapters for generated apps.

This package hosts optional scanner wrappers for the SC security
compliance track.  Adapters normalise vendor output into small
dataclasses so later rows can wire them into generated workspaces,
commit triggers, and policy gates without depending on one CLI vendor.
"""

from __future__ import annotations

from backend.security_scanning.sast import (
    DEFAULT_FAIL_ON,
    SASTCommitScan,
    SAST_COMMIT_SCAN_ARTIFACT,
    SASTFinding,
    SASTReport,
    SASTSeverity,
    scan_generated_workspace_commit,
    scan_sast,
    write_sast_commit_scan_artifact,
)
from backend.security_scanning.dast import (
    DASTFinding,
    DASTPreviewScan,
    DASTReport,
    DASTSeverity,
    scan_web_preview_zap,
    scan_zap_baseline,
)
from backend.security_scanning.sca import (
    SCA_FIX_PR_ARTIFACT,
    SCAFinding,
    SCAFixPR,
    SCAReport,
    SCASeverity,
    plan_sca_fix_prs,
    scan_sca,
    write_sca_fix_pr_artifact,
)
from backend.security_scanning.container import (
    ContainerArtifactReport,
    ContainerFinding,
    ContainerSeverity,
    scan_container_artifact,
)
from backend.security_scanning.secrets import (
    SecretFinding,
    SecretScanReport,
    SecretSeverity,
    scan_secrets,
)

__all__ = [
    "DEFAULT_FAIL_ON",
    "DASTFinding",
    "DASTPreviewScan",
    "DASTReport",
    "DASTSeverity",
    "SAST_COMMIT_SCAN_ARTIFACT",
    "SASTCommitScan",
    "SASTFinding",
    "SASTReport",
    "SASTSeverity",
    "SCAFinding",
    "SCAFixPR",
    "SCA_FIX_PR_ARTIFACT",
    "SCAReport",
    "SCASeverity",
    "SecretFinding",
    "SecretScanReport",
    "SecretSeverity",
    "ContainerArtifactReport",
    "ContainerFinding",
    "ContainerSeverity",
    "plan_sca_fix_prs",
    "scan_container_artifact",
    "scan_web_preview_zap",
    "scan_generated_workspace_commit",
    "scan_sca",
    "scan_sast",
    "scan_zap_baseline",
    "scan_secrets",
    "write_sca_fix_pr_artifact",
    "write_sast_commit_scan_artifact",
]
