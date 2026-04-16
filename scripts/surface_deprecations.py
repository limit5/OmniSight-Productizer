#!/usr/bin/env python3
"""N7 — surface deprecation warnings prominently in CI logs.

The N7 multi-version matrix runs the test suite under future Python
(3.13), future Node (22.x), and the latest-minor FastAPI on top of the
pinned baseline. The whole point of the forward-look matrix is to spot
deprecations *now* — months before they bite at upgrade time. But
pytest and vitest both bury deprecation warnings deep in their normal
output where humans skim past them.

This script consumes the captured log file from a matrix job, extracts
every line that mentions a deprecation, and emits two things:

  1. one ``::warning ...`` GitHub Actions annotation per unique
     deprecation, so they show up in the PR/run UI sidebar (the same
     place lint warnings appear) instead of being lost in 5 000 lines
     of pytest noise.
  2. a markdown section appended to ``$GITHUB_STEP_SUMMARY`` (when set)
     with a deduplicated table — count, source, message — so the matrix
     run summary tells the operator at a glance which package families
     are about to deprecate APIs we depend on.

stdlib-only by policy: this script runs as the last step of every
matrix job, so adding a pip dep here would mean the script can be
broken by the very upgrade preview it is summarising — same self-
defense argument as N5's ``upgrade_preview.py`` and N6's
``check_eol.py``.

Usage (CI):

    python3 scripts/surface_deprecations.py \\
        --log _matrix/pytest.log \\
        --kind python \\
        --label "py3.13"

    python3 scripts/surface_deprecations.py \\
        --log _matrix/vitest.log \\
        --kind node \\
        --label "node22"

Exit code is always 0 — deprecations are warnings, not gates. The N7
matrix itself is advisory; only the primary PR pipeline (`ci.yml`)
gates merges. The script is non-destructive even when the log is
missing or empty, so a failed matrix job doesn't double-fail at the
deprecation-surfacing step.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Hard cap so a runaway log full of identical deprecations can't make
# us emit 50 000 ::warning lines (which the GitHub UI silently drops
# past ~10 anyway). The summary table still aggregates the full count.
ANNOTATION_CAP = 30

# pytest emits warnings in either the inline ``-W`` rendered form or
# the per-test summary block at the end. Both share the substring
# "DeprecationWarning". We also catch PendingDeprecationWarning and
# the explicit "deprecated" keyword (lowercase) to cover libraries that
# use FutureWarning or print plain "X is deprecated, use Y".
PYTHON_PATTERNS = (
    re.compile(r"DeprecationWarning"),
    re.compile(r"PendingDeprecationWarning"),
    re.compile(r"FutureWarning"),
)

# vitest / playwright / next surface deprecations through Node's
# `--no-deprecation` style channel: lines like
#   "(node:1234) [DEP0040] DeprecationWarning: The `punycode` module..."
# or terse "(deprecated)" markers next to a require chain. Vitest
# itself prints "warn  Deprecated:" with a leading marker.
NODE_PATTERNS = (
    re.compile(r"DeprecationWarning"),
    re.compile(r"\bDEP0\d{3}\b"),
    re.compile(r"\bdeprecated\b", re.IGNORECASE),
)

# Drop common false positives where the literal word "deprecated"
# appears in test fixtures, snapshot strings, or the command line.
# Tighten this list as new noise surfaces — it's better to surface a
# spurious warning than to suppress a real one. Each entry is a
# substring match (case-insensitive) on the raw line.
NODE_NOISE_SUBSTRINGS = (
    "--no-deprecation",
    "deprecation_policy",     # our own internal symbol
    "deprecate(",             # rxjs operator inside snapshot
)


@dataclass(frozen=True)
class Finding:
    """A single deprecation occurrence extracted from a log line."""

    source: str        # "pytest" or "node"
    message: str       # truncated single-line message used as dedupe key
    line_no: int       # 1-based line number in the source log
    raw: str           # the unmodified log line (kept for debugging)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 240) -> str:
    """Single-line, length-capped form used as the dedupe key.

    GitHub annotation messages are silently truncated past ~400 chars,
    and the dedupe table reads better when long stack frames don't
    visually drown shorter messages from the same package.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "\u2026"


