# Troubleshooting — when the dashboard tells you something is wrong

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

Every common failure mode the dashboard can surface, grouped by what
the operator actually sees. If your situation isn't here, check the
Orchestrator AI panel's log stream (REPORTER VORTEX) and the backend
stderr.

## Red banner in a panel

### `[AUTH] ...`
The backend rejected the call with 401 / 403.

- **Cause**: `OMNISIGHT_DECISION_BEARER` is set on the backend but
  the frontend's stored token is wrong or missing.
- **Fix**: open Settings → provider tab → re-enter the bearer; or
  unset `OMNISIGHT_DECISION_BEARER` in `.env` for single-user local
  deploys.

### `[RATE LIMITED] ...`
Sliding-window throttle kicked in (30 req / 10 s per client IP by
default).

- **Cause**: scripted polling, or a runaway UI retry loop.
- **Fix**: wait for the banner to auto-clear (10 s), or raise the
  limit via `OMNISIGHT_DECISION_RL_MAX` / `_WINDOW_S`. See
  `.env.example`.

### `[NOT FOUND] ...`
Endpoint returned 404.

- **Cause**: frontend calling an endpoint the backend removed or
  renamed. Usually a version skew after a partial deploy.
- **Fix**: hard-reload the page. If the error persists, backend and
  frontend are out of sync — restart both.

### `[BACKEND DOWN] ...`
Backend returned 5xx.

- **Cause**: uvicorn not running, or an unhandled exception in a
  router. Check `/tmp/omni-backend.log` (dev) or the service log
  (prod).
- **Fix**: restart the backend. If it crashes on startup, run
  `python3 -m uvicorn backend.main:app` in the foreground to see the
  stack.

### `[NETWORK] ...`
Fetch failed before reaching the backend.

- **Cause**: backend process dead, wrong port, or a proxy / VPN
  dropping the connection.
- **Fix**: `curl http://127.0.0.1:8000/api/v1/health`. If that
  returns, the frontend's `NEXT_PUBLIC_API_URL` or rewrite config is
  wrong. If it doesn't, start the backend.

## Decision Queue looks stuck

### Pending decision won't dismiss on approve / reject
- **Cause 1**: backend returned 409 — the decision is already resolved
  (someone else approved it from another tab). The UI will reconcile
  on the next SSE event; hit **RETRY** in the panel header to force.
- **Cause 2**: destructive-severity `window.confirm()` dialog is
  still open in a hidden tab. Look at every open dashboard tab.

### Decisions keep timing out before you click
- Default `timeout_s` on propose is 60. If the producer set a shorter
  deadline and you can't respond in time, the sweep loop resolves to
  the safe default. This is intended behaviour.
- To get more time: switch to MANUAL mode (which holds decisions
  indefinitely by not setting a deadline — verify by checking
  `deadline_at` in the decision payload).

### SWEEP button does nothing
- It only resolves decisions whose deadline has **already passed**.
  If everything is still in its window, 0 will be resolved and the
  button says so in a transient message.

## Toast problems

### "+N MORE PENDING" chip won't go away
- Dismiss all visible toasts (Esc on each, or click ✕). The overflow
  counter only clears when the stack hits 0.
- If it still lingers, the backend is firing new `decision_pending`
  events faster than you can dismiss them. Switch MODE down (SUPERVISED
  or MANUAL) to stop auto-executing routine decisions from producing
  new risky/destructive follow-ups.

### Countdown is frozen at 100 %
- Clock skew between backend and browser. Check
  `date -u` on both machines.
- If the backend clock is ahead of the browser, the bar stays full
  until the real deadline passes, then snaps to 0.

### Countdown shows NaN or weird values
- The backend sent a malformed `deadline_at`. The validator added in
  audit B2 should coerce this, but if you still see it: hard-reload
  (stale JS); if persists, file an issue with the raw SSE payload.

## Agent problems

### Agent stuck "working" for > 30 min
- Watchdog fires after 30 min and will propose a stuck-remediation
  decision (switch model / spawn alternate / escalate). Check the
  Decision Queue.
- If nothing shows up in 60 s, the watchdog thinks the agent has an
  active heartbeat. Use **Emergency Stop** → Resume to force-reset.

### Agent keeps hitting the same error
- Error ring buffer (size 10 per agent) is fed by the node graph.
  After the 3rd identical error within the window the stuck detector
  auto-proposes a `switch_model` remediation in FULL AUTO / TURBO,
  or queues it for approval in the lower modes.
- If it never gets that far, the error may not be surfacing as a
  tool error — check REPORTER VORTEX.

### Provider health shows red but my key is fine
- Provider health = last 3 probe pings. Ran out of quota counts as a
  health failure. Check your provider's dashboard.
- If the key is valid, the keyring may have loaded a stale version.
  Settings → Provider Keys → re-save.

## Mobile / tablet problems

### Can't reach some panels on phone
- Bottom nav dot row maps all 12 panels. If you see fewer dots,
  you're on a build older than Phase 50D. Hard-reload.
- The swipe prev/next buttons cycle through them in order.

### Deep link opens the wrong panel
- `?panel=` takes priority over `?decision=`. Drop the `?panel=`
  component or ensure it's `?panel=decisions` if you're deep-linking
  a decision id.

## If you're truly stuck

- `curl http://localhost:8000/api/v1/system/sse-schema | jq` — sanity
  check the backend is up and emitting the event types the frontend
  expects.
- `pytest backend/tests/test_decision_engine.py` — the decision
  engine's 27-test suite completes in < 1 s and will catch most
  backend regressions.
- Open an issue with: backend commit hash (`git rev-parse HEAD`),
  the red banner text, and the last 50 lines of `REPORTER VORTEX`.

## Related

- [Operation Modes](reference/operation-modes.md)
- [Decision Severity](reference/decision-severity.md)
- [Glossary](reference/glossary.md)
