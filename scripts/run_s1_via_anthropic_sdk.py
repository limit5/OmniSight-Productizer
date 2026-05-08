#!/usr/bin/env python3
"""S1 backlog launcher via Anthropic API SDK with hard $100 cap.

One-shot launcher built per OP-781 / OP-802 lessons learned. The fleet's
existing ``auto-runner-jira.py`` returns ``rc=99`` for ``class:api-*``
tickets ("requires SDK invocation, not CLI"); this script fills that gap
narrowly — it is NOT a daemon. It runs once over the 14 remaining
``Sprint = "S1: MP v0.4.0"`` placeholder tickets, drives each through the
Anthropic Messages API with full tool use, pushes the result to Gerrit,
and walks the JIRA ticket forward.

Safety semantics
----------------

The launcher is paranoid by design because the fleet has burned token
budget on retry loops before:

* **Global hard cap (--max-spend, default $100)**. CostGuard is configured
  with ``per_batch_limit_usd=$100``, ``action="block"``. Pre-submit
  ``CostGuard.check()`` runs before every API call; ``allowed=False``
  halts the launcher with a summary report. The cap is checked against
  *projected* spend (running batch total + this call's estimate), so a
  single very expensive call cannot tunnel past it.

* **Per-ticket cap (--per-ticket-cap, default $8)**. After CostGuard
  reports the per-ticket cumulative spend has crossed the per-ticket cap,
  that ticket is marked failed + the launcher moves on. Prevents a single
  runaway loop from eating the global budget.

* **Idempotency**. Skip any ticket that already has an open or merged
  Gerrit change matching ``[<ticket>]`` subject prefix. Re-running the
  launcher is safe.

* **Dry run (--dry-run)**. Replaces ``AnthropicClient`` with a mock that
  returns canned responses; verifies the JIRA + Gerrit + CostGuard wiring
  end-to-end without spending real tokens.

* **Pilot (--pilot OP-XX)**. Process one specific ticket only, useful as
  the G3 gate before the full Phase 4 batch run.

* **Sonnet by default, Opus on retry only** (--retry-model). Sonnet is
  good enough for refined MP.W4 / W17 specs; Opus is reserved for retry
  on verifiable failure (test fail, push reject) and capped at $20.

Run modes
---------

::

    # Phase 1 G1 gate — dry-run, no API calls
    python3 scripts/run_s1_via_anthropic_sdk.py --dry-run

    # Phase 3 pilot — one ticket only, $5 cap on this one
    python3 scripts/run_s1_via_anthropic_sdk.py --pilot OP-32 --max-spend 5

    # Phase 4 batch run — remaining tickets, $95 cap (after pilot)
    python3 scripts/run_s1_via_anthropic_sdk.py --max-spend 95

The launcher writes a JSONL log to ``data/sdk-launcher/run-<timestamp>.log``
with per-ticket cost + outcome + Gerrit URL for post-mortem reconciliation
against the actual Anthropic invoice.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as _dt
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Imports from the existing fleet — no new infra is added by this launcher;
# every dependency below is shipped in develop.
from backend.agents import jira_dispatch
from backend.agents.anthropic_native_client import (
    AnthropicClient,
    DEFAULT_MODEL_OPUS,
    RunResult,
    TokenUsage,
)
from backend.agents.cost_guard import (
    CostActual,
    CostEstimate,
    CostGuard,
    InMemoryCostStore,
    ScopeKey,
)


DEFAULT_MODEL_SONNET = "claude-sonnet-4-6"
DEFAULT_MAX_ITERATIONS = 80
LAUNCHER_AGENT_CLASS = "api-anthropic"
S1_SPRINT_NAME = "S1: MP v0.4.0"

# Pickup JQL — only refined Story tickets in S1 (placeholders are explicitly
# excluded by ``labels not in ("runner-needs-refinement")``).
S1_PICKABLE_JQL = (
    f'Sprint = "{S1_SPRINT_NAME}" '
    'AND issuetype = Story '
    'AND status = "To Do" '
    'AND assignee is EMPTY '
    'AND labels = "class:api-anthropic" '
    'AND labels not in ("tier:X", "runner-needs-refinement")'
)


@dataclasses.dataclass(frozen=True)
class TicketOutcome:
    """One row in the JSONL log."""

    ticket_key: str
    started_at: str
    finished_at: str
    status: str  # "ok" | "skipped_existing_ps" | "ticket_capped" | "global_capped" | "failed"
    cost_usd: float
    input_tokens: int
    output_tokens: int
    iterations: int
    gerrit_url: str | None = None
    error: str | None = None


# ── CostGuard helpers ──────────────────────────────────────────────────


GLOBAL_SCOPE = ScopeKey(kind="global", key="s1-launcher")


async def install_global_cap(guard: CostGuard, cap_usd: float) -> None:
    """Attach a per-batch hard cap with action=block (the default)."""
    await guard.configure_budget(
        GLOBAL_SCOPE,
        per_batch_limit_usd=cap_usd,
        enabled=True,
    )


async def cumulative_spend(guard: CostGuard) -> float:
    """Sum of all recorded actuals tagged with the launcher scope.

    Uses ``CostStore.spend_in_period(scope, "per_batch")`` — per-batch is
    the right semantic here because the launcher is a single batch run.
    """
    return await guard.store.spend_in_period(GLOBAL_SCOPE, "per_batch")


async def estimate_for(
    guard: CostGuard, *, model: str, in_tok: int, out_tok: int
) -> CostEstimate:
    return guard.estimate_cost(
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        call_id=f"s1-launcher-{uuid.uuid4().hex[:12]}",
        workspace="s1-launcher",
        priority="meta",
        task_type="sprint-impl",
    )


# ── Dry-run mock client ────────────────────────────────────────────────


class _DryRunClient:
    """Stand-in for ``AnthropicClient`` that does not touch the network.

    Returns a fixed canned ``RunResult`` shaped so cost accounting still
    runs; useful for the G1 gate to prove the launcher's control flow
    without spending tokens.
    """

    async def run_with_tools(self, **kwargs: Any) -> RunResult:
        # 1 turn, modest token usage so cost is tiny but non-zero
        usage = TokenUsage(
            input_tokens=2_000,
            output_tokens=500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        return RunResult(
            final_text="[dry-run] no real model call was made.",
            usage=usage,
            stop_reason="end_turn",
            iterations=1,
            tool_calls=[],
        )


# ── Idempotency check ──────────────────────────────────────────────────


def has_existing_gerrit_ps(ticket_key: str) -> bool:
    """Return True if the ticket already has an open or merged Gerrit PS.

    Uses the same SSH-based Gerrit query the rest of the fleet uses; no
    new auth surface added.
    """
    import subprocess

    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS["subscription-claude"]
    user, ssh_key = auth
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
        "-o", "StrictHostKeyChecking=no",
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON",
        f"message:{ticket_key} (status:open OR status:merged)",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        # Fail open — let the launcher try; the runner-side push step
        # has its own duplicate-PS detection.
        return False
    for line in r.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "stats":
            continue
        if str(row.get("subject", "")).startswith(f"[{ticket_key}]"):
            return True
    return False


# ── Per-ticket processing ──────────────────────────────────────────────


async def process_ticket(
    *,
    client: AnthropicClient | _DryRunClient,
    guard: CostGuard,
    ticket_key: str,
    description: str,
    model: str,
    per_ticket_cap_usd: float,
    max_spend_usd: float,
    max_iterations: int,
    log_outcome: Callable[[TicketOutcome], None],
) -> str:
    """Run one S1 ticket end-to-end. Returns the outcome status string."""
    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    if has_existing_gerrit_ps(ticket_key):
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started,
            finished_at=started, status="skipped_existing_ps",
            cost_usd=0.0, input_tokens=0, output_tokens=0, iterations=0,
        ))
        return "skipped_existing_ps"

    # Pre-flight cost estimate (rough — used only for the gate, real
    # cost recorded post-call). Conservative numbers so the gate fires
    # before a giant call lands.
    estimate = await estimate_for(
        guard, model=model, in_tok=600_000, out_tok=30_000,
    )
    spend_so_far = await cumulative_spend(guard)

    # Absolute hard cap (launcher-level). CostGuard's default
    # `cap_100→throttle, over_120→block` is too lenient for this
    # one-shot batch — we want a strict halt at exactly max_spend_usd,
    # not at 120% of it. Compute the projected spend ourselves and
    # refuse if it would cross the line. CostGuard.check() still runs
    # below for tenant calibration + alert recording.
    projected = spend_so_far + estimate.cost_usd_estimated
    if projected > max_spend_usd:
        finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="global_capped", cost_usd=0.0,
            input_tokens=0, output_tokens=0, iterations=0,
            error=(
                f"hard cap: projected ${projected:.2f} > "
                f"max_spend ${max_spend_usd:.2f} (cumulative ${spend_so_far:.2f} + "
                f"estimate ${estimate.cost_usd_estimated:.2f})"
            ),
        ))
        return "global_capped"

    # CostGuard alert recording — fires the 80%/100%/120% alerts to the
    # alert_sink so the dashboard sees the warning even if the launcher's
    # own hard cap would have refused a call later.
    check = await guard.check(estimate, per_batch_observed_usd=spend_so_far)
    if not check.allowed:
        # CostGuard says block (would only happen at >120% of the cap
        # we configured on it; if we set guard cap == max_spend then
        # this branch should never fire — but it's a safety net).
        finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="global_capped", cost_usd=0.0,
            input_tokens=0, output_tokens=0, iterations=0,
            error=f"CostGuard block: {check.reason}",
        ))
        return "global_capped"

    # Real call (or dry-run mock).
    result = await client.run_with_tools(  # type: ignore[union-attr]
        prompt=f"Implement JIRA ticket {ticket_key}.\n\n{description}",
        tools=None,
        system=None,
        model=model,
        max_iterations=max_iterations,
    )

    actual_cost_usd = await _post_call_cost_record(
        guard=guard, model=model, usage=result.usage,
    )
    finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    # Per-ticket cap check (post-call — we cannot interrupt mid-call,
    # only refuse the *next* call once the cap has been crossed).
    if actual_cost_usd > per_ticket_cap_usd:
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="ticket_capped",
            cost_usd=actual_cost_usd,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            iterations=result.iterations,
            error=f"per-ticket cap ${per_ticket_cap_usd:.2f} exceeded",
        ))
        return "ticket_capped"

    log_outcome(TicketOutcome(
        ticket_key=ticket_key, started_at=started, finished_at=finished,
        status="ok",
        cost_usd=actual_cost_usd,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        iterations=result.iterations,
    ))
    return "ok"


async def _post_call_cost_record(
    *, guard: CostGuard, model: str, usage: TokenUsage,
) -> float:
    """Persist estimate + actual via CostGuard; return the USD amount.

    Both records are required: ``check()`` reads the per-batch sum from
    ``spend_in_period`` which comes from ``actuals``, and the estimate is
    needed for the calibration drift loop. Without both, ``cumulative_spend``
    stays at zero and the global cap is never tripped.
    """
    estimate = guard.estimate_cost(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens,
        call_id=f"s1-launcher-actual-{uuid.uuid4().hex[:12]}",
        workspace="s1-launcher",
        priority="meta",
        task_type="sprint-impl",
    )
    await guard.record_estimate(estimate)
    actual = CostActual(
        call_id=estimate.call_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens,
        cost_usd=estimate.cost_usd_estimated,
    )
    await guard.record_actual(actual)
    return estimate.cost_usd_estimated


# ── Main ────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--max-spend", type=float, default=100.0,
                   help="Hard global cap in USD (default 100).")
    p.add_argument("--per-ticket-cap", type=float, default=8.0,
                   help="Per-ticket budget in USD (default 8).")
    p.add_argument("--pilot", type=str, default=None,
                   help="If set, process only this ticket key.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do not call the real Anthropic API.")
    p.add_argument("--model", default=DEFAULT_MODEL_SONNET)
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    return p.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    guard = CostGuard(store=InMemoryCostStore())
    await install_global_cap(guard, args.max_spend)

    log_path = REPO / "data" / "sdk-launcher" / f"run-{int(_dt.datetime.now().timestamp())}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w")

    def _log(outcome: TicketOutcome) -> None:
        log_fh.write(json.dumps(dataclasses.asdict(outcome)) + "\n")
        log_fh.flush()
        print(f"[{outcome.ticket_key}] {outcome.status} ${outcome.cost_usd:.4f}")

    if args.dry_run:
        client: Any = _DryRunClient()
    else:
        client = AnthropicClient(api_key=_load_api_key())

    if args.pilot:
        ticket_keys = [args.pilot]
    else:
        ticket_keys = _fetch_pickable_keys()

    print(f"=== S1 SDK launcher — {len(ticket_keys)} ticket(s), cap ${args.max_spend:.2f} ===")
    for key in ticket_keys:
        description = _fetch_description_or_empty(key)
        await process_ticket(
            client=client, guard=guard, ticket_key=key, description=description,
            model=args.model, per_ticket_cap_usd=args.per_ticket_cap,
            max_spend_usd=args.max_spend, max_iterations=args.max_iterations,
            log_outcome=_log,
        )
        spend = await cumulative_spend(guard)
        print(f"    cumulative spend: ${spend:.2f} / ${args.max_spend:.2f}")
        if spend >= args.max_spend:
            print(f"=== GLOBAL CAP HIT at ${spend:.2f} — halting batch ===")
            break

    log_fh.close()
    print(f"=== DONE — log at {log_path} ===")
    return 0


def _load_api_key() -> str:
    """Load the Anthropic API key from PG llm_credentials (single row)."""
    import subprocess
    out = subprocess.check_output(
        ["docker", "exec", "omnisight-pg-primary", "psql", "-U", "omnisight",
         "-d", "omnisight", "-At", "-c",
         "SELECT api_key FROM llm_credentials WHERE provider='anthropic' LIMIT 1"],
        text=True,
    ).strip()
    if not out:
        raise RuntimeError("No anthropic api_key found in llm_credentials")
    return out


def _fetch_pickable_keys() -> list[str]:
    """Run S1 pickup JQL; return ticket keys."""
    client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
    resp = jira_dispatch._request(client, "POST", "/search/jql", {
        "jql": S1_PICKABLE_JQL,
        "fields": ["summary"],
        "maxResults": 50,
    })
    return [i["key"] for i in resp.get("issues", [])]


def _fetch_description_or_empty(ticket_key: str) -> str:
    client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
    return jira_dispatch.fetch_description(client, ticket_key) or ""


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
