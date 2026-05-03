"""SC.4.1 — Container vulnerability scanners for W4 deploy artifacts.

Dispatches to whichever container scanner is on ``PATH``:

    trivy  — Trivy filesystem scan, emitted as JSON
    grype  — Grype directory scan, emitted as JSON

Both scanners are optional. The scan returns a uniform
``ContainerArtifactReport`` whose ``source`` field distinguishes which
tool produced the data. When no tool is available, the report is marked
``source="mock"`` so later SC gates can treat that as skipped rather
than clean.

SC.4.1 deliberately stops at the forced scan contract: callers must pass
a valid W4 ``BuildArtifact`` (or artifact path), and HIGH/CRITICAL
findings fail the report by default. Wiring the result into deploy
adapter blocking is left to SC.4.2.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.deploy.base import BuildArtifact, DeployArtifactError
from backend.security_scanning.sast import DEFAULT_FAIL_ON, SEVERITY_ORDER

logger = logging.getLogger(__name__)


ContainerSeverity = str


@dataclass
class ContainerFinding:
    vulnerability_id: str
    package: str
    installed_version: str = ""
    fixed_version: str = ""
    severity: ContainerSeverity = "INFO"
    title: str = ""
    target: str = ""
    path: str = ""
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContainerArtifactReport:
    source: str = "mock"  # "trivy" / "grype" / "mock"
    artifact_path: str = ""
    artifact_framework: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[ContainerFinding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[ContainerFinding]:
        fail_on_set = {s.upper() for s in self.fail_on}
        return [f for f in self.findings if f.severity in fail_on_set]

    @property
    def passed(self) -> bool:
        return not self.error and not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "artifact_path": self.artifact_path,
            "artifact_framework": self.artifact_framework,
            "scanner_binary": self.scanner_binary,
            "passed": self.passed,
            "total_findings": self.total_findings,
            "severity_counts": dict(self.severity_counts),
            "fail_on": list(self.fail_on),
            "blocking_count": len(self.blocking_findings),
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
        }


def _normalise_severity(raw: Any) -> ContainerSeverity:
    if raw is None:
        return "INFO"
    s = str(raw).strip().upper()
    if s in {"UNKNOWN", "NEGLIGIBLE"}:
        return "INFO"
    if s in SEVERITY_ORDER:
        return s
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


def _json_loads(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_trivy(payload: dict[str, Any]) -> list[ContainerFinding]:
    findings: list[ContainerFinding] = []
    for result in payload.get("Results") or []:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "")
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            findings.append(
                ContainerFinding(
                    vulnerability_id=str(vuln.get("VulnerabilityID") or ""),
                    package=str(vuln.get("PkgName") or ""),
                    installed_version=str(vuln.get("InstalledVersion") or ""),
                    fixed_version=str(vuln.get("FixedVersion") or ""),
                    severity=_normalise_severity(vuln.get("Severity")),
                    title=str(vuln.get("Title") or ""),
                    target=target,
                    tool="trivy",
                )
            )
    return findings


def _parse_grype(payload: dict[str, Any]) -> list[ContainerFinding]:
    findings: list[ContainerFinding] = []
    for match in payload.get("matches") or []:
        if not isinstance(match, dict):
            continue
        vuln = match.get("vulnerability") or {}
        artifact = match.get("artifact") or {}
        fix = vuln.get("fix") or {}
        locations = artifact.get("locations") or []
        first_location = locations[0] if locations else {}
        if not isinstance(first_location, dict):
            first_location = {}
        fixed_versions = fix.get("versions") or []
        findings.append(
            ContainerFinding(
                vulnerability_id=str(vuln.get("id") or ""),
                package=str(artifact.get("name") or ""),
                installed_version=str(artifact.get("version") or ""),
                fixed_version=", ".join(str(v) for v in fixed_versions)
                if isinstance(fixed_versions, list)
                else str(fixed_versions or ""),
                severity=_normalise_severity(vuln.get("severity")),
                title=str(vuln.get("description") or ""),
                target=str(artifact.get("type") or ""),
                path=str(first_location.get("path") or ""),
                tool="grype",
            )
        )
    return findings


def _run_trivy(
    artifact_path: Path,
    timeout: int,
) -> tuple[list[ContainerFinding], str, str]:
    bin_path = shutil.which("trivy")
    if not bin_path:
        return [], "", ""
    rc, out, err = _run(
        [
            "trivy",
            "fs",
            "--format",
            "json",
            "--quiet",
            str(artifact_path),
        ],
        cwd=artifact_path,
        timeout=timeout,
    )
    if rc != 0:
        logger.info("trivy fs failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_trivy(_json_loads(out)), bin_path, ""


def _run_grype(
    artifact_path: Path,
    timeout: int,
) -> tuple[list[ContainerFinding], str, str]:
    bin_path = shutil.which("grype")
    if not bin_path:
        return [], "", ""
    rc, out, err = _run(
        ["grype", f"dir:{artifact_path}", "-o", "json"],
        cwd=artifact_path,
        timeout=timeout,
    )
    if rc != 0:
        logger.info("grype dir scan failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_grype(_json_loads(out)), bin_path, ""


_SCANNER_ORDER: tuple[tuple[str, Any], ...] = (
    ("trivy", _run_trivy),
    ("grype", _run_grype),
)


def _finalise_report(
    report: ContainerArtifactReport,
    *,
    findings: list[ContainerFinding],
    source: str,
    scanner_binary: str,
) -> ContainerArtifactReport:
    report.source = source
    report.scanner_binary = scanner_binary
    report.findings = findings
    report.total_findings = len(findings)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    report.severity_counts = counts
    return report


def _coerce_artifact(artifact: BuildArtifact | Path | str) -> BuildArtifact:
    if isinstance(artifact, BuildArtifact):
        return artifact
    return BuildArtifact(path=Path(artifact), framework=None)


def scan_container_artifact(
    artifact: BuildArtifact | Path | str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 600,
) -> ContainerArtifactReport:
    """Run Trivy/Grype against a W4 deploy artifact directory.

    Module-global state audit: this helper reads immutable scanner
    constants only; every worker derives scan state from the artifact
    path, PATH-discovered scanner binary, and subprocess output.
    """
    build_artifact = _coerce_artifact(artifact)
    artifact_path = build_artifact.path.resolve()
    report = ContainerArtifactReport(
        artifact_path=str(artifact_path),
        artifact_framework=build_artifact.framework or "",
    )
    report.fail_on = sorted({s.upper() for s in fail_on})

    try:
        build_artifact.validate()
    except DeployArtifactError as exc:
        report.error = str(exc)
        return report

    candidates: tuple[tuple[str, Any], ...]
    if scanner is None:
        candidates = _SCANNER_ORDER
    else:
        key = scanner.strip().lower()
        chosen = next(((n, fn) for n, fn in _SCANNER_ORDER if n == key), None)
        if chosen is None:
            report.error = (
                f"unknown scanner {scanner!r} "
                "(supported: trivy, grype)"
            )
            return report
        candidates = (chosen,)

    for name, fn in candidates:
        findings, bin_path, error = fn(artifact_path, timeout)
        if error:
            report.source = name
            report.scanner_binary = bin_path
            report.error = error
            return report
        if bin_path:
            return _finalise_report(
                report,
                findings=findings,
                source=name,
                scanner_binary=bin_path,
            )

    return report


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run an OmniSight container vulnerability scan.",
    )
    parser.add_argument("--artifact-path", required=True)
    parser.add_argument("--framework", default="")
    parser.add_argument("--scanner", default="")
    parser.add_argument("--fail-on", default="CRITICAL,HIGH")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    fail_on = [s.strip() for s in args.fail_on.split(",") if s.strip()]
    report = scan_container_artifact(
        BuildArtifact(
            path=Path(args.artifact_path),
            framework=args.framework or None,
        ),
        scanner=args.scanner or None,
        fail_on=fail_on,
        timeout=args.timeout,
    )
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


__all__ = [
    "ContainerArtifactReport",
    "ContainerFinding",
    "ContainerSeverity",
    "scan_container_artifact",
]


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
