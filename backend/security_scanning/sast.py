"""SC.1.1 — SAST scanner adapters.

Dispatches to whichever static-analysis scanner is on ``PATH``:

    codeql      — CodeQL CLI, emitted as SARIF
    semgrep     — Semgrep OSS/Cloud rules, emitted as JSON
    snyk-code   — Snyk Code CLI, emitted as JSON/SARIF

All three are optional.  The scan returns a uniform ``SASTReport`` whose
``source`` field distinguishes which tool produced the data.  When no
tool is available, the report is marked ``source="mock"`` so later SC
gates can treat that as skipped rather than clean.

Severity thresholding mirrors ``software_compliance.cves``: ``HIGH``
and ``CRITICAL`` findings block by default, while the raw finding list
is preserved so downstream policy can tighten or loosen independently.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


SASTSeverity = str

SEVERITY_ORDER: tuple[str, ...] = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")

DEFAULT_FAIL_ON: frozenset[str] = frozenset({"CRITICAL", "HIGH"})


def _normalise_severity(raw: Any) -> SASTSeverity:
    if raw is None:
        return "INFO"
    s = str(raw).strip().upper()
    if s in SEVERITY_ORDER:
        return s
    if s in {"ERROR", "ERR"}:
        return "HIGH"
    if s in {"WARNING", "WARN"}:
        return "MEDIUM"
    if s in {"NOTE", "NOTICE", "NONE"}:
        return "LOW"
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


@dataclass
class SASTFinding:
    rule_id: str
    message: str
    path: str = ""
    line: int = 0
    column: int = 0
    severity: SASTSeverity = "INFO"
    cwe: list[str] = field(default_factory=list)
    owasp: list[str] = field(default_factory=list)
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SASTReport:
    source: str = "mock"  # "codeql" / "semgrep" / "snyk-code" / "mock"
    app_path: str = ""
    scanner_binary: str = ""
    total_findings: int = 0
    findings: list[SASTFinding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    fail_on: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def blocking_findings(self) -> list[SASTFinding]:
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


def _collect_tags(raw: Any, prefix: str) -> list[str]:
    tags: list[str] = []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(v) for v in raw]
    else:
        values = []
    for value in values:
        cleaned = value.strip()
        if cleaned.lower().startswith(prefix):
            tags.append(cleaned)
    return sorted(set(tags))


def _sarif_severity(result: dict[str, Any], rule_props: dict[str, Any]) -> SASTSeverity:
    props = result.get("properties") or {}
    for candidate in (
        props.get("security-severity"),
        rule_props.get("security-severity"),
        props.get("severity"),
        result.get("level"),
        rule_props.get("problem.severity"),
    ):
        sev = _normalise_severity(candidate)
        if sev != "INFO" or candidate is not None:
            return sev
    return "INFO"


def _parse_sarif(payload: dict[str, Any], *, tool: str) -> list[SASTFinding]:
    findings: list[SASTFinding] = []
    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        rules: dict[str, dict[str, Any]] = {}
        driver = (run.get("tool") or {}).get("driver") or {}
        for rule in driver.get("rules") or []:
            if isinstance(rule, dict):
                rules[str(rule.get("id") or "")] = rule.get("properties") or {}
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            rule_id = str(result.get("ruleId") or "")
            message_raw = result.get("message") or {}
            if isinstance(message_raw, dict):
                message = str(message_raw.get("text") or message_raw.get("markdown") or "")
            else:
                message = str(message_raw or "")
            path = ""
            line = 0
            column = 0
            locations = result.get("locations") or []
            if locations:
                physical = (locations[0].get("physicalLocation") or {})
                artifact = physical.get("artifactLocation") or {}
                region = physical.get("region") or {}
                path = str(artifact.get("uri") or "")
                line = int(region.get("startLine") or 0)
                column = int(region.get("startColumn") or 0)
            props = result.get("properties") or {}
            rule_props = rules.get(rule_id, {})
            tags = []
            for raw_tags in (props.get("tags"), rule_props.get("tags")):
                if isinstance(raw_tags, list):
                    tags.extend(str(t) for t in raw_tags)
            findings.append(
                SASTFinding(
                    rule_id=rule_id,
                    message=message,
                    path=path,
                    line=line,
                    column=column,
                    severity=_sarif_severity(result, rule_props),
                    cwe=_collect_tags(tags, "external/cwe/"),
                    owasp=_collect_tags(tags, "external/owasp/"),
                    tool=tool,
                )
            )
    return findings


def _parse_semgrep(payload: dict[str, Any]) -> list[SASTFinding]:
    if "runs" in payload:
        return _parse_sarif(payload, tool="semgrep")
    findings: list[SASTFinding] = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        extra = result.get("extra") or {}
        start = result.get("start") or {}
        metadata = extra.get("metadata") or {}
        findings.append(
            SASTFinding(
                rule_id=str(result.get("check_id") or result.get("rule_id") or ""),
                message=str(extra.get("message") or result.get("message") or ""),
                path=str(result.get("path") or ""),
                line=int(start.get("line") or 0),
                column=int(start.get("col") or 0),
                severity=_normalise_severity(extra.get("severity")),
                cwe=_collect_tags(metadata.get("cwe"), "cwe-"),
                owasp=_collect_tags(metadata.get("owasp"), "owasp"),
                tool="semgrep",
            )
        )
    return findings


def _parse_snyk(payload: dict[str, Any]) -> list[SASTFinding]:
    if "runs" in payload:
        return _parse_sarif(payload, tool="snyk-code")
    findings: list[SASTFinding] = []
    for result in payload.get("results") or payload.get("issues") or []:
        if not isinstance(result, dict):
            continue
        locations = result.get("locations") or []
        location = locations[0] if locations else result.get("location") or {}
        findings.append(
            SASTFinding(
                rule_id=str(result.get("id") or result.get("ruleId") or ""),
                message=str(result.get("title") or result.get("message") or ""),
                path=str(location.get("path") or location.get("file") or ""),
                line=int(location.get("line") or 0),
                column=int(location.get("column") or 0),
                severity=_normalise_severity(result.get("severity") or result.get("level")),
                cwe=_collect_tags(result.get("cwe"), "cwe-"),
                owasp=_collect_tags(result.get("owasp"), "owasp"),
                tool="snyk-code",
            )
        )
    return findings


def _detect_codeql_language(app_path: Path) -> str:
    markers: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("javascript-typescript", ("package.json", "tsconfig.json", "next.config.js")),
        ("python", ("pyproject.toml", "requirements.txt", "setup.py")),
        ("go", ("go.mod",)),
        ("java-kotlin", ("pom.xml", "build.gradle", "build.gradle.kts")),
        ("rust", ("Cargo.toml",)),
        ("c-cpp", ("CMakeLists.txt", "Makefile")),
        ("csharp", ("*.csproj", "*.sln")),
        ("ruby", ("Gemfile",)),
    )
    for language, names in markers:
        for name in names:
            if "*" in name:
                if any(app_path.glob(name)):
                    return language
            elif (app_path / name).exists():
                return language
    return "javascript-typescript"


def _run_codeql(app_path: Path, timeout: int) -> tuple[list[SASTFinding], str]:
    bin_path = shutil.which("codeql")
    if not bin_path:
        return [], ""
    language = _detect_codeql_language(app_path)
    with tempfile.TemporaryDirectory(prefix="omnisight-codeql-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "db"
        sarif_path = tmp_path / "codeql.sarif"
        rc, _out, err = _run(
            [
                "codeql",
                "database",
                "create",
                str(db_path),
                "--overwrite",
                f"--language={language}",
                f"--source-root={app_path}",
            ],
            cwd=app_path,
            timeout=timeout,
        )
        if rc != 0:
            logger.info("codeql database create failed rc=%s err=%s", rc, err[:200])
            return [], bin_path
        rc, out, err = _run(
            [
                "codeql",
                "database",
                "analyze",
                str(db_path),
                f"codeql/{language}-queries",
                "--format=sarif-latest",
                f"--output={sarif_path}",
                "--download",
            ],
            cwd=app_path,
            timeout=timeout,
        )
        if rc not in (0, 1):
            logger.info("codeql analyze failed rc=%s err=%s", rc, err[:200])
            return [], bin_path
        raw = sarif_path.read_text() if sarif_path.exists() else out
    return _parse_sarif(_json_loads(raw), tool="codeql"), bin_path


def _run_semgrep(app_path: Path, timeout: int) -> tuple[list[SASTFinding], str]:
    bin_path = shutil.which("semgrep")
    if not bin_path:
        return [], ""
    rc, out, err = _run(
        ["semgrep", "scan", "--json", "--config", "auto", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("semgrep failed rc=%s err=%s", rc, err[:200])
        return [], bin_path
    return _parse_semgrep(_json_loads(out)), bin_path


def _run_snyk_code(app_path: Path, timeout: int) -> tuple[list[SASTFinding], str]:
    bin_path = shutil.which("snyk")
    if not bin_path:
        return [], ""
    rc, out, err = _run(
        ["snyk", "code", "test", "--json", str(app_path)],
        cwd=app_path,
        timeout=timeout,
    )
    if rc not in (0, 1):
        logger.info("snyk code failed rc=%s err=%s", rc, err[:200])
        return [], bin_path
    return _parse_snyk(_json_loads(out)), bin_path


_SCANNER_ORDER: tuple[tuple[str, Any], ...] = (
    ("codeql", _run_codeql),
    ("semgrep", _run_semgrep),
    ("snyk-code", _run_snyk_code),
)


def scan_sast(
    app_path: Path | str,
    *,
    scanner: Optional[str] = None,
    fail_on: Iterable[str] = DEFAULT_FAIL_ON,
    timeout: int = 300,
) -> SASTReport:
    """Run a SAST scanner and normalise its findings.

    When ``scanner`` is ``None`` we probe CodeQL -> Semgrep -> Snyk Code
    and use the first one on PATH.  Pass an explicit name to force.
    """
    root = Path(app_path).resolve()
    report = SASTReport(app_path=str(root))
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
                "(supported: codeql, semgrep, snyk-code)"
            )
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
            for finding in findings:
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            report.severity_counts = counts
            return report

    return report


__all__ = [
    "DEFAULT_FAIL_ON",
    "SASTFinding",
    "SASTReport",
    "SASTSeverity",
    "SEVERITY_ORDER",
    "scan_sast",
]
