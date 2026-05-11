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
from backend.agents.lesson_retrieval import (
    build_index as build_lesson_index,
    build_lessons_system_message,
    retrieve_lessons,
)
from backend.agents.tdd_applicability import (
    TDDApplicability,
    fetch_for_ticket as fetch_tdd_applicability,
)
from backend.agents.tdd_orchestrator import (
    TDDOrchestrator,
    installed_tdd_guard,
)
from backend.agents.anthropic_native_client import (
    AnthropicClient,
    DEFAULT_MODEL_OPUS,
    RunResult,
    TokenUsage,
)
from backend.agents.context_reset import (
    DetectorAwareDispatcher,
    run_with_resets,
)
from backend.agents.cost_guard import (
    CostActual,
    CostEstimate,
    CostGuard,
    InMemoryCostStore,
    ScopeKey,
)
from backend.agents.critic_agent import (
    CRITIC_TIMEOUT_SECONDS,
    CriticBackend,
    CriticReviewOutcome,
    CriticVerdict,
    resolve_critic_model,
    review_with_dissent_protocol,
)
from backend.agents.loop_detector import (
    LoopDetector,
    OutcomesConfig,
    OutcomesGraderUnavailable,
    OutcomesVerdict,
    load_outcomes_config,
)
from backend.agents.reflection_loop import (
    ERROR_CAP_EXCEEDED as REFLECTION_ERROR_CAP_EXCEEDED,
    REFLECTION_LIMIT,
    ReflectionCounter,
    ReflectionInput,
    build_lint_reflection_input,
    build_test_reflection_input,
from backend.agents.runner_handlers import make_runner_dispatcher
from backend.agents.skills_loader import (
    SkillRegistry,
    load_default_scopes,
    make_skill_handler,
)
from backend.agents.static_analysis_gate import (
    run_static_analysis,
    wrap_text_editor_with_static_analysis,
)
from backend.agents.sub_agent import make_agent_tool_handler
from backend.agents.tom_scratchpad import ToMScratchpad
from backend.agents.tool_dispatcher import (
    BashHandlerV2,
    TextEditorHandler,
    WORKTREE_ENV_VAR,
    ptc_sandbox_handler,
)


DEFAULT_MODEL_SONNET = "claude-sonnet-4-6"
DEFAULT_MAX_ITERATIONS = 40  # Lower than auto-runner-sdk's 80; per W14.5
                              # lesson, max_iterations_exceeded is a
                              # decompose signal, not a retry signal.
DEFAULT_STRUCTURAL_RETRY_CAP_USD = 20.0
LAUNCHER_AGENT_CLASS = "api-anthropic"
S1_SPRINT_NAME = "S1: MP v0.4.0"

# Tool list — mirrors ``auto-runner-sdk.RUNNER_TOOLS``. OP-811 (A3) adds
# ``Skill`` (project verbs via SkillRegistry) and ``Agent`` (sub-agent
# decomposition) so the api-anthropic launcher has the same tool surface as
# the SDK runner. Without these, a single agent-loop iteration is forced to
# carry every refactor end-to-end — large tickets cannot decompose, and any
# project-defined verb (lint_changed, run_tests, etc.) goes unused.
RUNNER_TOOLS: list[str] = [
    "Read", "Write", "Edit", "Bash", "Grep", "Glob", "Skill", "Agent",
]

# OP-828 (B1) — Anthropic built-in tools spec. The model issues tool_use
# blocks against these names; ``str_replace_based_edit_tool`` and ``bash``
# round-trip through the dispatcher's local handlers, while
# ``code_execution`` runs server-side in Anthropic's PTC sandbox. The
# ``allowed_callers`` whitelist is the AC #3 boundary that prevents the
# sandbox from issuing unrelated tool calls (HTTP / DB).
BUILT_IN_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "text_editor_20250728",
        "name": "str_replace_based_edit_tool",
    },
    {
        "type": "bash_20250124",
        "name": "bash",
    },
    {
        "type": "code_execution_20260120",
        "name": "code_execution",
        "allowed_callers": [
            "text_editor_20250728",
            "bash_20250124",
        ],
    },
]

# Stop reasons that indicate the task is structurally too big — DO NOT
# retry the same prompt (it'll burn the same budget for the same outcome,
# cf. W14.5 incident which lost $25 on two identical max_tokens retries).
NON_RETRYABLE_STOP_REASONS: frozenset[str] = frozenset({
    "max_tokens", "max_iterations_exceeded",
})
STRUCTURAL_ESCALATION_STOP_REASONS = NON_RETRYABLE_STOP_REASONS

# B16 (OP-847) — grader prompt header. The Haiku grader receives this
# preamble + the rubric + the runner's final assistant text, and is
# asked to emit a single JSON line. The shape mirrors the reconstructed
# Anthropic Outcomes beta response shape from the OP-843 spike §1
# (``verdict``, ``grader_reasoning``) — see docs/research/b3-outcomes-spike-2026-05.md.
OUTCOMES_GRADER_PROMPT_TEMPLATE = (
    "You are an Outcomes grader for an autonomous code-implementation runner. "
    "Apply the rubric below to the runner's final assistant turn (no tools, "
    "no follow-up). Reply with ONE LINE of JSON containing keys "
    '"verdict" ("pass" | "fail") and "grader_reasoning" (≤200 chars). '
    "No prose outside the JSON.\n\n"
    "=== Rubric ===\n{rubric}\n\n"
    "=== Runner final text ===\n{final_text}\n"
)

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


# ── Full JIRA pipeline (Stage C) ───────────────────────────────────────


# Per-ticket worktree path (api-anthropic uses claude-bot's worktree;
# claude-bot has Code-Review +1 ACL we'll need for AI Reviewer down the
# line, and the same SSH key is what jira_dispatch already authenticates).
WORKTREE_PATH = REPO.parent / "OmniSight-claude-worktree"
LINT_PROGRESS_PATH = REPO / "data" / "sdk-launcher" / "progress.txt"


