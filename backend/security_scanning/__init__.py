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
    SCAFinding,
    SCAReport,
    SCASeverity,
    scan_sca,
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
    "SCAReport",
    "SCASeverity",
    "scan_web_preview_zap",
    "scan_generated_workspace_commit",
    "scan_sca",
    "scan_sast",
    "scan_zap_baseline",
    "write_sast_commit_scan_artifact",
]
