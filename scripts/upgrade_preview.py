#!/usr/bin/env python3
"""N5 — render the nightly Renovate upgrade-preview report.

The companion to ``.github/workflows/upgrade-preview.yml``. The workflow
collects raw artifacts (``pip list --outdated`` JSON, ``pnpm outdated``
JSON, ``pip-compile --upgrade`` diff, ``pnpm update`` diff, pytest log,
playwright log, and step outcomes); this script consumes those files and
produces the markdown body that gets posted as a GitHub issue tagged
``dependency-preview``.

Why a standalone script instead of inline shell:
  * unit-testable — pure functions for parse + render (no GitHub /
    network), so we get coverage without spinning up GH Actions.
  * stdlib-only — no extra ``pip install`` step in the workflow, which
    keeps the preview job fast and immune to "the dep we're trying to
    preview is the one we just broke".
  * single source of truth for what counts as a "suspected breaking"
    upgrade (the heuristic lives in :func:`classify_pip_bump` /
    :func:`classify_pnpm_bump`).

Usage (CI):
    python scripts/upgrade_preview.py \\
        --pip-outdated _upgrade_preview/pip-outdated.json \\
        --pnpm-outdated _upgrade_preview/pnpm-outdated.json \\
        --pip-diff _upgrade_preview/pip.diff \\
        --pnpm-diff _upgrade_preview/pnpm.diff \\
        --pytest-log _upgrade_preview/pytest.log \\
        --playwright-log _upgrade_preview/playwright.log \\
        --pytest-status success \\
        --playwright-status failure \\
        --pip-install-status success \\
        --pnpm-install-status success \\
        --out _upgrade_preview/issue-body.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# GitHub issue body has a 65 536-byte hard cap. We leave ~10 KiB of
# headroom for the prose around the diffs/logs and split the rest.
ISSUE_BODY_MAX = 60_000
DIFF_LINES_CAP = 200       # how many diff lines to inline per section
LOG_LINES_CAP = 80         # tail lines from pytest / playwright logs
OUTDATED_TABLE_CAP = 60    # max rows in the outdated tables (full list in artifact)

# Package families we always flag as "watch closely" because the project
# pins peer-coupled versions and a single breaking change is enough to
# take production down. Add to this list as new strategic packages land.
WATCHLIST_PIP = (
    "langchain",
    "langgraph",
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "alembic",
)
WATCHLIST_NPM = (
    "next",
    "react",
    "react-dom",
    "@radix-ui",
    "@ai-sdk",
    "ai",
    "playwright",
    "vitest",
    "msw",
    "openapi-typescript",
)


# ─────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class OutdatedItem:
    name: str
    current: str
    latest: str
    bump: str  # "major" | "minor" | "patch" | "unknown"
    breaking: bool
    extra: str = ""  # rendered into the trailing column (manager-specific)


@dataclass
class Report:
    pip_outdated: list[OutdatedItem] = field(default_factory=list)
    npm_outdated: list[OutdatedItem] = field(default_factory=list)
    pip_diff: str = ""
    npm_diff: str = ""
    pytest_log: str = ""
    playwright_log: str = ""
    pytest_status: str = "skipped"
    playwright_status: str = "skipped"
    pip_install_status: str = "skipped"
    npm_install_status: str = "skipped"
    run_url: str = ""

    @property
    def breaking(self) -> list[OutdatedItem]:
        return [i for i in (self.pip_outdated + self.npm_outdated) if i.breaking]


# ─────────────────────────────────────────────────────────────────────────
# Version classification
# ─────────────────────────────────────────────────────────────────────────


_VER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _split_version(v: str) -> tuple[int, int, int] | None:
    """Best-effort parse of ``X.Y.Z`` prefixes; returns ``None`` if we
    cannot extract a leading integer (e.g. git URLs, ``latest`` tags)."""
    m = _VER_RE.match(v.strip().lstrip("vV"))
    if not m:
        return None
    parts = [int(g) if g is not None else 0 for g in m.groups()]
    return parts[0], parts[1], parts[2]


def classify_bump(current: str, latest: str) -> tuple[str, bool]:
    """Return ``(bump_kind, breaking)``.

    Heuristic:
      * ``major`` when the leading integer differs.
      * For ``0.x`` series we treat any minor bump as breaking — this is
        the SemVer convention used by most pre-1.0 libraries (langchain
        included) and the source of most surprise breakages.
      * ``patch`` is never breaking.
      * Unparsable versions fall through as ``unknown`` + breaking=True
        so they get human eyeballs (better safe than silent).
    """
    cur = _split_version(current)
    new = _split_version(latest)
    if cur is None or new is None:
        return "unknown", True
    if new == cur:
        return "patch", False
    if new[0] != cur[0]:
        return "major", True
    if cur[0] == 0 and new[1] != cur[1]:
        # 0.x: minor bump is treated as breaking under SemVer.
        return "minor", True
    if new[1] != cur[1]:
        return "minor", False
    return "patch", False


def _is_watchlisted(name: str, watchlist: Iterable[str]) -> bool:
    n = name.lower()
    return any(n == w or n.startswith(w + "-") or n.startswith(w + "/") or n.startswith(w) for w in watchlist)


# ─────────────────────────────────────────────────────────────────────────
# Parsers — pip / pnpm outdated JSON
# ─────────────────────────────────────────────────────────────────────────


def parse_pip_outdated(json_text: str) -> list[OutdatedItem]:
    """Parse the output of ``pip list --outdated --format=json``.

    Each entry is ``{"name", "version", "latest_version", "latest_filetype"}``.
    Empty / malformed input returns ``[]`` rather than raising — the
    nightly job is best-effort and a parser blow-up shouldn't lose the
    whole report.
    """
    if not json_text.strip():
        return []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return []
    items: list[OutdatedItem] = []
    for entry in data:
        name = str(entry.get("name", "")).strip()
        cur = str(entry.get("version", "")).strip()
        new = str(entry.get("latest_version", "")).strip()
        if not name or not cur or not new:
            continue
        bump, breaking = classify_bump(cur, new)
        breaking = breaking or _is_watchlisted(name, WATCHLIST_PIP)
        items.append(OutdatedItem(
            name=name,
            current=cur,
            latest=new,
            bump=bump,
            breaking=breaking,
            extra=str(entry.get("latest_filetype", "")),
        ))
    items.sort(key=lambda i: (not i.breaking, i.name.lower()))
    return items


def parse_pnpm_outdated(json_text: str) -> list[OutdatedItem]:
    """Parse ``pnpm outdated --json``.

    Two shapes show up in the wild:

      * top-level object keyed by package name (the pnpm default)
      * an object whose ``packages`` key holds the table (pnpm 9 with
        ``--long``)

    Each value contains ``current`` / ``wanted`` / ``latest`` /
    ``dependencyType``. We render ``latest`` (not ``wanted``) because the
    preview is asking *"what would Renovate try?"* and Renovate by
    default tracks ``latest``.
    """
    if not json_text.strip():
        return []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict) and "packages" in data and isinstance(data["packages"], dict):
        data = data["packages"]
    if not isinstance(data, dict):
        return []
    items: list[OutdatedItem] = []
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        cur = str(entry.get("current", "")).strip()
        new = str(entry.get("latest", "")).strip()
        if not cur or not new:
            continue
        bump, breaking = classify_bump(cur, new)
        breaking = breaking or _is_watchlisted(name, WATCHLIST_NPM)
        items.append(OutdatedItem(
            name=name,
            current=cur,
            latest=new,
            bump=bump,
            breaking=breaking,
            extra=str(entry.get("dependencyType", "")),
        ))
    items.sort(key=lambda i: (not i.breaking, i.name.lower()))
    return items


# ─────────────────────────────────────────────────────────────────────────
# Renderers
# ─────────────────────────────────────────────────────────────────────────


def _status_emoji(status: str) -> str:
    s = (status or "").lower()
    return {
        "success": "✅",
        "failure": "❌",
        "skipped": "⚪",
        "cancelled": "⚠️",
    }.get(s, "❔")


def _tail(text: str, lines: int) -> str:
    if not text:
        return "(empty)"
    parts = text.splitlines()
    if len(parts) <= lines:
        return text.rstrip()
    return "\n".join(parts[-lines:]).rstrip()


def _truncate_diff(text: str, lines: int) -> tuple[str, int]:
    """Return ``(truncated_text, dropped_count)``."""
    if not text:
        return "(no diff — no upgrade available)", 0
    parts = text.splitlines()
    if len(parts) <= lines:
        return text.rstrip(), 0
    head = "\n".join(parts[:lines]).rstrip()
    return head, len(parts) - lines


def _outdated_table(items: list[OutdatedItem], cap: int, header_extra: str) -> str:
    if not items:
        return "_No outdated packages — lockfile already on the newest pinned versions._"
    rows = ["| Pkg | Current | Latest | Bump | " + header_extra + " |",
            "|---|---|---|---|---|"]
    for item in items[:cap]:
        flag = "🔥 " if item.breaking else ""
        rows.append(
            f"| {flag}`{item.name}` | `{item.current}` | `{item.latest}` "
            f"| {item.bump} | {item.extra} |"
        )
    suffix = ""
    if len(items) > cap:
        suffix = f"\n_… {len(items) - cap} more rows truncated — see workflow artifact._"
    return "\n".join(rows) + suffix


def _breaking_section(items: list[OutdatedItem]) -> str:
    if not items:
        return "_None detected._"
    return "\n".join(
        f"- 🔥 **{it.name}**: `{it.current}` → `{it.latest}` ({it.bump})"
        for it in items
    )


def render_issue_body(report: Report, *, run_id: str = "", date_str: str = "") -> str:
    """Compose the full markdown issue body.

    Caller is responsible for keeping the body under ``ISSUE_BODY_MAX``;
    if the result is over-budget we additionally drop the diffs and
    point readers to the workflow artifact (which already contains them
    verbatim).
    """
    breaking = report.breaking
    summary_rows = [
        ("pip install (upgraded)", report.pip_install_status),
        ("pnpm install (upgraded)", report.npm_install_status),
        ("backend pytest (upgraded)", report.pytest_status),
        ("playwright chromium (upgraded)", report.playwright_status),
    ]
    summary_table = "| Step | Outcome |\n|---|---|\n" + "\n".join(
        f"| {label} | {_status_emoji(status)} {status or 'n/a'} |"
        for label, status in summary_rows
    )

    pip_diff_text, pip_dropped = _truncate_diff(report.pip_diff, DIFF_LINES_CAP)
    npm_diff_text, npm_dropped = _truncate_diff(report.npm_diff, DIFF_LINES_CAP)
    pytest_tail = _tail(report.pytest_log, LOG_LINES_CAP)
    playwright_tail = _tail(report.playwright_log, LOG_LINES_CAP)

    header = (
        "# Nightly Dependency Upgrade Preview"
        + (f" — {date_str}" if date_str else "")
        + "\n\n"
        + "_N5 (`docs/ops/upgrade_preview.md`) — informational. "
        "Tests run with **upgraded** dependencies to forecast what the next weekend Renovate batch will break._\n"
    )
    if run_id:
        header += f"\nWorkflow run: `{run_id}`"
        if report.run_url:
            header += f" — {report.run_url}"
        header += "\n"

    parts = [
        header,
        "## Summary\n\n" + summary_table + "\n",
        f"## Suspected breaking ({len(breaking)})\n\n"
        + _breaking_section(breaking) + "\n",
        f"## pip outdated ({len(report.pip_outdated)})\n\n"
        + _outdated_table(report.pip_outdated, OUTDATED_TABLE_CAP, "Wheel") + "\n",
        f"## pnpm outdated ({len(report.npm_outdated)})\n\n"
        + _outdated_table(report.npm_outdated, OUTDATED_TABLE_CAP, "Scope") + "\n",
        "## `pip-compile --upgrade` diff" + (
            f" _(truncated — {pip_dropped} more lines in artifact)_" if pip_dropped else ""
        ) + "\n\n```diff\n" + pip_diff_text + "\n```\n",
        "## `pnpm update` diff" + (
            f" _(truncated — {npm_dropped} more lines in artifact)_" if npm_dropped else ""
        ) + "\n\n```diff\n" + npm_diff_text + "\n```\n",
        f"## pytest tail (last {LOG_LINES_CAP} lines)\n\n```\n" + pytest_tail + "\n```\n",
        f"## playwright tail (last {LOG_LINES_CAP} lines)\n\n```\n" + playwright_tail + "\n```\n",
        "---\n\n"
        "_Triage: see `docs/ops/upgrade_preview.md`. Renovate batches "
        "land on weekends per N2 (`docs/ops/renovate_policy.md`)._",
    ]

    body = "\n".join(parts)
    if len(body.encode("utf-8")) <= ISSUE_BODY_MAX:
        return body

    # Over budget — drop the diffs (the artifact carries them verbatim).
    parts[5] = (
        "## `pip-compile --upgrade` diff\n\n"
        "_Diff omitted (issue size budget). See workflow artifact for full output._\n"
    )
    parts[6] = (
        "## `pnpm update` diff\n\n"
        "_Diff omitted (issue size budget). See workflow artifact for full output._\n"
    )
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────


def _read(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_report_from_args(args: argparse.Namespace) -> Report:
    return Report(
        pip_outdated=parse_pip_outdated(_read(args.pip_outdated)),
        npm_outdated=parse_pnpm_outdated(_read(args.pnpm_outdated)),
        pip_diff=_read(args.pip_diff),
        npm_diff=_read(args.pnpm_diff),
        pytest_log=_read(args.pytest_log),
        playwright_log=_read(args.playwright_log),
        pytest_status=args.pytest_status or "skipped",
        playwright_status=args.playwright_status or "skipped",
        pip_install_status=args.pip_install_status or "skipped",
        npm_install_status=args.pnpm_install_status or "skipped",
        run_url=args.run_url or "",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pip-outdated", help="Path to pip-outdated JSON")
    ap.add_argument("--pnpm-outdated", help="Path to pnpm-outdated JSON")
    ap.add_argument("--pip-diff", help="Path to pip-compile --upgrade diff")
    ap.add_argument("--pnpm-diff", help="Path to pnpm update lockfile diff")
    ap.add_argument("--pytest-log", help="Path to pytest stdout/stderr log")
    ap.add_argument("--playwright-log", help="Path to playwright stdout/stderr log")
    ap.add_argument("--pytest-status", default="skipped",
                    help="Outcome of the pytest step (success/failure/skipped/cancelled)")
    ap.add_argument("--playwright-status", default="skipped")
    ap.add_argument("--pip-install-status", default="skipped")
    ap.add_argument("--pnpm-install-status", default="skipped")
    ap.add_argument("--run-url", default="",
                    help="Workflow run URL (rendered as a link in the issue body)")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    ap.add_argument("--date", default="",
                    help="Date string for the issue title/header (default: today UTC)")
    ap.add_argument("--out", required=True, help="Where to write the issue body markdown")
    args = ap.parse_args(argv)

    report = build_report_from_args(args)
    date_str = args.date or _today_utc()
    body = render_issue_body(report, run_id=args.run_id, date_str=date_str)
    Path(args.out).write_text(body, encoding="utf-8")
    print(f"[upgrade_preview] wrote {args.out} ({len(body):,} chars, "
          f"{len(report.pip_outdated)} pip + {len(report.npm_outdated)} npm outdated, "
          f"{len(report.breaking)} suspected breaking)")
    return 0


def _today_utc() -> str:
    # Imported lazily so unit tests can stub the value via --date.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sys.exit(main())
