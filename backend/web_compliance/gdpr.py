"""W5 #279 — GDPR posture scan.

Scans a web application's source/build directory plus its server config
for the four GDPR posture items the W5 ticket calls out:

    1. **Cookie banner** — any recognised consent-manager string
       (OneTrust / Cookiebot / Klaro / usercentrics / Iubenda / a
       hand-rolled `<cookie-banner>` element) appears in the rendered
       HTML or one of its scripts.
    2. **Data retention policy** — a file at
       ``docs/privacy/retention.md`` / ``PRIVACY.md`` /
       ``privacy-policy.{md,html}`` exists and mentions a retention
       horizon (days / months / years).
    3. **DPA template** — a signed or fillable DPA at
       ``docs/privacy/dpa*.md`` / ``docs/legal/dpa*.pdf`` is present.
    4. **Right-to-be-forgotten endpoint** — at least one HTTP handler
       matches one of the canonical RTBF path patterns (``/gdpr/delete``
       / ``/privacy/delete`` / ``/account/delete`` / ``/user/erase``
       / ``/v*/users/:id/delete``) OR the source grep finds an RTBF
       decorator / comment sentinel (``# gdpr:rtbf`` / ``@rtbf`` /
       ``rightToBeForgotten``).

The scan is intentionally string-based and static — it never hits the
live service — so it works the same in CI and sandbox. False positives
are fine (a repo that uses the word "cookie" ten times will pass the
banner check); this gate is about whether the *evidence exists*, not
about measuring runtime compliance. Runtime compliance is the job of
OneTrust/equivalent.

Each individual item is independently pass/fail so the emitted
``GDPRReport`` can tell the reviewer *which* artefacts are missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Detection rules ─────────────────────────────────────────────────

COOKIE_BANNER_SIGNATURES = (
    "onetrust",
    "cookiebot",
    "klaro",
    "usercentrics",
    "iubenda",
    "cookie-banner",
    "cookieconsent",
    "cookie_consent",
    "tarteaucitron",
    "consent-manager",
    "consentmanager",
    "gdpr-banner",
    "gdprbanner",
)

RETENTION_FILE_CANDIDATES = (
    "docs/privacy/retention.md",
    "docs/privacy/retention.mdx",
    "docs/privacy/RETENTION.md",
    "docs/legal/retention.md",
    "PRIVACY.md",
    "privacy.md",
    "privacy-policy.md",
    "privacy-policy.html",
    "docs/privacy-policy.md",
    "docs/policies/retention.md",
)

DPA_FILE_CANDIDATES = (
    "docs/privacy/dpa.md",
    "docs/privacy/DPA.md",
    "docs/privacy/dpa-template.md",
    "docs/legal/dpa.md",
    "docs/legal/dpa-template.md",
    "docs/legal/dpa.pdf",
    "docs/legal/DPA.pdf",
    "DPA.md",
)

RTBF_ROUTE_PATTERNS = (
    re.compile(r"/gdpr/(?:delete|erase|forget)", re.I),
    re.compile(r"/privacy/(?:delete|erase|forget)", re.I),
    re.compile(r"/account/delete", re.I),
    re.compile(r"/user/(?:erase|forget)", re.I),
    re.compile(r"/users?/[:$][a-zA-Z_]+/delete", re.I),
    re.compile(r"/v\d+/users?/[^/]+/delete", re.I),
)

RTBF_SENTINELS = (
    "gdpr:rtbf",
    "@rtbf",
    "righttobeforgotten",
    "right_to_be_forgotten",
    "right-to-be-forgotten",
    "data_subject_deletion",
    "erase_user_data",
)

RETENTION_HORIZON_RE = re.compile(
    r"\b(\d{1,4})\s*(day|days|month|months|year|years|週|月|年)\b",
    re.I,
)


# ── Result dataclasses ──────────────────────────────────────────────

@dataclass
class GDPRCheck:
    """One GDPR posture check and its outcome."""

    id: str
    name: str
    passed: bool = False
    evidence: str = ""
    details: list[str] = field(default_factory=list)


@dataclass
class GDPRReport:
    """Aggregate of the four GDPR posture checks."""

    app_path: str = ""
    cookie_banner: GDPRCheck = field(default_factory=lambda: GDPRCheck(
        id="gdpr.cookie_banner", name="Cookie banner present"))
    retention_policy: GDPRCheck = field(default_factory=lambda: GDPRCheck(
        id="gdpr.retention_policy", name="Data retention policy documented"))
    dpa_template: GDPRCheck = field(default_factory=lambda: GDPRCheck(
        id="gdpr.dpa_template", name="DPA template available"))
    rtbf_endpoint: GDPRCheck = field(default_factory=lambda: GDPRCheck(
        id="gdpr.rtbf_endpoint", name="Right-to-be-forgotten endpoint exists"))

    @property
    def checks(self) -> list[GDPRCheck]:
        return [
            self.cookie_banner,
            self.retention_policy,
            self.dpa_template,
            self.rtbf_endpoint,
        ]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_path": self.app_path,
            "passed": self.passed,
            "passed_count": self.passed_count,
            "total_checks": len(self.checks),
            "checks": [asdict(c) for c in self.checks],
        }


# ── Scan helpers ────────────────────────────────────────────────────

def _iter_text_files(root: Path, extensions: Iterable[str]) -> Iterable[Path]:
    """Yield every file under ``root`` whose suffix is in ``extensions``."""
    ext_set = {e.lower() for e in extensions}
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in ext_set:
            # Skip the usual heavy / generated directories so a scan
            # against a fresh clone doesn't drag in node_modules.
            parts = set(p.parts)
            if parts & {"node_modules", ".git", "dist", "build", ".next",
                        ".output", ".vercel", "coverage", ".cache"}:
                continue
            yield p


def _scan_cookie_banner(root: Path) -> GDPRCheck:
    check = GDPRCheck(
        id="gdpr.cookie_banner",
        name="Cookie banner present",
    )
    signatures_found: list[str] = []
    for path in _iter_text_files(
        root, (".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"),
    ):
        try:
            blob = path.read_text(errors="ignore").lower()
        except OSError:
            continue
        for sig in COOKIE_BANNER_SIGNATURES:
            if sig in blob:
                signatures_found.append(f"{path.relative_to(root)}: {sig}")
                break  # one hit per file is enough
    if signatures_found:
        check.passed = True
        check.evidence = signatures_found[0]
        check.details = signatures_found[:20]
    else:
        check.details = [
            "No recognised consent-manager signature found "
            f"(looked for: {', '.join(COOKIE_BANNER_SIGNATURES)})"
        ]
    return check


def _scan_retention_policy(root: Path) -> GDPRCheck:
    check = GDPRCheck(
        id="gdpr.retention_policy",
        name="Data retention policy documented",
    )
    for rel in RETENTION_FILE_CANDIDATES:
        p = root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        horizon = RETENTION_HORIZON_RE.search(text)
        if horizon:
            check.passed = True
            check.evidence = f"{rel}: '{horizon.group(0)}'"
            check.details.append(f"horizon phrase: {horizon.group(0)}")
        else:
            check.passed = False
            check.evidence = f"{rel}: present but no retention horizon"
            check.details.append(
                f"{rel}: file exists but no 'N days/months/years' horizon"
            )
        return check
    check.details = [
        f"No retention policy file found (looked at: {', '.join(RETENTION_FILE_CANDIDATES[:4])}…)"
    ]
    return check


def _scan_dpa_template(root: Path) -> GDPRCheck:
    check = GDPRCheck(
        id="gdpr.dpa_template",
        name="DPA template available",
    )
    for rel in DPA_FILE_CANDIDATES:
        p = root / rel
        if p.is_file():
            check.passed = True
            check.evidence = rel
            check.details = [rel]
            return check
    check.details = [
        f"No DPA template found (looked at: {', '.join(DPA_FILE_CANDIDATES[:4])}…)"
    ]
    return check


def _scan_rtbf_endpoint(root: Path) -> GDPRCheck:
    check = GDPRCheck(
        id="gdpr.rtbf_endpoint",
        name="Right-to-be-forgotten endpoint exists",
    )
    hits: list[str] = []
    for path in _iter_text_files(
        root, (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
               ".java", ".kt", ".rb", ".php"),
    ):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        low = text.lower()
        for sentinel in RTBF_SENTINELS:
            if sentinel in low:
                hits.append(f"{rel}: sentinel '{sentinel}'")
                break
        else:
            for pat in RTBF_ROUTE_PATTERNS:
                if pat.search(text):
                    hits.append(f"{rel}: route match '{pat.pattern}'")
                    break
    if hits:
        check.passed = True
        check.evidence = hits[0]
        check.details = hits[:20]
    else:
        check.details = [
            "No RTBF endpoint or sentinel (gdpr:rtbf / @rtbf / "
            "rightToBeForgotten) found in source."
        ]
    return check


# ── Public entry ────────────────────────────────────────────────────

def scan_gdpr(app_path: Path | str) -> GDPRReport:
    """Run all four GDPR posture checks against the project directory."""
    root = Path(app_path).resolve()
    report = GDPRReport(app_path=str(root))
    if not root.is_dir():
        for check in report.checks:
            check.details = [f"app_path '{root}' is not a directory"]
        return report
    report.cookie_banner = _scan_cookie_banner(root)
    report.retention_policy = _scan_retention_policy(root)
    report.dpa_template = _scan_dpa_template(root)
    report.rtbf_endpoint = _scan_rtbf_endpoint(root)
    return report


__all__ = [
    "GDPRReport",
    "GDPRCheck",
    "scan_gdpr",
    "COOKIE_BANNER_SIGNATURES",
    "DPA_FILE_CANDIDATES",
    "RETENTION_FILE_CANDIDATES",
    "RTBF_ROUTE_PATTERNS",
    "RTBF_SENTINELS",
]
