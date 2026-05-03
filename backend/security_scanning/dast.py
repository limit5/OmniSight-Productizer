"""SC.2.1 — OWASP ZAP adapter for W14 live-preview scans.

Runs OWASP ZAP against a live W14 preview URL and normalises the JSON
report into the same small dataclass style used by SC.1 SAST adapters.
The adapter is deliberately optional: when neither ``zap-baseline.py``
nor ``docker`` is available, the report is marked ``source="mock"`` so
later SC rows can treat that as skipped rather than clean.

``scan_web_preview_zap`` accepts a W14 ``WebSandboxInstance`` or its
``to_dict()`` snapshot, requires ``status="running"``, and prefers the
public ``ingress_url`` over the local ``preview_url``.  Wiring this into
the W14 sandbox lifecycle is left to SC.2.2.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from backend.security_scanning.sast import DEFAULT_FAIL_ON, SEVERITY_ORDER

logger = logging.getLogger(__name__)


DASTSeverity = str

ZAP_DOCKER_IMAGE = "ghcr.io/zaproxy/zaproxy:stable"


@dataclass
class DASTFinding:
    rule_id: str
    name: str
    url: str = ""
    param: str = ""
    evidence: str = ""
    severity: DASTSeverity = "INFO"
    confidence: str = ""
    cwe: list[str] = field(default_factory=list)
    owasp: list[str] = field(default_factory=list)
    description: str = ""
    solution: str = ""
    tool: str = "zap"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DASTReport:
    source: str = "mock"  # "zap" / "mock"
    target_url: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[DASTFinding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[DASTFinding]:
        fail_on_set = {s.upper() for s in self.fail_on}
        return [f for f in self.findings if f.severity in fail_on_set]

    @property
    def passed(self) -> bool:
        return not self.error and not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target_url": self.target_url,
            "scanner_binary": self.scanner_binary,
            "passed": self.passed,
            "total_findings": self.total_findings,
            "severity_counts": dict(self.severity_counts),
            "fail_on": list(self.fail_on),
            "blocking_count": len(self.blocking_findings),
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
        }


@dataclass
class DASTPreviewScan:
    workspace_id: str = ""
    sandbox_id: str = ""
    target_url: str = ""
    triggered: bool = False
    reason: str = ""
    report: DASTReport = field(default_factory=DASTReport)

    @property
    def passed(self) -> bool:
        return self.report.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "sandbox_id": self.sandbox_id,
            "target_url": self.target_url,
            "triggered": self.triggered,
            "reason": self.reason,
            "passed": self.passed,
            "report": self.report.to_dict(),
        }


def _normalise_severity(raw: Any) -> DASTSeverity:
    if raw is None:
        return "INFO"
    s = str(raw).strip().upper()
    if "(" in s:
        s = s.split("(", 1)[0].strip()
    if s in SEVERITY_ORDER:
        return s
    if s in {"INFORMATIONAL", "INFO", "NONE"}:
        return "INFO"
    if s in {"WARN", "WARNING"}:
        return "MEDIUM"
    try:
        score = float(s)
    except ValueError:
        return "INFO"
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "INFO"


def _json_loads(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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


def _zap_cwe(raw: Any) -> list[str]:
    try:
        cwe_id = int(raw)
    except (TypeError, ValueError):
        return []
    if cwe_id <= 0:
        return []
    return [f"CWE-{cwe_id}"]


def _zap_owasp(raw: Any) -> list[str]:
    value = str(raw or "").strip()
    if not value or value == "0":
        return []
    return [f"WASC-{value}"]


def _zap_risk_severity(alert: Mapping[str, Any]) -> DASTSeverity:
    for key in ("riskdesc", "risk"):
        if alert.get(key) is not None:
            return _normalise_severity(alert.get(key))
    risk_code = str(alert.get("riskcode") or "").strip()
    if risk_code == "3":
        return "HIGH"
    if risk_code == "2":
        return "MEDIUM"
    if risk_code == "1":
        return "LOW"
    if risk_code == "0":
        return "INFO"
    return _normalise_severity(risk_code)


def _parse_zap(payload: dict[str, Any]) -> list[DASTFinding]:
    findings: list[DASTFinding] = []
    sites = payload.get("site")
    if not isinstance(sites, list):
        sites = [{"alerts": payload.get("alerts") or []}]
    for site in sites:
        if not isinstance(site, dict):
            continue
        for alert in site.get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            instances = alert.get("instances") or [{}]
            first = instances[0] if isinstance(instances, list) and instances else {}
            if not isinstance(first, dict):
                first = {}
            findings.append(
                DASTFinding(
                    rule_id=str(alert.get("pluginid") or alert.get("id") or ""),
                    name=str(alert.get("alert") or alert.get("name") or ""),
                    url=str(first.get("uri") or alert.get("url") or ""),
                    param=str(first.get("param") or alert.get("param") or ""),
                    evidence=str(first.get("evidence") or alert.get("evidence") or ""),
                    severity=_zap_risk_severity(alert),
                    confidence=str(alert.get("confidence") or ""),
                    cwe=_zap_cwe(alert.get("cweid")),
                    owasp=_zap_owasp(alert.get("wascid")),
                    description=str(alert.get("desc") or ""),
                    solution=str(alert.get("solution") or ""),
                    tool="zap",
                )
            )
    return findings


def _run_zap_baseline(target_url: str, timeout: int) -> tuple[list[DASTFinding], str, str]:
    bin_path = shutil.which("zap-baseline.py")
    docker_path = shutil.which("docker")
    if not bin_path and not docker_path:
        return [], "", ""

    with tempfile.TemporaryDirectory(prefix="omnisight-zap-") as tmp:
        tmp_path = Path(tmp)
        report_path = tmp_path / "zap.json"
        if bin_path:
            cmd = [
                "zap-baseline.py",
                "-t",
                target_url,
                "-J",
                str(report_path),
            ]
            scanner_binary = bin_path
        else:
            cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{tmp_path}:/zap/wrk:rw",
                ZAP_DOCKER_IMAGE,
                "zap-baseline.py",
                "-t",
                target_url,
                "-J",
                "zap.json",
            ]
            scanner_binary = docker_path or "docker"

        rc, out, err = _run(cmd, cwd=tmp_path, timeout=timeout)
        if rc not in (0, 1, 2):
            logger.info("zap baseline failed rc=%s err=%s", rc, err[:200])
            return [], scanner_binary, err or out
        raw = report_path.read_text() if report_path.exists() else out
    return _parse_zap(_json_loads(raw)), scanner_binary, ""


def _finalise_report(
    report: DASTReport,
    *,
    findings: list[DASTFinding],
    source: str,
    scanner_binary: str,
) -> DASTReport:
    report.source = source
    report.scanner_binary = scanner_binary
    report.findings = findings
    report.total_findings = len(findings)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    report.severity_counts = counts
    return report


def scan_zap_baseline(
    target_url: str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 600,
) -> DASTReport:
    """Run OWASP ZAP baseline against ``target_url``.

    Module-global state audit: this helper reads immutable scanner
    constants only; every worker derives scan state from the target URL,
    PATH-discovered scanner binary, and subprocess output.
    """
    target = str(target_url or "").strip()
    report = DASTReport(target_url=target)
    report.fail_on = sorted({s.upper() for s in fail_on})

    if not _valid_target_url(target):
        report.error = f"target_url {target!r} must be an http(s) URL"
        return report

    if scanner is not None and scanner.strip().lower() not in {"zap", "owasp-zap"}:
        report.error = f"unknown scanner {scanner!r} (supported: zap)"
        return report

    findings, bin_path, error = _run_zap_baseline(target, timeout)
    if error:
        report.source = "zap"
        report.scanner_binary = bin_path
        report.error = error
        return report
    if bin_path:
        return _finalise_report(
            report,
            findings=findings,
            source="zap",
            scanner_binary=bin_path,
        )
    return report


def _preview_field(preview: Any, name: str) -> Any:
    if isinstance(preview, Mapping):
        return preview.get(name)
    return getattr(preview, name, None)


def _resolve_preview_target(preview: Any) -> tuple[str, str, str, str]:
    workspace_id = str(_preview_field(preview, "workspace_id") or "")
    sandbox_id = str(_preview_field(preview, "sandbox_id") or "")
    status = _preview_field(preview, "status")
    if hasattr(status, "value"):
        status = status.value
    status_text = str(status or "")
    target_url = str(
        _preview_field(preview, "ingress_url")
        or _preview_field(preview, "preview_url")
        or ""
    )
    return workspace_id, sandbox_id, status_text, target_url


def scan_web_preview_zap(
    preview: Any,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 600,
) -> DASTPreviewScan:
    """Run OWASP ZAP against a running W14 live-preview snapshot.

    ``preview`` may be a ``WebSandboxInstance`` or ``to_dict()`` shape.
    SC.2.1 only adapts the W14 live-preview contract; lifecycle trigger
    wiring stays out of scope for SC.2.2.
    """
    workspace_id, sandbox_id, status, target_url = _resolve_preview_target(preview)
    report = DASTReport(
        target_url=target_url,
        fail_on=sorted({s.upper() for s in fail_on}),
    )
    scan = DASTPreviewScan(
        workspace_id=workspace_id,
        sandbox_id=sandbox_id,
        target_url=target_url,
        reason="web_preview",
        report=report,
    )

    if status != "running":
        report.error = f"web preview status must be 'running', got {status!r}"
        scan.reason = "preview_not_ready"
        return scan
    if not target_url:
        report.error = "web preview has no ingress_url or preview_url"
        scan.reason = "missing_preview_url"
        return scan

    report = scan_zap_baseline(
        target_url,
        scanner=scanner,
        fail_on=fail_on,
        timeout=timeout,
    )
    scan.report = report
    scan.triggered = not bool(report.error)
    return scan


__all__ = [
    "DASTFinding",
    "DASTPreviewScan",
    "DASTReport",
    "DASTSeverity",
    "ZAP_DOCKER_IMAGE",
    "scan_web_preview_zap",
    "scan_zap_baseline",
]
