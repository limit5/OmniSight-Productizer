# Budget Strategies — the 4 cards in the Budget Strategy panel

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — for PMs

Budget Strategy picks **how expensive each agent invocation is
allowed to be**. Four presets tuned for different jobs; no custom
strategy support (yet — file an issue if you need one).

| Strategy | When to pick it | One-line cost / quality trade-off |
|---|---|---|
| **QUALITY** | Critical release, safety-certified firmware | Top-tier model, 3 retries, no auto-downgrade — most accurate, most spend |
| **BALANCED** | Default daily development | Default-tier model, 2 retries, downgrade at 90 % daily token usage |
| **COST_SAVER** | Exploratory work, side projects, experimentation | Budget-tier model, 1 retry, downgrade as early as 70 % |
| **SPRINT** | Demo crunch, deadline push | Default tier, 2 retries, prefers parallel execution |

Switching is instant and fans out to every connected browser via the
`budget_strategy_changed` SSE event.

## The 5 tuning knobs

Each strategy is a frozen combination of five knobs. You can read the
current values live in the bottom strip of the Budget Strategy panel.

| Knob | Range | What it does |
|---|---|---|
| **TIER** | `premium` / `default` / `budget` | Picks which model rung the provider chain uses first. `premium` → the strongest model the provider offers; `budget` → the cheapest. Provider config maps concrete models per tier. |
| **RETRIES** | 0 – 5 | How many times an agent retries after a transient LLM error (rate limit, 5xx) before giving up on this attempt. |
| **DOWNGRADE** | 0 – 100 % | Daily token-budget threshold at which the system auto-switches to a cheaper tier for the remainder of the day. |
| **FREEZE** | 0 – 100 % | Threshold at which all non-critical LLM calls are frozen and further agent work blocks on explicit operator approval. |
| **PARALLEL** | YES / NO | Whether the orchestrator eagerly parallelises independent agents (SPRINT prefers yes). |

`DOWNGRADE < FREEZE` — freezing is the stricter stop. If both are set
to 100 %, neither kicks in.

## The 4 strategies in detail

### QUALITY
- TIER=premium · RETRIES=3 · DOWNGRADE=100 % · FREEZE=100 % · PARALLEL=NO
- **Use for**: anything shipping to a paying customer, safety
  reviews, final firmware builds.
- **Don't use for**: rapid iteration — spend per task is highest and
  premium models are typically the slowest.

### BALANCED (default)
- TIER=default · RETRIES=2 · DOWNGRADE=90 % · FREEZE=100 % · PARALLEL=NO
- **Use for**: everyday work. Hits the sweet spot of quality and
  cost; if you burn through 90 % of the daily budget the system
  quietly drops to budget-tier to carry you through.
- **Don't use for**: releases where the 10 % downgrade zone would
  risk quality regression.

### COST_SAVER
- TIER=budget · RETRIES=1 · DOWNGRADE=70 % · FREEZE=95 % · PARALLEL=NO
- **Use for**: exploratory coding, side projects, manual QA scripts.
- **Don't use for**: anything customer-facing. Budget-tier models
  miss edge cases that premium catches, and a single retry means
  transient failures surface to you as hard errors.

### SPRINT
- TIER=default · RETRIES=2 · DOWNGRADE=95 % · FREEZE=100 % · PARALLEL=YES
- **Use for**: deadline crunch, demo prep, parallel refactor passes.
  The `prefer_parallel=YES` flag tells the scheduler to saturate the
  MODE parallel cap (so FULL AUTO = 4 concurrent agents, TURBO = 8).
- **Don't use for**: low-parallelism tasks with strict ordering — the
  scheduler may run child tasks ahead of their parent if they don't
  declare a dependency.

## Interaction with MODE

Budget Strategy and Operation Mode are orthogonal:

- MODE decides **who approves** (you vs the AI).
- Budget Strategy decides **how expensive** the AI's decisions are.

Common pairings:

| MODE × Strategy | When it makes sense |
|---|---|
| SUPERVISED × BALANCED | Daily default — AI does routine, you approve risky, default model |
| TURBO × SPRINT | Weekend batch refactor — max parallelism, max autonomy |
| MANUAL × QUALITY | Final release review — human in every loop, premium model |
| FULL AUTO × COST_SAVER | Exploratory prototype — AI pushes ahead, cheap model |

## Token budget interaction

The DOWNGRADE and FREEZE thresholds are read against the daily LLM
token budget (configured via `OMNISIGHT_LLM_TOKEN_BUDGET_DAILY`). An
SSE `token_warning` event fires at 80 / 90 / 100 %; the Budget
Strategy tuning decides whether those trigger an auto-downgrade.

## Who can change strategy

Like mode, the PUT endpoint (`/api/v1/budget-strategy`) is behind
`OMNISIGHT_DECISION_BEARER` if that env var is set, and is rate-limited
to 30 requests / 10 s per client IP.

## Under the hood

- Backend: `backend/budget_strategy.py` · `_TUNINGS` is the 4-row
  frozen dict above. `set_strategy()` emits `budget_strategy_changed`.
- Frontend: `components/omnisight/budget-strategy-panel.tsx` · 4
  cards + 5 knob cells (TuningCell) + SSE sync.
- Event: `SSEBudgetStrategyChanged` in `backend/sse_schemas.py`.

## Related reading

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md) — severity tags work the
  same regardless of budget
- [Troubleshooting](../troubleshooting.md) — if the panel shows a red
  error banner
