/**
 * AS.4.1 — Honeypot field generator + bot detection (TypeScript twin).
 *
 * Behaviourally aligned with `backend/security/honeypot.py`. Provides the
 * canonical hidden-form-field name generator + validator that the AS.7.x
 * scaffolded apps wire onto in their generated server-side handlers.  The
 * 5-attribute spec (rare field name + off-screen CSS hide + `tabindex="-1"`
 * + `autocomplete="off"` + `aria-hidden="true"`) is frozen by AS.0.7 §2
 * and mirrored byte-for-byte across the two twins.
 *
 * Plan / spec source
 * ──────────────────
 *   * `docs/security/as_0_7_honeypot_field_design.md`
 *     — 12-word rare pool (§2.1), 4 form-prefix mapping (§2.1 / §4.1),
 *     5-attribute hidden-field invariant (§2), validate helper interface
 *     (§3.1), bypass short-circuit precedence (§3.3), 3-event audit
 *     family (§3.4), 30-day rotation epoch (§2.1), AS.0.5 phase metric
 *     decoupling (§3.5), AS.0.6 bypass interaction (§3.3),
 *     AS.0.8 single-knob noop (§4.3), drift guards (§8).
 *   * `docs/design/as-auth-security-shared-library.md` §3 — twin pattern.
 *
 * Cross-twin contract (enforced by AS.4.1 drift guard)
 * ────────────────────────────────────────────────────
 *   1. **12 rare words** — byte-equal across the two twins.
 *   2. **4 form-prefix entries** — same path → same prefix.
 *   3. **CSS class** — `"os-honeypot-field"`.
 *   4. **Hide CSS body** — `position:absolute;left:-9999px;...`,
 *      byte-equal across twins; off-screen positioning only — never
 *      `display:none` / `visibility:hidden`.
 *   5. **5 input attributes** — `tabindex` / `autocomplete` /
 *      `data-1p-ignore` / `data-lpignore` / `data-bwignore` /
 *      `aria-hidden` / `aria-label` (the canonical 5 + 2 password-manager
 *      ignores per AS.0.7 §2.4 / §2.5).
 *   6. **3 audit-event strings** — `bot_challenge.honeypot_pass` /
 *      `bot_challenge.honeypot_fail` / `bot_challenge.honeypot_form_drift`.
 *   7. **4 outcome literals** — `honeypot_pass` / `honeypot_fail` /
 *      `honeypot_form_drift` / `honeypot_bypass`.
 *   8. **30-day rotation period** — `30 * 86400` seconds.
 *   9. **Reject code + status** — `"bot_challenge_failed"` / `429`.
 *  10. **`honeypotFieldName` SHA-256 deterministic** — same triple
 *      `(form, tenant, epoch)` → same name across the two twins.
 *
 * Drift is caught by `backend/tests/test_honeypot_shape_drift.py`
 * (regex-extracted static pins + Node-spawned behavioural parity matrix).
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * No module-level mutable state — only frozen object literals,
 *     frozen arrays, classes, and pure functions.
 *   * The 4-prefix + 12-word + CSS class constants are
 *     `Object.freeze`d.  The result objects from validateHoneypot are
 *     `Object.freeze`d before return (mirrors Python `frozen=True`
 *     dataclass).
 *   * No env reads at module top-level — `isEnabled()` reads
 *     `OMNISIGHT_AS_FRONTEND_ENABLED` lazily on every call.  Each
 *     browser tab / Node worker derives the same value from the same
 *     env source — answer #1 of SOP §1 audit (deterministic-by-
 *     construction across workers).
 *   * Importing the module is free of side effects.
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 *   * `isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the
 *     **frontend** twin of the Python `settings.as_enabled` —
 *     deliberately decoupled per AS.0.8 §2.5).  Default `true`.
 *   * `validateHoneypot()` short-circuits with a bypass-shape result
 *     when knob-off, matching the Python lib.
 */

