#!/usr/bin/env python3
"""OP-876 — Merger Agent proactive-trigger spike harness.

Verifies three unknowns before committing dev effort to OP-713 Phase 2
(patchset-created consumer) or OP-733 (change-merged webhook subscription):

  Q1. ``mergeable`` race at ``patchset-created`` time — does Gerrit
      return ``mergeable=null`` immediately after a patchset arrives,
      and if so for how long?
  Q2. OP-733 backend consumer end-to-end behaviour — does the
      ``change-merged`` path that the bridge / auto-rebase sweeper
      depends on actually fire when triggered?
  Q3. ``ai-reviewer-webhook`` auth still on ``secret = ...`` (broken
      per L-OP-713 — webhooks plugin v3.13.5 silently drops the key)?

This is a SPIKE — no production code changes. The script has two layers:

  * Static-analysis layer (``--mode=static``, default): pure-Python
    checks against project.config + the relevant backend handlers.
    Runs offline, no Gerrit/network needed. Output is the answer for
    Q3 outright + the "is the OP-733 consumer wired through SSH bridge
    or webhook?" answer for Q2.
  * Live-Gerrit layer (``--mode=live --gerrit-url ... --auth ...``):
    push N synthetic conflict patchsets, capture the t_event →
    t_mergeable_ready latency histogram, tail the backend log for
    Q3's X-Gerrit-Signature absence. Operator-run; does NOT execute by
    default and the smoke test only exercises the static layer.

Decision rules live in the docstring of :func:`q1_decide`,
:func:`q2_decide`, and :func:`q3_decide` and are the authoritative
mapping from observation → verdict. The decision document
``docs/research/merger-proactive-trigger-spike-2026-05.md`` repeats
them verbatim for operator review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Literal


# ─── repo layout ───────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_CONFIG = Path("/tmp/gerrit-meta/project.config")
WEBHOOKS_HANDLER = REPO_ROOT / "backend" / "routers" / "webhooks.py"
AUTO_REBASE_MODULE = REPO_ROOT / "backend" / "agents" / "auto_rebase.py"
BRIDGE_MODULE = REPO_ROOT / "backend" / "agents" / "gerrit_jira_bridge.py"

# Decision-rule constants. Bumping these means re-running the spike.
Q1_FAST_THRESHOLD_S = 1.0     # <1s → trigger viable real-time
Q1_BACKOFF_THRESHOLD_S = 10.0  # 1-10s → 3-attempt backoff; >10s → no-go
Q1_MAX_WAIT_S = 30.0          # SpikeMergeableNeverReady threshold
Q1_SAMPLE_COUNT_DEFAULT = 10


# ─── data classes ──────────────────────────────────────────────────────


Verdict = Literal["go", "go_with_backoff", "no_go", "deferred"]


@dataclass(frozen=True)
class MergeableSample:
    """One push → first-non-null-mergeable observation."""
    t_event: float
    t_mergeable_ready: float
    mergeable_final: bool | None
    poll_attempts: int

    @property
    def delta_s(self) -> float:
        return self.t_mergeable_ready - self.t_event


@dataclass
class Q1Result:
    samples: list[MergeableSample] = field(default_factory=list)
    timed_out: int = 0
    verdict: Verdict = "deferred"
    rationale: str = ""

    def histogram(self) -> dict[str, int]:
        """Bucket the deltas into the decision-rule bands."""
        buckets = {"<1s": 0, "1-10s": 0, ">10s": 0, "timeout": self.timed_out}
        for s in self.samples:
            if s.delta_s < Q1_FAST_THRESHOLD_S:
                buckets["<1s"] += 1
            elif s.delta_s <= Q1_BACKOFF_THRESHOLD_S:
                buckets["1-10s"] += 1
            else:
                buckets[">10s"] += 1
        return buckets


@dataclass
class Q2Checkpoint:
    """One step of the webhook → backend → consumer chain."""
    name: str
    passed: bool | None
    evidence: str

    @classmethod
    def deferred(cls, name: str, reason: str) -> "Q2Checkpoint":
        return cls(name=name, passed=None, evidence=f"deferred: {reason}")


@dataclass
class Q2Result:
    checkpoints: list[Q2Checkpoint] = field(default_factory=list)
    static_finding: str = ""
    verdict: Verdict = "deferred"
    rationale: str = ""


@dataclass
class Q3Result:
    project_config_path: str
    has_secret_line: bool
    has_header_auth: bool
    headers_observed: list[str] = field(default_factory=list)
    backend_status_code: int | None = None
    verdict: Verdict = "deferred"
    rationale: str = ""


@dataclass
class SpikeReport:
    generated_at: str
    mode: str
    q1: Q1Result
    q2: Q2Result
    q3: Q3Result


# ─── Q1 — mergeable race ───────────────────────────────────────────────


def q1_simulate_samples(
    deltas_s: Iterable[float],
    *,
    timeout_s: float = Q1_MAX_WAIT_S,
) -> Q1Result:
    """Offline simulator — feed a list of synthetic deltas and produce
    the same shape of Q1Result we'd get from a live Gerrit run.

    Used by the smoke test and by static-mode reports as a placeholder
    so the decision-rule mapping is exercised even without real data.
    """
    result = Q1Result()
    base_event_t = 1_000_000.0  # arbitrary epoch — only deltas matter
    for i, delta in enumerate(deltas_s):
        if delta > timeout_s:
            result.timed_out += 1
            continue
        result.samples.append(
            MergeableSample(
                t_event=base_event_t + i,
                t_mergeable_ready=base_event_t + i + delta,
                mergeable_final=False,
                poll_attempts=max(1, int(delta // 0.5) + 1),
            )
        )
    result.verdict, result.rationale = q1_decide(result)
    return result


def q1_decide(result: Q1Result) -> tuple[Verdict, str]:
    """Decision rule for Q1 (mergeable race).

    Maps observed sample deltas → patchset-created trigger verdict:

      * All deltas <1s        → ``go``               (real-time viable)
      * All deltas ≤10s       → ``go_with_backoff``  (3-attempt 0/2/5s)
      * Any delta >10s or any timeout → ``no_go``    (fall back to
        ref-updated poll-after-N-seconds)
      * Empty sample set      → ``deferred``         (operator must run
        live harness against staging Gerrit)
    """
    if not result.samples and result.timed_out == 0:
        return "deferred", (
            "no samples captured — run with --mode=live against staging "
            "Gerrit; static analysis cannot answer this question"
        )
    if result.timed_out > 0:
        return "no_go", (
            f"{result.timed_out} sample(s) exceeded {Q1_MAX_WAIT_S}s "
            "(SpikeMergeableNeverReady) — patchset-created is not a "
            "safe trigger; use ref-updated poll-after-N-seconds"
        )
    over_10 = [s for s in result.samples if s.delta_s > Q1_BACKOFF_THRESHOLD_S]
    if over_10:
        return "no_go", (
            f"{len(over_10)}/{len(result.samples)} samples > "
            f"{Q1_BACKOFF_THRESHOLD_S}s — too slow for live trigger; "
            "fall back to ref-updated poll-after-N-seconds"
        )
    over_fast = [s for s in result.samples if s.delta_s >= Q1_FAST_THRESHOLD_S]
    if over_fast:
        return "go_with_backoff", (
            f"{len(over_fast)}/{len(result.samples)} samples in 1-10s "
            "band — backend needs a 3-attempt backoff (0s, 2s, 5s) "
            "before declaring mergeable_unknown"
        )
    return "go", (
        f"all {len(result.samples)} samples <{Q1_FAST_THRESHOLD_S}s — "
        "patchset-created is a viable real-time trigger, no retry"
    )


def q1_summarise(result: Q1Result) -> dict[str, Any]:
    """Render the histogram + a few percentiles for the markdown table."""
    deltas = [s.delta_s for s in result.samples]
    summary: dict[str, Any] = {
        "n_samples": len(result.samples),
        "n_timeout": result.timed_out,
        "histogram": result.histogram(),
        "verdict": result.verdict,
        "rationale": result.rationale,
    }
    if deltas:
        summary["p50_s"] = round(statistics.median(deltas), 3)
        summary["p95_s"] = round(_percentile(deltas, 0.95), 3)
        summary["max_s"] = round(max(deltas), 3)
    return summary


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[idx]


# ─── Q2 — OP-733 end-to-end ────────────────────────────────────────────


def q2_static_check(
    *,
    auto_rebase: Path = AUTO_REBASE_MODULE,
    bridge: Path = BRIDGE_MODULE,
    webhooks: Path = WEBHOOKS_HANDLER,
) -> Q2Result:
    """Static-analysis answer for Q2.

    The ticket Q2 hypothesis was: "OP-733 code shipped, but the
    webhook subscription was never added to webhooks.config — therefore
    the consumer has never been triggered."

    Code reading reveals OP-733 is wired through the SSH stream-events
    daemon (``gerrit_jira_bridge.py::_schedule_auto_rebase_sweep``),
    not through the HTTP webhook. So the webhook-subscription premise
    is incorrect; the real Q2 question becomes: "does the SSH bridge
    actually fire the sweep on change-merged?"

    This check confirms the wiring is present in source. End-to-end
    "does it actually fire in prod?" requires either a staging trigger
    or log inspection — both deferred to the operator.
    """
    result = Q2Result()
    bridge_text = _safe_read(bridge)
    auto_text = _safe_read(auto_rebase)
    web_text = _safe_read(webhooks)

    # Checkpoint 1: auto-rebase module imports cleanly + advertises OP-733.
    result.checkpoints.append(Q2Checkpoint(
        name="auto_rebase_module_present",
        passed=auto_text is not None and "OP-733" in auto_text,
        evidence=(
            f"{auto_rebase}: OP-733 reference {'present' if auto_text and 'OP-733' in auto_text else 'absent'}"
        ),
    ))

    # Checkpoint 2: bridge schedules the sweep on change-merged.
    bridge_schedules = bool(
        bridge_text and "_schedule_auto_rebase_sweep" in bridge_text
        and "change-merged" in bridge_text
    )
    result.checkpoints.append(Q2Checkpoint(
        name="bridge_wires_sweep_on_change_merged",
        passed=bridge_schedules,
        evidence=(
            f"{bridge}: _schedule_auto_rebase_sweep "
            f"{'wired' if bridge_schedules else 'NOT wired'} to change-merged"
        ),
    ))

    # Checkpoint 3: webhook _on_change_merged does NOT call OP-733.
    # (Confirms the premise that webhook-side is a no-op for OP-733.)
    webhook_no_op = (
        web_text is not None
        and "_on_change_merged" in web_text
        and "auto_rebase" not in web_text.split("_on_change_merged", 1)[1][:4000]
    )
    result.checkpoints.append(Q2Checkpoint(
        name="webhook_handler_does_not_call_op733",
        passed=webhook_no_op,
        evidence=(
            f"{webhooks}: _on_change_merged "
            f"{'does not import auto_rebase (premise confirmed)' if webhook_no_op else 'unexpectedly imports auto_rebase'}"
        ),
    ))

    # Checkpoint 4: live trigger — deferred.
    result.checkpoints.append(Q2Checkpoint.deferred(
        "live_trigger_fires_in_staging",
        "operator must trigger synthetic merge in staging + tail logs",
    ))
    # Checkpoint 5: live observable effect — deferred.
    result.checkpoints.append(Q2Checkpoint.deferred(
        "live_sweep_pushes_ps2",
        "operator must observe a merger PS2 land on an open conflicted PS",
    ))

    static_pass = all(
        c.passed is True for c in result.checkpoints if c.passed is not None
    )
    deferred = [c for c in result.checkpoints if c.passed is None]
    result.static_finding = (
        "OP-733 lives in the SSH stream-events bridge, not the HTTP "
        "webhook. Adding [remote ...] change-merged subscription is "
        "redundant (would double-fire) and not necessary."
    )

    if not static_pass:
        result.verdict = "no_go"
        result.rationale = (
            "static wiring failed at least one checkpoint — fix the "
            "module wiring before any live test"
        )
    elif deferred:
        result.verdict = "deferred"
        result.rationale = (
            f"static wiring confirmed; {len(deferred)} live checkpoint(s) "
            "still need staging exercise before the path can be declared "
            "production-ready"
        )
    else:
        result.verdict = "go"
        result.rationale = "all checkpoints green"

    return result


# ─── Q3 — ai-reviewer-webhook auth ─────────────────────────────────────

_SECRET_RE = re.compile(r"^\s*secret\s*=", re.MULTILINE)
_HEADER_AUTH_RE = re.compile(
    r"^\s*header\s*=\s*Authorization\s*:\s*Bearer\b", re.MULTILINE
)
_AI_REVIEWER_BLOCK_RE = re.compile(
    r'\[remote\s+"ai-reviewer-webhook"\][^\[]*', re.DOTALL
)


def q3_inspect_config(path: Path = DEFAULT_PROJECT_CONFIG) -> Q3Result:
    """Read project.config + classify the ai-reviewer-webhook block.

    Decision rule (no observed headers; static-only):

      * ``secret = ...`` present + no ``header = Authorization: Bearer``
        → ``no_go`` (L-OP-713 confirmed broken; patch to header-auth)
      * ``header = Authorization: Bearer`` present (with or without
        legacy ``secret``) → ``go`` (matches OP-708 working pattern)
      * Block absent / config unreadable → ``deferred``
    """
    result = Q3Result(
        project_config_path=str(path),
        has_secret_line=False,
        has_header_auth=False,
    )
    text = _safe_read(path)
    if text is None:
        result.verdict = "deferred"
        result.rationale = f"project.config not readable at {path}"
        return result
    m = _AI_REVIEWER_BLOCK_RE.search(text)
    if not m:
        result.verdict = "deferred"
        result.rationale = (
            "[remote \"ai-reviewer-webhook\"] block not present in "
            f"{path} — cannot verify auth shape"
        )
        return result
    block = m.group(0)
    result.has_secret_line = bool(_SECRET_RE.search(block))
    result.has_header_auth = bool(_HEADER_AUTH_RE.search(block))
    if result.has_header_auth:
        result.verdict = "go"
        result.rationale = (
            "block already uses `header = Authorization: Bearer` "
            "(L-OP-708 pattern) — no fix needed"
        )
    elif result.has_secret_line:
        result.verdict = "no_go"
        result.rationale = (
            "block uses `secret = ...` which webhooks plugin v3.13.5 "
            "silently drops (L-OP-713). Patch: switch to "
            "`header = Authorization: Bearer <api_key>` like the "
            "merge-conflict-webhook block does (L-OP-708)."
        )
    else:
        result.verdict = "no_go"
        result.rationale = (
            "block has neither secret nor header auth — Gerrit is "
            "POSTing unauthenticated events, backend will 401"
        )
    return result


def q3_classify_live_headers(
    observed: list[str], backend_status: int
) -> Q3Result:
    """Decision rule for Q3 when live header dump is available.

    Operator captures the request headers from Caddy / backend access
    log and feeds them in; we apply the same decision rule layered on
    top of evidence rather than static config.
    """
    lowered = [h.lower() for h in observed]
    has_sig = any(h.startswith("x-gerrit-signature") for h in lowered)
    has_bearer = any(
        h.startswith("authorization:") and "bearer" in h
        for h in lowered
    )
    result = Q3Result(
        project_config_path="<live capture>",
        has_secret_line=False,
        has_header_auth=has_bearer,
        headers_observed=observed,
        backend_status_code=backend_status,
    )
    if has_sig and backend_status == 200:
        result.verdict = "go"
        result.rationale = (
            "X-Gerrit-Signature arrived AND backend accepted — "
            "would mean webhooks plugin v3.13.5 silently fixed; "
            "VERY unlikely — re-verify plugin version"
        )
    elif has_bearer and backend_status == 200:
        result.verdict = "go"
        result.rationale = "header-auth path works as designed (L-OP-708)"
    else:
        result.verdict = "no_go"
        result.rationale = (
            "no X-Gerrit-Signature, no Bearer header, backend status="
            f"{backend_status} — confirmed broken per L-OP-713; patch "
            "project.config to header-auth"
        )
    return result


# ─── live-Gerrit driver (operator-only; lazy-imported) ────────────────


def q1_run_live(
    gerrit_url: str,
    auth_token: str,
    *,
    sample_count: int = Q1_SAMPLE_COUNT_DEFAULT,
    project: str = "OmniSight-Productizer",
    branch: str = "develop",
    timeout_s: float = Q1_MAX_WAIT_S,
) -> Q1Result:
    """Operator-run only. Pushes synthetic conflict patchsets and polls
    ``GET /changes/{id}?o=MERGEABLE`` until mergeable resolves.

    Synthetic-PS generation strategy (no test_assets writes):
      1. Fetch ``origin/develop~5`` so the parent SHA is reliably
         behind the tip.
      2. Edit a hot file (default ``auto-runner-jira.py``) with a
         marker line so the patch conflicts with whatever later commits
         touched it.
      3. ``git push origin HEAD:refs/for/{branch}%hashtag=spike-OP-876``
         and capture wallclock as ``t_event``.
      4. Poll ``GET /a/changes/<id>?o=MERGEABLE`` until non-null
         (interval 0.5s, cap ``timeout_s``); record ``t_mergeable_ready``.
      5. Abandon the change via SSH (``gerrit review --abandon``).

    Requires the ``requests`` library + an HTTP password for an account
    with write access to the project. Not executed in CI; the smoke
    test exercises the simulator instead.
    """
    try:
        import requests  # type: ignore
    except ImportError:
        result = Q1Result()
        result.verdict = "deferred"
        result.rationale = "requests not installed — install + re-run"
        return result

    raise NotImplementedError(
        "live mode requires operator-supplied push credentials. "
        "Wire credentials via the existing per-bot key-bag "
        "(see backend/agents/auto_rebase.py:OWNER_HTTP_PASSWORD_PATHS) "
        "and unblock this branch."
    )


# ─── utility ───────────────────────────────────────────────────────────


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def render_markdown(report: SpikeReport) -> str:
    q1_summary = q1_summarise(report.q1)
    lines = [
        "# OP-876 Merger Proactive-Trigger Spike — Auto-generated Summary",
        "",
        f"Generated: {report.generated_at}  ·  Mode: `{report.mode}`",
        "",
        "## Q1 — `mergeable` race at patchset-created",
        "",
        "| metric | value |",
        "|---|---|",
        f"| samples | {q1_summary['n_samples']} |",
        f"| timeouts | {q1_summary['n_timeout']} |",
        f"| <1s | {q1_summary['histogram']['<1s']} |",
        f"| 1-10s | {q1_summary['histogram']['1-10s']} |",
        f"| >10s | {q1_summary['histogram']['>10s']} |",
    ]
    if "p50_s" in q1_summary:
        lines.extend([
            f"| p50 (s) | {q1_summary['p50_s']} |",
            f"| p95 (s) | {q1_summary['p95_s']} |",
            f"| max (s) | {q1_summary['max_s']} |",
        ])
    lines.extend([
        "",
        f"**Verdict**: `{report.q1.verdict}` — {report.q1.rationale}",
        "",
        "## Q2 — OP-733 end-to-end behaviour",
        "",
        f"**Static finding**: {report.q2.static_finding}",
        "",
        "| checkpoint | result | evidence |",
        "|---|---|---|",
    ])
    for cp in report.q2.checkpoints:
        marker = {True: "✓", False: "✗", None: "·"}[cp.passed]
        lines.append(f"| {cp.name} | {marker} | {cp.evidence} |")
    lines.extend([
        "",
        f"**Verdict**: `{report.q2.verdict}` — {report.q2.rationale}",
        "",
        "## Q3 — `ai-reviewer-webhook` auth shape",
        "",
        f"- project.config: `{report.q3.project_config_path}`",
        f"- `secret = ...` line: {report.q3.has_secret_line}",
        f"- `header = Authorization: Bearer ...` line: {report.q3.has_header_auth}",
    ])
    if report.q3.headers_observed:
        lines.append(
            "- headers observed: " + ", ".join(
                f"`{h.split(':', 1)[0]}`" for h in report.q3.headers_observed
            )
        )
    if report.q3.backend_status_code is not None:
        lines.append(f"- backend status: `{report.q3.backend_status_code}`")
    lines.extend([
        "",
        f"**Verdict**: `{report.q3.verdict}` — {report.q3.rationale}",
        "",
    ])
    return "\n".join(lines) + "\n"


# ─── CLI ───────────────────────────────────────────────────────────────


def build_static_report() -> SpikeReport:
    """Run all three static checks; Q1 has no real data so the
    simulator is given an empty deltas list and reports ``deferred``."""
    return SpikeReport(
        generated_at=time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
        mode="static",
        q1=q1_simulate_samples([]),
        q2=q2_static_check(),
        q3=q3_inspect_config(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spike_merger_proactive_trigger")
    parser.add_argument(
        "--mode", choices=("static", "live"), default="static",
        help="static = offline checks (default); live = push synthetic PSes",
    )
    parser.add_argument(
        "--project-config",
        type=Path,
        default=DEFAULT_PROJECT_CONFIG,
        help="path to Gerrit project.config (default: /tmp/gerrit-meta/...)",
    )
    parser.add_argument(
        "--samples", type=int, default=Q1_SAMPLE_COUNT_DEFAULT,
        help="Q1 sample count (live mode only)",
    )
    parser.add_argument(
        "--gerrit-url", help="Gerrit base URL (live mode only)",
    )
    parser.add_argument(
        "--auth-env", default="OMNISIGHT_GERRIT_CLAUDE_HTTP_PASSWORD",
        help="env var holding HTTP password (live mode only)",
    )
    parser.add_argument("--json", help="optional JSON output path")
    parser.add_argument("--output", help="optional markdown output path")
    args = parser.parse_args(argv)

    if args.mode == "live":
        token = os.environ.get(args.auth_env)
        if not (args.gerrit_url and token):
            parser.error(
                "live mode needs --gerrit-url AND a non-empty value in "
                f"${args.auth_env}"
            )
        q1 = q1_run_live(
            args.gerrit_url, token, sample_count=args.samples,
        )
        q2 = q2_static_check()
        q3 = q3_inspect_config(args.project_config)
        report = SpikeReport(
            generated_at=time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
            mode="live",
            q1=q1, q2=q2, q3=q3,
        )
    else:
        report = build_static_report()
        if args.project_config != DEFAULT_PROJECT_CONFIG:
            report.q3 = q3_inspect_config(args.project_config)

    md = render_markdown(report)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)

    if args.json:
        # asdict on dataclasses with nested dataclasses produces JSON-ready dicts.
        payload = asdict(report)
        Path(args.json).write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

    # Exit non-zero if any verdict is no_go so CI can gate on it later.
    if any(v == "no_go" for v in (report.q1.verdict, report.q2.verdict, report.q3.verdict)):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