def bind_built_in_tools_with_static_analysis(
    dispatcher: Any,
    *,
    worktree_root: Path | str,
    progress_path: Path | str | None = None,
) -> tuple[TextEditorHandler, BashHandlerV2]:
    """Register OP-828 built-ins with OP-829 post-edit lint feedback."""
    text_editor = TextEditorHandler(worktree_root=worktree_root)
    linting_text_editor = wrap_text_editor_with_static_analysis(
        text_editor,
        worktree_root=worktree_root,
        progress_path=progress_path,
    )
    bash_v2 = BashHandlerV2(cwd=worktree_root)
    dispatcher.register("str_replace_based_edit_tool", linting_text_editor)
    dispatcher.register("bash", bash_v2)
    dispatcher.register("code_execution", ptc_sandbox_handler)
    return text_editor, bash_v2


def _mark_lint_partial_commit(worktree_path: Path, progress_path: Path) -> bool:
    """Prefix HEAD subject when the OP-829 lint cap was reached."""
    try:
        progress = progress_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "lint_partial=true" not in progress.splitlines():
        return False

    import subprocess

    current = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip()
    lines = current.splitlines() or ["lint_partial"]
    if "[lint_partial]" in lines[0]:
        return False
    lines[0] = f"[lint_partial] {lines[0]}"
    subprocess.run(
        ["git", "commit", "--amend", "-m", "\n".join(lines)],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return True


def _build_system_prompt(ticket_key: str, ticket_summary: str) -> str:
    """System prompt for the SDK runner. Pins scope discipline + W14.5 lesson."""
    sop_path = REPO / "docs" / "sop" / "implement_phase_step.md"
    sop_text = sop_path.read_text() if sop_path.is_file() else ""
    claude_md = REPO / "CLAUDE.md"
    claude_md_text = claude_md.read_text() if claude_md.is_file() else ""

    return (
        f"# Execution context\n"
        f"- PROJECT_ROOT: `{WORKTREE_PATH}` — Read/Write/Edit/Bash/Grep/Glob "
        f"refuse paths outside this root\n"
        f"- Working on JIRA ticket {ticket_key}: {ticket_summary}\n"
        f"- You are running unattended via the api-anthropic SDK launcher; "
        f"there is no operator to ask questions of\n\n"
        f"# 🚨 BASH TOOL RESTRICTIONS (CRITICAL — these characters are REJECTED) 🚨\n"
        f"The Bash tool runs WITHOUT a shell. The following characters are **REJECTED** "
        f"by the validator: `|`, `&`, `;`, `(`, `)`, `<`, `>`, `$`, `` ` ``, newline, CR.\n"
        f"This means you CANNOT use:\n"
        f"  ❌ `find . -name X.py | grep Y`        (no `|`)\n"
        f"  ❌ `cmd1 && cmd2`                       (no `&`)\n"
        f"  ❌ `python -c \"...\" > out.txt`         (no `>`)\n"
        f"  ❌ `cmd; cmd`                           (no `;`)\n"
        f"  ❌ `python -c \"$(cat file)\"`            (no `$`/backtick)\n"
        f"INSTEAD do this:\n"
        f"  ✅ Use the **Grep** tool for `grep` / `rg` searches (it's purpose-built)\n"
        f"  ✅ Use the **Glob** tool for `find -name` patterns\n"
        f"  ✅ Use the **Read** tool to read file contents (no `cat`)\n"
        f"  ✅ Make **multiple separate Bash calls** instead of pipelines\n"
        f"  ✅ Use the **Write** tool instead of `> file`\n"
        f"  ✅ Bash is for: `python -m pytest path/to/test.py -v`, `git log --oneline`, "
        f"`ls -la dir`, etc. — single foreground commands without redirection.\n\n"
        f"If you hit a Bash error about 'shell metacharacter', the validator rejected "
        f"the command. Re-read this restriction list and use the right tool.\n\n"
        f"# CRITICAL scope discipline (W14.5 lesson)\n"
        f"- The W14.5 incident burned $25 on two identical max_tokens retries. "
        f"DO NOT attempt work that would exceed your token budget.\n"
        f"- If you hit the same tool error 2+ times in a row, STOP and reconsider — "
        f"don't blindly retry the same broken tool call (this burns iterations).\n"
        f"- If the ticket Acceptance Criteria includes 5+ concrete deliverables "
        f"and you realise mid-implementation that completing all of them would "
        f"exceed ~300 lines of changes OR require 30+ tool calls, **halt early**: "
        f"finish whatever you've started cleanly, mark the rest as TODO in a "
        f"comment block within the file you were editing, and commit what's done.\n"
        f"- It is FAR better to ship 60% of the work cleanly than to attempt 100% "
        f"and produce broken/half-done code.\n"
        f"- Sign-off marker — when you're done, write the literal phrase "
        f"`✅ ITEM_DONE` (success) or `🛑 SCOPE_SURRENDERED <reason>` (partial) "
        f"as the LAST line of your final response.\n\n"
        f"# Implementation SOP\n{sop_text}\n"
        f"# Project rules (CLAUDE.md L1, immutable)\n{claude_md_text}\n"
    )


def _make_outcomes_grader(
    client: AnthropicClient | _DryRunClient,
    *,
    grader_model: str,
):
    """B16 (OP-847) — construct the grader callable for ``run_with_resets``.

    The grader issues one ``client.simple`` Haiku call, parses the JSON
    verdict line, and surfaces ``usage.input_tokens`` /
    ``usage.output_tokens`` so the orchestrator can thread the grader's
    cost through ``_post_call_cost_record`` (AC #4). Any parse / API
    failure raises ``OutcomesGraderUnavailable`` so the orchestrator
    degrades to pure B3 acceptance for that attempt (AC #1 error catalog).
    """

    async def _grade(rubric: str, result: RunResult) -> OutcomesVerdict:
        prompt = OUTCOMES_GRADER_PROMPT_TEMPLATE.format(
            rubric=rubric,
            final_text=(result.final_text or "")[:8000],
        )
        try:
            text, usage = await asyncio.to_thread(
                client.simple,  # type: ignore[union-attr]
                prompt=prompt,
                model=grader_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            raise OutcomesGraderUnavailable(
                f"grader API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        verdict_str, reasoning = _parse_grader_verdict(text)
        return OutcomesVerdict(
            verdict=verdict_str,
            grader_reasoning=reasoning,
            grader_input_tokens=usage.input_tokens,
            grader_output_tokens=usage.output_tokens,
        )

    return _grade


def _parse_grader_verdict(text: str) -> tuple[str, str]:
    """Extract ``{"verdict": ..., "grader_reasoning": ...}`` from a
    grader response. Tolerant of leading/trailing whitespace + extra
    text around the JSON line (some Haiku replies prepend a brief
    explanation despite the prompt). Returns
    ``(verdict, grader_reasoning)``; raises
    :class:`OutcomesGraderUnavailable` on any parse failure so the
    orchestrator falls back to pure B3 acceptance.
    """
    candidate = text.strip()
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        # Take the first {...} JSON object found by brace matching.
        start = candidate.find("{")
        end = candidate.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, dict):
        raise OutcomesGraderUnavailable(
            f"grader response did not contain JSON: {text[:200]!r}"
        )
    verdict = str(payload.get("verdict", "")).lower().strip()
    if verdict not in {"pass", "fail"}:
        raise OutcomesGraderUnavailable(
            f"grader verdict not in pass/fail: {verdict!r} (raw {text[:200]!r})"
        )
    reasoning = str(payload.get("grader_reasoning", ""))[:500]
    return verdict, reasoning


def _build_lesson_system_prompt(ticket_summary: str, ticket_description: str) -> str:
    """Build the OP-848 prior-lessons system-message block."""
    lessons = retrieve_lessons(
        WORKTREE_PATH / "docs" / "sop" / "lessons",
        ticket_title=ticket_summary,
        acceptance_criteria=ticket_description,
        top_k=3,
    )
    return build_lessons_system_message(lessons)


def _build_user_prompt(ticket_key: str, ticket_description: str) -> str:
    return (
        f"Implement JIRA ticket {ticket_key}.\n\n"
        f"=== Ticket description ===\n{ticket_description}\n\n"
        f"=== Your task ===\n"
        f"1. Read the relevant existing code (use Grep/Glob to find files matching "
        f"the ticket's Files / Paths section).\n"
        f"2. Implement the Acceptance Criteria. Stay within the declared file paths.\n"
        f"3. Write tests for any new logic (mirror the AC's evidence pattern).\n"
        f"4. Run the tests via Bash: `python3 -m pytest <test_file> -v`. They MUST pass.\n"
        f"5. After tests pass, write a final response ending with `✅ ITEM_DONE` and "
        f"a brief AC checklist showing which items are verified.\n"
        f"6. Do NOT commit — the launcher commits + pushes after you exit.\n"
        f"7. Do NOT update CLAUDE.md, HANDOFF.md, or memory rules.\n"
        f"8. Do NOT touch files outside the declared Files / Paths section "
        f"unless absolutely required (and document why in commit message).\n"
        f"\n"
        f"If you cannot fit the work in scope, write `🛑 SCOPE_SURRENDERED <reason>` "
        f"and we will reschedule with a smaller decomposition. **Do not attempt to "
        f"force-fit broken code through.**\n"
    )


def _locator_tdd_decision(ticket_description: str) -> str:
    """Temporary B7 locator stand-in for OP-834 conditional tickets."""
    text = ticket_description.lower()
    infra_markers = (
        "no testable behavior",
        "infra/scaffold",
        "scaffold only",
        "tooling only",
        "documentation only",
    )
    if any(marker in text for marker in infra_markers):
        return "bypass"
    return "active"


def _build_tdd_orchestrator(
    *,
    ticket_key: str,
    ticket_description: str,
    applicability: TDDApplicability,
) -> TDDOrchestrator:
    orchestrator = TDDOrchestrator(
        ticket_key=ticket_key,
        applicable=applicability.value,
    )
    if applicability.value == "conditional":
        orchestrator.record_locator_decision(
            _locator_tdd_decision(ticket_description)
        )
    return orchestrator


def _is_retryable(stop_reason: str | None) -> bool:
    """W14.5 lesson: max_tokens / max_iterations_exceeded are not retryable."""
    return stop_reason not in NON_RETRYABLE_STOP_REASONS


async def _preflight_cost_gate(
    *,
    guard: CostGuard,
    model: str,
    max_spend_usd: float,
) -> tuple[bool, str | None]:
    """Run the launcher's strict global cap gate for one SDK attempt."""
    estimate = await estimate_for(guard, model=model, in_tok=600_000, out_tok=30_000)
    spend_so_far = await cumulative_spend(guard)
    projected = spend_so_far + estimate.cost_usd_estimated
    if projected > max_spend_usd:
        return (
            False,
            f"hard cap: projected ${projected:.2f} > ${max_spend_usd:.2f}",
        )
    await guard.check(estimate, per_batch_observed_usd=spend_so_far)
    return True, None


def _should_escalate_structural_stop(
    *,
    ticket_key: str,
    stop_reason: str | None,
    escalation_counts: dict[str, int],
) -> bool:
    """Allow exactly one Sonnet -> Opus escalation per ticket."""
    if stop_reason not in STRUCTURAL_ESCALATION_STOP_REASONS:
        return False
    return escalation_counts.get(ticket_key, 0) < 1


def _detect_reflection_failure(
    *,
    worktree_path: Path,
    lint_progress_path: Path,
    tdd_orchestrator: TDDOrchestrator,
) -> ReflectionInput | None:
    """Probe B2 (lint) / B5 (TDD) surfaces for unresolved failures (OP-850 AC #1).

    Lint surface (B2): if ``LINT_PROGRESS_PATH`` reports ``lint_partial=true``,
    re-run static analysis on the diff vs. ``HEAD`` to recover fresh
    diagnostics for the structured payload.

    Test surface (B5): if the TDD orchestrator observed a red test and a
    subsequent source edit but never green-confirmed, treat the run as
    leaving a test failure unresolved.

    Returns ``None`` when neither surface reports a failure (the common
    happy-path case), so the caller can ``if rv is not None:`` cheaply.
    """
    try:
        lint_partial = "lint_partial=true" in lint_progress_path.read_text(encoding="utf-8")
    except OSError:
        lint_partial = False
    if lint_partial:
        import subprocess
        try:
            diff = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=worktree_path, capture_output=True, text=True, check=True, timeout=10,
            )
            changed = [f for f in diff.stdout.splitlines() if f.strip()]
        except (OSError, subprocess.SubprocessError):
            changed = []
        if changed:
            result = run_static_analysis(
                changed, worktree_root=worktree_path, progress_path=None,
            )
            payload = build_lint_reflection_input(result)
            if payload is not None:
                return payload

    state = tdd_orchestrator.state
    if state.source_edited and state.coder_unlocked and not state.green_confirmed:
        last_event = state.events[-1] if state.events else "no_events"
        return build_test_reflection_input(
            file=f"<tdd:{state.ticket_key}>",
            line=0,
            expected="green test run after source edits",
            actual=f"tdd orchestrator never green-confirmed (last_event={last_event})",
            traceback=state.last_error or "no captured traceback; rerun pytest to surface failure",
        )
    return None


async def process_ticket_full(
    *,
    client: AnthropicClient | _DryRunClient,
    guard: CostGuard,
    ticket_key: str,
    ticket_summary: str,
    ticket_description: str,
    model: str,
    per_ticket_cap_usd: float,
    max_spend_usd: float,
    max_iterations: int,
    log_outcome: Callable[[TicketOutcome], None],
    dry_run: bool = False,
    escalation_counts: dict[str, int] | None = None,
) -> str:
    """Full pipeline: sync worktree → JIRA In Progress → SDK invoke →
    push Gerrit → walk JIRA. Returns the outcome status string.

    On structural failure (max_tokens, max_iterations), retry once with
    Opus and doubled max_iterations. A second structural failure walks the
    ticket back to To Do.
    """
    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    escalation_counts = escalation_counts if escalation_counts is not None else {}

    # === Idempotency ===
    if has_existing_gerrit_ps(ticket_key):
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=started,
            status="skipped_existing_ps", cost_usd=0.0,
            input_tokens=0, output_tokens=0, iterations=0,
        ))
        return "skipped_existing_ps"

    # === Pre-flight cost gate ===
    allowed, block_reason = await _preflight_cost_gate(
        guard=guard, model=model, max_spend_usd=max_spend_usd,
    )
    if not allowed:
        finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="global_capped", cost_usd=0.0,
            input_tokens=0, output_tokens=0, iterations=0,
            error=block_reason,
        ))
        return "global_capped"

    # === JIRA: In Progress ===
    if not dry_run:
        jira_client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
        try:
            jira_dispatch.transition_to_in_progress(jira_client, ticket_key)
        except Exception as exc:
            return _abort_and_log(
                log_outcome, ticket_key, started, status="failed",
                error=f"transition to In Progress failed: {exc}",
            )

    # === Worktree sync ===
    if not dry_run:
        try:
            sync = jira_dispatch.sync_to_gerrit_develop(
                WORKTREE_PATH, LAUNCHER_AGENT_CLASS, ticket_key,
            )
            print(f"  [{ticket_key}] worktree synced: {sync.detail}")
        except Exception as exc:
            return _abort_and_log(
                log_outcome, ticket_key, started, status="failed",
                error=f"worktree sync failed: {exc}",
            )

    # === SDK invocation ===
    if dry_run:
        applicability = TDDApplicability(value="no", source="dry-run")
    else:
        applicability = fetch_tdd_applicability(jira_client, ticket_key)
    tdd_orchestrator = _build_tdd_orchestrator(
        ticket_key=ticket_key,
        ticket_description=ticket_description,
        applicability=applicability,
    )
    system_prompt = _build_system_prompt(ticket_key, ticket_summary)
    lesson_system_prompt = _build_lesson_system_prompt(ticket_summary, ticket_description)
    if lesson_system_prompt:
        print(f"  [{ticket_key}] injecting system message: ## Relevant prior lessons")
        system_prompt = f"{system_prompt}\n\n{lesson_system_prompt}\n"
    user_prompt = _build_user_prompt(ticket_key, ticket_description)
    attempt_model = model
    attempt_max_iterations = max_iterations
    attempt_cap_usd = per_ticket_cap_usd
    total_cost_usd = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_iterations = 0

    # OP-830 (B3) — per-ticket loop detector + ToM scratchpad. The
    # ``DetectorAwareDispatcher`` is swapped onto ``client.dispatcher``
    # for the duration of this ticket; ``run_with_resets`` drives the
    # context-reset attempt loop on top of each ``run_with_tools`` call.
    b3_detector = LoopDetector(ticket_key=ticket_key)
    b3_scratchpad = ToMScratchpad(
        progress_path=WORKTREE_PATH / ".runner" / f"progress-{ticket_key}.txt",
    )
    # OP-850 (B12) — task-level reflection counter; reuses B3 cap pattern
    # + JSONL persistence (AC #5). The counter lives next to the
    # scratchpad so a runner restart can rehydrate via ``.load(path)``.
    reflection_counter = ReflectionCounter(
        ticket_key=ticket_key,
        persistence_path=WORKTREE_PATH / ".runner" / f"reflection-{ticket_key}.jsonl",
    )
    inner_dispatcher = getattr(client, "dispatcher", None)
    if inner_dispatcher is not None and not dry_run:
        client.dispatcher = DetectorAwareDispatcher(  # type: ignore[union-attr]
            inner=inner_dispatcher, detector=b3_detector,
        )

    # OP-847 (B16) — load Outcomes config per-ticket. When the env flag
    # is unset (default) this returns ``enabled=False`` and the
    # orchestrator runs as pure B3. When set, the rubric is derived
    # from the JIRA ticket's ``## Acceptance criteria`` section (with
    # Goodhart guards in ``loop_detector.load_outcomes_config``).
    outcomes_config = load_outcomes_config(
        ticket_description=ticket_description,
    )
    outcomes_grader = (
        _make_outcomes_grader(client, grader_model=outcomes_config.grader_model)
        if outcomes_config.enabled else None
    )
    if outcomes_config.enabled:
        print(
            f"  [{ticket_key}] B16 outcomes-final-attempt ON "
            f"(grader={outcomes_config.grader_model}, "
            f"warnings={len(outcomes_config.rubric_warnings)})"
        )

    last_attempt_cost_usd = 0.0

    async def _record_attempt_usage(usage: TokenUsage) -> None:
        nonlocal total_cost_usd, total_input_tokens, total_output_tokens
        nonlocal last_attempt_cost_usd
        cost = await _post_call_cost_record(
            guard=guard, model=attempt_model, usage=usage,
        )
        last_attempt_cost_usd = cost
        total_cost_usd += cost
        total_input_tokens += usage.input_tokens
        total_output_tokens += usage.output_tokens

    try:
        with installed_tdd_guard(inner_dispatcher, tdd_orchestrator):
            while True:
                async def _runner(*, prompt: str) -> RunResult:
                    return await client.run_with_tools(  # type: ignore[union-attr]
                        prompt=prompt,
                        raw_tools=BUILT_IN_TOOLS_SPEC,
                        system=system_prompt,
                        model=attempt_model,
                        max_iterations=attempt_max_iterations,
                        enable_cache=True,
                        on_tool_call="log",
                    )

                try:
                    outcome = await run_with_resets(
                        runner=_runner,
                        detector=b3_detector,
                        scratchpad=b3_scratchpad,
                        first_user_message=user_prompt,
                        on_attempt_usage=_record_attempt_usage,
                        outcomes_config=outcomes_config,
                        outcomes_grader=outcomes_grader,
                    )
                except Exception as exc:
                    return _abort_and_log(
                        log_outcome, ticket_key, started, status="failed",
                        error=f"SDK call exception: {type(exc).__name__}: {exc}",
                    )

                finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

                if outcome.aborted_terminal:
                    # B3 AC #5 — 3 resets used, still looping. Hard surrender.
                    sig = outcome.aborted_signature
                    sig_text = sig.render() if sig is not None else "<unknown>"
                    if not dry_run:
                        _surrender_ticket(
                            ticket_key,
                            f"loop_aborted_terminal: {sig_text} repeated past "
                            f"{b3_detector.reset_limit} resets (B3 / OP-830).",
                        )
                    log_outcome(TicketOutcome(
                        ticket_key=ticket_key, started_at=started, finished_at=finished,
                        status="failed", cost_usd=total_cost_usd,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        iterations=total_iterations,
                        error=f"loop_aborted_terminal: {sig_text}",
                    ))
                    return "failed"

                assert outcome.final_result is not None  # not aborted => RunResult set
                result = outcome.final_result
                total_iterations += result.iterations
                actual_cost_usd = last_attempt_cost_usd

                # Per-attempt cap check (post-call; can't pre-empt mid-call).
                if actual_cost_usd > attempt_cap_usd:
                    if not dry_run:
                        _surrender_ticket(
                            ticket_key,
                            f"per-ticket cap ${attempt_cap_usd:.2f} exceeded "
                            f"for model={attempt_model} (actual ${actual_cost_usd:.4f}). "
                            f"Will not retry.",
                        )
                    log_outcome(TicketOutcome(
                        ticket_key=ticket_key, started_at=started, finished_at=finished,
                        status="ticket_capped", cost_usd=total_cost_usd,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        iterations=total_iterations,
                        error="per-ticket cap exceeded",
                    ))
                    return "ticket_capped"

                if _should_escalate_structural_stop(
                    ticket_key=ticket_key,
                    stop_reason=result.stop_reason,
                    escalation_counts=escalation_counts,
                ):
                    escalation_counts[ticket_key] = escalation_counts.get(ticket_key, 0) + 1
                    attempt_model = DEFAULT_MODEL_OPUS
                    attempt_max_iterations = max_iterations * 2
                    attempt_cap_usd = DEFAULT_STRUCTURAL_RETRY_CAP_USD
                    allowed, block_reason = await _preflight_cost_gate(
                        guard=guard, model=attempt_model, max_spend_usd=max_spend_usd,
                    )
                    if not allowed:
                        log_outcome(TicketOutcome(
                            ticket_key=ticket_key, started_at=started,
                            finished_at=finished, status="global_capped",
                            cost_usd=total_cost_usd, input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens, iterations=total_iterations,
                            error=block_reason,
                        ))
                        return "global_capped"
                    continue

                # === Stop reason classification (W14.5 lesson) ===
                if not _is_retryable(result.stop_reason):
                    if not dry_run:
                        _surrender_ticket(
                            ticket_key,
                            f"stop_reason={result.stop_reason} after "
                            f"{escalation_counts.get(ticket_key, 0)} Opus escalation(s) "
                            f"(structural — task too large, decompose needed).",
                        )
                    log_outcome(TicketOutcome(
                        ticket_key=ticket_key, started_at=started, finished_at=finished,
                        status="failed", cost_usd=total_cost_usd,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        iterations=total_iterations,
                        error=f"non-retryable stop_reason: {result.stop_reason}",
                    ))
                    return "failed"

                # === B12 (OP-850) reflection on B2/B5 unresolved failure ===
                # Task-level retry distinct from B3 call-level loop detection
                # (L-OP-843 disambiguation). Each reflection consumes 0.5 of
                # ``max_iterations`` (AC #4); per-type cap is 3 (AC #3).
                reflection_payload = (
                    None if dry_run else _detect_reflection_failure(
                        worktree_path=WORKTREE_PATH,
                        lint_progress_path=LINT_PROGRESS_PATH,
                        tdd_orchestrator=tdd_orchestrator,
                    )
                )
                if reflection_payload is not None:
                    failure_type = reflection_payload.failure_type
                    if reflection_counter.can_reflect(failure_type):
                        reflection_counter.record_reflection(failure_type)
                        user_prompt = (
                            f"{user_prompt}\n\n---\n\n{reflection_payload.to_user_turn()}"
                        )
                        attempt_max_iterations = (
                            reflection_counter.adjusted_max_iterations(max_iterations)
                        )
                        continue
                    _surrender_ticket(
                        ticket_key,
                        f"{REFLECTION_ERROR_CAP_EXCEEDED}: {failure_type} failures "
                        f"unresolved after {REFLECTION_LIMIT} reflections (B12 / OP-850).",
                    )
                    log_outcome(TicketOutcome(
                        ticket_key=ticket_key, started_at=started, finished_at=finished,
                        status="failed", cost_usd=total_cost_usd,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        iterations=total_iterations,
                        error=f"{REFLECTION_ERROR_CAP_EXCEEDED}: {failure_type}",
                    ))
                    return "failed"
                break
    finally:
        # Restore the underlying dispatcher so subsequent tickets do not
        # inherit this ticket's detector state.
        if inner_dispatcher is not None:
            client.dispatcher = inner_dispatcher  # type: ignore[union-attr]

    # === Detect surrender marker (model said it gave up cleanly) ===
    if "🛑 SCOPE_SURRENDERED" in result.final_text:
        if not dry_run:
            _surrender_ticket(
                ticket_key,
                f"Model surrendered scope: {result.final_text.split('SCOPE_SURRENDERED', 1)[-1][:200]}",
            )
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="surrendered", cost_usd=total_cost_usd,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            iterations=total_iterations,
            error="model called SCOPE_SURRENDERED",
        ))
        return "surrendered"

    # === Pre-commit critic (B4 / OP-833) ===
    # Read-only Haiku critic reviews the diff against the AC before push.
    # On 1st dissent the coder retries once; on 2nd dissent the launcher
    # labels the ticket ``under_review:critic_dissent`` and surrenders.
    # Critic NEVER posts to Gerrit (AC #6) — the verdict lives in the
    # commit-message footer + a JIRA comment.
    if not dry_run:
        critic_outcome = await _run_critic_phase(
            client=client,
            ticket_key=ticket_key,
            ticket_description=ticket_description,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            base_ref=sync.develop_sha,
        )
        if critic_outcome.escalated:
            _surrender_with_critic_dissent(ticket_key, critic_outcome)
            log_outcome(TicketOutcome(
                ticket_key=ticket_key, started_at=started, finished_at=finished,
                status="critic_dissent", cost_usd=total_cost_usd,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                iterations=total_iterations,
                error=f"critic_dissent: {critic_outcome.final_verdict.reason_code.value}",
            ))
            return "critic_dissent"
        _append_critic_footer(WORKTREE_PATH, critic_outcome.final_verdict)
    else:
        critic_outcome = None

    # === Push to Gerrit + walk JIRA ===
    if dry_run:
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="ok", cost_usd=total_cost_usd,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            iterations=total_iterations,
            gerrit_url="dry-run",
        ))
        return "ok"

    try:
        _mark_lint_partial_commit(WORKTREE_PATH, LINT_PROGRESS_PATH)
        jira_dispatch.ensure_change_ids(WORKTREE_PATH, base_ref=sync.develop_sha)
        push = jira_dispatch.push_to_gerrit_for_review(
            WORKTREE_PATH, LAUNCHER_AGENT_CLASS, target="develop",
        )
    except Exception as exc:
        _surrender_ticket(ticket_key, f"Gerrit setup failed: {type(exc).__name__}: {exc}")
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="failed", cost_usd=total_cost_usd,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            iterations=total_iterations,
            error=f"gerrit setup: {exc}",
        ))
        return "failed"

    if not push.success:
        _surrender_ticket(ticket_key, f"Gerrit push rejected: {push.detail[:200]}")
        log_outcome(TicketOutcome(
            ticket_key=ticket_key, started_at=started, finished_at=finished,
            status="failed", cost_usd=total_cost_usd,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            iterations=total_iterations,
            error=f"push fail: {push.detail[:200]}",
        ))
        return "failed"

    # === Success path: post AC verification + transition Under Review ===
    critic_jira_block = (
        f"\n\n{critic_outcome.final_verdict.to_jira_comment()}"
        if critic_outcome is not None else ""
    )
    try:
        jira_dispatch.add_comment(
            jira_client, ticket_key,
            f"[ai-implemented 2026-05-09 SDK launcher] {result.final_text[:1500]}\n\n"
            f"Gerrit: {push.change_url}{critic_jira_block}",
        )
        jira_dispatch.transition_to_under_review(
            jira_client, ticket_key, push.change_url,
        )
    except Exception as exc:
        # Soft-fail — PS is up; JIRA walk can be done by operator/bridge.
        print(f"  [{ticket_key}] post-push JIRA update failed: {exc} (PS still landed)")

    log_outcome(TicketOutcome(
        ticket_key=ticket_key, started_at=started, finished_at=finished,
        status="ok", cost_usd=total_cost_usd,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        iterations=total_iterations,
        gerrit_url=push.change_url,
    ))
    return "ok"


