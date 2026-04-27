/**
 * AS.7.1 — Login page form helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * login page composes:
 *
 *   1. **Honeypot field-name resolver** mirroring the backend
 *      AS.6.4 `_anonymous`-tenant 30-day-rotating field name so the
 *      hidden input round-trips through `LoginRequest`'s
 *      `extra="allow"` and lands on the backend
 *      `validate_honeypot()` check. The Python twin lives at
 *      `backend/security/honeypot.py::honeypot_field_name`; the
 *      Node twin at `templates/_shared/honeypot/index.ts` uses
 *      `node:crypto`. **This** twin uses Web Crypto
 *      (`crypto.subtle.digest`) so it bundles for both browsers
 *      and the vitest jsdom environment.
 *
 *   2. **Unified login error** copy — collapses every backend 4xx
 *      shape into the four canonical strings the AS.7.1 design
 *      pinned. The contract (AS.0.7 §3.4 / AS.0.5 §6) is that
 *      "invalid email" and "invalid password" share one message
 *      so the response shape doesn't leak account existence.
 *
 *   3. **Shake-bump key** for the spring-shake error animation —
 *      monotonically increases on every fresh error so React can
 *      replay the keyframe via `key={errorBumpKey}` on the form
 *      wrapper.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - The 12-word `RARE_WORD_POOL` is byte-equal to the backend
 *     `_RARE_WORD_POOL` (drift guard test pins the array). Since
 *     it's `as const` frozen, no other module can patch it at
 *     runtime.
 *   - `honeypotFieldName()` is deterministic: same `(formPath,
 *     tenantId, epoch)` always yields the same name. Cross-worker
 *     / cross-tab derivation is trivially identical (Answer #1 of
 *     the SOP audit).
 *   - The unified-error helper reads no module state; it dispatches
 *     on the input args only.
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 *
 * Why we don't reuse `templates/_shared/honeypot/index.ts`: that
 * twin imports `node:crypto`, which Next.js refuses to bundle for
 * the client side. The two twins keep the same constants byte-equal
 * and a drift guard test (see `test/lib/auth/login-form-helpers.test.ts`)
 * pins them to each other so a backend rotation can't desync the
 * frontend silently.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — byte-equal to backend AS.4.1 / AS.6.4
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend canonical login form path (used as the SHA-256 prefix
 *  partition + the honeypot field-name prefix lookup key). Must
 *  byte-match `backend/security/honeypot_form_verifier.py
 *  ::FORM_PATH_LOGIN`. */
export const FORM_PATH_LOGIN: string = "/api/v1/auth/login"

/** Sentinel tenant_id for anonymous login forms (login / signup /
 *  password-reset before the user is identified). Byte-equal to
 *  `backend/security/honeypot_form_verifier.py::ANONYMOUS_TENANT_ID`. */
export const ANONYMOUS_TENANT_ID: string = "_anonymous"

/** 30-day rotation cadence per AS.0.7 §2.1, in seconds. Byte-equal
 *  to backend `HONEYPOT_ROTATION_PERIOD_SECONDS`. */
export const HONEYPOT_ROTATION_PERIOD_SECONDS: number = 30 * 86400

/** Per-form-path prefix for the generated honeypot field name.
 *  Byte-equal to backend `_FORM_PREFIXES`. Frozen — assigning to a
 *  key at runtime throws TypeError. */
export const FORM_PREFIXES: Readonly<Record<string, string>> = Object.freeze({
  "/api/v1/auth/login": "lg_",
  "/api/v1/auth/signup": "sg_",
  "/api/v1/auth/password-reset": "pr_",
  "/api/v1/auth/contact": "ct_",
})

/** 12-word rare pool per AS.0.7 §2.1. Frozen tuple — drift guard
 *  test pins this against the backend `_RARE_WORD_POOL`. */
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

/** Five required HTML attributes the hidden honeypot input must
 *  render with per AS.0.7 §2.6. Frozen object so the cross-twin
 *  drift guard locks the same set as Python `HONEYPOT_INPUT_ATTRS`.
 *  See `<AuthHoneypotField>` for the React rendering. */
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

/** CSS class name on the hidden field. Byte-equal to backend
 *  `OS_HONEYPOT_CLASS`. */
export const OS_HONEYPOT_CLASS: string = "os-honeypot-field"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Honeypot field-name resolver — Web Crypto async
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Return the current 30-day rotation epoch.
 *
 *  `nowMs` optional override (testing). Default: `Date.now()`.
 *  Byte-equal to backend `_current_epoch()`. */
