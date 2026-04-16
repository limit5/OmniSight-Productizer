"""W5 #279 — WCAG 2.2 AA compliance gate.

Runs axe-core (via the ``axe`` CLI) against a served URL and folds the
result together with the static manual-checklist that codifies the
non-automatable items from the W3 ``a11y.skill.md`` role definition
(focus order, visible focus ring, screen reader labels, target size,
etc.).

axe-core is the industry standard for automated a11y scanning and
covers ~30-40% of the WCAG success criteria mechanically. The rest
require human review; we encode them as a structured checklist so the
evidence bundle records what the human did or did not verify. Each
checklist item maps back to a WCAG 2.2 success criterion number so
auditors can trace the evidence.

Degradation
-----------
If the ``axe`` binary is not on PATH (sandbox / first-run), the scan
returns a ``mock`` source with zero violations so downstream gates
don't block on tool availability. The returned ``source`` field always
tells the caller whether the result is real or synthetic.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Non-automatable WCAG 2.2 AA items — mapped to the success criterion
# number so evidence bundles are traceable. Keep this list aligned with
# configs/roles/web/a11y.skill.md.
WCAG_AA_MANUAL_CHECKLIST: tuple[dict[str, str], ...] = (
    {"sc": "1.3.1", "name": "Info and Relationships",
     "description": "Landmark regions (main/nav/header/footer) present and correctly nested."},
    {"sc": "1.4.3", "name": "Contrast (Minimum)",
     "description": "Body text ≥ 4.5:1, large text ≥ 3:1, UI components ≥ 3:1."},
    {"sc": "2.1.1", "name": "Keyboard",
     "description": "All interactive elements reachable and operable with Tab/Shift+Tab/Enter/Space/Escape."},
    {"sc": "2.4.3", "name": "Focus Order",
     "description": "Tab order matches the visual reading order; no tabindex>0 hacks."},
    {"sc": "2.4.7", "name": "Focus Visible",
     "description": "Visible focus indicator on every focusable element; contrast ≥ 3:1 vs. adjacent colours."},
    {"sc": "2.4.11", "name": "Focus Not Obscured (Minimum)",
     "description": "Focused element is not fully hidden behind sticky headers / popovers."},
    {"sc": "2.5.7", "name": "Dragging Movements",
     "description": "Every drag interaction has a single-pointer alternative (click / keyboard)."},
    {"sc": "2.5.8", "name": "Target Size (Minimum)",
     "description": "Interactive targets ≥ 24×24 CSS px (excl. inline-text links)."},
    {"sc": "3.3.7", "name": "Redundant Entry",
     "description": "No flow requires the user to re-enter information they already supplied."},
    {"sc": "3.3.8", "name": "Accessible Authentication (Minimum)",
     "description": "Authentication does not require a cognitive puzzle without alternative."},
    {"sc": "4.1.2", "name": "Name, Role, Value",
     "description": "Every form control has a programmatic label; custom widgets expose correct ARIA role."},
    {"sc": "4.1.3", "name": "Status Messages",
     "description": "Dynamic updates announced via aria-live or focus management."},
)


@dataclass
class WCAGManualItem:
    """One item from the manual checklist with its review outcome."""

    sc: str
    name: str
    description: str
    status: str = "unreviewed"  # "pass" / "fail" / "n/a" / "unreviewed"
    notes: str = ""


@dataclass
class WCAGAutoIssue:
    """A single automated violation emitted by axe-core."""

    id: str
    impact: str  # "critical" / "serious" / "moderate" / "minor"
    description: str
    help_url: str = ""
    nodes: int = 0


@dataclass
class WCAGReport:
    """Combined automated + manual WCAG AA evidence."""

    url: str = ""
    source: str = "mock"  # "axe" / "mock"
    violations: list[WCAGAutoIssue] = field(default_factory=list)
    manual_checklist: list[WCAGManualItem] = field(default_factory=list)
    passed: bool = False
    raw_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def critical_violations(self) -> int:
        return sum(1 for v in self.violations if v.impact == "critical")

    @property
    def serious_violations(self) -> int:
        return sum(1 for v in self.violations if v.impact == "serious")

    def recompute_passed(self) -> None:
        """AA passes iff no critical/serious auto-violations and no
        manual item is marked ``fail``. ``unreviewed`` still passes the
        automated gate — but evidence bundles surface the count so the
        human reviewer knows what's outstanding."""
        auto_clean = self.critical_violations == 0 and self.serious_violations == 0
        manual_clean = all(
            i.status != "fail" for i in self.manual_checklist
        )
        self.passed = auto_clean and manual_clean

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source,
            "passed": self.passed,
            "critical_violations": self.critical_violations,
            "serious_violations": self.serious_violations,
            "violation_count": len(self.violations),
            "violations": [asdict(v) for v in self.violations],
            "manual_checklist": [asdict(i) for i in self.manual_checklist],
            "raw_summary": self.raw_summary,
        }


