---
id: L-OP-2571c
ticket: OP-2571
title: Fast-failure loops trip the runner stoploss circuit in minutes — that is the design; diagnose the systemic cause
date: 2026-07-11
tags: [runner, jira, reliability, anthropic]
---

# Fast-failure loops trip the runner stoploss circuit in minutes — that is the design; diagnose the systemic cause

**Situation**: When a *systemic* blocker makes every CLI invocation die in
seconds — subscription quota exhausted, the wrong/stale CLI binary on PATH, an
infra 400 — the runner does not stall; it reverts-and-repicks the same ticket
in tight sub-minute cycles. The per-ticket circuit breaker
(`backend/agents/runner_stoploss.py`, OP-1140) records a
`runner-stoploss:revert-<ts>` label per §11-revert and, at
`DEFAULT_THRESHOLD = 3` reverts inside `DEFAULT_WINDOW_MIN = 15` minutes
(`runner_stoploss.py:48-49`), adds a `runner-stoploss:circuit-tripped-<ts>`
label (`TRIPPED_LABEL_PREFIX`, `runner_stoploss.py:43`) that gates all further
pickup. During the U4 marathon this fired twice from fast-failure loops, both
systemic: (1) claude subscription quota exhaustion — the CLI log showed "hit
your limit", resets on its weekly window; and (2) the codex lane's daemon
resolving a *stale* codex binary via a bare system PATH — the log showed
"requires a newer version". The trip is the breaker working as intended (it
stopped OP-1126/OP-1124-style overnight token burn in exactly this loop), NOT
evidence the ticket is bad.

**Fix**: Read the trip as "a fast loop happened", then find the SYSTEMIC cause
before touching the ticket. Grep the runner log for the CLI's actual error
string — "hit your limit" ⇒ quota (wait for the weekly reset or switch class),
"requires a newer version" ⇒ stale binary (point the lane at the known-good
CLI, e.g. the working codex under `~/.nvm/versions/node/.../bin/codex` rather
than a bare-PATH one), an infra 400 ⇒ the upstream service. Only after the root
cause is fixed, re-enable pickup by clearing the wedge: strip the
`runner-stoploss:circuit-tripped-*` (and any `revert-*` / `claim:*`) labels and
clear the assignee so `PICKUP_JQL` (which requires assignee EMPTY) can re-grab
it — the rescue helper does exactly this (`tools.py:3187`, `tools.py:3219`).

**Verification**: In both instances, after fixing the systemic cause and
clearing the circuit-tripped label + assignee, the same ticket re-picked and
completed on the next tick — proving the ticket was always sound and the trip
was a symptom, not a verdict. The breaker thresholds and label shape were left
unchanged; the fix was upstream (quota window / correct binary), not a
loosening of the stoploss.

**Generalisation**: A circuit breaker that trips on a burst of identical fast
failures is doing its job — it converts an infinite money-burning retry loop
into a single stop. So a `circuit-tripped-*` label is a "something systemic is
wrong" signal, never "this task is bad": resist the reflex to rewrite or
abandon the ticket. Always recover the underlying CLI/tool's real error from
the log first, fix the shared cause (quota, binary version, infra), and only
then clear the trip labels + assignee to re-arm pickup. Sub-minute revert
cycles specifically point at a systemic blocker (every attempt failing the same
way at the same early stage), not at a per-ticket logic bug.
