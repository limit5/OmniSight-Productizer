---
id: L-OP-802
ticket: OP-802
title: api-anthropic SDK launcher pilots failed — model can't internalize Bash metachar restriction
date: 2026-05-09
tags: [sdk, anthropic, runner-pipeline, prompt-engineering, cost-overrun-prevented]
---

# api-anthropic SDK launcher pilots failed — model can't internalize Bash metachar restriction

**Situation**: 2026-05-09 02:50-03:05+08, executing the 14 remaining S1 (`MP v0.4.0`) placeholder tickets via `scripts/run_s1_via_anthropic_sdk.py` (Phase 1 launcher merged as Gerrit #312). Phase 0 pre-flight cleared (API key OK, JIRA + Gerrit reachable, MCP imports clean, live haiku ping cost $0.00005). Stage E pilot on the smallest ticket OP-32 (~30 LOC quota integration) was attempted three times:

1. **Pilot v1** — launcher v1 (no dispatcher wired). Model surrendered immediately: "no_handler_registered" for every tool. Cost $0.08.
2. **Pilot v2** — launcher v2 with `make_runner_dispatcher()` wired. Model called `find ... | grep ...` style Bash, hit `_validate_bash_command`'s `_SHELL_METACHARS = ('|','&',';','(',')','<','>','$','`','\\n','\\r')` reject, and **kept retrying the same broken command pattern** until `max_iterations=40` exhausted. Cost $1.12, 0 PSes.
3. **Pilot v3** — launcher v2 with explicit prompt instructions:
   - Listed all 11 forbidden chars
   - Showed ❌ examples (`find . -name X | grep Y`, `cmd && cmd`, `python -c "..." > out.txt`)
   - Showed ✅ alternatives (use Grep tool, Glob tool, multiple Bash calls, Write tool)
   - Added "if you hit the same tool error 2+ times in a row, STOP and reconsider"

   Result: **identical failure**. Same `find ... | grep ...` pattern, same validator reject, same retry loop, max_iterations exhausted. Cost $1.00, 0 PSes.

   The model received the error message 3+ times in the conversation but did not change strategy.

**Fix**: Pivot from `class:api-anthropic` → `class:subscription-codex` for all 14 S1 placeholder tickets. The codex CLI has full shell access via tmux (no `_SHELL_METACHARS` validator on its Bash) so it doesn't hit this gap. Ticket descriptions were already concretely refined (per-ticket LOC budget, AC list, file paths, scope-surrender condition) so codex picks them up runner-ready. No incremental API spend — uses the existing subscription. Total api-anthropic spend frozen at **$2.20 / $100 cap** (3 pilot attempts including dry-run).

**Verification**: 14 tickets visible in codex pickable JQL within 30s of label swap; codex-runner-loop + codex-runner-loop-2 (both alive) + restored claude-runner-loop pick them up across the next ~10 ticks per the existing fleet pattern. Final api-anthropic invoice should reconcile against $2.20 ± $0.10 (cost-guard recorded $2.21; small drift expected from estimator vs Anthropic billing rounding).

**Generalisation**: The api-anthropic SDK path has a **structural prompt-engineering ceiling** for tasks that require shell-style operations. Sonnet 4.6's tool-use loop will repeatedly retry a failing Bash invocation pattern even when:

* The system prompt explicitly forbids the failed pattern with concrete examples
* The error message is descriptive ("shell metacharacter '|' is not allowed; split into separate calls")
* The model has been told (via prompt) to halt on repeated tool errors
* `max_iterations` bound is in place

Two takeaways for any future SDK-driven runner work in this repo:

1. **Either loosen the Bash handler** (allow shell metachars inside the `BASE_DIR`-scoped wrapper — accept the audit-B4 RCE risk in exchange for SDK runner viability) **OR keep the handler strict and accept that the SDK runner is unsuitable for tasks with shell-style operations**. There is no third option that holds; prompt engineering alone does not work for this class of structural tool restriction.

2. **Always run a structural-failure pilot at < $5/ticket cap before scaling to a batch**. The W14.5 incident's $25 burn would be repeated at fleet scale (~$28-70 if 14 tickets each hit the same loop) without the per-ticket cap + max_iterations + non-retryable-stop classification + surrender path that OP-802 / OP-795 hardening built. **Budget burn was contained to $2.20 specifically because** the launcher's circuit breakers fired exactly as designed (max_iterations stop → non-retryable stop_reason → surrender ticket → halt batch).

**Where the launcher itself proved useful**: the v2 pipeline's surrender + walk-back-to-To-Do path correctly returned OP-32 to a clean state after each failed pilot. The 14 refined ticket descriptions from this run also remain valid for the codex CLI execution path — the refinement work was not wasted.
