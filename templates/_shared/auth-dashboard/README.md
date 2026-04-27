# `@omnisight/auth-dashboard` — AS.5.2 Per-tenant dashboard (TS twin)

> Behaviourally identical mirror of `backend/security/auth_dashboard.py`.
> The OmniSight backend ships the Python lib (which feeds the admin
> pane); this directory ships the generated-app TS lib so an
> offline / edge-deployed app can render the same dashboard widgets +
> raise the same suspicious-pattern alerts against its local copy of
> the auth audit stream.

## What this is

Pure-functional read-side companion to the AS.5.1 `auth_event` write
surface.  Two reducers + six detection rules + the frozen output types.

| Piece                          | Purpose                                           |
| ------------------------------ | ------------------------------------------------- |
| `summarise(rows, opts)`        | Reduce audit rows → `DashboardSummary` totals     |
| `detectSuspiciousPatterns(...)` | Run the six rules → `SuspiciousPatternAlert[]`   |
| `emptySummary(tenantId, opts)` | Zero-filled placeholder summary (knob-off banner) |
| `isEnabled()`                  | AS.0.8 single-knob check (env-driven)             |

## Six dashboard rules

| Rule                              | Default threshold     | Severity   |
| --------------------------------- | --------------------- | ---------- |
| `login_fail_burst`                | 10 fails / 60 s       | `warn`     |
| `bot_challenge_fail_spike`        | 20 fails / 60 s       | `warn`     |
| `token_refresh_storm`             | 10 refreshes / 60 s   | `warn`     |
| `honeypot_triggered`              | 1 trigger / 60 s      | `critical` |
| `oauth_revoke_relink_loop`        | 3 cycles / 600 s      | `info`     |
| `distributed_login_fail`          | 5 distinct IPs / 300s | `critical` |

Each rule consults the same audit rows; defaults can be overridden per-rule
via `thresholds: { rule: { count?, windowS? } }`.

## Cross-twin contract (8 invariants)

Pinned by `backend/tests/test_auth_dashboard_shape_drift.py`:

  1. **6 rule strings** — byte-equal across the two twins.
  2. **`ALL_DASHBOARD_RULES` order** — identical ordering.
  3. **3 severity strings** — `info` / `warn` / `critical`.
  4. **`DEFAULT_THRESHOLDS` integers** — count + window_s per rule.
  5. **`DEFAULT_RULE_SEVERITIES` mapping** — rule → severity.
  6. **`LIMIT_ROWS_DEFAULT` integer** — 50 000.
  7. **`DashboardSummary` field set** — every counter / rate / breakdown
     mirrors the Python dataclass field-for-field.
  8. **`SuspiciousPatternAlert` evidence keys** — per-rule shape locked
     so the AS.7.x notification template renders identical UI on both
     sides.

## Quick start

```ts
import {
  summarise,
  detectSuspiciousPatterns,
  type AuthAuditRow,
} from "@omnisight/auth-dashboard"

// 1. Pull rows from your local cache (or a fetch against OmniSight).
const rows: AuthAuditRow[] = await fetchAuthRows({ since, until })

// 2. Compute summary + alerts.
const summary = summarise(rows, { tenantId: "t-acme" })
const alerts = detectSuspiciousPatterns(rows, { tenantId: "t-acme" })

// 3. Render.  Render `summary.loginSuccessRate === null` as "no data",
//    not "0 % of N".
console.log(summary.loginSuccessRate, summary.botChallengePassRate)
console.log(alerts.length, "alerts")
```

## AS.0.8 single-knob behaviour

Pure helpers (`summarise` / `detectSuspiciousPatterns` / `emptySummary`)
deliberately ignore the knob — a doc generator or test harness needs to
inspect canonical shapes regardless.  Generated apps that wrap these
helpers behind a knob-aware UI gate consult `isEnabled()` themselves.

## Files

| File          | What                                                  |
| ------------- | ----------------------------------------------------- |
| `index.ts`    | Pure reducer + detector + frozen output interfaces    |
| `README.md`   | This file                                             |

## Public API

```ts
// Six rule constants + tuple
RULE_LOGIN_FAIL_BURST, RULE_BOT_CHALLENGE_FAIL_SPIKE,
RULE_TOKEN_REFRESH_STORM, RULE_HONEYPOT_TRIGGERED,
RULE_OAUTH_REVOKE_RELINK_LOOP, RULE_DISTRIBUTED_LOGIN_FAIL,
ALL_DASHBOARD_RULES

// Three severity literals
SEVERITY_INFO, SEVERITY_WARN, SEVERITY_CRITICAL, SEVERITIES

// Defaults
DEFAULT_THRESHOLDS, DEFAULT_RULE_SEVERITIES, LIMIT_ROWS_DEFAULT

// Frozen output interfaces
DashboardSummary, SuspiciousPatternAlert, DashboardResult,
RuleThreshold, AuthAuditRow

// Pure helpers
summarise, detectSuspiciousPatterns, emptySummary

// Knob hook
isEnabled
```
