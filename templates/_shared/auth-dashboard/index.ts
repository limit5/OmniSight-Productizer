/**
 * AS.5.2 — Per-tenant dashboard rollup + suspicious-pattern detection
 * (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/security/auth_dashboard.py`.
 * Generated apps that maintain a local copy of their auth events
 * (offline contexts, edge-deployed widgets, on-device summaries) call
 * `summarise()` + `detectSuspiciousPatterns()` on the same audit row
 * shape the OmniSight backend writes — so the dashboard widget the
 * generated app renders matches the OmniSight admin pane byte-for-byte.
 *
 * What this module ships
 * ──────────────────────
 *  * Six dashboard rule constants + `ALL_DASHBOARD_RULES` tuple.
 *  * Three severity literals + `SEVERITIES` set.
 *  * `DEFAULT_THRESHOLDS` (per-rule {count, windowS}) — frozen.
 *  * `DEFAULT_RULE_SEVERITIES` (per-rule severity) — frozen.
 *  * `LIMIT_ROWS_DEFAULT` integer.
 *  * Frozen `DashboardSummary` / `SuspiciousPatternAlert` /
 *    `DashboardResult` interfaces (TS interfaces — readonly fields).
 *  * Pure `summarise(rows, {tenantId, since?, until?})` reducer.
 *  * Pure `detectSuspiciousPatterns(rows, {tenantId, thresholds?,
 *    enabledRules?})` detector.
 *  * `emptySummary(tenantId, {since?, until?})` placeholder builder.
 *  * `isEnabled()` AS.0.8 knob hook (reads
 *    `OMNISIGHT_AS_FRONTEND_ENABLED` lazily, defaults true).
 *
 * Out of scope on the TS side
 * ───────────────────────────
 *  * The async `compute_dashboard` orchestrator on the Python side
 *    pulls rows from PG.  TS twin has no DB dependency — the generated
 *    app is responsible for sourcing rows (typically a local IndexedDB
 *    cache or a fetch against the OmniSight backend).  TS twin only
 *    ships the pure reducer + detector.
 *
 * Cross-twin contract (enforced by AS.5.2 drift guard)
 * ────────────────────────────────────────────────────
 * For every export the following MUST be byte-identical:
 *
 *   * Six rule strings + the `ALL_DASHBOARD_RULES` order.
 *   * Three severity strings + the `SEVERITIES` set.
 *   * Per-rule `DEFAULT_THRESHOLDS` (count + windowS).
 *   * Per-rule `DEFAULT_RULE_SEVERITIES` mapping.
 *   * `LIMIT_ROWS_DEFAULT` integer.
 *   * Per-event counter mapping (login_success → loginSuccessCount, etc).
 *   * Per-rule alert evidence keys.
 *   * Sort key for stable alert ordering (rule then a per-rule subject).
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *  * No module-level mutable state — frozen constants + pure functions.
 *  * `isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` lazily.
 *  * No env reads at module top.  No DB reads anywhere (TS twin has
 *    no DB).
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 * Pure helpers (`summarise`, `detectSuspiciousPatterns`,
 * `emptySummary`) deliberately ignore the knob — a doc-generator or
 * test harness needs to inspect canonical shapes regardless.  Generated
 * apps that wrap these helpers behind a knob-aware UI gate consult
 * `isEnabled()` themselves.
 */

import {
  ALL_AUTH_EVENTS,
  AUTH_METHODS,
  BOT_CHALLENGE_FAIL_HONEYPOT,
  BOT_CHALLENGE_FAIL_REASONS,
  BOT_CHALLENGE_PASS_KINDS,
  EVENT_AUTH_BOT_CHALLENGE_FAIL,
  EVENT_AUTH_BOT_CHALLENGE_PASS,
  EVENT_AUTH_LOGIN_FAIL,
  EVENT_AUTH_LOGIN_SUCCESS,
  EVENT_AUTH_OAUTH_CONNECT,
  EVENT_AUTH_OAUTH_REVOKE,
  EVENT_AUTH_TOKEN_REFRESH,
  EVENT_AUTH_TOKEN_ROTATED,
  LOGIN_FAIL_REASONS,
  OAUTH_REVOKE_INITIATORS,
  TOKEN_REFRESH_OUTCOMES,
} from "../auth-event/index.ts"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Six dashboard-rule constants — AS.5.2 SoT
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const RULE_LOGIN_FAIL_BURST = "login_fail_burst"
export const RULE_BOT_CHALLENGE_FAIL_SPIKE = "bot_challenge_fail_spike"
export const RULE_TOKEN_REFRESH_STORM = "token_refresh_storm"
export const RULE_HONEYPOT_TRIGGERED = "honeypot_triggered"
export const RULE_OAUTH_REVOKE_RELINK_LOOP = "oauth_revoke_relink_loop"
export const RULE_DISTRIBUTED_LOGIN_FAIL = "distributed_login_fail"