def _matches_any(line: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(p.search(line) for p in patterns)


def _is_node_noise(line: str) -> bool:
    lowered = line.lower()
    return any(noise in lowered for noise in NODE_NOISE_SUBSTRINGS)


def parse_python_log(log: str) -> list[Finding]:
    """Extract deprecation findings from a captured pytest log."""
    out: list[Finding] = []
    for idx, raw in enumerate(log.splitlines(), start=1):
        if not _matches_any(raw, PYTHON_PATTERNS):
            continue
        out.append(
            Finding(
                source="pytest",
                message=_truncate(raw),
                line_no=idx,
                raw=raw,
            )
        )
    return out


def parse_node_log(log: str) -> list[Finding]:
    """Extract deprecation findings from a captured node/vitest log."""
    out: list[Finding] = []
    for idx, raw in enumerate(log.splitlines(), start=1):
        if not _matches_any(raw, NODE_PATTERNS):
            continue
        if _is_node_noise(raw):
            continue
        out.append(
            Finding(
                source="node",
                message=_truncate(raw),
                line_no=idx,
                raw=raw,
            )
        )
    return out


def parse_log(log: str, kind: str) -> list[Finding]:
    if kind == "python":
        return parse_python_log(log)
    if kind == "node":
        return parse_node_log(log)
    raise ValueError(f"unknown kind: {kind!r} (expected 'python' or 'node')")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _gh_escape(value: str) -> str:
    """Escape a value for a GitHub workflow command parameter.

    See: https://docs.github.com/actions/using-workflows/workflow-commands-for-github-actions#example-setting-a-warning-message
    """
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def render_annotations(
    findings: list[Finding],
    log_path: str,
    label: str,
) -> list[str]:
    """One ``::warning`` annotation per *unique* finding (capped)."""
    seen: set[str] = set()
    lines: list[str] = []
    for f in findings:
        if f.message in seen:
            continue
        seen.add(f.message)
        if len(lines) >= ANNOTATION_CAP:
            lines.append(
                f"::warning ::[{label}] {len(findings) - ANNOTATION_CAP} more "
                f"deprecation(s) suppressed; see {log_path} or the step summary."
            )
            break
        lines.append(
            f"::warning file={_gh_escape(log_path)},"
            f"line={f.line_no}::"
            f"[{_gh_escape(label)}] {_gh_escape(f.message)}"
        )
    return lines


def render_summary(
    findings: list[Finding],
    label: str,
    log_path: str,
) -> str:
    """Markdown section appended to GITHUB_STEP_SUMMARY."""
    if not findings:
        return (
            f"### Deprecation warnings — `{label}`\n\n"
            f"No deprecation warnings detected in `{log_path}`. \u2705\n"
        )

    counts = Counter(f.message for f in findings)
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    lines = [
        f"### Deprecation warnings — `{label}`",
        "",
        f"**{len(findings)}** total occurrence(s) across "
        f"**{len(counts)}** unique message(s) in `{log_path}`.",
        "",
        "| Count | Message |",
        "|---:|---|",
    ]
    for msg, count in rows:
        # Markdown tables: escape pipes inside the message cell.
        safe = msg.replace("|", "\\|")
        lines.append(f"| {count} | `{safe}` |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# IO glue
# ---------------------------------------------------------------------------

def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _append_step_summary(body: str) -> None:
    """Write the rendered summary to ``GITHUB_STEP_SUMMARY`` if set."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(body)
        if not body.endswith("\n"):
            fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", required=True, help="captured test-run log file")
    parser.add_argument(
        "--kind",
        required=True,
        choices=("python", "node"),
        help="which test runner produced the log",
    )
    parser.add_argument(
        "--label",
        required=True,
        help="matrix-cell label (e.g. py3.13, node22, fastapi-latest)",
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log)
    log = _read_log(log_path)
    findings = parse_log(log, args.kind)

    for line in render_annotations(findings, str(log_path), args.label):
        # Annotations must go to stdout to be picked up by the runner.
        print(line)

    _append_step_summary(render_summary(findings, args.label, str(log_path)))

    # Always succeed — surfacing is advisory by design.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
