#!/usr/bin/env python3
"""N6 — triage OSV-Scanner JSON output into a GitHub issue body.

Companion to ``.github/workflows/cve-scan.yml``. The workflow runs
``osv-scanner`` daily; this script consumes the JSON report and emits:

1. a markdown issue body (``--out``) that summarises severe findings
   with per-package drill-down, and
2. a ``has_severe`` flag on ``$GITHUB_OUTPUT`` that tells the workflow
   whether to open / close the tracking issue.

Why a standalone script instead of inline shell:
  * unit-testable — pure-function parse/classify/render (no network,
    no GitHub API), so we can cover the triage logic without spinning
    up GH Actions.
  * stdlib-only — the CVE scanner is the *one* job that MUST keep
    working when something else is broken. Adding a pip dependency on
    ``requests`` / ``pyyaml`` creates a catch-22 where a CVE against
    one of those packages would take the CVE scanner offline.
  * deterministic severity rules — the markdown thresholds live in
    :func:`classify_severity` and are covered by unit tests.

Usage (CI):
    python3 scripts/cve_triage.py \\
        --input osv-scan.json \\
        --out cve-issue-body.md \\
        --severity-threshold HIGH \\
        --run-url https://github.com/.../actions/runs/12345

Exit code is always 0 unless the input file is unreadable. The
workflow determines "fail vs pass" by reading ``has_severe`` from
``$GITHUB_OUTPUT``, not from the exit code — we never want a scan
failure to cascade into a CI gate (the whole point is async tracking).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# GitHub issue body hard-cap is 65 536 bytes; leave headroom for
# markdown rendering overhead and the trailing footer.
ISSUE_BODY_MAX = 60_000

# Severity ordering — higher index means more severe. We classify by
# mapping OSV's CVSS score or string severity to one of these.
SEVERITY_ORDER = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
SEVERITY_INDEX = {name: i for i, name in enumerate(SEVERITY_ORDER)}


@dataclass
class Finding:
    """Normalized view of a single OSV finding."""

    package: str
    ecosystem: str
    version: str
    vulnerability_id: str
    aliases: list[str] = field(default_factory=list)
    severity: str = "UNKNOWN"
    cvss_score: float | None = None
    summary: str = ""
    fixed_versions: list[str] = field(default_factory=list)
    source_path: str = ""

    @property
    def primary_id(self) -> str:
        """Prefer CVE-* alias over GHSA-* or OSV-* for display."""
        for candidate in [self.vulnerability_id, *self.aliases]:
            if candidate.startswith("CVE-"):
                return candidate
        return self.vulnerability_id

    @property
    def severity_rank(self) -> int:
        return SEVERITY_INDEX.get(self.severity, 0)


def classify_severity(raw: dict) -> tuple[str, float | None]:
    """Map an OSV `severity` block to (label, score).

    OSV records carry severity as a list of ``{type, score}`` dicts
    where ``type`` is ``CVSS_V3`` / ``CVSS_V4`` and ``score`` is a
    vector string whose first numeric component is the base score.

    Some OSV records (notably those derived from GHSA) carry a
    string-form ``database_specific.severity`` that is already
    ``LOW`` / ``MODERATE`` / ``HIGH`` / ``CRITICAL``. We honour both.
    """
    # Path 1: GHSA-style string severity (fast path, no parsing).
    ghsa_severity = (raw.get("database_specific") or {}).get("severity")
    if isinstance(ghsa_severity, str):
        up = ghsa_severity.upper()
        if up == "MODERATE":
            up = "MEDIUM"
        if up in SEVERITY_INDEX:
            return up, None

    # Path 2: CVSS vector — parse the leading numeric score.
    severities = raw.get("severity") or []
    best_score: float | None = None
    for entry in severities:
        if not isinstance(entry, dict):
            continue
        score_raw = entry.get("score")
        if not isinstance(score_raw, str):
            continue
        # Vector strings look like "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/..."
        # The overall score is passed separately in some feeds; when
        # absent, fall back to a conservative "UNKNOWN" label rather
        # than guessing.
        parsed = _extract_base_score(score_raw)
        if parsed is None:
            continue
        best_score = parsed if best_score is None else max(best_score, parsed)

    if best_score is None:
        return "UNKNOWN", None

    # Standard CVSS v3.x label boundaries.
    if best_score >= 9.0:
        return "CRITICAL", best_score
    if best_score >= 7.0:
        return "HIGH", best_score
    if best_score >= 4.0:
        return "MEDIUM", best_score
    if best_score > 0.0:
        return "LOW", best_score
    return "UNKNOWN", best_score


def _extract_base_score(score: str) -> float | None:
    """Pull a float out of a CVSS vector string or raw number string."""
    # Plain numeric ("7.5"), common in some ecosystems.
    try:
        return float(score)
    except ValueError:
        pass
    # Some OSV records carry the base score as a leading numeric in
    # the vector: "7.5 (CVSS:3.1/...)"
    head = score.split(None, 1)[0] if score else ""
    try:
        return float(head.rstrip(","))
    except ValueError:
        return None


def parse_osv_report(raw: dict) -> list[Finding]:
    """Flatten an OSV-Scanner JSON report into a list of findings.

    The scanner's output shape (v1 / v2) is:

    ```
    {"results": [{"source": {...}, "packages": [
        {"package": {"name": ..., "ecosystem": ..., "version": ...},
         "vulnerabilities": [{"id": ..., "aliases": [...], ...}],
         "groups": [{"ids": [...], "max_severity": "7.5"}]
        }
    ]}]}
    ```

    We iterate over every package × vulnerability pair and project
    onto :class:`Finding`. Unknown / malformed structures are skipped
    silently; a malformed scan should not prevent reporting the
    well-formed parts.
    """
    findings: list[Finding] = []
    results = raw.get("results") or []
    if not isinstance(results, list):
        return findings
    for result in results:
        if not isinstance(result, dict):
            continue
        source_path = (result.get("source") or {}).get("path", "")
        packages = result.get("packages") or []
        if not isinstance(packages, list):
            continue
        for pkg_entry in packages:
            if not isinstance(pkg_entry, dict):
                continue
            pkg = pkg_entry.get("package") or {}
            name = pkg.get("name", "") or ""
            ecosystem = pkg.get("ecosystem", "") or ""
            version = pkg.get("version", "") or ""
            vulns = pkg_entry.get("vulnerabilities") or []
            if not isinstance(vulns, list):
                continue
            for vuln in vulns:
                if not isinstance(vuln, dict):
                    continue
                severity, score = classify_severity(vuln)
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
                findings.append(
                    Finding(
                        package=name,
                        ecosystem=ecosystem,
                        version=version,
                        vulnerability_id=vuln.get("id", "") or "",
                        aliases=[
                            a for a in (vuln.get("aliases") or [])
                            if isinstance(a, str)
                        ],
                        severity=severity,
                        cvss_score=score,
                        summary=(vuln.get("summary") or "").strip(),
                        fixed_versions=sorted(set(fixed)),
                        source_path=source_path,
                    )
                )
    return findings


def filter_severe(
    findings: list[Finding], threshold: str = "HIGH"
) -> list[Finding]:
    """Retain findings at or above the given severity label."""
    cutoff = SEVERITY_INDEX.get(threshold.upper(), SEVERITY_INDEX["HIGH"])
    return [f for f in findings if f.severity_rank >= cutoff]


def render_issue_body(
    severe: list[Finding],
    all_findings: list[Finding],
    run_url: str,
) -> str:
    """Render the GitHub issue body for the daily CVE tracking issue."""
    lines: list[str] = []
    date = _utc_date()
    lines.append(f"# Daily CVE Scan — {date}")
    lines.append("")
    lines.append(
        f"`osv-scanner` surfaced **{len(severe)} severe findings** "
        f"(HIGH/CRITICAL) out of {len(all_findings)} total. "
        f"Run: {run_url}"
    )
    lines.append("")
    lines.append(
        "This issue is a **tracking record** for the daily CVE scan "
        "(N6). The actual *fix PR* should flow through Renovate's "
        "vulnerability fast-path (N2 `vulnerabilityAlerts` + "
        "`osvVulnerabilityAlerts`). Operator action is to confirm the "
        "Renovate PR landed and the production deploy carries the fix."
    )
    lines.append("")
    lines.append("## Severe findings")
    lines.append("")
    if not severe:
        lines.append(
            "_No severe findings — this issue should not have been "
            "created; file a bug against `scripts/cve_triage.py`._"
        )
    else:
        # Summary table first.
        lines.append("| Severity | CVSS | Package | Ecosystem | Version | CVE / ID | Fix |")
        lines.append("|---|---|---|---|---|---|---|")
        for f in sorted(
            severe, key=lambda x: (-x.severity_rank, -(x.cvss_score or 0.0))
        ):
            fix = ", ".join(f.fixed_versions) if f.fixed_versions else "—"
            cvss = f"{f.cvss_score:.1f}" if f.cvss_score is not None else "—"
            lines.append(
                f"| {f.severity} | {cvss} | `{f.package}` | {f.ecosystem} "
                f"| `{f.version}` | {f.primary_id} | {fix} |"
            )
        lines.append("")
        lines.append("## Per-CVE detail")
        lines.append("")
        for f in sorted(
            severe, key=lambda x: (-x.severity_rank, -(x.cvss_score or 0.0))
        ):
            lines.append(f"### {f.primary_id} — `{f.package}@{f.version}`")
            lines.append("")
            if f.summary:
                lines.append(f"> {f.summary}")
                lines.append("")
            meta: list[str] = [
                f"- **Severity**: {f.severity}"
                + (f" (CVSS {f.cvss_score:.1f})" if f.cvss_score is not None else ""),
                f"- **Ecosystem**: {f.ecosystem}",
                f"- **Affected file**: `{f.source_path}`",
            ]
            if f.fixed_versions:
                meta.append(f"- **Fixed in**: {', '.join(f.fixed_versions)}")
            if f.aliases:
                meta.append(f"- **Aliases**: {', '.join(f.aliases)}")
            lines.extend(meta)
            lines.append("")
    lines.append("---")
    lines.append(
        "_Generated by `scripts/cve_triage.py` from the "
        "`.github/workflows/cve-scan.yml` nightly job._"
    )
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > ISSUE_BODY_MAX:
        # Hard-cap: keep the summary table but drop per-CVE detail.
        head, _, _ = body.partition("## Per-CVE detail")
        body = head + (
            "\n_Per-CVE detail omitted: exceeded GitHub issue body "
            "cap. See the `osv-scan-${run_id}` artifact for the full "
            "report._\n"
        )
    return body


def _utc_date() -> str:
    # datetime.datetime.utcnow() is deprecated in 3.12; use timezone-aware.
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def emit_github_output(has_severe: bool, findings_count: int) -> None:
    """Write outputs for the workflow's ``steps.<id>.outputs`` map."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        # Local / test invocation — write nothing.
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"has_severe={'true' if has_severe else 'false'}\n")
        fh.write(f"findings_count={findings_count}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--severity-threshold", default="HIGH")
    parser.add_argument("--run-url", default="")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        # If the scan itself failed, emit an issue body explaining so
        # and call it "severe" so the workflow opens a tracking issue.
        print(
            f"::warning::osv-scanner output {args.input} is missing — "
            "assuming scan failure",
            file=sys.stderr,
        )
        body = (
            f"# Daily CVE Scan — {_utc_date()}\n\n"
            f"⚠️ `osv-scanner` did not produce output at `{args.input}`. "
            f"Re-run the workflow (`{args.run_url}`) or inspect the "
            "action logs for the underlying failure.\n"
        )
        args.out.write_text(body, encoding="utf-8")
        emit_github_output(has_severe=True, findings_count=0)
        return 0

    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"::error::malformed JSON in {args.input}: {exc}", file=sys.stderr)
        # Treat malformed JSON as an actionable alert: something is
        # wrong with the scanner itself, operators need to look.
        body = (
            f"# Daily CVE Scan — {_utc_date()}\n\n"
            f"⚠️ osv-scanner output failed to parse: `{exc}`. "
            f"Run: {args.run_url}\n"
        )
        args.out.write_text(body, encoding="utf-8")
        emit_github_output(has_severe=True, findings_count=0)
        return 0

    findings = parse_osv_report(raw)
    severe = filter_severe(findings, args.severity_threshold)
    body = render_issue_body(severe, findings, args.run_url)
    args.out.write_text(body, encoding="utf-8")
    emit_github_output(has_severe=bool(severe), findings_count=len(findings))
    # stdout for workflow log:
    print(
        f"CVE scan processed: {len(findings)} total, "
        f"{len(severe)} severe (>= {args.severity_threshold})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
