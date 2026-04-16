#!/usr/bin/env python3
"""N6 — EOL (end-of-life) early warning for strategic dependencies.

Queries `https://endoflife.date/api/<product>.json` once per product,
looks up the version currently pinned in this repo, and emits a
warning whenever support ends within the configured horizon (default:
6 months).

Products tracked:
  * Python       — pinned via CI matrix + Dockerfile.backend base image
  * Node.js      — pinned via .nvmrc / .node-version
  * FastAPI      — pinned via backend/requirements.in
  * Next.js      — pinned via package.json

stdlib-only policy: this script MUST remain free of third-party
imports. Two reasons:

  1. N6 runs on a monthly cron — adding a pip dep to the workflow
     would mean one more thing that can break and silently disable
     the EOL warning (the same self-defense argument as N5's
     `upgrade_preview.py`).
  2. endoflife.date is a simple JSON API; `urllib.request` covers it
     trivially, with no auth or rate limiting in the free tier.

Usage:
    python3 scripts/check_eol.py \\
        --out eol-issue-body.md \\
        --warn-days 180

Exit code is always 0 unless the version-extraction layer fails
(which means the repo state is inconsistent — loud failure is the
right thing there). Whether the workflow opens an issue is driven by
the ``has_warnings`` line on `$GITHUB_OUTPUT`, not the exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 10-second budget per request. The endoflife.date API is CDN-backed
# and usually responds in <500ms; anything longer is probably a
# transient network issue, and we'd rather report "API unreachable"
# than hang the monthly cron.
HTTP_TIMEOUT = 10
USER_AGENT = "OmniSight-N6-EOLChecker/1.0 (+https://endoflife.date)"

# Default warning horizon — 6 months matches the N6 spec and gives
# enough lead time for a blue-green major upgrade (≥1 month prep +
# staging soak per the runbook).
DEFAULT_WARN_DAYS = 180

# GitHub issue body hard cap (same reasoning as other N* scripts).
ISSUE_BODY_MAX = 60_000


@dataclass
class Product:
    """A tracked product plus how to discover the pinned version."""

    name: str                # display name, e.g. "Python"
    api_slug: str            # endoflife.date slug, e.g. "python"
    current_version: str     # what's pinned in this repo, e.g. "3.12"
    pin_source: str          # where the pin lives, for the operator

    def cycle_key(self) -> str:
        """API `cycle` key to match. endoflife.date uses X.Y.

        endoflife.date indexes by major.minor cycle (e.g. ``3.12``),
        not full patch versions (not ``3.12.7``). Callers need to
        trim trailing ``.patch`` before comparing.
        """
        parts = self.current_version.split(".")
        if len(parts) >= 2:
            return ".".join(parts[:2])
        return self.current_version


@dataclass
class Warning:
    """One EOL event that falls inside the warning horizon."""

    product: str
    current: str
    cycle: str
    eol_date: str
    days_remaining: int
    pin_source: str
    latest_in_cycle: str | None = None
    latest_overall: str | None = None


@dataclass
class Report:
    """Aggregate report for all tracked products."""

    checked_at: str
    horizon_days: int
    warnings: list[Warning] = field(default_factory=list)
    ok: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Version discovery — one function per product, testable in isolation.
# ---------------------------------------------------------------------------


def read_python_version(root: Path) -> str:
    """Read the pinned Python version.

    Source of truth order:
    1. CI workflow `python-version` — what gets run in tests.
    2. `Dockerfile.backend` `FROM python:X.Y` — what ships to prod.

    These must match; if they don't we return the CI value (tests
    catch the mismatch in `test_dependency_governance.py`).
    """
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r'python-version:\s*"([0-9]+\.[0-9]+)"', ci)
    if match:
        return match.group(1)
    dockerfile = (root / "Dockerfile.backend").read_text(encoding="utf-8")
    match = re.search(r"FROM\s+python:([0-9]+\.[0-9]+)", dockerfile)
    if match:
        return match.group(1)
    raise RuntimeError(
        "Could not determine pinned Python version from ci.yml or "
        "Dockerfile.backend — check_eol is out of sync with the repo"
    )


def read_node_version(root: Path) -> str:
    """Read the pinned Node major.minor from .nvmrc."""
    raw = (root / ".nvmrc").read_text(encoding="utf-8").strip()
    # .nvmrc is a full semver like "20.17.0"; endoflife.date indexes
    # by major for nodejs (20 / 22), so we keep just the leading int.
    parts = raw.split(".")
    if not parts or not parts[0].isdigit():
        raise RuntimeError(f".nvmrc has unexpected shape: {raw!r}")
    return parts[0]


def read_fastapi_version(root: Path) -> str:
    """Read the pinned FastAPI major.minor from backend/requirements.in."""
    text = (root / "backend" / "requirements.in").read_text(encoding="utf-8")
    match = re.search(r"^\s*fastapi==([0-9]+\.[0-9]+)", text, re.MULTILINE)
    if not match:
        raise RuntimeError(
            "Could not locate `fastapi==X.Y` pin in backend/requirements.in"
        )
    return match.group(1)


def read_nextjs_version(root: Path) -> str:
    """Read the pinned Next.js major from package.json."""
    pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    dep = pkg.get("dependencies", {}).get("next")
    if not dep:
        raise RuntimeError("Could not locate `next` in package.json dependencies")
    match = re.match(r"^[~^]?([0-9]+)", str(dep))
    if not match:
        raise RuntimeError(f"Unexpected next version pin: {dep!r}")
    # Next.js endoflife.date cycles are keyed by major (e.g. "16"),
    # consistent with how releases are cut.
    return match.group(1)


# ---------------------------------------------------------------------------
# HTTP fetch layer — stdlib only, one function to stub in tests.
# ---------------------------------------------------------------------------


def fetch_cycles(api_slug: str, *, timeout: int = HTTP_TIMEOUT) -> list[dict[str, Any]]:
    """Fetch the cycle list for a product from endoflife.date.

    Wrapped in a thin function so tests can monkeypatch it without
    standing up an HTTP server.
    """
    url = f"https://endoflife.date/api/{api_slug}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected endoflife.date response shape for {api_slug!r}: "
            f"{type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# Core logic — takes an injectable `fetch` callable for testability.
# ---------------------------------------------------------------------------


def evaluate_products(
    products: list[Product],
    *,
    today: date,
    warn_days: int,
    fetch=fetch_cycles,
) -> Report:
    """Check every product and produce a combined report."""
    report = Report(
        checked_at=today.isoformat(),
        horizon_days=warn_days,
    )
    for product in products:
        try:
            cycles = fetch(product.api_slug)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # endoflife.date does not track this product — not an
                # error on our side, but operators should know the
                # entry is not automatically monitored (e.g. FastAPI
                # has no endoflife.date cycle feed as of 2026).
                report.errors.append(
                    f"{product.name}: not tracked on endoflife.date "
                    f"(slug={product.api_slug!r}, 404) — monitor manually"
                )
            else:
                report.errors.append(
                    f"{product.name}: endoflife.date HTTP {exc.code} ({exc.reason})"
                )
            continue
        except (urllib.error.URLError, TimeoutError) as exc:
            report.errors.append(
                f"{product.name}: endoflife.date unreachable ({exc})"
            )
            continue
        except Exception as exc:  # noqa: BLE001 — we want to capture every path
            report.errors.append(f"{product.name}: {exc}")
            continue

        cycle_key = product.cycle_key()
        matched = _find_cycle(cycles, cycle_key)
        if matched is None:
            report.errors.append(
                f"{product.name}: cycle {cycle_key!r} not found in endoflife.date feed"
            )
            continue

        eol_raw = matched.get("eol")
        eol_date = _parse_eol(eol_raw)
        # Determine latest patch on this cycle and latest across product.
        latest_in_cycle = matched.get("latest")
        latest_overall = None
        for c in cycles:
            if c.get("latest") and not latest_overall:
                latest_overall = c.get("latest")
                break

        if eol_date is None:
            # `eol: false` means "no scheduled EOL" — treat as safe.
            report.ok.append(
                {
                    "product": product.name,
                    "cycle": cycle_key,
                    "current": product.current_version,
                    "eol_date": "no scheduled EOL",
                    "days_remaining": None,
                    "latest_in_cycle": latest_in_cycle,
                    "latest_overall": latest_overall,
                }
            )
            continue

        delta = (eol_date - today).days
        if delta <= warn_days:
            report.warnings.append(
                Warning(
                    product=product.name,
                    current=product.current_version,
                    cycle=cycle_key,
                    eol_date=eol_date.isoformat(),
                    days_remaining=delta,
                    pin_source=product.pin_source,
                    latest_in_cycle=latest_in_cycle,
                    latest_overall=latest_overall,
                )
            )
        else:
            report.ok.append(
                {
                    "product": product.name,
                    "cycle": cycle_key,
                    "current": product.current_version,
                    "eol_date": eol_date.isoformat(),
                    "days_remaining": delta,
                    "latest_in_cycle": latest_in_cycle,
                    "latest_overall": latest_overall,
                }
            )
    return report


def _find_cycle(cycles: list[dict[str, Any]], cycle_key: str) -> dict | None:
    """Find the cycle entry matching `cycle_key` (e.g. '3.12' or '20')."""
    for entry in cycles:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("cycle", "")).strip() == cycle_key:
            return entry
    return None


def _parse_eol(raw: Any) -> date | None:
    """Parse the endoflife.date `eol` field.

    The field may be:
      * a date string "YYYY-MM-DD"
      * the boolean `false` (no scheduled EOL)
      * the boolean `true` (already EOL — we report remaining = 0)
      * missing entirely

    We return `None` for "no scheduled EOL" and a `date` otherwise.
    """
    if raw is False or raw is None or raw == "":
        return None
    if raw is True:
        # Already EOL — represent as today to force the warning path.
        return date.today()
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Rendering — markdown issue body.
# ---------------------------------------------------------------------------


def render_issue_body(report: Report) -> str:
    """Render the monthly EOL issue body (markdown)."""
    lines: list[str] = []
    lines.append(f"# Dependency EOL Check — {report.checked_at}")
    lines.append("")
    lines.append(
        f"Scanned with a **{report.horizon_days}-day** warning horizon "
        "against the [endoflife.date](https://endoflife.date) feed."
    )
    lines.append("")
    lines.append(
        f"- ⚠️ Warnings: **{len(report.warnings)}**\n"
        f"- ✅ OK: **{len(report.ok)}**\n"
        f"- ❌ Errors: **{len(report.errors)}**"
    )
    lines.append("")

    if report.warnings:
        lines.append("## ⚠️ Warnings — action required")
        lines.append("")
        lines.append(
            "| Product | Current | Cycle | EOL date | Days remaining | Pin source |"
        )
        lines.append("|---|---|---|---|---|---|")
        for w in sorted(report.warnings, key=lambda x: x.days_remaining):
            lines.append(
                f"| {w.product} | `{w.current}` | {w.cycle} | {w.eol_date} "
                f"| **{w.days_remaining}** | `{w.pin_source}` |"
            )
        lines.append("")
        lines.append("### Suggested next steps")
        lines.append("")
        for w in sorted(report.warnings, key=lambda x: x.days_remaining):
            if w.days_remaining <= 30:
                urgency = "🚨 **URGENT** (≤30 days)"
            elif w.days_remaining <= 90:
                urgency = "⏰ schedule within the next sprint"
            else:
                urgency = "📅 schedule within the quarter"
            latest = (
                f"latest in cycle: `{w.latest_in_cycle}` · "
                f"latest overall: `{w.latest_overall or '—'}`"
            )
            lines.append(
                f"- **{w.product}** — {urgency}. {latest}. "
                f"Follow [`docs/ops/dependency_upgrade_runbook.md`]"
                "(../docs/ops/dependency_upgrade_runbook.md) for the upgrade + "
                "rollback procedure."
            )
        lines.append("")

    if report.ok:
        lines.append("## ✅ OK — outside warning horizon")
        lines.append("")
        lines.append("| Product | Current | Cycle | EOL date | Days remaining |")
        lines.append("|---|---|---|---|---|")
        for e in sorted(report.ok, key=lambda x: x["product"]):
            remaining = (
                str(e["days_remaining"])
                if e["days_remaining"] is not None
                else "—"
            )
            lines.append(
                f"| {e['product']} | `{e['current']}` | {e['cycle']} "
                f"| {e['eol_date']} | {remaining} |"
            )
        lines.append("")

    if report.errors:
        lines.append("## ❌ Errors")
        lines.append("")
        for err in report.errors:
            lines.append(f"- {err}")
        lines.append("")
        lines.append(
            "_Errors do not by themselves trigger an issue opening; "
            "see `scripts/check_eol.py` for the list of tracked "
            "products and their pin sources._"
        )

    lines.append("---")
    lines.append(
        "_Generated by `scripts/check_eol.py` (N6) from the "
        "`.github/workflows/eol-check.yml` monthly job._"
    )
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > ISSUE_BODY_MAX:
        # Realistically never hits; cap is defensive.
        body = body[:ISSUE_BODY_MAX]
    return body


# ---------------------------------------------------------------------------
# CLI entrypoint.
# ---------------------------------------------------------------------------


def build_products(root: Path) -> list[Product]:
    """Assemble the list of tracked products from the current repo."""
    py = read_python_version(root)
    node = read_node_version(root)
    fastapi = read_fastapi_version(root)
    nextjs = read_nextjs_version(root)
    return [
        Product(
            name="Python",
            api_slug="python",
            current_version=py,
            pin_source=".github/workflows/ci.yml :: python-version",
        ),
        Product(
            name="Node.js",
            api_slug="nodejs",
            current_version=node,
            pin_source=".nvmrc",
        ),
        Product(
            name="FastAPI",
            api_slug="fastapi",
            current_version=fastapi,
            pin_source="backend/requirements.in",
        ),
        Product(
            name="Next.js",
            api_slug="nextjs",
            current_version=nextjs,
            pin_source="package.json :: dependencies.next",
        ),
    ]


def emit_github_output(has_warnings: bool, errors: int) -> None:
    """Write outputs for the workflow's ``steps.<id>.outputs`` map."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"has_warnings={'true' if has_warnings else 'false'}\n")
        fh.write(f"error_count={errors}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repo root (default: auto-detected from script location)",
    )
    args = parser.parse_args(argv)

    try:
        products = build_products(args.root)
    except Exception as exc:  # noqa: BLE001
        print(f"::error::Failed to discover pinned versions: {exc}", file=sys.stderr)
        return 2

    report = evaluate_products(
        products,
        today=date.today(),
        warn_days=args.warn_days,
    )
    body = render_issue_body(report)
    args.out.write_text(body, encoding="utf-8")
    emit_github_output(
        has_warnings=bool(report.warnings),
        errors=len(report.errors),
    )
    print(
        f"EOL check: {len(report.warnings)} warning(s), "
        f"{len(report.ok)} ok, {len(report.errors)} error(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