import { createHash } from "node:crypto"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — frozen mappings + arrays (AS.0.7 §2.1 / §4.1)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Per-form prefix mapping per AS.0.7 §4.1.  Frozen — assigning to a
 * key at runtime throws `TypeError`.  Cross-twin drift guard locks the
 * 4-entry pair set against the Python `_FORM_PREFIXES`. */
export const FORM_PREFIXES: Readonly<Record<string, string>> = Object.freeze({
  "/api/v1/auth/login": "lg_",
  "/api/v1/auth/signup": "sg_",
  "/api/v1/auth/password-reset": "pr_",
  "/api/v1/auth/contact": "ct_",
})

export type FormPath = keyof typeof FORM_PREFIXES

/** 12-word rare pool per AS.0.7 §2.1.  Frozen tuple — see Python
 * `_RARE_WORD_POOL` for selection rationale (no WHATWG autocomplete
 * collision, no OmniSight existing form-name collision, plausible
 * enough that naive form-fill bots will populate them). */
export const RARE_WORD_POOL: ReadonlyArray<string> = Object.freeze([
  "fax_office",
  "secondary_address",
  "company_role",
  "alt_contact",
  "referral_source",
  "marketing_pref",
  "newsletter_freq",
  "preferred_language",
  "fax_number",
  "secondary_email",
  "alt_phone",
  "office_extension",
])

/** CSS class name on the hidden field.  AS.0.7 §2.2 invariant: the
 * hide style MUST be off-screen positioning; `display:none` /
 * `visibility:hidden` are forbidden (Selenium / Playwright headless
 * skip them, defeating the trap). */
export const OS_HONEYPOT_CLASS: string = "os-honeypot-field"

/** Canonical CSS rule body (newline-stripped) for the off-screen hide
 * style.  Byte-equal to Python `HONEYPOT_HIDE_CSS`. */
export const HONEYPOT_HIDE_CSS: string =
  "position:absolute;left:-9999px;top:auto;" +
  "width:1px;height:1px;overflow:hidden;"

/** Five required HTML attributes per AS.0.7 §2.6 — every honeypot
 * input must render with all of these.  Frozen object so the cross-
 * twin drift guard locks the same set as Python `HONEYPOT_INPUT_ATTRS`. */
export const HONEYPOT_INPUT_ATTRS: Readonly<Record<string, string>> =
  Object.freeze({
    tabindex: "-1",
    autocomplete: "off",
    "data-1p-ignore": "true",
    "data-lpignore": "true",
    "data-bwignore": "true",
    "aria-hidden": "true",
    "aria-label": "Do not fill",
  })

/** 30-day rotation cadence per AS.0.7 §2.1, in seconds. */
export const HONEYPOT_ROTATION_PERIOD_SECONDS: number = 30 * 86400

/** Same surface as AS.3.4 `BOT_CHALLENGE_REJECTED_CODE` so the front-
 * end UI keys on a single error code regardless of which AS layer
 * caught the bot. */
export const HONEYPOT_REJECTED_CODE: string = "bot_challenge_failed"

/** Same HTTP status as AS.3.4 — 429 (rate-limit class) over 401 (auth
 * class) deliberately, denying the per-failure-mode side-channel. */
export const HONEYPOT_REJECTED_HTTP_STATUS: number = 429

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Audit event canonical names — AS.0.7 §3.4
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const EVENT_BOT_CHALLENGE_HONEYPOT_PASS: string =
  "bot_challenge.honeypot_pass"
export const EVENT_BOT_CHALLENGE_HONEYPOT_FAIL: string =
  "bot_challenge.honeypot_fail"
export const EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT: string =
  "bot_challenge.honeypot_form_drift"

export const ALL_HONEYPOT_EVENTS: ReadonlyArray<string> = Object.freeze([
  EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
  EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
  EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
])

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Outcome literals
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const OUTCOME_HONEYPOT_PASS: string = "honeypot_pass"
export const OUTCOME_HONEYPOT_FAIL: string = "honeypot_fail"
export const OUTCOME_HONEYPOT_FORM_DRIFT: string = "honeypot_form_drift"
export const OUTCOME_HONEYPOT_BYPASS: string = "honeypot_bypass"

