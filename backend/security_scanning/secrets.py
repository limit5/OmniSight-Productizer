"""SC.5.1 — Secret scanners for generated workspaces and repos.

Dispatches to whichever secret scanner is on ``PATH``:

    gitleaks    — Gitleaks repository/worktree scan, emitted as JSON
    trufflehog  — TruffleHog filesystem scan, emitted as JSON lines

Both scanners are optional. The scan returns a uniform
``SecretScanReport`` whose ``source`` field distinguishes which tool
produced the data. When no tool is available, the report is marked
``source="mock"`` so later SC gates can treat that as skipped rather
than clean.

Raw secret values are never stored in the normalised report. Findings
preserve scanner fingerprints, redacted evidence when the scanner
provides it, and a short SHA-256 prefix for correlation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.security_scanning.sast import DEFAULT_FAIL_ON, SEVERITY_ORDER

logger = logging.getLogger(__name__)


SecretSeverity = str


@dataclass
class SecretFinding:
    rule_id: str
    description: str = ""
    path: str = ""
    line: int = 0
    commit: str = ""
    fingerprint: str = ""
    redacted: str = ""
    secret_sha256_prefix: str = ""
    verified: bool = False
    severity: SecretSeverity = "HIGH"
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecretScanReport:
    source: str = "mock"  # "gitleaks" / "trufflehog" / "mock"
    app_path: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[SecretFinding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[SecretFinding]:
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


def _normalise_severity(raw: Any) -> SecretSeverity:
    if raw is None:
        return "HIGH"
    s = str(raw).strip().upper()
    if s in SEVERITY_ORDER:
        return s
    if s in {"TRUE", "VERIFIED", "CONFIRMED"}:
        return "CRITICAL"
    if s in {"FALSE", "UNVERIFIED", "UNKNOWN"}:
        return "HIGH"
    try:
        score = float(s)
    except ValueError:
        return "HIGH"
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


def _json_loads(raw: str) -> Any:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def _secret_hash(raw: Any) -> str:
    if not raw:
        return ""
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:16]


def _int_value(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _parse_gitleaks(payload: Any) -> list[SecretFinding]:
    if isinstance(payload, dict):
        raw_findings = payload.get("findings") or payload.get("Findings") or []
    elif isinstance(payload, list):
        raw_findings = payload
    else:
        raw_findings = []

    findings: list[SecretFinding] = []
    for entry in raw_findings:
        if not isinstance(entry, dict):
            continue
        rule_id = str(entry.get("RuleID") or entry.get("rule_id") or "")
        raw_secret = entry.get("Secret") or entry.get("secret")
        findings.append(
            SecretFinding(
                rule_id=rule_id,
                description=str(
                    entry.get("Description") or entry.get("description") or ""
                ),
                path=str(entry.get("File") or entry.get("file") or ""),
                line=_int_value(entry.get("StartLine") or entry.get("start_line")),
                commit=str(entry.get("Commit") or entry.get("commit") or ""),
                fingerprint=str(
                    entry.get("Fingerprint") or entry.get("fingerprint") or ""
                ),
                secret_sha256_prefix=_secret_hash(raw_secret),
                severity="HIGH",
                tool="gitleaks",
            )
        )
    return findings


def _trufflehog_git_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    source_metadata = entry.get("SourceMetadata") or {}
    data = source_metadata.get("Data") or {}
    for key in ("Git", "Filesystem"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _parse_trufflehog_jsonl(raw: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        payload = _json_loads(line)
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            metadata = _trufflehog_git_metadata(entry)
            verified = bool(entry.get("Verified"))
            raw_secret = entry.get("Raw") or entry.get("RawV2")
            findings.append(
                SecretFinding(
                    rule_id=str(
                        entry.get("DetectorName")
                        or entry.get("DetectorType")
                        or ""
                    ),
                    description=str(entry.get("DecoderName") or ""),
                    path=str(
                        metadata.get("file")
                        or metadata.get("File")
                        or metadata.get("path")
                        or ""
                    ),
                    line=_int_value(metadata.get("line") or metadata.get("Line")),
                    commit=str(metadata.get("commit") or metadata.get("Commit") or ""),
                    fingerprint=str(entry.get("SourceID") or ""),
                    redacted=str(entry.get("Redacted") or ""),
                    secret_sha256_prefix=_secret_hash(raw_secret),
                    verified=verified,
                    severity="CRITICAL" if verified else "HIGH",
                    tool="trufflehog",
                )
            )
    return findings


def _run_gitleaks(
    app_path: Path,
    timeout: int,
) -> tuple[list[SecretFinding], str, str]:
    bin_path = shutil.which("gitleaks")
    if not bin_path:
        return [], "", ""
    with tempfile.TemporaryDirectory(prefix="omnisight-gitleaks-") as tmp:
        report_path = Path(tmp) / "gitleaks.json"
        rc, out, err = _run(
            [
                "gitleaks",
                "detect",
                "--source",
                str(app_path),
                "--report-format",
                "json",
                "--report-path",
                str(report_path),
                "--no-banner",
                "--redact",
            ],
            cwd=app_path,
            timeout=timeout,
        )
        if rc not in (0, 1):
            logger.info("gitleaks detect failed rc=%s err=%s", rc, err[:200])
            return [], bin_path, err or out
        raw = report_path.read_text() if report_path.exists() else out
    return _parse_gitleaks(_json_loads(raw)), bin_path, ""


def _run_trufflehog(
    app_path: Path,
    timeout: int,
) -> tuple[list[SecretFinding], str, str]:
    bin_path = shutil.which("trufflehog")
    if not bin_path:
        return [], "", ""
    rc, out, err = _run(
        ["trufflehog", "filesystem", "--json", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 183):
        logger.info("trufflehog filesystem failed rc=%s err=%s", rc, err[:200])
        return [], bin_path, err or out
    return _parse_trufflehog_jsonl(out), bin_path, ""


_SCANNER_ORDER: tuple[tuple[str, Any], ...] = (
    ("gitleaks", _run_gitleaks),
    ("trufflehog", _run_trufflehog),
)


def _finalise_report(
    report: SecretScanReport,
    *,
    findings: list[SecretFinding],
    source: str,
    scanner_binary: str,
) -> SecretScanReport:
    report.source = source
    report.scanner_binary = scanner_binary
    report.findings = findings
    report.total_findings = len(findings)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    report.severity_counts = counts
    return report


def scan_secrets(
    app_path: Path | str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 300,
) -> SecretScanReport:
    """Run Gitleaks/TruffleHog and normalise secret findings.

    Module-global state audit: this helper reads immutable scanner
    constants only; every worker derives scan state from the app path,
    PATH-discovered scanner binary, and subprocess output.
    """
    root = Path(app_path).resolve()
    report = SecretScanReport(app_path=str(root))
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
                "(supported: gitleaks, trufflehog)"
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


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run an OmniSight secret scan.",
    )
    parser.add_argument("--app-path", required=True)
    parser.add_argument("--scanner", default="")
    parser.add_argument("--fail-on", default="CRITICAL,HIGH")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    fail_on = [s.strip() for s in args.fail_on.split(",") if s.strip()]
    report = scan_secrets(
        args.app_path,
        scanner=args.scanner or None,
        fail_on=fail_on,
        timeout=args.timeout,
    )
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


__all__ = [
    "SecretFinding",
    "SecretScanReport",
    "SecretSeverity",
    "scan_secrets",
]


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
