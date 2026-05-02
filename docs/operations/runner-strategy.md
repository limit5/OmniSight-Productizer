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
    section. Filter the proposed Phase through "user-product preview"
    or "runner perfectionism".
  * Before declaring Phase X done: ensure `backend/agents/*` carries
    the durable artifact, not the runner script.
  * When user / operator pushes back on a proposed Phase: take the
    pushback as the empirical signal Phase 6 deferral relies on. Don't
    rationalise around it.
