"""SC.3.1 — Dependency vulnerability scanner adapters.

Dispatches to whichever dependency scanner is on ``PATH``:

    npm-audit    — built into npm, best default for generated Node apps
    osv-scanner  — Google OSV lockfile scanner, multi-ecosystem
    snyk         — Snyk Open Source CLI, emitted as JSON

All three are optional. The scan returns a uniform ``SCAReport`` whose
``source`` field distinguishes which tool produced the data. When no
tool is available, the report is marked ``source="mock"`` so later SC
gates can treat that as skipped rather than clean.

Severity thresholding mirrors SC.1/SC.2: ``HIGH`` and ``CRITICAL``
findings block by default, while the raw finding list is preserved so
downstream policy can tighten or loosen independently.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.security_scanning.sast import DEFAULT_FAIL_ON, SEVERITY_ORDER

logger = logging.getLogger(__name__)


SCASeverity = str
SCA_FIX_PR_ARTIFACT = Path(".omnisight/security/sca-fix-prs.json")


@dataclass
class SCAFinding:
    vulnerability_id: str
    package: str
    version: str = ""
    fixed_version: str = ""
    severity: SCASeverity = "INFO"
    title: str = ""
    ecosystem: str = ""
    path: str = ""
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SCAReport:
    source: str = "mock"  # "npm-audit" / "osv-scanner" / "snyk" / "mock"
    app_path: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[SCAFinding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[SCAFinding]:
        fail_on_set = {s.upper() for s in self.fail_on}
        return [f for f in self.findings if f.severity in fail_on_set]

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
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
        }


@dataclass
class SCAFixPR:
    package: str
    ecosystem: str
    fixed_version: str
    vulnerability_ids: list[str] = field(default_factory=list)
    current_versions: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    severity: SCASeverity = "INFO"
    base_branch: str = "master"
    branch: str = ""
    title: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    update_command: str = ""
    automerge: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_severity(raw: Any) -> SCASeverity:
    if raw is None:
        return "INFO"
    s = str(raw).strip().upper()
    if s == "MODERATE":
        return "MEDIUM"
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


def _first_via_advisory(via: Any) -> dict[str, Any]:
    if isinstance(via, dict):
        return via
    if isinstance(via, list):
        for entry in via:
            if isinstance(entry, dict):
                return entry
    return {}


def _parse_npm_audit(payload: dict[str, Any]) -> list[SCAFinding]:
    findings: list[SCAFinding] = []
    vulnerabilities = payload.get("vulnerabilities")
    if isinstance(vulnerabilities, dict):
        for name, vuln in vulnerabilities.items():
            if not isinstance(vuln, dict):
                continue
            advisory = _first_via_advisory(vuln.get("via"))
            fix_available = vuln.get("fixAvailable")
            fixed_version = ""
            if isinstance(fix_available, dict):
                fixed_version = str(fix_available.get("version") or "")
            vuln_id = str(
                advisory.get("source")
                or advisory.get("url")
                or advisory.get("id")
                or name
            )
            findings.append(
                SCAFinding(
                    vulnerability_id=vuln_id,
                    package=str(vuln.get("name") or name),
                    fixed_version=fixed_version,
                    severity=_normalise_severity(
                        advisory.get("severity") or vuln.get("severity")
                    ),
                    title=str(advisory.get("title") or vuln.get("title") or ""),
                    ecosystem="npm",
                    path=str(vuln.get("range") or advisory.get("range") or ""),
                    tool="npm-audit",
                )
            )
        return findings

    advisories = payload.get("advisories")
    if isinstance(advisories, dict):
        for advisory in advisories.values():
            if not isinstance(advisory, dict):
                continue
            findings.append(
                SCAFinding(
                    vulnerability_id=str(advisory.get("id") or advisory.get("url") or ""),
                    package=str(advisory.get("module_name") or ""),
                    version=str(advisory.get("vulnerable_versions") or ""),
                    fixed_version=str(advisory.get("patched_versions") or ""),
                    severity=_normalise_severity(advisory.get("severity")),
                    title=str(advisory.get("title") or ""),
                    ecosystem="npm",
                    tool="npm-audit",
                )
            )
    return findings


def _osv_vuln_severity(vuln: dict[str, Any], pkg_entry: dict[str, Any]) -> SCASeverity:
    db = vuln.get("database_specific") or {}
    if db.get("severity"):
        return _normalise_severity(db.get("severity"))
    for severity in vuln.get("severity") or []:
        if isinstance(severity, dict):
            normalised = _normalise_severity(severity.get("score"))
            if normalised != "INFO":
                return normalised
    for group in pkg_entry.get("groups") or []:
        if isinstance(group, dict) and group.get("max_severity") is not None:
            normalised = _normalise_severity(group.get("max_severity"))
            if normalised != "INFO":
                return normalised
    return "INFO"


def _osv_fixed_versions(vuln: dict[str, Any]) -> str:
    fixed: list[str] = []
    for affected in vuln.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        for rng in affected.get("ranges") or []:
            if not isinstance(rng, dict):
                continue
            for event in rng.get("events") or []:
                if isinstance(event, dict) and event.get("fixed"):
                    fixed.append(str(event["fixed"]))
    return ", ".join(sorted(set(fixed)))


def _parse_osv(payload: dict[str, Any]) -> list[SCAFinding]:
    findings: list[SCAFinding] = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        source_path = str((result.get("source") or {}).get("path") or "")
        for pkg_entry in result.get("packages") or []:
            if not isinstance(pkg_entry, dict):
                continue
            pkg = pkg_entry.get("package") or {}
            pkg_name = str(pkg.get("name") or "")
            pkg_ver = str(pkg.get("version") or "")
            ecosystem = str(pkg.get("ecosystem") or "")
            for vuln in pkg_entry.get("vulnerabilities") or []:
                if not isinstance(vuln, dict):
                    continue
                findings.append(
                    SCAFinding(
                        vulnerability_id=str(vuln.get("id") or ""),
                        package=pkg_name,
                        version=pkg_ver,
                        fixed_version=_osv_fixed_versions(vuln),
                        severity=_osv_vuln_severity(vuln, pkg_entry),
                        title=str(vuln.get("summary") or ""),
                        ecosystem=ecosystem,
                        path=source_path,
                        tool="osv-scanner",
                    )
                )
    return findings


def _parse_snyk(payload: dict[str, Any]) -> list[SCAFinding]:
    findings: list[SCAFinding] = []
    projects: list[dict[str, Any]]
    if isinstance(payload.get("vulnerabilities"), list) or isinstance(payload.get("issues"), list):
        projects = [payload]
    else:
        raw_projects = payload.get("projects")
        projects = [p for p in raw_projects if isinstance(p, dict)] if isinstance(raw_projects, list) else []

    for project in projects:
        path = str(project.get("path") or project.get("targetFile") or "")
        raw_findings = project.get("vulnerabilities") or project.get("issues") or []
        for vuln in raw_findings:
            if not isinstance(vuln, dict):
                continue
            pkg_name = str(
                vuln.get("packageName")
                or vuln.get("name")
                or vuln.get("pkgName")
                or ""
            )
            fixed_in = vuln.get("fixedIn") or vuln.get("fixedInVersions") or []
            fixed_version = ", ".join(str(v) for v in fixed_in) if isinstance(fixed_in, list) else str(fixed_in or "")
            version = str(vuln.get("version") or "")
            if not version and isinstance(vuln.get("from"), list):
                version = str(vuln["from"][-1])
            findings.append(
                SCAFinding(
                    vulnerability_id=str(vuln.get("id") or vuln.get("issueId") or ""),
                    package=pkg_name,
                    version=version,
                    fixed_version=fixed_version,
                    severity=_normalise_severity(vuln.get("severity")),
                    title=str(vuln.get("title") or vuln.get("description") or ""),
                    ecosystem=str(project.get("packageManager") or vuln.get("packageManager") or ""),
                    path=path,
                    tool="snyk",
                )
            )
    return findings


def _run_npm_audit(app_path: Path, timeout: int) -> tuple[list[SCAFinding], str, str]:
    bin_path = shutil.which("npm")
    if not bin_path:
        return [], "", ""
    if not (app_path / "package.json").exists():
        return [], "", ""
    rc, out, err = _run(
        ["npm", "audit", "--json"],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("npm audit failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_npm_audit(_json_loads(out)), bin_path, ""


def _run_osv(app_path: Path, timeout: int) -> tuple[list[SCAFinding], str, str]:
    bin_path = shutil.which("osv-scanner")
    if not bin_path:
        return [], "", ""
    rc, out, err = _run(
        ["osv-scanner", "--format", "json", "-r", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("osv-scanner failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_osv(_json_loads(out)), bin_path, ""


def _run_snyk(app_path: Path, timeout: int) -> tuple[list[SCAFinding], str, str]:
    bin_path = shutil.which("snyk")
    if not bin_path:
        return [], "", ""
    rc, out, err = _run(
        ["snyk", "test", "--json", "--all-projects", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("snyk test failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_snyk(_json_loads(out)), bin_path, ""


_SCANNER_ORDER: tuple[tuple[str, Any], ...] = (
    ("npm-audit", _run_npm_audit),
    ("osv-scanner", _run_osv),
    ("snyk", _run_snyk),
)


def _finalise_report(
    report: SCAReport,
    *,
    findings: list[SCAFinding],
    source: str,
    scanner_binary: str,
) -> SCAReport:
    report.source = source
    report.scanner_binary = scanner_binary
    report.findings = findings
    report.total_findings = len(findings)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    report.severity_counts = counts
    return report


def scan_sca(
    app_path: Path | str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 300,
) -> SCAReport:
    """Run a dependency vulnerability scanner and normalise findings.

    Module-global state audit: this helper reads immutable scanner
    constants only; every worker derives scan state from the app path,
    PATH-discovered scanner binary, and subprocess output.
    """
    root = Path(app_path).resolve()
    report = SCAReport(app_path=str(root))
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
            report.error = (
                f"unknown scanner {scanner!r} "
                "(supported: npm-audit, osv-scanner, snyk)"
            )
            return report
        candidates = (chosen,)

    for name, fn in candidates:
        findings, bin_path, error = fn(root, timeout)
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


def _severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity.upper())
    except ValueError:
        return 0


def _first_fixed_version(raw: str) -> str:
    for chunk in re.split(r"[,;\s]+", raw or ""):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        match = re.search(r"\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]+)?", cleaned)
        if match:
            return match.group(0)
    return ""


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.lower()).strip("-._")
    return cleaned or "dependency"


def _ecosystem_for_fix(finding: SCAFinding) -> str:
    ecosystem = (finding.ecosystem or "").strip().lower()
    if ecosystem:
        return "npm" if ecosystem in {"node", "javascript"} else ecosystem
    path = finding.path.lower()
    if "package" in path or finding.tool == "npm-audit":
        return "npm"
    return "generic"


def _update_command(ecosystem: str, package: str, fixed_version: str) -> str:
    if ecosystem == "npm":
        return f"npm install {package}@{fixed_version} --package-lock-only"
    if ecosystem in {"pypi", "python"}:
        return (
            "pip-compile --upgrade-package "
            f"{package}=={fixed_version} backend/requirements.in"
        )
    return f"update {package} to {fixed_version} in the dependency lockfile"


def _render_fix_pr_body(fix: SCAFixPR) -> str:
    vuln_lines = "\n".join(f"- `{v}`" for v in fix.vulnerability_ids)
    version_lines = "\n".join(f"- `{v}`" for v in fix.current_versions) or "- unknown"
    path_lines = "\n".join(f"- `{p}`" for p in fix.source_paths) or "- scanner output"
    return "\n".join(
        [
            "## Security dependency update",
            "",
            f"- Package: `{fix.package}`",
            f"- Ecosystem: `{fix.ecosystem}`",
            f"- Severity: `{fix.severity}`",
            f"- Fixed version: `{fix.fixed_version}`",
            "",
            "## Vulnerabilities",
            "",
            vuln_lines,
            "",
            "## Current versions",
            "",
            version_lines,
            "",
            "## Affected manifests / lockfiles",
            "",
            path_lines,
            "",
            "## Update command",
            "",
            f"```bash\n{fix.update_command}\n```",
            "",
            "## Review checklist",
            "",
            "- [ ] Lockfile regenerated in the same commit",
            "- [ ] Relevant dependency tests pass",
            "- [ ] Follow docs/ops/dependency_upgrade_runbook.md if CI fails",
            "",
            "_Generated by `backend.security_scanning.sca` SC.3.2 "
            "dependabot-style fix-PR planner._",
        ]
    )


def plan_sca_fix_prs(
    report: SCAReport,
    *,
    base_branch: str = "master",
) -> list[SCAFixPR]:
    """Build dependabot-style PR plans for fixable blocking findings.

    Module-global state audit: immutable constants only; every worker
    derives fix-PR plans from the supplied report findings.
    """
    groups: dict[tuple[str, str, str], list[SCAFinding]] = {}
    for finding in report.blocking_findings:
        fixed_version = _first_fixed_version(finding.fixed_version)
        if not fixed_version or not finding.package:
            continue
        ecosystem = _ecosystem_for_fix(finding)
        key = (ecosystem, finding.package, fixed_version)
        groups.setdefault(key, []).append(finding)

    fixes: list[SCAFixPR] = []
    for (ecosystem, package, fixed_version), findings in sorted(groups.items()):
        max_severity = max(
            (f.severity for f in findings),
            key=_severity_rank,
            default="INFO",
        )
        vuln_ids = sorted({f.vulnerability_id for f in findings if f.vulnerability_id})
        current_versions = sorted({f.version for f in findings if f.version})
        source_paths = sorted({f.path for f in findings if f.path})
        branch = (
            "omnisight/sca-fix/"
            f"{_slug(ecosystem)}/{_slug(package)}-{_slug(fixed_version)}"
        )
        title = (
            f"fix(deps): bump {package} to {fixed_version}"
            + (f" for {vuln_ids[0]}" if len(vuln_ids) == 1 else "")
        )
        labels = [
            "security",
            "dependencies",
            "auto-merge",
            "priority/critical" if max_severity == "CRITICAL" else "priority/high",
        ]
        fix = SCAFixPR(
            package=package,
            ecosystem=ecosystem,
            fixed_version=fixed_version,
            vulnerability_ids=vuln_ids,
            current_versions=current_versions,
            source_paths=source_paths,
            severity=max_severity,
            base_branch=base_branch,
            branch=branch,
            title=title,
            labels=labels,
            update_command=_update_command(ecosystem, package, fixed_version),
            automerge=True,
        )
        fix.body = _render_fix_pr_body(fix)
        fixes.append(fix)
    return fixes


def write_sca_fix_pr_artifact(
    fixes: list[SCAFixPR],
    workspace_path: Path | str,
) -> Path:
    out = Path(workspace_path).resolve() / SCA_FIX_PR_ARTIFACT
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fix_pr_count": len(fixes),
        "fix_prs": [fix.to_dict() for fix in fixes],
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return out


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run an OmniSight SCA dependency vulnerability scan.",
    )
    parser.add_argument("--app-path", required=True)
    parser.add_argument("--scanner", default="")
    parser.add_argument("--fail-on", default="CRITICAL,HIGH")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--base-branch", default="master")
    parser.add_argument("--fix-pr-out", default="")
    args = parser.parse_args(argv)

    fail_on = [s.strip() for s in args.fail_on.split(",") if s.strip()]
    report = scan_sca(
        args.app_path,
        scanner=args.scanner or None,
        fail_on=fail_on,
        timeout=args.timeout,
    )
    if args.fix_pr_out:
        fixes = plan_sca_fix_prs(report, base_branch=args.base_branch)
        out = write_sca_fix_pr_artifact(fixes, args.fix_pr_out)
        logger.info("wrote SCA fix-PR artifact to %s", out)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


__all__ = [
    "SCA_FIX_PR_ARTIFACT",
    "SCAFinding",
    "SCAFixPR",
    "SCAReport",
    "SCASeverity",
    "plan_sca_fix_prs",
    "scan_sca",
    "write_sca_fix_pr_artifact",
]


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
