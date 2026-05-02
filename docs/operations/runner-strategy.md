# Runner Strategy & Phase Roadmap

> **Status**: Living strategy doc (last reviewed 2026-05-02)
> **Owner**: project lead + LLM dev partner
> **Companion**: [`auto-runner.py`](../../auto-runner.py) (subscription
> CLI version), [`auto-runner-sdk.py`](../../auto-runner-sdk.py) (Anthropic
> API version)

---

## What this doc is

The OmniSight repo ships **two runner scripts** that drive the same
TODO / HANDOFF / SOP contract through different LLM execution layers:

  * `auto-runner.py` — wraps Claude Code CLI (subscription quota)
  * `auto-runner-sdk.py` — drives the Anthropic Messages API directly
    (per-token billing, full programmatic control)

Both are dev tools today, but the **runner concept** itself outlives
the scripts. This doc captures how the runner relates to the rest of
OmniSight and what discipline keeps runner improvements aligned with
product goals (rather than runner perfectionism).

---

## The three branches the runner becomes

The runner is **not** just a dev tool. It already plays three roles
that diverge over time:

### Branch 1 — User-facing Orchestrator (future, becomes product)

When OmniSight ships to users, the user-facing "tell my agent to do
this task" interface is **conceptually identical** to the runner. Each
runner capability today is a feature the user-facing Orchestrator will
need:

| Runner Phase | Future user-facing OmniSight feature |
|---|---|
| Phase 1 — MCP integration | User plugin marketplace (Figma / Drive / etc.) |
| Phase 2 — Skills loader (3-scope) | User skill packs + skill discovery / promotion |
| Phase 3 — Sub-agent dispatch | Agent guild / specialist routing visible to user |
| Phase 4 — Multi-rule memory walker | Per-tenant rule layer (org / team / project policy) |
| Phase 5 — Fail-fast classification | "Task failed for X reason — try Y instead" UX |
| Phase 7 — Stop-on-failure (planned) | Graceful degradation + escalation pattern |
| (Future) Cost projection | User quota / billing / spend visibility |
| (Future) Self-tuning | Adaptive workflow optimization |

**Implication**: each runner Phase is **pre-paid design work** for the
future product surface. When done well, every Phase converts to an
OmniSight feature spec for free.

### Branch 2 — Developer dev/debug tool (current, fades)

The CLI scripts (`auto-runner.py` / `auto-runner-sdk.py`) are the
visible "runner" today. They will retire when OmniSight has its own
Orchestrator UI. Until then, they should stay simple — they are
disposable Phase-A wrappers, not durable infrastructure.

### Branch 3 — Internal agent runtime (already real, load-bearing)

`backend/agents/*` is the **runtime that every internal LLM call goes
through** — HD agent, BSP agent, HAL agent, Architect, Orchestrator,
specialist nodes, etc. The runner CLIs are just one consumer of this
library. OmniSight backend services consume it too. **This is the
most load-bearing branch** because it runs every second the system is
serving requests, not just when a developer types `python auto-runner-sdk.py`.

**Implication**: when adding capability, prefer adding to
`backend/agents/*` (durable, all 3 branches benefit) over adding to
the runner script (disposable, Branch 2 only).

---

## The "mirror" property

> Every weakness the runner exposes today is a weakness the user-facing
> Orchestrator will expose tomorrow. Every problem we solve in the
> runner is a partial solution for the product.

Concretely:

  * Runner blowing $25 on `max_tokens` retry → product would do the
    same to a paying user. **Phase 5 (fail-fast)** prevents both.
  * Runner continuing past a failed item with unmet dependencies →
    product would surface broken downstream tasks to user. **Phase 7
    (stop-on-failure)** prevents both.
  * Runner not knowing CLAUDE.md trailer rule → product would violate
    org-level commit policies. **Phase 4 (multi-rule memory)** fixes
    both.

When prioritising runner work, this filter applies:

> **Does this teach us something we'll definitely need for the user-
> facing product, or is it runner-only optimization?**

If the latter, defer.

---

## Phase status (2026-05-02)

| Phase | What | Status |
|---|---|---|
| 1 | MCP integration via env-token registry | ✅ shipped (`f860b6ce`) |
| 2 | 3-scope Skills loader + lazy `Skill` tool | ✅ shipped (`ef244903`) |
| 3 | Sub-agent (`Agent` tool, 3 default types) | ✅ shipped (`b563a7d6`) |
| 4 | Multi-rule memory walker | ✅ shipped (`4eeaa2c9`) |
| 5 | Fail-fast classification (max_tokens / max_iter / per-item cap) | ✅ shipped (`7d6214fe`) |
| 6 | Auto-decompose + 3-gate cost projection | 🔴 **deferred indefinitely** |
| 7 | Stop-on-failure default + structured HANDOFF stop reason | 🟡 next |