export function currentEpoch(nowMs?: number): number {
  const ms = nowMs !== undefined ? nowMs : Date.now()
  return Math.floor(ms / 1000 / HONEYPOT_ROTATION_PERIOD_SECONDS)
}

/** Resolve the Web Crypto `subtle` instance, throwing a clear error
 *  when neither browser nor Node 16+ runtime is available. Done
 *  lazily so the module imports cleanly even in environments where
 *  the API is missing — only callers that *use* the honeypot path
 *  see the error surface. */
function _resolveSubtle(): SubtleCrypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || !c.subtle) {
    throw new Error(
      "AS.7.1 honeypot: SubtleCrypto unavailable (need Web Crypto " +
        "API — modern browser or Node ≥ 16). Falling back to a " +
        "missing-honeypot field would trigger backend form_drift.",
    )
  }
  return c.subtle
}

/** Compute SHA-256 of a UTF-8 string and return the lowercase hex
 *  digest (64 chars). Mirrors Python `hashlib.sha256(seed).hexdigest()`. */
async function _sha256Hex(seed: string): Promise<string> {
  const subtle = _resolveSubtle()
  const data = new TextEncoder().encode(seed)
  const buf = await subtle.digest("SHA-256", data)
  const bytes = new Uint8Array(buf)
  let hex = ""
  for (let i = 0; i < bytes.length; i += 1) {
    hex += bytes[i].toString(16).padStart(2, "0")
  }
  return hex
}

/** Convert the 64-hex-char SHA-256 digest into a `RARE_WORD_POOL`
 *  index. The 256-bit digest doesn't fit in JS's 53-bit safe-integer
 *  range, so we use BigInt for the modulo — same algorithm as the
 *  Node twin (`templates/_shared/honeypot/index.ts`). */
export function _hexDigestToWordIndex(hex: string): number {
  return Number(BigInt("0x" + hex) % BigInt(RARE_WORD_POOL.length))
}

/** Return the canonical honeypot field name for a (form, tenant,
 *  epoch) triple. Async because Web Crypto's `digest()` is. Output
 *  is byte-equal to the Python / Node twins for the same input. */
export async function honeypotFieldName(
  formPath: string,
  tenantId: string,
  epoch: number,
): Promise<string> {
  const prefix = FORM_PREFIXES[formPath]
  if (prefix === undefined) {
    throw new Error(
      `AS.7.1 honeypot: unknown form_path: ${JSON.stringify(formPath)} ` +
        `(supported: ${JSON.stringify(Object.keys(FORM_PREFIXES))})`,
    )
  }
  const seed = `${tenantId}:${epoch}`
  const digest = await _sha256Hex(seed)
  const idx = _hexDigestToWordIndex(digest)
  return prefix + RARE_WORD_POOL[idx]
}

/** Return `[currentEpochName, prevEpochName]` — both accepted by
 *  the backend `validate_honeypot` (the 30-day boundary 1-request
 *  grace per AS.0.7 §2.1). Frontend renders only the current-epoch
 *  field; the prev-epoch name is exposed for tests / drift guards. */
export async function expectedFieldNames(
  formPath: string,
  tenantId: string,
  nowMs?: number,
): Promise<readonly [string, string]> {
  const epochNow = currentEpoch(nowMs)
  const [now, prev] = await Promise.all([
    honeypotFieldName(formPath, tenantId, epochNow),
    honeypotFieldName(formPath, tenantId, epochNow - 1),
  ])
  return Object.freeze([now, prev] as const)
}

/** Resolve the field name the AS.7.1 login form should render in
 *  the hidden honeypot input. Convenience wrapper that pins the
 *  form_path + tenant_id pair to the login-anonymous tuple. */