def _abort_and_log(
    log_outcome: Callable, ticket_key: str, started: str, *,
    status: str, error: str,
) -> str:
    finished = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    log_outcome(TicketOutcome(
        ticket_key=ticket_key, started_at=started, finished_at=finished,
        status=status, cost_usd=0.0,
        input_tokens=0, output_tokens=0, iterations=0,
        error=error,
    ))
    return status


def _surrender_ticket(ticket_key: str, reason: str) -> None:
    """Walk a ticket back to To Do + post explanatory comment so the
    operator (or a future tick) can pick it up cleanly. Failures here are
    swallowed — JIRA being down shouldn't crash the batch run."""
    try:
        jira_client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
        jira_dispatch.transition_back_to_todo(
            jira_client, ticket_key,
            f"[sdk-launcher 2026-05-09] {reason}",
        )
    except Exception as exc:
        print(f"  [{ticket_key}] surrender failed: {exc}")


# ── Critic phase (B4 / OP-833) ────────────────────────────────────────


CRITIC_DISSENT_LABEL = "under_review:critic_dissent"


class _AnthropicCriticBackend:
    """Adapter wrapping ``AnthropicClient.simple`` (sync) for the critic.

    The critic is intentionally a single-shot classifier; it does NOT
    take the ToM scratchpad / loop detector path. We wrap the sync call
    in ``asyncio.to_thread`` so the timeout in :func:`review_once` works
    uniformly across real and mock backends.
    """

    def __init__(self, client: AnthropicClient) -> None:
        self._client = client

    async def invoke(self, *, prompt: str, model: str) -> str:
        text, _usage = await asyncio.to_thread(
            self._client.simple, prompt=prompt, model=model,
        )
        return text