export const ALL_DASHBOARD_RULES: ReadonlyArray<string> = Object.freeze([
  RULE_LOGIN_FAIL_BURST,
  RULE_BOT_CHALLENGE_FAIL_SPIKE,
  RULE_TOKEN_REFRESH_STORM,
  RULE_HONEYPOT_TRIGGERED,
  RULE_OAUTH_REVOKE_RELINK_LOOP,
  RULE_DISTRIBUTED_LOGIN_FAIL,
])

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Three severity literals
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const SEVERITY_INFO = "info"
export const SEVERITY_WARN = "warn"
export const SEVERITY_CRITICAL = "critical"

export const SEVERITIES: ReadonlySet<string> = Object.freeze(
  new Set<string>([SEVERITY_INFO, SEVERITY_WARN, SEVERITY_CRITICAL]),
)

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Default thresholds — per-rule (count, windowS)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Per-rule default thresholds.  Camel-case `windowS` on the TS side
 * maps to snake_case `window_s` on the Python side; the drift guard
 * enforces equality on the integer values. */
export interface RuleThreshold {
  readonly count: number
  readonly windowS: number
}

export const DEFAULT_THRESHOLDS: Readonly<Record<string, RuleThreshold>> =
  Object.freeze({
    [RULE_LOGIN_FAIL_BURST]: Object.freeze({ count: 10, windowS: 60 }),
    [RULE_BOT_CHALLENGE_FAIL_SPIKE]: Object.freeze({ count: 20, windowS: 60 }),
    [RULE_TOKEN_REFRESH_STORM]: Object.freeze({ count: 10, windowS: 60 }),
    [RULE_HONEYPOT_TRIGGERED]: Object.freeze({ count: 1, windowS: 60 }),
    [RULE_OAUTH_REVOKE_RELINK_LOOP]: Object.freeze({ count: 3, windowS: 600 }),
    [RULE_DISTRIBUTED_LOGIN_FAIL]: Object.freeze({ count: 5, windowS: 300 }),
  })

export const DEFAULT_RULE_SEVERITIES: Readonly<Record<string, string>> =
  Object.freeze({
    [RULE_LOGIN_FAIL_BURST]: SEVERITY_WARN,
    [RULE_BOT_CHALLENGE_FAIL_SPIKE]: SEVERITY_WARN,
    [RULE_TOKEN_REFRESH_STORM]: SEVERITY_WARN,
    [RULE_HONEYPOT_TRIGGERED]: SEVERITY_CRITICAL,
    [RULE_OAUTH_REVOKE_RELINK_LOOP]: SEVERITY_INFO,
    [RULE_DISTRIBUTED_LOGIN_FAIL]: SEVERITY_CRITICAL,
  })

export const LIMIT_ROWS_DEFAULT = 50000

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Audit row shape (input)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** One audit row — the shape `backend.audit.query` returns and the
 * shape an emitter sink writes.  The TS twin reads only the four
 * fields the rollup + detection rules consult; everything else is
 * passed through. */