export async function loginHoneypotFieldName(nowMs?: number): Promise<string> {
  return honeypotFieldName(FORM_PATH_LOGIN, ANONYMOUS_TENANT_ID, currentEpoch(nowMs))
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Unified error message normaliser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical AS.7.1 error copy — five kinds the login page UI
 *  branches on. Frozen `as const` so a new branch needs to update
 *  this table + the test in lockstep. */
export const LOGIN_ERROR_KIND = {
  invalidCredentials: "invalid_credentials",
  rateLimited: "rate_limited",
  accountLocked: "account_locked",
  botChallenge: "bot_challenge_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type LoginErrorKind =
  (typeof LOGIN_ERROR_KIND)[keyof typeof LOGIN_ERROR_KIND]

export interface LoginErrorOutcome {
  readonly kind: LoginErrorKind
  readonly message: string
  /** Set on the 423 / lockout path so the page can show the
   *  blue-tint frozen-overlay "account locked" visual. */
  readonly accountLocked: boolean
  /** Set on a 429 with Retry-After so the page can render a
   *  countdown next to the message. May be `null` if the header
   *  was absent or unparseable. */
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by `login-form-helpers.test.ts`; do
 *  not edit without updating the test. */
export const LOGIN_ERROR_COPY: Readonly<Record<LoginErrorKind, string>> =
  Object.freeze({
    invalid_credentials: "Invalid email or password.",
    rate_limited: "Too many attempts. Please wait a few minutes and retry.",
    account_locked:
      "This account is temporarily locked. Please wait before retrying.",
    bot_challenge_failed:
      "Verification failed. Please refresh the page and try again.",
    service_unavailable:
      "Login is temporarily unavailable. Please try again in a moment.",
  })

/** Parse a `Retry-After` HTTP header value into integer seconds.
 *  Returns `null` for empty / malformed inputs. Both delta-seconds
 *  and HTTP-date forms are supported (RFC 9110 §10.2.3). */
export function parseRetryAfter(header: string | null | undefined): number | null {
  if (!header) return null
  const trimmed = header.trim()
  if (!trimmed) return null
  // Numeric prefix path — treat anything that looks like a signed
  // integer (single optional `-`, digits) as the delta-seconds form.
  // Reject negative values outright (RFC §10.2.3 forbids them) so a
  // weird "-1" doesn't fall through to `Date.parse` and yield 0.
  if (/^-?\d+$/.test(trimmed)) {
    const asInt = Number(trimmed)
    if (Number.isFinite(asInt) && asInt >= 0) return asInt
    return null
  }
  const asDate = Date.parse(trimmed)
  if (Number.isFinite(asDate)) {
    const diff = Math.ceil((asDate - Date.now()) / 1000)
    return diff > 0 ? diff : 0
  }
  return null
}

interface LoginErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Map any backend response into the canonical `LoginErrorOutcome`
 *  the UI keys on. The page passes the parsed status / message /
 *  retry-after triple; this helper picks the right copy + flags
 *  the locked-overlay branch.
 *
 *  Status precedence (most specific → least specific):
 *    1. 423 → account locked
 *    2. 401 → invalid credentials (also catches the unified-error
 *             contract — caller can't distinguish bad-email vs
 *             bad-password)
 *    3. 429 with body code = "bot_challenge_failed" → bot reject
 *    4. 429 → rate limited
 *    5. ≥ 500 or status === null → service unavailable
 *    6. anything else → invalid credentials (defensive default —
 *       the design's unified-error contract means we never want a
 *       weird 4xx to expose unknown copy to the user)
 */
export function classifyLoginError(input: LoginErrorInput): LoginErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parseRetryAfter(input.retryAfter)

  if (status === 423) {
    return Object.freeze({
      kind: LOGIN_ERROR_KIND.accountLocked,
      message: LOGIN_ERROR_COPY.account_locked,
      accountLocked: true,
      retryAfterSeconds,
    })
  }
  if (status === 401) {
    return Object.freeze({
      kind: LOGIN_ERROR_KIND.invalidCredentials,
      message: LOGIN_ERROR_COPY.invalid_credentials,
      accountLocked: false,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: LOGIN_ERROR_KIND.botChallenge,
      message: LOGIN_ERROR_COPY.bot_challenge_failed,
      accountLocked: false,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: LOGIN_ERROR_KIND.rateLimited,
      message: LOGIN_ERROR_COPY.rate_limited,
      accountLocked: false,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: LOGIN_ERROR_KIND.serviceUnavailable,
      message: LOGIN_ERROR_COPY.service_unavailable,
      accountLocked: false,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: LOGIN_ERROR_KIND.invalidCredentials,
    message: LOGIN_ERROR_COPY.invalid_credentials,
    accountLocked: false,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Misc — shake-bump key
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Compute the next "shake bump" key from the previous one. The
 *  login form re-mounts its error-shake CSS animation by setting
 *  `key={errorBumpKey}` on the form wrapper; bumping the key on
 *  every fresh error is what triggers the spring-shake replay. */
export function bumpShakeKey(prev: number): number {
  return prev + 1
}
