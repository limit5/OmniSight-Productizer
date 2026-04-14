# Operation Modes — MODE pill at the top of the screen

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — for PMs

MODE decides **how much the AI is allowed to do without asking you
first**. Four settings, from "ask me everything" to "do everything,
I'll stop it if it's wrong". The icon colour matches the risk.

| Mode | Icon colour | One-line meaning |
|---|---|---|
| **MANUAL** (MAN) | cyan | Every step waits for your approval |
| **SUPERVISED** (SUP) | blue | Routine work auto-runs, risky stuff stops for you — **default** |
| **FULL AUTO** (AUT) | amber | Only destructive work stops for you |
| **TURBO** (TRB) | red | Everything auto-runs, including destructive — you have a 60 s escape window |

Switching mode takes effect instantly across every connected browser
(desktop, phone, tablet).

## How it interacts with Decision Severity

Every thing the AI wants to do gets one of four severity tags (see
[Decision Severity](decision-severity.md)). The mode's job is to pick
a row from this table:

| Severity ↓ / Mode → | MANUAL | SUPERVISED | FULL AUTO | TURBO |
|---|---|---|---|---|
| `info` (logging, reads) | queued | auto | auto | auto |
| `routine` (normal writes) | queued | auto | auto | auto |
| `risky` (recoverable writes) | queued | queued | auto | auto |
| `destructive` (ship / deploy / delete) | queued | queued | queued | auto (60 s timer) |

"queued" means the decision appears in the **Decision Queue** panel;
your approval is required before the AI continues.

## Parallelism budget

MODE also sets how many agents the system runs in parallel. You can
see this as `in_flight / cap` next to the pill.

| Mode | Parallel cap |
|---|---|
| MANUAL | 1 |
| SUPERVISED | 2 |
| FULL AUTO | 4 |
| TURBO | 8 |

Higher parallelism = faster throughput but more API token spend. If
your token budget is tight, look at **Budget Strategy** first before
bumping mode.

## Common situations

- **Leaving for lunch / overnight** — switch to MANUAL so nothing
  surprising lands while you're away. Unresolved decisions age up and
  you'll see them on return.
- **Daily development** — SUPERVISED is the sweet spot. AI picks up
  routine work (file reads, tool calls, analysis) but stops before
  anything irreversible.
- **Demo-day crunch** — FULL AUTO. You'll still approve destructive
  pushes but nothing else blocks you.
- **Weekend refactor batch** — TURBO. Watch the phone toast for the
  60 s destructive countdown; use Emergency Stop if something looks
  wrong.

## Who can change mode

If `OMNISIGHT_DECISION_BEARER` is set in the backend `.env`, only
callers presenting that token via the API can change mode (the UI
takes the token from local storage). Otherwise the control is open to
anyone with network access — acceptable for a single-user local
deployment, not for shared instances.

## Under the hood

- Frontend: `components/omnisight/mode-selector.tsx` — the segmented
  pill + SSE subscriber that keeps every tab in sync.
- Backend: `backend/decision_engine.py` · `set_mode()` / `get_mode()` ·
  `should_auto_execute(severity)` is the matrix shown above.
- Event: switching publishes `mode_changed` on the SSE bus; schema
  available at `GET /api/v1/system/sse-schema`.
- Persistence: **not yet persisted** across restarts — the default
  always comes back as SUPERVISED. Tracked for a future phase.

## Related reading

- [Decision Severity](decision-severity.md) — what makes something
  `risky` vs `destructive`
- [Budget Strategies](budget-strategies.md) — the token / cost knob
  that lives next to MODE
- [Panels Overview](panels-overview.md) — where to find the Decision
  Queue once items start queuing up