export const ALL_HONEYPOT_OUTCOMES: ReadonlyArray<string> = Object.freeze([
  OUTCOME_HONEYPOT_PASS,
  OUTCOME_HONEYPOT_FAIL,
  OUTCOME_HONEYPOT_FORM_DRIFT,
  OUTCOME_HONEYPOT_BYPASS,
])

const _OUTCOME_TO_EVENT: Readonly<Record<string, string | null>> = Object.freeze({
  [OUTCOME_HONEYPOT_PASS]: EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
  [OUTCOME_HONEYPOT_FAIL]: EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
  [OUTCOME_HONEYPOT_FORM_DRIFT]: EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
  [OUTCOME_HONEYPOT_BYPASS]: null,
})

/** Return the canonical `bot_challenge.honeypot_*` event string for an
 * outcome literal, or `null` for the bypass outcome (caller emits the
 * AS.0.6 `bypass_*` event from its own layer).  Throws on unknown. */
export function eventForHoneypotOutcome(outcome: string): string | null {
  if (!(outcome in _OUTCOME_TO_EVENT)) {
    throw new Error(`unknown honeypot outcome: ${JSON.stringify(outcome)}`)
  }
  return _OUTCOME_TO_EVENT[outcome]
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Failure reason vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const FAILURE_REASON_FIELD_FILLED: string = "field_filled"
export const FAILURE_REASON_FIELD_MISSING_IN_FORM: string =
  "field_missing_in_form"
export const FAILURE_REASON_FORM_PATH_UNKNOWN: string = "form_path_unknown"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Bypass-kind vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const BYPASS_KIND_API_KEY: string = "apikey"
export const BYPASS_KIND_TEST_TOKEN: string = "test_token"
export const BYPASS_KIND_IP_ALLOWLIST: string = "ip_allowlist"
export const BYPASS_KIND_KNOB_OFF: string = "knob_off"
export const BYPASS_KIND_TENANT_DISABLED: string = "tenant_disabled"

export const ALL_BYPASS_KINDS: ReadonlyArray<string> = Object.freeze([
  BYPASS_KIND_API_KEY,
  BYPASS_KIND_TEST_TOKEN,
  BYPASS_KIND_IP_ALLOWLIST,
  BYPASS_KIND_KNOB_OFF,
  BYPASS_KIND_TENANT_DISABLED,
])

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Result type — frozen on construction to mirror Python's frozen=True
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface HoneypotResult {
  readonly allow: boolean
  readonly outcome: string
  readonly auditEvent: string | null
  readonly bypassKind: string | null
  readonly fieldNameUsed: string | null
  readonly failureReason: string | null
  readonly auditMetadata: Readonly<Record<string, unknown>>
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Errors
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export class HoneypotError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "HoneypotError"
  }
}

export class HoneypotRejected extends HoneypotError {
  readonly result: HoneypotResult
  readonly code: string
  readonly httpStatus: number

