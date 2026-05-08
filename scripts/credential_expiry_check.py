#!/usr/bin/env python3
"""OP-727 — Credential expiry tracker.

Reads the credential inventory at ``configs/credentials.yaml`` and emits
alerts at the 30 / 7 / 1-day-before-expiry ladder. Designed to run daily
from cron / systemd timer; downstream alert dispatch (T1) consumes the
JSON report on stdout or the per-credential lines on stderr.

Behaviour
---------
For each credential entry:

* ``expiry: never``                 → skipped silently (e.g. ed25519 SSH
                                      keys with no policy expiry).
* ``days_until_expiry > 30``        → no alert, listed under ``ok``.
* ``7 < days_until_expiry <= 30``   → ``warning`` tier (30-day band).
* ``1 < days_until_expiry <= 7``    → ``urgent`` tier (7-day band).
* ``0 < days_until_expiry <= 1``    → ``critical`` tier (1-day band).
* ``days_until_expiry <= 0``        → ``expired`` tier (already lapsed).

The bands are inclusive at the upper edge to match the AC: setting
expiry = today + 30d MUST fire an alert (boundary case), expiry = today
+ 31d MUST NOT. Boundaries are intentionally overlapping so a daily cron
that misses one day still re-fires the next day — single-shot
``== 30`` semantics would silently drop alerts on cron skips.

After rotation, the operator (or the rotation script per T7 runbook)
updates ``expiry`` and ``last_rotated_at`` in the inventory; the next
cron run sees ``days_until_expiry > 30`` and the alert clears.

Output
------
``--format text`` (default): human-readable, one line per alert.
``--format json``           : structured report, suitable for piping
                              into the T1 alert dispatcher.

Exit codes (so cron + alert routing can differentiate severity):

  0  — no alerts
  1  — at least one warning-tier alert (30-day band)
  2  — at least one urgent-tier alert (7-day band)
  3  — at least one critical-tier or expired credential
  64 — usage error (bad CLI flags)
  65 — inventory file invalid or unreadable

stdlib + PyYAML only. PyYAML is already a transitive dep of the repo
(see ``scripts/extract_handoff_status.py``).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - defensive
    sys.stderr.write(
        "credential_expiry_check.py requires PyYAML. "
        "Install with `pip install pyyaml`.\n"
    )
    raise SystemExit(65) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = REPO_ROOT / "configs" / "credentials.yaml"
EXAMPLE_INVENTORY = REPO_ROOT / "configs" / "credentials.example.yaml"

VALID_TYPES = frozenset({
    "jira_token",
    "gerrit_http",
    "ssh_key",
    "api_key",
    "encryption_key",
})

REQUIRED_FIELDS = ("id", "type", "expiry", "last_rotated_at", "owner")

# Alert bands. Order matters: most-severe first, most-permissive last.
# An entry crossing multiple bands is reported at the most-severe one.
WARNING_DAYS = 30
URGENT_DAYS = 7
CRITICAL_DAYS = 1

# Exit codes by max severity seen.
EXIT_OK = 0
EXIT_WARNING = 1
EXIT_URGENT = 2
EXIT_CRITICAL = 3
EXIT_USAGE = 64
EXIT_INVENTORY_BAD = 65

SEVERITY_ORDER = {
    "ok": 0,
    "warning": EXIT_WARNING,
    "urgent": EXIT_URGENT,
    "critical": EXIT_CRITICAL,
    "expired": EXIT_CRITICAL,
}


@dataclass
class Credential:
    """Validated inventory entry."""

    id: str
    type: str
    expiry: str  # "never" or YYYY-MM-DD
    last_rotated_at: str  # YYYY-MM-DD
    owner: str
    runbook: str | None = None
    secret_ref: str | None = None
    notes: str | None = None


@dataclass
class Alert:
    """One credential's evaluation result for a given run."""

    id: str
    type: str
    owner: str
    severity: str  # ok | warning | urgent | critical | expired | skipped
    days_until_expiry: int | None  # None for skipped (expiry=never)
    expiry: str
    runbook: str | None = None
    message: str = ""


