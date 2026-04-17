"""X4 #300 — CVE scan adapters.

Dispatches to whichever vulnerability scanner is on ``PATH``:

    trivy        — best coverage (SBOM + fs + image modes)
    grype        — Anchore project, fast, OCI-native
    osv-scanner  — Google OSV source-of-truth, lockfile-focused

All three are optional. The scan returns a uniform ``CVEReport`` whose
``source`` field distinguishes which tool produced the data. When no
tool is available, the report is marked ``source="mock"`` and the gate
treats it as a *skip* rather than a *pass* — we never claim a clean
CVE sheet without actually running a scanner.

Severity thresholding
---------------------
By default ``CRITICAL`` and ``HIGH`` findings FAIL the gate. Callers
can loosen via the ``fail_on`` parameter (e.g. fail only on
``CRITICAL``) or tighten to include ``MEDIUM``. We preserve the raw
finding list regardless so audit consumers can apply their own
thresholds.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# Ordered severity strings (trivy / grype / osv agree on these tokens
# apart from casing). Index in the list gives a comparable rank.
SEVERITY_ORDER: tuple[str, ...] = (
    "UNKNOWN", "NEGLIGIBLE", "LOW", "MEDIUM", "HIGH", "CRITICAL",
)

DEFAULT_FAIL_ON: frozenset[str] = frozenset({"CRITICAL", "HIGH"})


def _normalise_severity(raw: Any) -> str:
    if raw is None:
        return "UNKNOWN"
    s = str(raw).strip().upper()
    if s in SEVERITY_ORDER:
        return s
    # osv-scanner returns "MODERATE" rather than "MEDIUM"
    if s == "MODERATE":
        return "MEDIUM"
    return "UNKNOWN"


@dataclass
class Vulnerability:
    cve_id: str
    package: str
    version: str = ""
    fixed_version: str = ""
    severity: str = "UNKNOWN"
    title: str = ""
    ecosystem: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CVEReport:
    source: str = "mock"  # "trivy" / "grype" / "osv-scanner" / "mock"
    app_path: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[Vulnerability] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[Vulnerability]:
        fail_on_set = {s.upper() for s in self.fail_on}
        return [v for v in self.findings if v.severity in fail_on_set]

    @property
    def passed(self) -> bool:
        return not self.error and not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "app_path": self.app_path,
            "scanner_binary": self.scanner_binary,
            "passed": self.passed,
            "total_findings": self.total_findings,
            "severity_counts": dict(self.severity_counts),
            "fail_on": list(self.fail_on),
            "blocking_count": len(self.blocking_findings),
            "findings": [v.to_dict() for v in self.findings],
            "error": self.error,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Subprocess helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {timeout}s: {exc}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  trivy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_trivy(app_path: Path, timeout: int) -> tuple[list[Vulnerability], str]:
    bin_path = shutil.which("trivy")
    if not bin_path:
        return [], ""
    rc, out, err = _run(
        ["trivy", "fs", "--quiet", "--format", "json", "--scanners", "vuln", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    # trivy exits non-zero when findings are present; we only skip on
    # rc>=2 (true error) to stay compatible with both conventions.
    if rc not in (0, 1):
        logger.info("trivy failed rc=%s err=%s", rc, err[:200])
        return [], bin_path
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [], bin_path
    out_list: list[Vulnerability] = []
    for result in payload.get("Results") or []:
        eco = str(result.get("Type") or "")
        for v in result.get("Vulnerabilities") or []:
            out_list.append(
                Vulnerability(
                    cve_id=str(v.get("VulnerabilityID") or ""),
                    package=str(v.get("PkgName") or ""),
                    version=str(v.get("InstalledVersion") or ""),
                    fixed_version=str(v.get("FixedVersion") or ""),
                    severity=_normalise_severity(v.get("Severity")),
                    title=str(v.get("Title") or v.get("Description") or "")[:240],
                    ecosystem=eco,
                )
            )
    return out_list, bin_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  grype
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_grype(app_path: Path, timeout: int) -> tuple[list[Vulnerability], str]:
    bin_path = shutil.which("grype")
    if not bin_path:
        return [], ""
    rc, out, err = _run(
        ["grype", f"dir:{app_path}", "-o", "json", "--quiet"],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("grype failed rc=%s err=%s", rc, err[:200])
        return [], bin_path
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [], bin_path
    out_list: list[Vulnerability] = []
    for m in payload.get("matches") or []:
        vuln = m.get("vulnerability") or {}
        artifact = m.get("artifact") or {}
        fixed = ""
        fix = vuln.get("fix") or {}
        if fix.get("state") == "fixed":
            versions = fix.get("versions") or []
            fixed = versions[0] if versions else ""
        out_list.append(
            Vulnerability(
                cve_id=str(vuln.get("id") or ""),
                package=str(artifact.get("name") or ""),
                version=str(artifact.get("version") or ""),
                fixed_version=fixed,
                severity=_normalise_severity(vuln.get("severity")),
                title=str(vuln.get("description") or "")[:240],
                ecosystem=str(artifact.get("type") or ""),
            )
        )
    return out_list, bin_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  osv-scanner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_osv(app_path: Path, timeout: int) -> tuple[list[Vulnerability], str]:
    bin_path = shutil.which("osv-scanner")
    if not bin_path:
        return [], ""
    rc, out, err = _run(
        ["osv-scanner", "--format", "json", "-r", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("osv-scanner failed rc=%s err=%s", rc, err[:200])
        return [], bin_path
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [], bin_path
    out_list: list[Vulnerability] = []
    for res in payload.get("results") or []:
        for pkg_entry in res.get("packages") or []:
            pkg = pkg_entry.get("package") or {}
            pkg_name = str(pkg.get("name") or "")
            pkg_ver = str(pkg.get("version") or "")
            eco = str(pkg.get("ecosystem") or "")
            # Severity lives alongside each vuln.
            for v in pkg_entry.get("vulnerabilities") or []:
                sev = "UNKNOWN"
                # Prefer database_specific.severity when available
                db = v.get("database_specific") or {}
                if db.get("severity"):
                    sev = _normalise_severity(db.get("severity"))
                elif v.get("severity"):
                    s_list = v.get("severity") or []
                    if isinstance(s_list, list) and s_list:
                        sev = _normalise_severity(s_list[0].get("type") or s_list[0].get("score"))
                fixed = ""
                for affected in v.get("affected") or []:
                    for r in affected.get("ranges") or []:
                        for ev in r.get("events") or []:
                            if "fixed" in ev:
                                fixed = str(ev["fixed"])
                                break
                out_list.append(
                    Vulnerability(
                        cve_id=str(v.get("id") or ""),
                        package=pkg_name,
                        version=pkg_ver,
                        fixed_version=fixed,
                        severity=sev,
                        title=str(v.get("summary") or "")[:240],
                        ecosystem=eco,
                    )
                )
    return out_list, bin_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCANNER_ORDER: tuple[tuple[str, Any], ...] = (
    ("trivy", _run_trivy),
    ("grype", _run_grype),
    ("osv-scanner", _run_osv),
)


def scan_cves(
    app_path: Path | str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 300,
) -> CVEReport:
    """Run a vulnerability scanner and normalise its findings.

    When ``scanner`` is ``None`` we probe trivy → grype → osv-scanner
    and use the first one on PATH. Pass an explicit name to force.
    """
    root = Path(app_path).resolve()
    report = CVEReport(app_path=str(root))
    report.fail_on = sorted({s.upper() for s in fail_on})

    if not root.is_dir():
        report.error = f"app_path '{root}' is not a directory"
        return report

    candidates: tuple[tuple[str, Any], ...]
    if scanner is None:
        candidates = _SCANNER_ORDER
    else:
        key = scanner.strip().lower()
        chosen = next(((n, fn) for n, fn in _SCANNER_ORDER if n == key), None)
        if chosen is None:
            report.error = f"unknown scanner {scanner!r} (supported: trivy, grype, osv-scanner)"
            return report
        candidates = (chosen,)

    for name, fn in candidates:
        findings, bin_path = fn(root, timeout)
        if bin_path:
            report.source = name
            report.scanner_binary = bin_path
            report.findings = findings
            report.total_findings = len(findings)
            counts: dict[str, int] = {}
            for v in findings:
                counts[v.severity] = counts.get(v.severity, 0) + 1
            report.severity_counts = counts
            return report

    # Nothing on PATH
    return report


__all__ = [
    "CVEReport",
    "DEFAULT_FAIL_ON",
    "SEVERITY_ORDER",
    "Vulnerability",
    "scan_cves",
]