  constructor(
    result: HoneypotResult,
    opts?: { code?: string; httpStatus?: number },
  ) {
    const code = opts?.code ?? HONEYPOT_REJECTED_CODE
    const httpStatus = opts?.httpStatus ?? HONEYPOT_REJECTED_HTTP_STATUS
    super(
      `honeypot rejected: outcome=${result.outcome} ` +
        `reason=${result.failureReason ?? "null"} ` +
        `code=${code} http_status=${httpStatus}`,
    )
    this.name = "HoneypotRejected"
    this.result = result
    this.code = code
    this.httpStatus = httpStatus
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  is_enabled — AS.0.8 single-knob hook
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Whether the AS family is enabled per AS.0.8 §3.1 noop matrix.
 *
 * Reads `OMNISIGHT_AS_FRONTEND_ENABLED` lazily — defaults to `true`
 * (forward-promotion guard).  Mirrors the Python lib's
 * `is_enabled()`.
 */
export function isEnabled(): boolean {
  // Node side: read process.env.  Browser side: feature flag /
  // build-time inline (out of scope for the lib).
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
//  Field-name generator (AS.0.7 §2.1)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Return the supported form paths.  Mirrors Python
 * `supported_form_paths()`. */
export function supportedFormPaths(): ReadonlyArray<string> {
  return Object.freeze(Object.keys(FORM_PREFIXES))
}

/** Return the current 30-day rotation epoch.
 *
 * `nowMs` optional override (testing).  Default: `Date.now()`. */
export function currentEpoch(nowMs?: number): number {
  const ms = nowMs !== undefined ? nowMs : Date.now()
  return Math.floor(ms / 1000 / HONEYPOT_ROTATION_PERIOD_SECONDS)
}

/** Return the canonical honeypot field name for a (form, tenant,
 * epoch) triple.  SHA-256 deterministic — same input → same output as
 * the Python twin. */
export function honeypotFieldName(
  formPath: string,
  tenantId: string,
  epoch: number,
): string {
  const prefix = FORM_PREFIXES[formPath]
  if (prefix === undefined) {
    throw new Error(
      `unknown form_path: ${JSON.stringify(formPath)} ` +
        `(supported: ${JSON.stringify(Object.keys(FORM_PREFIXES))})`,
    )
  }
  const seed = `${tenantId}:${epoch}`
  const digest = createHash("sha256").update(seed, "utf-8").digest("hex")
  // BigInt conversion is required because the 64-hex-char digest doesn't
  // fit in JavaScript's 53-bit safe-integer range.
  const idx = Number(BigInt("0x" + digest) % BigInt(RARE_WORD_POOL.length))
  return prefix + RARE_WORD_POOL[idx]
}

/** Return `[currentEpochName, prevEpochName]` — both accepted by
 * `validateHoneypot` (30-day boundary 1-request grace per AS.0.7 §2.1). */
export function expectedFieldNames(
  formPath: string,
  tenantId: string,
  nowMs?: number,
): readonly [string, string] {
  const epochNow = currentEpoch(nowMs)
  return Object.freeze([
    honeypotFieldName(formPath, tenantId, epochNow),
    honeypotFieldName(formPath, tenantId, epochNow - 1),
  ] as const)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Validator — pure function over the form submission (AS.0.7 §3.1)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function _bypassResult(formPath: string, bypassKind: string): HoneypotResult {
  return Object.freeze({
    allow: true,
    outcome: OUTCOME_HONEYPOT_BYPASS,
    auditEvent: null,
    bypassKind,
    fieldNameUsed: null,
    failureReason: null,
    auditMetadata: Object.freeze({
      form_path: formPath,
      bypass_kind: bypassKind,
    }),
  })
}

function _valueIsFilled(raw: unknown): boolean {
  if (raw === null || raw === undefined) return false
  if (typeof raw === "string") return raw.trim().length > 0
  if (Array.isArray(raw)) return raw.some(_valueIsFilled)
  return String(raw).trim().length > 0
}

function _valueLength(raw: unknown): number {
  if (raw === null || raw === undefined) return 0
  if (typeof raw === "string") return raw.length
  if (Array.isArray(raw))
    return raw.reduce((acc: number, v) => acc + _valueLength(v), 0)
  return String(raw).length
}

export interface ValidateOptions {
  bypassKind?: string | null
  tenantHoneypotActive?: boolean
  nowMs?: number
}

/** Validate a form submission against the honeypot field.  See the
 * Python `validate_honeypot` docstring for the precedence rules. */
export function validateHoneypot(
  formPath: string,
  tenantId: string,
  submitted: Readonly<Record<string, unknown>>,
  opts: ValidateOptions = {},
): HoneypotResult {
  const bypassKind = opts.bypassKind ?? null
  const tenantHoneypotActive = opts.tenantHoneypotActive ?? true

  // 1. AS.0.8 single-knob: knob-off overrides everything.
  if (!isEnabled()) {
    return _bypassResult(formPath, BYPASS_KIND_KNOB_OFF)
  }

  // 2. AS.0.6 axis hit → caller pre-detected; trust + short-circuit.
  if (bypassKind) {
    if (!ALL_BYPASS_KINDS.includes(bypassKind)) {
      throw new Error(
        `unknown bypass_kind: ${JSON.stringify(bypassKind)} ` +
          `(supported: ${JSON.stringify(ALL_BYPASS_KINDS)})`,
      )
    }
    return _bypassResult(formPath, bypassKind)
  }

  // 3. AS.0.7 §4.3 per-tenant opt-out.
  if (!tenantHoneypotActive) {
    return _bypassResult(formPath, BYPASS_KIND_TENANT_DISABLED)
  }

  // 4. Form path must be one of the 4 known.
  if (!(formPath in FORM_PREFIXES)) {
    return Object.freeze({
      allow: false,
      outcome: OUTCOME_HONEYPOT_FORM_DRIFT,
      auditEvent: EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
      bypassKind: null,
      fieldNameUsed: null,
      failureReason: FAILURE_REASON_FORM_PATH_UNKNOWN,
      auditMetadata: Object.freeze({
        form_path: formPath,
        supported_paths: Object.freeze([...Object.keys(FORM_PREFIXES)]),
      }),
    })
  }

  const epochNow = currentEpoch(opts.nowMs)
  const nameNow = honeypotFieldName(formPath, tenantId, epochNow)
  const namePrev = honeypotFieldName(formPath, tenantId, epochNow - 1)

  const submittedKeys = Object.keys(submitted)
  const hasNow = submittedKeys.includes(nameNow)
  const hasPrev = submittedKeys.includes(namePrev)

  // 5. Field-missing-in-form: frontend deploy-drift alarm.
  if (!hasNow && !hasPrev) {
    return Object.freeze({
      allow: false,
      outcome: OUTCOME_HONEYPOT_FORM_DRIFT,
      auditEvent: EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
      bypassKind: null,
      fieldNameUsed: nameNow,
      failureReason: FAILURE_REASON_FIELD_MISSING_IN_FORM,
      auditMetadata: Object.freeze({
        form_path: formPath,
        epoch: epochNow,
        expected_field_names: Object.freeze([nameNow, namePrev]),
        submitted_keys: Object.freeze([...submittedKeys].sort()),
      }),
    })
  }

  // 6 / 7. Pull the value from whichever epoch matched (prefer current).
  const fieldUsed = hasNow ? nameNow : namePrev
  const rawValue = submitted[fieldUsed]

  if (_valueIsFilled(rawValue)) {
    return Object.freeze({
      allow: false,
      outcome: OUTCOME_HONEYPOT_FAIL,
      auditEvent: EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
      bypassKind: null,
      fieldNameUsed: fieldUsed,
      failureReason: FAILURE_REASON_FIELD_FILLED,
      auditMetadata: Object.freeze({
        form_path: formPath,
        epoch: epochNow,
        field_filled_length: _valueLength(rawValue),
      }),
    })
  }

  return Object.freeze({
    allow: true,
    outcome: OUTCOME_HONEYPOT_PASS,
    auditEvent: EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
    bypassKind: null,
    fieldNameUsed: fieldUsed,
    failureReason: null,
    auditMetadata: Object.freeze({
      form_path: formPath,
      epoch: epochNow,
    }),
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Reject-enforcement primitives — mirror AS.3.4 surface
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Pure predicate: `!result.allow`. */
export function shouldReject(result: HoneypotResult): boolean {
  return !result.allow
}

/** Run `validateHoneypot`; on a reject result, throw `HoneypotRejected`.
 * On pass / bypass, return the result. */
export function validateAndEnforce(
  formPath: string,
  tenantId: string,
  submitted: Readonly<Record<string, unknown>>,
  opts: ValidateOptions = {},
): HoneypotResult {
  const result = validateHoneypot(formPath, tenantId, submitted, opts)
  if (shouldReject(result)) {
    throw new HoneypotRejected(result)
  }
  return result
}