### Why Phase 6 is deferred

Phase 6's thresholds (when to defer, max steps, max projected cost)
are **guesses without empirical data**. The W14.5 / W15.1 / W15.2
failures gave us 3 sample points; a real Phase 6 needs 10-20 stop
samples across diverse TODO sections to calibrate. Doing Phase 6 first
== guessing.

**Order**: Phase 7 first → accumulate stop samples in real runs →
revisit Phase 6 only if data shows clear pattern. Possibly never if
manual decomposition + subscription-CLI fallback covers most cases.

### After Phase 7: passive improvement mode

Runner does NOT proactively get Phase 8+. Trigger for resuming runner
work is:

  * Real recurring pain during OmniSight main-feature dev (e.g., HD
    agent needs feature X, runner is the natural place to prototype X)
  * Operator (human) repeatedly hitting the same workaround
  * Stop sample data accumulates a clear unsolved class

Not: hypothetical futures, "wouldn't it be nice", premature generality.

---

## Time budget tradeoff

Every hour spent on runner improvements is an hour not spent on
production-readiness work (multi-tenant, billing, audit, UI, public
API, migration story for existing users). The 1-2 days spent on Phase
1-5 + W14/W15 dry-runs cost 2-3 KS / W14 epic items of progress.

**Discipline**: treat runner as 90% sufficient for Phase A. The
remaining 10% perfectionism does not justify deferring main-feature
work indefinitely.

---

## Multi-agent collaboration (Claude × Codex × human, 2026-05-02)

The repo now ships **three** runner scripts:

| Script | LLM driver | Auth | Best for |
|---|---|---|---|
| `auto-runner.py` | Claude Code CLI (`claude -p`) | Anthropic subscription (Max 20x) | Multi-subsystem integration; Claude's strength |
| `auto-runner-sdk.py` | Anthropic Messages API (programmatic) | API key, per-token billing | Pattern-bounded single-file work, batchable items, observable cost |
| `auto-runner-codex.py` | OpenAI Codex CLI (`codex exec`) | OpenAI ChatGPT subscription (Pro 5x) | Pattern-replicated work where GPT-class structuring shines |

These coexist by design — different sweet spots, complementary capacity.
The companion docs that govern their coexistence:

  * **`AGENTS.md`** (project root) — Codex's L1 rule layer (mirrors
    `CLAUDE.md`'s structure but adds GPT-specific strict rules:
    "mirror existing patterns, ask, do not invent" / scope discipline /
    retreat when uncertain / Tier marker / TODO+HANDOFF prefix).
  * **`coordination.md`** (project root) — section ownership matrix
    (Claude vs Codex), Tier A/B classification, worktree layout,
    same-branch parallel-runner safety contract.

### Section ownership (default)

  * **Claude**: agent infra (`backend/agents/*`), security (`KS.*`),
    cross-subsystem epics (`BP.A`/`B`/`C`/`F`/`H`), web sandbox
    (`W11-W16`), domain-specific (`HD.*`), new architecture (`BP.A2A`,
    `BP.Q`, `BP.W3.*`).
  * **Codex**: pattern-replicated adapters (`FS.*`), security scan
    integrations (`SC.*`), audit-skill markdown (`BP.D.7`), 27 skill
    pack rework (`BP.W3.1`), Gerrit hooks (`BP.G`), single-file glue
    (`BP.J.2`), documentation, follow-up tests for already-shipped code,
    high-volume mechanical fixes (`B9` ESLint).

When ambiguous → default to Claude. The full table is in
`coordination.md`.

### Tier A vs Tier B (per-task safety)

Every task is classified as one of two tiers:

  * **Tier A** — direct commit to `master`. Used when output is
    pattern-constrained AND single-file AND `git revert` is
    sufficient recovery. Codex's runner with `OMNISIGHT_CODEX_TIER=A`.
  * **Tier B** — commit goes to `codex-work` branch via worktree at
    `../OmniSight-codex-worktree`. Reviewed by human/Claude before
    merge. Default tier for Codex.

Decision rule: when uncertain → assume Tier B.

### Same-branch parallel safety contract

