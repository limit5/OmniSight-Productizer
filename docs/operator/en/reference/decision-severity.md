# Decision Severity — info / routine / risky / destructive

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — for PMs

Every decision the AI surfaces carries a risk label. The label decides
the icon, the colour, the countdown, and whether MODE auto-executes
it. **Destructive is the one to pay attention to.**

| Severity | Icon | Colour | Reversible? | Typical example |
|---|---|---|---|---|
| **info** | Info circle | neutral | yes | "I read 12 files to answer this" |
| **routine** | Info circle | neutral | yes | Pick a model to use for this task |
| **risky** | Warning triangle | amber | recoverable | Switch an agent's LLM provider mid-task |
| **destructive** | Warning octagon | red | **no** | Push to production, delete workspace, ship release |

## When does the AI pick which?

Severity is chosen at the moment a decision is proposed. Two sources:

1. **Hardcoded defaults** — the engine knows e.g. that `deploy/*` is
   `destructive`, `switch_model` is `risky`.
2. **Decision Rules** — operator-defined overrides. You can declare
   that any `deploy/staging` is only `risky` for your team, or that
   `git_push/experimental/*` should auto-execute in FULL AUTO. See
   the Decision Rules panel.

## UI cues

In the **Decision Queue** panel and the top-right **Toast**:

- **Destructive** — red AlertOctagon icon, red border, red countdown
  bar, and a browser `confirm()` dialog when you click APPROVE or
  REJECT (B10 safeguard).
- **Risky** — amber AlertTriangle icon, amber border, no confirm
  dialog but countdown still visible.
- **Routine / info** — blue Info icon, no countdown unless a
  `timeout_s` was set.

When fewer than 10 seconds remain on a pending decision, the
countdown turns **red and pulses** on both panel and toast so you
notice from across the room.

## Timeout behaviour

If a pending decision times out without your input:

- It resolves to its `default_option_id` (usually the safe option)
- Its `resolver` is recorded as `"timeout"`
- It emits `decision_resolved` SSE and moves to history
- The 30 s sweep loop handles this; you can also manually trigger it
  with the **SWEEP** button in the Decision Queue header.

Override the cadence with `OMNISIGHT_DECISION_SWEEP_INTERVAL_S`
(default 10).

## Destructive confirm — double-check guard

Added by audit B10. Clicking APPROVE or REJECT on a destructive
decision pops a browser confirm dialog with the title and chosen
option. This means:

- You cannot accidentally greenlight "push to prod" by fat-fingering
  the keyboard shortcut `A`.
- Reject also confirms, since rejecting a destructive deploy may
  leave a branch half-merged.

To bypass the dialog (e.g. in a scripted E2E), use the backend API
directly rather than the UI.

## Rate limiting

Decision mutator endpoints (`/approve`, `/reject`, `/undo`, `/sweep`,
`/operation-mode`, `/budget-strategy`) are sliding-window rate
limited — default 30 requests / 10 seconds per client IP. Tune with
`OMNISIGHT_DECISION_RL_WINDOW_S` and `OMNISIGHT_DECISION_RL_MAX`.

## Under the hood

- Enum: `backend/decision_engine.py · DecisionSeverity`
- Auto-execute matrix: `should_auto_execute(severity, mode)`
- Destructive confirm: `components/omnisight/decision-dashboard.tsx ·
  doApprove / doReject`
- Rate limit: `backend/routers/decisions.py · _rate_limit()`

## Related reading

- [Operation Modes](operation-modes.md) — how severity × mode
  determines auto vs queued
- [Panels Overview](panels-overview.md) — where to click to see
  pending / history
