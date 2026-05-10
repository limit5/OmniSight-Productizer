#!/usr/bin/env python3
"""OP-822 — SDK runner failure-pattern detector.

After 50+ tickets have shipped through the auto-runner-sdk / s1-launcher
fleet, repeating failure modes start to emerge: "tickets touching
``backend/security/*`` fail 80% of the time at ``max_iterations``",
"tier:X tickets always blow ``max_tokens``", and so on. Operators have
been finding these by hand-grepping JSONL run logs. This audit replaces
that by clustering failures along four dimensions — ``stop_reason``,
``file paths``, ``tier``, ``ticket area`` — and emitting a markdown
report with the top 3 patterns plus suggested mitigations.

Inputs
------
JSONL run logs written by:

  * ``scripts/run_s1_via_anthropic_sdk.py`` → ``data/sdk-launcher/run-*.log``
    (one ``TicketOutcome`` row per ticket: ticket_key, status, cost_usd,
    iterations, error, gerrit_url, ...)

  * Any other process emitting compatible JSONL with the union schema
    documented on ``RunLogEntry`` below — e.g. an ops-side log that
    augments the launcher rows with ``stop_reason``, ``tier``, ``area``,
    or ``files_touched`` fields.

Cost data is sourced from the ``cost_usd`` field on each row (which the
launcher records via CostGuard). No separate CostGuard query needed —
the launcher persists the post-call cost at row-write time, so the run
log IS the CostGuard view, joined to ticket metadata.

Algorithm
---------
1. Load all rows from ``--log-dir`` (recursively, default
   ``data/sdk-launcher`` + ``data/runner``).
2. Mark each row as ``failure`` if ``status not in {ok, skipped_existing_ps}``
   OR (``stop_reason in NON_RETRYABLE_STOP_REASONS``).
3. Generate candidate cluster signatures:

     * single-dim:  ``stop_reason=X``, ``tier=X``, ``area=X``,
                    ``agent_class=X``, ``status=X``
     * path-prefix: ``file_prefix=p`` for every 1-, 2-, 3-segment
                    prefix observed in ``files_touched``
     * 2-dim cross: ``(area, stop_reason)`` and ``(tier, stop_reason)``

4. For each signature compute (support, failure_count, failure_rate).
   Keep only signatures where support ≥ ``MIN_SUPPORT`` (default 5)
   AND failure_rate ≥ ``MIN_FAILURE_RATE`` (default 0.50).
5. Score by ``failure_count × failure_rate × specificity``. Specificity
   prefers more-specific signatures (3-segment prefix > 1-segment;
   2-dim cross > single-dim) so we surface the actionable cluster, not
   a generic restatement of the overall failure rate.
6. Greedy de-dup: walk the ranked list, drop a candidate if it shares
   ≥80% of failing rows with an already-picked higher-ranked pattern.
7. Take top 3. Attach a mitigation suggestion via heuristics on the
   signature shape (max_iterations → claude class, max_tokens → Plan
   sub-agent, tier:X → decompose, security area → human review, ...).

Output
------
Markdown report to ``--output`` (default
``/var/log/omnisight/sdk-failure-audit-<YYYY-MM-DD>.md``). Stdout if
``--stdout``. The systemd companion fires this weekly; format is human-
readable so an oncall can scan it at standup.

CLI
---
::
    sdk_failure_pattern_audit.py
    sdk_failure_pattern_audit.py --log-dir data/sdk-launcher
    sdk_failure_pattern_audit.py --window-days 14 --top 5
    sdk_failure_pattern_audit.py --stdout --min-support 3
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_LOG_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "data" / "sdk-launcher",
    REPO_ROOT / "data" / "runner",
)
DEFAULT_OUTPUT_DIR = Path("/var/log/omnisight")

# Stop reasons that auto-runner-sdk classifies as structural (W14.5 lesson).
NON_RETRYABLE_STOP_REASONS: frozenset[str] = frozenset({
    "max_tokens", "max_iterations_exceeded",
})
# Outcome statuses that count as success / non-failure.
SUCCESS_STATUSES: frozenset[str] = frozenset({"ok", "skipped_existing_ps"})

# Candidate filtering thresholds.
DEFAULT_MIN_SUPPORT = 5
DEFAULT_MIN_FAILURE_RATE = 0.50
DEFAULT_TOP_N = 3
# Drop a candidate if ≥ this share of its failing tickets are already
# explained by previously-picked patterns. 0.50 lets the second slot
# surface a novel cluster as soon as half of its failures are new (i.e.
# the existing slots only cover the other half).
DEFAULT_DEDUP_OVERLAP = 0.50
PATH_PREFIX_DEPTHS: tuple[int, ...] = (1, 2, 3)


@dataclass(frozen=True)
class RunLogEntry:
    """One row from a JSONL run log, normalised."""

    ticket_key: str
    status: str
    stop_reason: str | None = None
    iterations: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    tier: str | None = None
    area: tuple[str, ...] = ()
    files_touched: tuple[str, ...] = ()
    agent_class: str | None = None
    finished_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_failure(self) -> bool:
        if self.status not in SUCCESS_STATUSES:
            return True
        if self.stop_reason in NON_RETRYABLE_STOP_REASONS:
            return True
        return False


@dataclass(frozen=True)
class Pattern:
    """One detected failure pattern."""

    signature: tuple[tuple[str, str], ...]  # frozenset-shaped k=v pairs
    support: int
    failure_count: int
    failure_rate: float
    specificity: int
    score: float
    mitigation: str
    matching_tickets: tuple[str, ...]

    @property
    def signature_str(self) -> str:
        return " AND ".join(f"{k}={v}" for k, v in self.signature)


# ─── Loaders ─────────────────────────────────────────────────────────


def _coerce_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(p.strip() for p in value.split(",") if p.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None and str(v).strip())
    return ()


def parse_entry(raw: dict[str, Any]) -> RunLogEntry | None:
    """Normalise one JSONL row. Returns None for rows missing ticket_key."""
    ticket_key = raw.get("ticket_key") or raw.get("ticket") or raw.get("key")
    if not ticket_key:
        return None
    return RunLogEntry(
        ticket_key=str(ticket_key),
        status=str(raw.get("status", "unknown")),
        stop_reason=raw.get("stop_reason") or _stop_reason_from_error(raw.get("error")),
        iterations=int(raw.get("iterations") or 0),
        cost_usd=float(raw.get("cost_usd") or 0.0),
        error=raw.get("error"),
        tier=raw.get("tier"),
        area=_coerce_list(raw.get("area") or raw.get("areas")),
        files_touched=_coerce_list(raw.get("files_touched") or raw.get("files")),
        agent_class=raw.get("agent_class"),
        finished_at=raw.get("finished_at"),
        raw=dict(raw),
    )


def _stop_reason_from_error(error: Any) -> str | None:
    """Best-effort: pull a stop_reason hint out of a free-text ``error``.

    The launcher's TicketOutcome.error embeds phrases like
    ``stop=max_iterations_exceeded`` / ``per-ticket cap ... exceeded``.
    A second-pass heuristic so we still cluster usefully on legacy logs
    that pre-date the explicit ``stop_reason`` field."""
    if not isinstance(error, str):
        return None
    lowered = error.lower()
    if "max_iterations_exceeded" in lowered or "max iterations" in lowered:
        return "max_iterations_exceeded"
    if "max_tokens" in lowered or "max tokens" in lowered:
        return "max_tokens"
    if "per-ticket cap" in lowered or "per_ticket cap" in lowered:
        return "ticket_capped"
    if "hard cap" in lowered or "global cap" in lowered:
        return "global_capped"
    return None


def iter_run_logs(log_dirs: Sequence[Path]) -> Iterable[Path]:
    for d in log_dirs:
        if not d.exists():
            continue
        yield from sorted(p for p in d.rglob("*.log") if p.is_file())
        yield from sorted(p for p in d.rglob("*.jsonl") if p.is_file())


def load_entries(
    log_dirs: Sequence[Path],
    *,
    since: datetime | None = None,
) -> list[RunLogEntry]:
    """Load + normalise all JSONL entries under the given dirs."""
    out: list[RunLogEntry] = []
    for path in iter_run_logs(log_dirs):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            entry = parse_entry(raw)
            if entry is None:
                continue
            if since is not None and not _entry_after(entry, since):
                continue
            out.append(entry)
    return out


def _entry_after(entry: RunLogEntry, since: datetime) -> bool:
    if not entry.finished_at:
        return True  # no timestamp → keep, can't filter
    try:
        ts = datetime.fromisoformat(entry.finished_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= since


# ─── Pattern generation ──────────────────────────────────────────────


def _path_prefixes(path: str, depths: Sequence[int] = PATH_PREFIX_DEPTHS) -> set[str]:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    out: set[str] = set()
    for d in depths:
        if 0 < d <= len(parts):
            out.add("/".join(parts[:d]) + ("/" if d < len(parts) else ""))
    return out


def _candidate_signatures(entry: RunLogEntry) -> set[tuple[tuple[str, str], ...]]:
    """All (sorted) signatures this row contributes to.

    Dimensions per OP-822 spec: stop_reason, file paths, tier, ticket area.
    ``status`` and ``agent_class`` are deliberately NOT clustered on —
    ``status=failed`` is tautological (it IS the failure flag) and
    ``agent_class`` describes routing rather than failure shape; both
    appear in the per-dimension breakdown appendix instead.
    """
    sigs: set[tuple[tuple[str, str], ...]] = set()
    one_dim: list[tuple[str, str]] = []
    if entry.stop_reason:
        one_dim.append(("stop_reason", entry.stop_reason))
    if entry.tier:
        one_dim.append(("tier", entry.tier))
    for a in entry.area:
        one_dim.append(("area", a))
    # Single-dim signatures
    for kv in one_dim:
        sigs.add((kv,))
    # File-prefix signatures
    prefixes: set[str] = set()
    for f in entry.files_touched:
        prefixes |= _path_prefixes(f)
    for p in prefixes:
        sigs.add((("file_prefix", p),))
    # 2-dim cross — area × stop_reason, tier × stop_reason, and
    # file_prefix × stop_reason. These are the actionable shapes for
    # mitigation routing.
    if entry.stop_reason:
        for a in entry.area:
            sigs.add(tuple(sorted((("area", a), ("stop_reason", entry.stop_reason)))))
        if entry.tier:
            sigs.add(tuple(sorted((("tier", entry.tier), ("stop_reason", entry.stop_reason)))))
        for p in prefixes:
            sigs.add(tuple(sorted((("file_prefix", p), ("stop_reason", entry.stop_reason)))))
    return sigs


def _specificity(signature: tuple[tuple[str, str], ...]) -> int:
    """Higher = more specific. Multi-dim and deeper paths score higher."""
    score = len(signature) * 10
    for k, v in signature:
        if k == "file_prefix":
            score += v.count("/")
    return score


def _suggest_mitigation(signature: tuple[tuple[str, str], ...]) -> str:
    sig = dict(signature)
    stop = sig.get("stop_reason")
    area = sig.get("area", "")
    tier = sig.get("tier")
    file_prefix = sig.get("file_prefix", "")
    status = sig.get("status")

    has_security = "security" in area or "security" in file_prefix
    if has_security:
        return (
            "Route to non-AI reviewer queue (Gerrit human +2 required per "
            "CLAUDE.md L1). Consider blocklisting this scope from "
            "subscription-claude until manual root-cause review."
        )
    if stop == "max_iterations_exceeded":
        return (
            "Move tickets matching this scope to ``class:subscription-claude`` "
            "(80-iteration budget vs 40 on api-anthropic) OR decompose per "
            "docs/sop/jira-ticket-conventions.md §11 (discovered-dependency "
            "split). Retrying the same prompt burns tokens for the same "
            "outcome (W14.5 lesson)."
        )
    if stop == "max_tokens":
        return (
            "Pre-call decomposition required: invoke the Plan sub-agent "
            "before implementation, or raise ``OMNISIGHT_SDK_MAX_TOKENS`` "
            "for this ticket family. Single-shot output is hitting the "
            "model's response cap."
        )
    if tier == "X":
        return (
            "tier:X tickets are oversize by definition — split into ≤3 "
            "child tickets (M or L tier each) before assigning to runner."
        )
    if status in {"global_capped", "ticket_capped"}:
        return (
            "Lower per-ticket budget cap and gather more data before retry. "
            "If runaway is structural (same ticket repeatedly capping), "
            "escalate to operator for manual decomposition."
        )
    if file_prefix:
        return (
            f"Cluster of failures touching ``{file_prefix}`` — schedule a "
            "META ticket to review whether this scope needs refactoring, "
            "better fixtures, or a specialised runner."
        )
    return (
        "Investigate cluster manually; gather more samples (≥10) before "
        "automating mitigation."
    )


# ─── Detection pipeline ──────────────────────────────────────────────


@dataclass
class AuditResult:
    total_runs: int
    total_failures: int
    baseline_failure_rate: float
    patterns: list[Pattern]
    by_dimension_summary: dict[str, list[tuple[str, int, int]]]


def detect_patterns(
    entries: Sequence[RunLogEntry],
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
    min_failure_rate: float = DEFAULT_MIN_FAILURE_RATE,
    top_n: int = DEFAULT_TOP_N,
    dedup_overlap: float = DEFAULT_DEDUP_OVERLAP,
) -> AuditResult:
    total_runs = len(entries)
    total_failures = sum(1 for e in entries if e.is_failure)
    baseline = total_failures / total_runs if total_runs else 0.0

    # Build sig → (rows, failure_rows)
    sig_rows: dict[tuple[tuple[str, str], ...], list[RunLogEntry]] = defaultdict(list)
    sig_fail_rows: dict[tuple[tuple[str, str], ...], list[RunLogEntry]] = defaultdict(list)
    for entry in entries:
        for sig in _candidate_signatures(entry):
            sig_rows[sig].append(entry)
            if entry.is_failure:
                sig_fail_rows[sig].append(entry)

    candidates: list[Pattern] = []
    for sig, rows in sig_rows.items():
        support = len(rows)
        if support < min_support:
            continue
        fails = sig_fail_rows.get(sig, [])
        failure_count = len(fails)
        if failure_count == 0:
            continue
        rate = failure_count / support
        if rate < min_failure_rate:
            continue
        spec = _specificity(sig)
        score = failure_count * rate * spec
        candidates.append(Pattern(
            signature=sig,
            support=support,
            failure_count=failure_count,
            failure_rate=rate,
            specificity=spec,
            score=score,
            mitigation=_suggest_mitigation(sig),
            matching_tickets=tuple(sorted({r.ticket_key for r in fails})),
        ))

    # Score-sorted, then de-dup by failing-ticket overlap. Dedup measures
    # ``what share of THIS candidate's failures are already explained by
    # the union of previously-picked patterns?'' — if most of them are,
    # the candidate is a broader re-statement (e.g. ``file_prefix=backend/``
    # after we already picked ``backend/security/* + max_iter``) and
    # adding it costs a slot that should surface a different cluster.
    candidates.sort(key=lambda p: (-p.score, -p.failure_count, -p.specificity))
    picked: list[Pattern] = []
    for cand in candidates:
        cand_fails = set(cand.matching_tickets)
        if not cand_fails:
            continue
        already_covered: set[str] = set()
        for already in picked:
            already_covered |= set(already.matching_tickets)
        covered = len(cand_fails & already_covered) / len(cand_fails)
        if covered >= dedup_overlap:
            continue
        picked.append(cand)
        if len(picked) >= top_n:
            break

    by_dim_summary = _dimension_summary(entries)
    return AuditResult(
        total_runs=total_runs,
        total_failures=total_failures,
        baseline_failure_rate=baseline,
        patterns=picked,
        by_dimension_summary=by_dim_summary,
    )


def _dimension_summary(
    entries: Sequence[RunLogEntry],
) -> dict[str, list[tuple[str, int, int]]]:
    """Per-dimension (value, support, failures) for the appendix."""
    out: dict[str, list[tuple[str, int, int]]] = {}
    for dim, fn in (
        ("stop_reason", lambda e: [e.stop_reason] if e.stop_reason else []),
        ("status", lambda e: [e.status]),
        ("tier", lambda e: [e.tier] if e.tier else []),
        ("agent_class", lambda e: [e.agent_class] if e.agent_class else []),
        ("area", lambda e: list(e.area)),
    ):
        support: Counter[str] = Counter()
        fails: Counter[str] = Counter()
        for entry in entries:
            for v in fn(entry):
                support[v] += 1
                if entry.is_failure:
                    fails[v] += 1
        rows = sorted(
            ((v, c, fails.get(v, 0)) for v, c in support.items()),
            key=lambda r: (-r[2], -r[1]),
        )
        out[dim] = rows
    return out


# ─── Markdown report ─────────────────────────────────────────────────


def render_markdown(result: AuditResult, *, generated_at: datetime) -> str:
    lines: list[str] = []
    lines.append("# SDK Runner Failure-Pattern Audit")
    lines.append("")
    lines.append(f"_Generated: {generated_at.isoformat(timespec='seconds')} (OP-822)_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total runs analysed: **{result.total_runs}**")
    lines.append(f"- Total failures: **{result.total_failures}**")
    lines.append(f"- Baseline failure rate: **{result.baseline_failure_rate:.1%}**")
    lines.append("")
    if not result.total_runs:
        lines.append(
            "_No run log entries found — nothing to cluster. Confirm "
            "``data/sdk-launcher/`` has been populated by the launcher._"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(f"## Top {len(result.patterns)} Failure Patterns")
    lines.append("")
    if not result.patterns:
        lines.append(
            "_No clusters cleared the support / failure-rate thresholds. "
            "Either runs are too few (raise ``--min-support``) or failures "
            "are not concentrated by the audited dimensions._"
        )
        lines.append("")
    else:
        for i, pat in enumerate(result.patterns, start=1):
            lift = pat.failure_rate / result.baseline_failure_rate if result.baseline_failure_rate else float("inf")
            lift_str = f"{lift:.1f}×" if lift != float("inf") else "∞"
            lines.append(f"### Pattern {i}: `{pat.signature_str}`")
            lines.append("")
            lines.append(
                f"- Support (rows matching): **{pat.support}**  "
                f"| Failures: **{pat.failure_count}**  "
                f"| Failure rate: **{pat.failure_rate:.1%}** ({lift_str} baseline)"
            )
            sample = ", ".join(pat.matching_tickets[:8])
            if len(pat.matching_tickets) > 8:
                sample += f", … (+{len(pat.matching_tickets) - 8} more)"
            lines.append(f"- Failing tickets: {sample}")
            lines.append("")
            lines.append(f"**Suggested mitigation:** {pat.mitigation}")
            lines.append("")

    lines.append("## Per-dimension breakdown")
    lines.append("")
    for dim, rows in result.by_dimension_summary.items():
        if not rows:
            continue
        lines.append(f"### {dim}")
        lines.append("")
        lines.append("| value | support | failures | failure-rate |")
        lines.append("|---|---:|---:|---:|")
        for value, support, fails in rows[:10]:
            rate = fails / support if support else 0.0
            lines.append(f"| `{value}` | {support} | {fails} | {rate:.1%} |")
        lines.append("")

    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--log-dir",
        type=Path,
        action="append",
        help=(
            "Directory containing JSONL run logs (repeatable). "
            f"Default: {', '.join(str(d.relative_to(REPO_ROOT)) for d in DEFAULT_LOG_DIRS)}"
        ),
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help=f"Markdown output path (default: {DEFAULT_OUTPUT_DIR}/sdk-failure-audit-<DATE>.md).",
    )
    p.add_argument("--stdout", action="store_true",
                   help="Print to stdout instead of writing --output.")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                   help=f"Max patterns to surface (default {DEFAULT_TOP_N}).")
    p.add_argument("--min-support", type=int, default=DEFAULT_MIN_SUPPORT,
                   help=f"Min rows per cluster (default {DEFAULT_MIN_SUPPORT}).")
    p.add_argument("--min-failure-rate", type=float, default=DEFAULT_MIN_FAILURE_RATE,
                   help=f"Min cluster failure rate 0-1 (default {DEFAULT_MIN_FAILURE_RATE}).")
    p.add_argument("--window-days", type=int, default=None,
                   help="Only include rows whose finished_at is within this many days.")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log_dirs = [Path(d) for d in (args.log_dir or DEFAULT_LOG_DIRS)]
    since = (
        datetime.now(timezone.utc) - timedelta(days=args.window_days)
        if args.window_days
        else None
    )
    entries = load_entries(log_dirs, since=since)
    result = detect_patterns(
        entries,
        min_support=args.min_support,
        min_failure_rate=args.min_failure_rate,
        top_n=args.top,
    )
    now = datetime.now(timezone.utc)
    markdown = render_markdown(result, generated_at=now)

    if args.stdout:
        sys.stdout.write(markdown)
        return 0

    output = args.output
    if output is None:
        output = DEFAULT_OUTPUT_DIR / f"sdk-failure-audit-{now:%Y-%m-%d}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {output} ({len(result.patterns)} pattern(s) of "
          f"{result.total_failures} failures across {result.total_runs} runs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