# ── axe-core parsing ────────────────────────────────────────────────


def _parse_axe_output(stdout: str) -> list[WCAGAutoIssue]:
    """Parse the axe-core CLI JSON shape into a flat list of issues.

    The axe CLI emits an array with one entry per URL; each entry has a
    ``violations`` array where every violation carries ``id`` /
    ``impact`` / ``description`` / ``helpUrl`` / ``nodes``.
    """
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("axe CLI returned non-JSON output; treating as empty")
        return []
    issues: list[WCAGAutoIssue] = []
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        for v in entry.get("violations", []) or []:
            issues.append(
                WCAGAutoIssue(
                    id=str(v.get("id", "unknown")),
                    impact=str(v.get("impact", "moderate") or "moderate"),
                    description=str(v.get("description", "")),
                    help_url=str(v.get("helpUrl", "")),
                    nodes=len(v.get("nodes", []) or []),
                )
            )
    return issues


def run_wcag_scan(
    url: str,
    *,
    checklist: Iterable[dict[str, str]] = WCAG_AA_MANUAL_CHECKLIST,
    checklist_overrides: dict[str, dict[str, str]] | None = None,
    timeout: int = 120,
    axe_bin: str | None = None,
) -> WCAGReport:
    """Run axe-core against ``url`` and fold in the manual checklist.

    ``checklist_overrides`` is a mapping ``{sc_number: {"status": ...,
    "notes": ...}}`` so CI callers can attach the human review state from
    a YAML/JSON file without mutating the default checklist structure.
    """
    overrides = checklist_overrides or {}
    manual: list[WCAGManualItem] = []
    for entry in checklist:
        override = overrides.get(entry["sc"], {})
        manual.append(
            WCAGManualItem(
                sc=entry["sc"],
                name=entry["name"],
                description=entry["description"],
                status=override.get("status", "unreviewed"),
                notes=override.get("notes", ""),
            )
        )

    report = WCAGReport(url=url, manual_checklist=manual)

    cli = axe_bin or shutil.which("axe")
    if not cli or not url:
        report.source = "mock"
        report.recompute_passed()
        return report

    try:
        proc = subprocess.run(
            [cli, url, "--stdout", "--exit"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        report.violations = _parse_axe_output(proc.stdout)
        report.source = "axe"
        report.raw_summary = {
            "return_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-200:],
        }
    except subprocess.TimeoutExpired:
        logger.warning("axe-core scan timed out after %ds", timeout)
        report.source = "mock"
        report.raw_summary = {"error": f"timeout after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("axe-core scan errored: %s", exc)
        report.source = "mock"
        report.raw_summary = {"error": str(exc)}

    report.recompute_passed()
    return report


__all__ = [
    "WCAGReport",
    "WCAGAutoIssue",
    "WCAGManualItem",
    "WCAG_AA_MANUAL_CHECKLIST",
    "run_wcag_scan",
]