def _git(*args: str, cwd: Path) -> str:
    """Run a read-only git subcommand inside the worktree."""
    import subprocess
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return r.stdout


def _compute_diff(worktree: Path, base_ref: str) -> str:
    """Return ``base_ref..HEAD`` diff, capped to keep the critic prompt sane."""
    try:
        out = _git("diff", f"{base_ref}..HEAD", cwd=worktree)
    except Exception as exc:  # noqa: BLE001
        print(f"  [critic] _compute_diff failed: {exc} — using empty diff")
        return ""
    # Anthropic prompt limits + cost discipline — truncate huge diffs.
    if len(out) > 200_000:
        out = out[:200_000] + "\n\n[... diff truncated at 200k chars ...]\n"
    return out


def _append_critic_footer(worktree: Path, verdict: CriticVerdict) -> None:
    """Amend HEAD's commit message with the critic verdict footer (AC #7).

    Uses ``git commit --amend --no-edit`` style append. Failures are
    swallowed (best-effort metadata) so the push still proceeds — the
    same verdict is also posted to JIRA, so the audit trail is intact.
    """
    import subprocess
    try:
        current = _git("log", "-1", "--format=%B", cwd=worktree).rstrip()
        footer = verdict.to_commit_footer()
        if footer in current:
            return
        new_message = f"{current}\n\n{footer}\n"
        subprocess.run(
            ["git", "commit", "--amend", "-m", new_message],
            cwd=str(worktree), capture_output=True, text=True, check=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [critic] footer amend failed: {exc} (verdict still in JIRA)")


async def _run_critic_phase(
    *,
    client: AnthropicClient | _DryRunClient,
    ticket_key: str,
    ticket_description: str,
    user_prompt: str,
    system_prompt: str,
    base_ref: str,
) -> CriticReviewOutcome:
    """Run the AC #4 dissent protocol against the freshly-committed diff.

    The coder retry callback re-invokes ``client.run_with_tools`` once
    with the critic dissent feedback prepended to the user prompt, then
    amends the existing HEAD commit so the critic re-reviews a single
    consolidated diff.
    """
    diff = _compute_diff(WORKTREE_PATH, base_ref)
    backend: CriticBackend = _AnthropicCriticBackend(client)  # type: ignore[arg-type]

    async def _coder_retry(verdict: CriticVerdict) -> str:
        feedback = (
            "\n\n=== CRITIC DISSENT (1 free retry) ===\n"
            f"reason_code: {verdict.reason_code.value}\n"
            f"reason_text: {verdict.reason_text}\n"
            "Address this concern; the launcher will amend the existing "
            "commit. Do NOT call git commit yourself."
        )
        try:
            await client.run_with_tools(  # type: ignore[union-attr]
                prompt=user_prompt + feedback,
                raw_tools=BUILT_IN_TOOLS_SPEC,
                system=system_prompt,
                model=DEFAULT_MODEL_SONNET,
                max_iterations=DEFAULT_MAX_ITERATIONS,
                enable_cache=True,
                on_tool_call="log",
            )
            # Amend any newly-staged or unstaged changes into HEAD so the
            # critic re-reviews one consolidated commit.
            import subprocess
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(WORKTREE_PATH), capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "commit", "--amend", "--no-edit", "--allow-empty"],
                cwd=str(WORKTREE_PATH), capture_output=True, text=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [critic] coder retry failed: {exc} — escalating")
            raise
        return _compute_diff(WORKTREE_PATH, base_ref)

    return await review_with_dissent_protocol(
        backend,
        diff=diff,
        ac_text=ticket_description,
        coder_retry=_coder_retry,
        model=resolve_critic_model(),
        timeout_s=CRITIC_TIMEOUT_SECONDS,
    )