@dataclass
class Report:
    """Aggregate run output."""

    checked_at: str
    today: str
    inventory_path: str
    alerts: list[Alert] = field(default_factory=list)
    skipped: list[Alert] = field(default_factory=list)
    ok: list[Alert] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Inventory loading + validation
# ─────────────────────────────────────────────────────────────────────


def load_inventory(path: Path) -> list[Credential]:
    """Parse the YAML inventory and return validated entries.

    Raises ``ValueError`` with a human-readable message on any
    schema violation. The caller decides how to surface it.
    """
    if not path.exists():
        raise ValueError(
            f"inventory not found: {path}. "
            f"Copy {EXAMPLE_INVENTORY.name} to {path.name} and fill in entries."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"inventory YAML parse error: {exc}") from exc

    if raw is None:
        raise ValueError(f"inventory is empty: {path}")
    if not isinstance(raw, dict):
        raise ValueError(
            f"inventory root must be a mapping with key 'credentials', "
            f"got {type(raw).__name__}"
        )
    entries = raw.get("credentials")
    if entries is None:
        raise ValueError("inventory is missing top-level 'credentials' key")
    if not isinstance(entries, list):
        raise ValueError(
            f"'credentials' must be a list, got {type(entries).__name__}"
        )

    credentials: list[Credential] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"credential[{idx}] must be a mapping, "
                f"got {type(entry).__name__}"
            )
        for field_name in REQUIRED_FIELDS:
            if field_name not in entry:
                raise ValueError(
                    f"credential[{idx}] missing required field '{field_name}'"
                )
        cred_id = str(entry["id"])
        if cred_id in seen_ids:
            raise ValueError(f"duplicate credential id: {cred_id!r}")
        seen_ids.add(cred_id)

        cred_type = str(entry["type"])
        if cred_type not in VALID_TYPES:
            raise ValueError(
                f"credential[{cred_id!r}]: unknown type {cred_type!r}; "
                f"valid types are {sorted(VALID_TYPES)}"
            )

        expiry = str(entry["expiry"])
        if expiry != "never":
            _parse_iso_date(expiry, where=f"credential[{cred_id!r}].expiry")

        last_rotated_at = str(entry["last_rotated_at"])
        _parse_iso_date(
            last_rotated_at,
            where=f"credential[{cred_id!r}].last_rotated_at",
        )

        credentials.append(
            Credential(
                id=cred_id,
                type=cred_type,
                expiry=expiry,
                last_rotated_at=last_rotated_at,
                owner=str(entry["owner"]),
                runbook=_optional_str(entry.get("runbook")),
                secret_ref=_optional_str(entry.get("secret_ref")),
                notes=_optional_str(entry.get("notes")),
            )
        )
    return credentials


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_iso_date(text: str, *, where: str) -> date:
    """Strict YYYY-MM-DD. Anything else fails loudly."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{where}: expected ISO date (YYYY-MM-DD), got {text!r}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────
# Severity classification
# ─────────────────────────────────────────────────────────────────────


def classify(credential: Credential, today: date) -> Alert:
    """Return the alert tier for a single credential as of ``today``."""
    runbook = credential.runbook
    if credential.expiry == "never":
        return Alert(
            id=credential.id,
            type=credential.type,
            owner=credential.owner,
            severity="skipped",
            days_until_expiry=None,
            expiry="never",
            runbook=runbook,
            message=f"{credential.id}: expiry=never (skipped silently)",
        )

    expiry_date = _parse_iso_date(
        credential.expiry, where=f"credential[{credential.id!r}].expiry"
    )
    days = (expiry_date - today).days

    if days <= 0:
        severity = "expired"
        msg = (
            f"{credential.id} ({credential.type}) EXPIRED "
            f"{abs(days)}d ago on {credential.expiry} — owner {credential.owner}"
        )
    elif days <= CRITICAL_DAYS:
        severity = "critical"
        msg = (
            f"{credential.id} ({credential.type}) expires in {days}d "
            f"on {credential.expiry} — owner {credential.owner}"
        )
    elif days <= URGENT_DAYS:
        severity = "urgent"
        msg = (
            f"{credential.id} ({credential.type}) expires in {days}d "
            f"on {credential.expiry} — owner {credential.owner}"
        )
    elif days <= WARNING_DAYS:
        severity = "warning"
        msg = (
            f"{credential.id} ({credential.type}) expires in {days}d "
            f"on {credential.expiry} — owner {credential.owner}"
        )
    else:
        severity = "ok"
        msg = (
            f"{credential.id} ({credential.type}) ok — "
            f"{days}d until {credential.expiry}"
        )

    return Alert(
        id=credential.id,
        type=credential.type,
        owner=credential.owner,
        severity=severity,
        days_until_expiry=days,
        expiry=credential.expiry,
        runbook=runbook,
        message=msg,
    )


def evaluate(
    credentials: Iterable[Credential],
    *,
    today: date,
    inventory_path: Path,
) -> Report:
    """Build the full report from a list of validated credentials."""
    report = Report(
        checked_at=datetime.now().isoformat(timespec="seconds"),
        today=today.isoformat(),
        inventory_path=str(inventory_path),
    )
    for cred in credentials:
        result = classify(cred, today)
        if result.severity == "skipped":
            report.skipped.append(result)
        elif result.severity == "ok":
            report.ok.append(result)
        else:
            report.alerts.append(result)
    return report


def report_exit_code(report: Report) -> int:
    """Highest-severity exit code seen across the report."""
    code = EXIT_OK
    for alert in report.alerts:
        code = max(code, SEVERITY_ORDER.get(alert.severity, EXIT_OK))
    return code


# ─────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────


def render_text(report: Report) -> str:
    lines: list[str] = []
    lines.append(f"Credential expiry check — {report.today}")
    lines.append(f"Inventory: {report.inventory_path}")
    lines.append("")
    if report.alerts:
        lines.append(f"ALERTS ({len(report.alerts)}):")
        for alert in sorted(
            report.alerts,
            key=lambda a: (
                -SEVERITY_ORDER.get(a.severity, 0),
                a.days_until_expiry if a.days_until_expiry is not None else 0,
            ),
        ):
            lines.append(f"  [{alert.severity.upper()}] {alert.message}")
            if alert.runbook:
                lines.append(f"      runbook: {alert.runbook}")
    else:
        lines.append("ALERTS: none")
    lines.append("")
    if report.skipped:
        lines.append(
            f"Skipped (expiry=never): {len(report.skipped)} — "
            + ", ".join(a.id for a in report.skipped)
        )
    lines.append(f"OK: {len(report.ok)}")
    if report.errors:
        lines.append("")
        lines.append("ERRORS:")
        for err in report.errors:
            lines.append(f"  {err}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    payload = {
        "checked_at": report.checked_at,
        "today": report.today,
        "inventory_path": report.inventory_path,
        "alerts": [asdict(a) for a in report.alerts],
        "skipped": [asdict(a) for a in report.skipped],
        "ok": [asdict(a) for a in report.ok],
        "errors": list(report.errors),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OP-727 credential expiry tracker (30 / 7 / 1-day alert ladder).",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY,
        help=f"Path to the inventory YAML (default: {DEFAULT_INVENTORY}).",
    )
    parser.add_argument(
        "--today",
        type=str,
        default=None,
        help=(
            "Override 'today' as YYYY-MM-DD. Used by tests; "
            "defaults to the current system date."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Validate the inventory schema and exit. Useful in CI to "
            "keep the inventory honest. Returns 0 on success, "
            f"{EXIT_INVENTORY_BAD} on schema violation."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.today is None:
        today = date.today()
    else:
        try:
            today = _parse_iso_date(args.today, where="--today")
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return EXIT_USAGE

    try:
        credentials = load_inventory(args.inventory)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVENTORY_BAD

    if args.validate:
        sys.stdout.write(
            f"OK: {len(credentials)} credential entries valid in "
            f"{args.inventory}\n"
        )
        return EXIT_OK

    report = evaluate(credentials, today=today, inventory_path=args.inventory)
    if args.format == "json":
        sys.stdout.write(render_json(report) + "\n")
    else:
        sys.stdout.write(render_text(report) + "\n")
    return report_exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