Multiple Claude runners on master is empirically safe (single model,
consistent judgement). Adding Codex on master in Tier A mode requires
strict invariants:

  1. Section non-overlap (each runner has distinct
     `OMNISIGHT_*_FILTER`)
  2. File non-overlap (cross-section file overlap → escalate, don't
     parallelize)
  3. TODO marker disjoint (Claude marks `[x][C]`, Codex marks
     `[x][G]`; neither modifies the other's tag)
  4. HANDOFF heading prefix (`## [Claude/Opus]` vs
     `## [Codex/GPT-5.5]`; append-only, no edits to other agent's
     entries)
  5. Atomic Co-Authored-By commit messages (separates authorship for
     `git log --grep "GPT-5.5"` audits)

If any invariant fails → stop runners, fix manually, restart.

### Why three runners and not just one router

A single runner that auto-picks "Claude vs Codex vs API" backend is
the eventual right design (it lives in BP.A2A or its successor — the
multi-vendor lingua franca pattern). Today we keep them as separate
scripts because:

  * Independent scripts are simpler to reason about and easier to
    kill / debug
  * Authentication differs (Claude Code session vs OpenAI session vs
    API key) — no clean unified config story yet
  * Operator-driven routing (per-task `OMNISIGHT_*_FILTER` selection)
    is fine at Phase A scale

When Phase B / C arrives and OmniSight ships an Orchestrator UI, the
three runners collapse into one auto-routing component (an instance of
the `BP.A2A` pattern operating on internal agents). Until then,
explicit-choice is the sane default.

---

## Why runner intentionally does NOT pursue Claude Code parity

A natural-but-wrong instinct, when seeing how much Claude Code can do
that the runner cannot, is "we should build all of those features so
OmniSight isn't a weaker product". This section pins down why that
framing is wrong and what the right framing is.

### The instinct: "OmniSight 不能比 Claude Code 還弱"

The premise: Claude Code as a CLI dev assistant offers ~30 tools, RLHF-
tuned tool-use behaviours, a slash-command syntax, plugins/hooks,
auto-memory, multiple sub-agent types, streaming UX, per-tool cost
estimation, approval workflows, and so on. The runner today has 9
tools, 3 sub-agent types, manual memory, no plugins, no slash
commands, no streaming UX. If OmniSight ships as a real product whose
value includes "agentic execution", losing this comparison loses the
product.

### Why the premise is wrong: different competitors

OmniSight is not in the same product category as Claude Code:

|                   | Claude Code                          | OmniSight                                                                       |
|-------------------|--------------------------------------|---------------------------------------------------------------------------------|
| Target user       | Individual developer                 | Embedded AI camera engineering team (Type A / B / C verticals)                  |
| Core value        | General-purpose agentic dev workflow | Multi-agent orchestration + domain expertise + multi-tenant + compliance audit  |
| Direct competitors | Cursor / Continue / Aider / etc.    | Vendor IDEs (Renesas e²studio / NXP MCUXpresso / ...), internal dev portals, vertical MLOps platforms |
| Moat              | Anthropic infra + RLHF + subscription distribution | Domain expertise (HD/BSP/HAL/Algo-CV/Optical/ISP) + multi-vendor integration + compliance audit + workflow DAG composition |

OmniSight's real users will not benchmark "OmniSight Read tool vs
Claude Code Read tool". They'll evaluate questions Claude Code can't
even answer:

  * Can it orchestrate Claude + Gemini + a third-party PCB review
    agent in one workflow? (A2A — `BP.A2A`)
  * Can it isolate per-tenant audit logs for IEC 62304 / ISO 13485?
    (`KS.*` + `BP.D` compliance matrices)
  * Can it run an EVK on real hardware via daemon RPC? (`BP.W3.12`
    Phase T Hardware Bridge)
  * Can it ingest a Figma file + a vendor SDK datasheet + a customer's
    private MCP server in the same task? (Phase 1 MCP + future
    integrations)

### The honest answer: OmniSight wraps Claude Code, not replaces it

The architecture that actually scales:

```
                      OmniSight orchestration layer
                       /         |         \
              Claude Code   Anthropic API   Other vendors
              (subscription) (per-token)    (Google/OpenAI/etc.)
                       \         |         /
              OmniSight domain specialist agents
              (HD / BSP / HAL / Algo-CV / Optical / ISP)
```

User-facing entry is OmniSight. Internally, OmniSight routes each
task to whichever execution backend fits: Claude Code subscription
for general agentic dev work, raw Anthropic API for batch/runner-
shape work, vendor APIs for specialised work. **OmniSight competes
on the orchestration / domain / compliance layer, not on raw
agentic execution.**

This already exists in skeleton form:

  * `auto-runner.py` (subscription) — drives Claude Code CLI
  * `auto-runner-sdk.py` (API) — drives Anthropic SDK directly
  * Future OmniSight Orchestrator will auto-route tasks across these
    rather than asking the user to pick

### Filter for future runner Phases (sharpened)

The runner-strategy filter from the *Time budget* section is updated
with a Claude-Code-axis check:

> Before adding a new runner Phase, ask:
>
> 1. **Mirror property**: does this teach us something we'll need for
>    OmniSight's user product, or is it runner-only optimization?
> 2. **Axis check**: is this an axis Claude Code already does well
>    (and we'd be reinventing), or one OmniSight uniquely needs?
>
> If (1) is "user product" AND (2) is "OmniSight uniquely needs" →
> proceed. If either is no → defer or skip.

The 3 epics added 2026-05-02 from *Agentic Design Patterns* review
all pass this filter:

  * `BP.A2A` (Ch 15) — Claude Code doesn't do cross-system A2A; it's
    OmniSight's native domain (multi-agent + multi-provider).
  * `BP.Q` RAG (Ch 14) — Claude Code has personal-codebase RAG; the
    multi-tenant + compliance-audit-able version is OmniSight-unique.
  * `KS.4.10-15` LLM Firewall (Ch 18) — Claude Code trusts its single
    user; OmniSight's multi-tenant model demands an input firewall.

What this filter CORRECTLY rejects, even though they look attractive:

  * Reinventing TaskCreate/Monitor/ScheduleWakeup-style coordination
    tools — Claude Code has these, we'd just be tracing a worse
    version.
  * Auto-memory write-back from runner execution — Claude Code does
    this well; for OmniSight backend service we'd build memory
    differently anyway (per-tenant rule layer).
  * Slash-command syntax — Claude Code-product-shape, not relevant
    to OmniSight's web/API surface.
  * RLHF-quality tool-use prompt tuning — Anthropic does this for
    Claude Code; we have neither the data nor the cycle to replicate
    competitively.

### The companion concern: TODO granularity

A related symptom of the same wrong frame: the project's TODO items
were written at "what a competent dev with Claude Code can finish in
one session" granularity, not "what one agentic loop can complete in
one commit-unit". This is why W14.5 / W15.1 / W15.2 each contained
3-6 implicit sub-tasks but appeared as single bullets — and why API-
mode runs blew through max_tokens / max_iterations.

**The fix is NOT runner-side** (we already learned Phase 6 auto-
decompose is premature without empirical samples). The fixes are:

  * **Going forward**: when adding new TODO items, target commit-unit
    granularity (one bullet ≈ one commit-shaped change), include
    explicit acceptance criteria, mark cross-bullet dependencies.
  * **For existing big bullets**: human-led decomposition pass when
    a specific epic comes up for execution. LLM can suggest the
    split, human validates.
  * **For OmniSight as user product**: task-sizing is one of the
    user-facing features OmniSight must build (mirror property). The
    runner's pain here previews user pain there.

---

## Open questions (Phase B+ concerns, not blocking now)

These are recorded for future visit when their phase arrives:

1. **Schema migration story** — when `cost_guard` / `batch_dispatcher`
   persisted state evolves, how do existing rows migrate? Currently
   in-memory only; PG-backed impls (alembic 0182-0185) lack migration
   scaffolding.

2. **Multi-developer concurrency** — two operators running
   `auto-runner-sdk.py` against the same TODO simultaneously will
   conflict on TODO marker writes + git commits. No coordination
   today. Acceptable for Phase A (single dev); breaks at Phase B.

3. **Telemetry / structured observability** — HANDOFF.md is human-
   readable but not machine-analyzable. Phase B feedback loop ("which
   item types fail most? at which iteration?") needs structured event
   stream.

4. **Failure mode taxonomy** — current markers `[!]` / `[~]` / `[O]`
   are coarse. Fine-grained codes (`max_tokens` / `dep_unmet` /
   `tool_unavailable` / `over_budget` / ...) would make analytics
   tractable.

5. **CLI retirement timing** — when does `auto-runner-sdk.py` get
   demoted to debug-only? Likely when an Orchestrator UI in the main
   product reaches feature parity. Worth tracking as a Phase C
   milestone.

---

## How to use this doc

  * Before adding a runner Phase: re-read the **mirror property**
    section + the **two-question filter** in *Why runner intentionally
    does NOT pursue Claude Code parity § Filter for future runner
    Phases*. Both must pass: (1) mirror to user product, (2) axis where
    OmniSight uniquely needs (not where Claude Code already does well).
  * Before declaring Phase X done: ensure `backend/agents/*` carries
    the durable artifact, not the runner script.
  * When user / operator pushes back on a proposed Phase: take the
    pushback as the empirical signal Phase 6 deferral relies on. Don't
    rationalise around it.
  * When tempted to "make runner more like Claude Code": stop. Re-read
    the parity section. The right move is usually to wrap Claude Code
    as a backend, not duplicate it.
  * When writing TODO items: target commit-unit granularity, include
    acceptance criteria, mark cross-bullet dependencies. See *The
    companion concern: TODO granularity* for why.