def _surrender_with_critic_dissent(
    ticket_key: str, outcome: CriticReviewOutcome,
) -> None:
    """Label + surrender the ticket on a 2nd critic dissent (AC #4)."""
    try:
        jira_client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
        jira_dispatch.add_label(jira_client, ticket_key, CRITIC_DISSENT_LABEL)
        history_block = "\n".join(
            f"- round {i}: {v.verdict} ({v.reason_code.value}) — {v.reason_text}"
            for i, v in enumerate(outcome.history, start=1)
        )
        jira_dispatch.add_comment(
            jira_client, ticket_key,
            f"[critic-agent escalated] 2 dissent rounds without convergence.\n"
            f"{history_block}\n\nTicket needs operator review before re-pickup.",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [{ticket_key}] critic dissent escalation surface failed: {exc}")
    _surrender_ticket(
        ticket_key,
        f"critic_dissent: {outcome.final_verdict.reason_code.value} — "
        f"{outcome.final_verdict.reason_text[:200]}",
    )


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
    p.add_argument("--per-ticket-cap", type=float, default=5.0,
                   help="Per-ticket budget in USD (default 5; lower than the "
                        "$10 that burned in W14.5).")
    p.add_argument("--pilot", type=str, default=None,
                   help="If set, process only this ticket key.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do not call the real Anthropic API; do not push.")
    p.add_argument("--simple", action="store_true",
                   help="Use the v1 narrow process_ticket() instead of the "
                        "full JIRA pipeline (test/debug only).")
    p.add_argument("--model", default=DEFAULT_MODEL_SONNET)
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--stop-loss-pct", type=float, default=70.0,
                   help="Halt batch when cumulative spend reaches this percent "
                        "of --max-spend (default 70). Operator can resume "
                        "with a higher --max-spend if everything looks healthy.")
    p.add_argument("--max-consecutive-failures", type=int, default=3,
                   help="Halt if this many tickets in a row fail or surrender "
                        "(default 3).")
    return p.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    guard = CostGuard(store=InMemoryCostStore())
    await install_global_cap(guard, args.max_spend)
    build_lesson_index(WORKTREE_PATH / "docs" / "sop" / "lessons")

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
        # OP-828 (B1): the launcher uses Anthropic built-in tools, so the
        # PTC sandbox needs OMNISIGHT_WORKTREE_PATH set in env (AC #2 — the
        # sandbox refuses to launch otherwise). Surface the precondition
        # here rather than letting the first ticket's tool call fail
        # mid-flight with a confusing structured error.
        if not os.environ.get(WORKTREE_ENV_VAR):
            os.environ[WORKTREE_ENV_VAR] = str(WORKTREE_PATH)

        # Build a real tool dispatcher so the model's Read/Write/Edit/Bash/
        # Grep/Glob calls actually resolve to handlers (without this the
        # SDK returns `no_handler_registered` for every tool call and the
        # model surrenders immediately — pilot run lesson 2026-05-09).
        dispatcher = make_runner_dispatcher()
        # OP-828 (B1): also register the Anthropic built-in tool names so
        # the dispatcher can locally round-trip text_editor / bash / PTC
        # calls when running outside the hosted sandbox (tests, dry-run
        # parity, future vendor-fallback paths).
        bind_built_in_tools_with_static_analysis(
            dispatcher,
            worktree_root=WORKTREE_PATH,
            progress_path=LINT_PROGRESS_PATH,
        )
        client = AnthropicClient(api_key=_load_api_key(), dispatcher=dispatcher)
        # OP-811 (A3): wire Skill (project verbs) + Agent (sub-agent decomposition)
        # onto the dispatcher AFTER the client exists so the Agent handler can
        # bind to the same client (Anthropic SDK contract: sub-agents share the
        # parent's dispatcher so sandbox + tool surface stay identical).
        # ``load_default_scopes`` needs the project root to discover the three
        # skill scopes (project / home / bundled) — we pass ``REPO`` defined
        # at module top.
        skill_registry: SkillRegistry = load_default_scopes(REPO)
        dispatcher.register("Skill", make_skill_handler(skill_registry))
        dispatcher.register("Agent", make_agent_tool_handler(client=client))

    if args.pilot:
        ticket_keys = [args.pilot]
    else:
        ticket_keys = _fetch_pickable_keys()

    print(f"=== S1 SDK launcher — {len(ticket_keys)} ticket(s), cap ${args.max_spend:.2f} ===")
    print(f"=== mode: {'simple (v1)' if args.simple else 'full JIRA pipeline (v2)'}{', dry-run' if args.dry_run else ''} ===")
    consecutive_failures = 0
    stop_loss_threshold = args.max_spend * (args.stop_loss_pct / 100.0)
    escalation_counts: dict[str, int] = {}

    for key in ticket_keys:
        if args.simple:
            description = _fetch_description_or_empty(key)
            status = await process_ticket(
                client=client, guard=guard, ticket_key=key,
                description=description, model=args.model,
                per_ticket_cap_usd=args.per_ticket_cap,
                max_spend_usd=args.max_spend,
                max_iterations=args.max_iterations,
                log_outcome=_log,
            )
        else:
            summary = _fetch_summary_or_key(key)
            description = _fetch_description_or_empty(key)
            status = await process_ticket_full(
                client=client, guard=guard, ticket_key=key,
                ticket_summary=summary, ticket_description=description,
                model=args.model,
                per_ticket_cap_usd=args.per_ticket_cap,
                max_spend_usd=args.max_spend,
                max_iterations=args.max_iterations,
                log_outcome=_log,
                dry_run=args.dry_run,
                escalation_counts=escalation_counts,
            )

        # Stop-loss handling
        spend = await cumulative_spend(guard)
        print(f"    cumulative spend: ${spend:.2f} / ${args.max_spend:.2f} ({spend/args.max_spend*100:.1f}%)")
        if status in ("ok", "skipped_existing_ps"):
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(f"    consecutive failures: {consecutive_failures}/{args.max_consecutive_failures}")

        if spend >= args.max_spend:
            print(f"=== 🛑 GLOBAL CAP HIT at ${spend:.2f} — halting batch ===")
            break
        if spend >= stop_loss_threshold:
            print(
                f"=== ⚠️  STOP-LOSS at ${spend:.2f} ({args.stop_loss_pct:.0f}% of cap) — "
                f"halting batch. Re-run with higher --max-spend or higher "
                f"--stop-loss-pct if everything looks healthy. ==="
            )
            break
        if consecutive_failures >= args.max_consecutive_failures:
            print(
                f"=== 🛑 CONSECUTIVE-FAILURE STOP-LOSS — {consecutive_failures} "
                f"tickets failed/surrendered in a row. Halting batch. ==="
            )
            break

    log_fh.close()
    print(f"=== DONE — log at {log_path} ===")
    return 0


def _fetch_summary_or_key(ticket_key: str) -> str:
    try:
        client = jira_dispatch.make_client(LAUNCHER_AGENT_CLASS)
        issue = jira_dispatch._request(client, "GET", f"/issue/{ticket_key}?fields=summary")
        return issue["fields"].get("summary", ticket_key)
    except Exception:
        return ticket_key


def _load_api_key() -> str:
    """Load the Anthropic API key from operator-provisioned file.

    Pre-flight (Stage B) saves the key to ``~/.config/omnisight/anthropic-api-key``
    by pulling from the running backend container's env (which itself reads
    from the encrypted llm_credentials table or the legacy Settings shim).
    The file is mode 0600.
    """
    path = os.path.expanduser("~/.config/omnisight/anthropic-api-key")
    if not os.path.isfile(path):
        raise RuntimeError(
            f"No API key at {path} — run pre-flight first to provision."
        )
    with open(path) as f:
        key = f.read().strip()
    if not key.startswith("sk-ant-"):
        raise RuntimeError(f"Key at {path} does not look like an Anthropic key")
    return key


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