export interface AuthAuditRow {
  readonly action?: string
  readonly ts?: number
  readonly entity_id?: string
  readonly after?: Record<string, unknown>
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Frozen output types
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface DashboardSummary {
  readonly tenantId: string
  readonly since: number | null
  readonly until: number | null
  readonly totalEvents: number
  readonly loginSuccessCount: number
  readonly loginFailCount: number
  readonly loginSuccessRate: number | null
  readonly loginFailReasons: Readonly<Record<string, number>>
  readonly authMethodDistribution: Readonly<Record<string, number>>
  readonly botChallengePassCount: number
  readonly botChallengeFailCount: number
  readonly botChallengePassRate: number | null
  readonly botChallengePassKinds: Readonly<Record<string, number>>
  readonly botChallengeFailReasons: Readonly<Record<string, number>>
  readonly oauthConnectCount: number
  readonly oauthRevokeCount: number
  readonly oauthRevokeInitiators: Readonly<Record<string, number>>
  readonly tokenRefreshCount: number
  readonly tokenRefreshOutcomes: Readonly<Record<string, number>>
  readonly tokenRotatedCount: number
}

export interface SuspiciousPatternAlert {
  readonly rule: string
  readonly severity: string
  readonly tenantId: string
  readonly evidence: Readonly<Record<string, unknown>>
}

export interface DashboardResult {
  readonly summary: DashboardSummary
  readonly alerts: ReadonlyArray<SuspiciousPatternAlert>
  readonly knobOff: boolean
  readonly rowCountObserved: number
  readonly rowCountTruncated: boolean
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Knob hook
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function isEnabled(): boolean {
  if (
    typeof globalThis !== "undefined" &&
    (globalThis as { process?: { env?: Record<string, string> } }).process?.env
  ) {
    const env = (globalThis as { process: { env: Record<string, string> } })
      .process.env
    const v = env["OMNISIGHT_AS_FRONTEND_ENABLED"]
    if (v === undefined) return true
    return v.toLowerCase() !== "false" && v !== "0"
  }
  return true
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Row helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function readAction(row: AuthAuditRow): string {
  return typeof row.action === "string" ? row.action : ""
}

function readAfter(row: AuthAuditRow): Record<string, unknown> {
  if (row.after && typeof row.after === "object") return row.after
  return {}
}

function readTs(row: AuthAuditRow): number {
  return typeof row.ts === "number" ? row.ts : 0
}

function readEntityId(row: AuthAuditRow): string {
  return typeof row.entity_id === "string" ? row.entity_id : ""
}

function readAfterString(
  after: Record<string, unknown>,
  key: string,
): string | null {
  const v = after[key]
  return typeof v === "string" && v.length > 0 ? v : null
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Pure summariser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function safeRate(num: number, denom: number): number | null {
  if (denom === 0) return null
  return num / denom
}

function bumpCounter(
  counter: Record<string, number>,
  key: string | null,
): void {
  if (key === null) return
  counter[key] = (counter[key] ?? 0) + 1
}

export interface SummariseOptions {
  readonly tenantId: string
  readonly since?: number | null
  readonly until?: number | null
}

export function emptySummary(
  tenantId: string,
  opts: { readonly since?: number | null; readonly until?: number | null } = {},
): DashboardSummary {
  return Object.freeze({
    tenantId,
    since: opts.since ?? null,
    until: opts.until ?? null,
    totalEvents: 0,
    loginSuccessCount: 0,
    loginFailCount: 0,
    loginSuccessRate: null,
    loginFailReasons: Object.freeze({} as Record<string, number>),
    authMethodDistribution: Object.freeze({} as Record<string, number>),
    botChallengePassCount: 0,
    botChallengeFailCount: 0,
    botChallengePassRate: null,
    botChallengePassKinds: Object.freeze({} as Record<string, number>),
    botChallengeFailReasons: Object.freeze({} as Record<string, number>),
    oauthConnectCount: 0,
    oauthRevokeCount: 0,
    oauthRevokeInitiators: Object.freeze({} as Record<string, number>),
    tokenRefreshCount: 0,
    tokenRefreshOutcomes: Object.freeze({} as Record<string, number>),
    tokenRotatedCount: 0,
  })
}

export function summarise(
  rows: ReadonlyArray<AuthAuditRow>,
  opts: SummariseOptions,
): DashboardSummary {
  let loginSuccess = 0
  let loginFail = 0
  const loginFailReasons: Record<string, number> = {}
  const authMethods: Record<string, number> = {}

  let bcPass = 0
  let bcFail = 0
  const bcPassKinds: Record<string, number> = {}
  const bcFailReasons: Record<string, number> = {}

  let oauthConnect = 0
  let oauthRevoke = 0
  const oauthRevokeInitiators: Record<string, number> = {}

  let tokenRefresh = 0
  const tokenRefreshOutcomes: Record<string, number> = {}
  let tokenRotated = 0

  let total = 0

  const allEvents = new Set<string>(ALL_AUTH_EVENTS)

  for (const row of rows) {
    const action = readAction(row)
    if (!allEvents.has(action)) continue
    const after = readAfter(row)
    total += 1

    if (action === EVENT_AUTH_LOGIN_SUCCESS) {
      loginSuccess += 1
      const method = readAfterString(after, "auth_method")
      if (method !== null && AUTH_METHODS.has(method)) {
        bumpCounter(authMethods, method)
      }
    } else if (action === EVENT_AUTH_LOGIN_FAIL) {
      loginFail += 1
      const reason = readAfterString(after, "fail_reason")
      if (reason !== null && LOGIN_FAIL_REASONS.has(reason)) {
        bumpCounter(loginFailReasons, reason)
      }
    } else if (action === EVENT_AUTH_BOT_CHALLENGE_PASS) {
      bcPass += 1
      const kind = readAfterString(after, "kind")
      if (kind !== null && BOT_CHALLENGE_PASS_KINDS.has(kind)) {
        bumpCounter(bcPassKinds, kind)
      }
    } else if (action === EVENT_AUTH_BOT_CHALLENGE_FAIL) {
      bcFail += 1
      const reason = readAfterString(after, "reason")
      if (reason !== null && BOT_CHALLENGE_FAIL_REASONS.has(reason)) {
        bumpCounter(bcFailReasons, reason)
      }
    } else if (action === EVENT_AUTH_OAUTH_CONNECT) {
      oauthConnect += 1
    } else if (action === EVENT_AUTH_OAUTH_REVOKE) {
      oauthRevoke += 1
      const initiator = readAfterString(after, "initiator")
      if (initiator !== null && OAUTH_REVOKE_INITIATORS.has(initiator)) {
        bumpCounter(oauthRevokeInitiators, initiator)
      }
    } else if (action === EVENT_AUTH_TOKEN_REFRESH) {
      tokenRefresh += 1
      const outcome = readAfterString(after, "outcome")
      if (outcome !== null && TOKEN_REFRESH_OUTCOMES.has(outcome)) {
        bumpCounter(tokenRefreshOutcomes, outcome)
      }
    } else if (action === EVENT_AUTH_TOKEN_ROTATED) {
      tokenRotated += 1
    }
  }

  return Object.freeze({
    tenantId: opts.tenantId,
    since: opts.since ?? null,
    until: opts.until ?? null,
    totalEvents: total,
    loginSuccessCount: loginSuccess,
    loginFailCount: loginFail,
    loginSuccessRate: safeRate(loginSuccess, loginSuccess + loginFail),
    loginFailReasons: Object.freeze(loginFailReasons),
    authMethodDistribution: Object.freeze(authMethods),
    botChallengePassCount: bcPass,
    botChallengeFailCount: bcFail,
    botChallengePassRate: safeRate(bcPass, bcPass + bcFail),
    botChallengePassKinds: Object.freeze(bcPassKinds),
    botChallengeFailReasons: Object.freeze(bcFailReasons),
    oauthConnectCount: oauthConnect,
    oauthRevokeCount: oauthRevoke,
    oauthRevokeInitiators: Object.freeze(oauthRevokeInitiators),
    tokenRefreshCount: tokenRefresh,
    tokenRefreshOutcomes: Object.freeze(tokenRefreshOutcomes),
    tokenRotatedCount: tokenRotated,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Suspicious-pattern detector
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function resolveThreshold(
  rule: string,
  overrides:
    | Readonly<Record<string, Partial<RuleThreshold>>>
    | undefined,
): RuleThreshold {
  const base = DEFAULT_THRESHOLDS[rule]
  if (!overrides || !(rule in overrides)) return base
  const override = overrides[rule]
  return Object.freeze({
    count: override.count ?? base.count,
    windowS: override.windowS ?? base.windowS,
  })
}

interface BurstResult {
  readonly triggered: boolean
  readonly firstTs: number | null
  readonly lastTs: number | null
}

function hasWindowBurst(
  timestamps: ReadonlyArray<number>,
  count: number,
  windowS: number,
): BurstResult {
  if (timestamps.length === 0 || count <= 0) {
    return { triggered: false, firstTs: null, lastTs: null }
  }
  const sorted = [...timestamps].sort((a, b) => a - b)
  let left = 0
  for (let right = 0; right < sorted.length; right++) {
    while (sorted[right] - sorted[left] > windowS) left += 1
    if (right - left + 1 >= count) {
      return { triggered: true, firstTs: sorted[left], lastTs: sorted[right] }
    }
  }
  return { triggered: false, firstTs: null, lastTs: null }
}

function detectLoginFailBurst(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byFp = new Map<string, number[]>()
  for (const row of rows) {
    if (readAction(row) !== EVENT_AUTH_LOGIN_FAIL) continue
    const after = readAfter(row)
    const fp = readAfterString(after, "attempted_user_fp")
    if (fp === null) continue
    if (!byFp.has(fp)) byFp.set(fp, [])
    byFp.get(fp)!.push(readTs(row))
  }
  const alerts: SuspiciousPatternAlert[] = []
  for (const [fp, timestamps] of byFp) {
    const r = hasWindowBurst(timestamps, threshold.count, threshold.windowS)
    if (!r.triggered) continue
    alerts.push(
      Object.freeze({
        rule: RULE_LOGIN_FAIL_BURST,
        severity: DEFAULT_RULE_SEVERITIES[RULE_LOGIN_FAIL_BURST],
        tenantId,
        evidence: Object.freeze({
          attempted_user_fp: fp,
          fail_count: timestamps.length,
          window_s: threshold.windowS,
          first_ts: r.firstTs,
          last_ts: r.lastTs,
          threshold: threshold.count,
        }),
      }),
    )
  }
  return alerts
}

function detectBotChallengeFailSpike(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byForm = new Map<string, number[]>()
  for (const row of rows) {
    if (readAction(row) !== EVENT_AUTH_BOT_CHALLENGE_FAIL) continue
    const after = readAfter(row)
    const formPath = readAfterString(after, "form_path")
    if (formPath === null) continue
    if (!byForm.has(formPath)) byForm.set(formPath, [])
    byForm.get(formPath)!.push(readTs(row))
  }
  const alerts: SuspiciousPatternAlert[] = []
  for (const [formPath, timestamps] of byForm) {
    const r = hasWindowBurst(timestamps, threshold.count, threshold.windowS)
    if (!r.triggered) continue
    alerts.push(
      Object.freeze({
        rule: RULE_BOT_CHALLENGE_FAIL_SPIKE,
        severity: DEFAULT_RULE_SEVERITIES[RULE_BOT_CHALLENGE_FAIL_SPIKE],
        tenantId,
        evidence: Object.freeze({
          form_path: formPath,
          fail_count: timestamps.length,
          window_s: threshold.windowS,
          first_ts: r.firstTs,
          last_ts: r.lastTs,
          threshold: threshold.count,
        }),
      }),
    )
  }
  return alerts
}

function detectTokenRefreshStorm(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byEntity = new Map<string, number[]>()
  for (const row of rows) {
    if (readAction(row) !== EVENT_AUTH_TOKEN_REFRESH) continue
    const eid = readEntityId(row)
    if (eid === "") continue
    if (!byEntity.has(eid)) byEntity.set(eid, [])
    byEntity.get(eid)!.push(readTs(row))
  }
  const alerts: SuspiciousPatternAlert[] = []
  for (const [eid, timestamps] of byEntity) {
    const r = hasWindowBurst(timestamps, threshold.count, threshold.windowS)
    if (!r.triggered) continue
    alerts.push(
      Object.freeze({
        rule: RULE_TOKEN_REFRESH_STORM,
        severity: DEFAULT_RULE_SEVERITIES[RULE_TOKEN_REFRESH_STORM],
        tenantId,
        evidence: Object.freeze({
          entity_id: eid,
          refresh_count: timestamps.length,
          window_s: threshold.windowS,
          first_ts: r.firstTs,
          last_ts: r.lastTs,
          threshold: threshold.count,
        }),
      }),
    )
  }
  return alerts
}

function detectHoneypotTriggered(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byForm = new Map<string, number[]>()
  for (const row of rows) {
    if (readAction(row) !== EVENT_AUTH_BOT_CHALLENGE_FAIL) continue
    const after = readAfter(row)
    if (after["reason"] !== BOT_CHALLENGE_FAIL_HONEYPOT) continue
    const formPath = readAfterString(after, "form_path")
    if (formPath === null) continue
    if (!byForm.has(formPath)) byForm.set(formPath, [])
    byForm.get(formPath)!.push(readTs(row))
  }
  const alerts: SuspiciousPatternAlert[] = []
  const minCount = Math.max(1, Math.trunc(threshold.count))
  for (const [formPath, timestamps] of byForm) {
    if (timestamps.length < minCount) continue
    const sorted = [...timestamps].sort((a, b) => a - b)
    alerts.push(
      Object.freeze({
        rule: RULE_HONEYPOT_TRIGGERED,
        severity: DEFAULT_RULE_SEVERITIES[RULE_HONEYPOT_TRIGGERED],
        tenantId,
        evidence: Object.freeze({
          form_path: formPath,
          trigger_count: sorted.length,
          first_ts: sorted[0],
          last_ts: sorted[sorted.length - 1],
          threshold: minCount,
        }),
      }),
    )
  }
  return alerts
}

function detectOAuthRevokeRelinkLoop(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byEntity = new Map<string, Array<{ readonly ts: number; readonly kind: string }>>()
  for (const row of rows) {
    const action = readAction(row)
    if (action !== EVENT_AUTH_OAUTH_CONNECT && action !== EVENT_AUTH_OAUTH_REVOKE) continue
    const eid = readEntityId(row)
    if (eid === "") continue
    if (!byEntity.has(eid)) byEntity.set(eid, [])
    byEntity.get(eid)!.push({ ts: readTs(row), kind: action })
  }
  const alerts: SuspiciousPatternAlert[] = []
  for (const [eid, events] of byEntity) {
    const sorted = [...events].sort((a, b) => a.ts - b.ts)
    const cycleTs: number[] = []
    let lastRevokeTs: number | null = null
    for (const ev of sorted) {
      if (ev.kind === EVENT_AUTH_OAUTH_REVOKE) {
        lastRevokeTs = ev.ts
      } else if (ev.kind === EVENT_AUTH_OAUTH_CONNECT && lastRevokeTs !== null) {
        cycleTs.push(ev.ts)
        lastRevokeTs = null
      }
    }
    const r = hasWindowBurst(cycleTs, threshold.count, threshold.windowS)
    if (!r.triggered) continue
    alerts.push(
      Object.freeze({
        rule: RULE_OAUTH_REVOKE_RELINK_LOOP,
        severity: DEFAULT_RULE_SEVERITIES[RULE_OAUTH_REVOKE_RELINK_LOOP],
        tenantId,
        evidence: Object.freeze({
          entity_id: eid,
          cycle_count: cycleTs.length,
          window_s: threshold.windowS,
          first_ts: r.firstTs,
          last_ts: r.lastTs,
          threshold: threshold.count,
        }),
      }),
    )
  }
  return alerts
}

function detectDistributedLoginFail(
  rows: ReadonlyArray<AuthAuditRow>,
  tenantId: string,
  threshold: RuleThreshold,
): SuspiciousPatternAlert[] {
  const byUser = new Map<string, Array<{ readonly ts: number; readonly ipFp: string }>>()
  for (const row of rows) {
    if (readAction(row) !== EVENT_AUTH_LOGIN_FAIL) continue
    const after = readAfter(row)
    const userFp = readAfterString(after, "attempted_user_fp")
    const ipFp = readAfterString(after, "ip_fp")
    if (userFp === null || ipFp === null) continue
    if (!byUser.has(userFp)) byUser.set(userFp, [])
    byUser.get(userFp)!.push({ ts: readTs(row), ipFp })
  }
  const alerts: SuspiciousPatternAlert[] = []
  for (const [userFp, events] of byUser) {
    const sorted = [...events].sort((a, b) => a.ts - b.ts)
    let left = 0
    const seenIps = new Map<string, number>()
    let triggered = false
    let firstTs: number | null = null
    let lastTs: number | null = null
    let winnerIps: string[] = []
    for (let right = 0; right < sorted.length; right++) {
      const cur = sorted[right]
      seenIps.set(cur.ipFp, (seenIps.get(cur.ipFp) ?? 0) + 1)
      while (cur.ts - sorted[left].ts > threshold.windowS) {
        const lh = sorted[left]
        const c = (seenIps.get(lh.ipFp) ?? 0) - 1
        if (c <= 0) seenIps.delete(lh.ipFp)
        else seenIps.set(lh.ipFp, c)
        left += 1
      }
      if (seenIps.size >= threshold.count) {
        triggered = true
        firstTs = sorted[left].ts
        lastTs = cur.ts
        winnerIps = [...seenIps.keys()].sort()
        break
      }
    }
    if (!triggered) continue
    alerts.push(
      Object.freeze({
        rule: RULE_DISTRIBUTED_LOGIN_FAIL,
        severity: DEFAULT_RULE_SEVERITIES[RULE_DISTRIBUTED_LOGIN_FAIL],
        tenantId,
        evidence: Object.freeze({
          attempted_user_fp: userFp,
          distinct_ip_count: winnerIps.length,
          ip_fps: Object.freeze(winnerIps),
          window_s: threshold.windowS,
          first_ts: firstTs,
          last_ts: lastTs,
          threshold: threshold.count,
        }),
      }),
    )
  }
  return alerts
}

const DETECTORS: Readonly<
  Record<
    string,
    (
      rows: ReadonlyArray<AuthAuditRow>,
      tenantId: string,
      threshold: RuleThreshold,
    ) => SuspiciousPatternAlert[]
  >
> = Object.freeze({
  [RULE_LOGIN_FAIL_BURST]: detectLoginFailBurst,
  [RULE_BOT_CHALLENGE_FAIL_SPIKE]: detectBotChallengeFailSpike,
  [RULE_TOKEN_REFRESH_STORM]: detectTokenRefreshStorm,
  [RULE_HONEYPOT_TRIGGERED]: detectHoneypotTriggered,
  [RULE_OAUTH_REVOKE_RELINK_LOOP]: detectOAuthRevokeRelinkLoop,
  [RULE_DISTRIBUTED_LOGIN_FAIL]: detectDistributedLoginFail,
})

export interface DetectOptions {
  readonly tenantId: string
  readonly thresholds?: Readonly<Record<string, Partial<RuleThreshold>>>
  readonly enabledRules?: ReadonlyArray<string>
}

function alertSortKey(alert: SuspiciousPatternAlert): string {
  for (const key of ["attempted_user_fp", "form_path", "entity_id"]) {
    const v = alert.evidence[key]
    if (typeof v === "string") return v
  }
  return ""
}

export function detectSuspiciousPatterns(
  rows: ReadonlyArray<AuthAuditRow>,
  opts: DetectOptions,
): ReadonlyArray<SuspiciousPatternAlert> {
  const ruleSet = opts.enabledRules ?? ALL_DASHBOARD_RULES
  const out: SuspiciousPatternAlert[] = []
  for (const rule of ruleSet) {
    if (!(rule in DETECTORS)) {
      throw new Error(`unknown dashboard rule: ${JSON.stringify(rule)}`)
    }
    const threshold = resolveThreshold(rule, opts.thresholds)
    out.push(...DETECTORS[rule](rows, opts.tenantId, threshold))
  }
  out.sort((a, b) => {
    const r = a.rule.localeCompare(b.rule)
    if (r !== 0) return r
    return alertSortKey(a).localeCompare(alertSortKey(b))
  })
  return Object.freeze(out)
}
