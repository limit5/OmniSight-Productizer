/**
 * AS.3.2 — Bot challenge unified interface (TypeScript twin).
 *
 * Behaviourally aligned with `backend/security/bot_challenge.py`. Provides
 * the four-provider unified surface (Turnstile / reCAPTCHA v2 / reCAPTCHA
 * v3 / hCaptcha) for emission into the generated-app workspace, where
 * scaffolded apps wire their own `login` / `signup` / `password-reset` /
 * `contact` forms onto the same provider-agnostic verify entry point.
 *
 * Plan / spec source
 * ──────────────────
 *   * `docs/security/as_0_5_turnstile_fail_open_phased_strategy.md`
 *     — phase semantics, fail-open invariant, audit event canonical
 *     names (§3, 13 + 4 `EVENT_BOT_CHALLENGE_*` strings), bypass-list
 *     precedence (§4), provider site-secret env wiring (§5).
 *   * `docs/security/as_0_6_automation_bypass_list.md`
 *     — three bypass mechanisms (API key auth / per-tenant IP allowlist
 *     / test-token header), axis-internal precedence A → C → B (§4),
 *     audit metadata schema (§3), 2 extra `bypass_*` events (§3).
 *   * `docs/design/as-auth-security-shared-library.md` §3
 *     — TS twin contract sketch.
 *
 * Cross-twin contract (enforced by AS.3.2 drift guard)
 * ────────────────────────────────────────────────────
 * What stays byte-equal across the Python and TS twin:
 *
 *   1. **Provider enum values** — `"turnstile" / "recaptcha_v2" /
 *      "recaptcha_v3" / "hcaptcha"`.
 *   2. **Siteverify URLs** — the 4 vendor `/siteverify` endpoints.
 *   3. **19 audit event strings** — 8 verify outcomes + 7 bypass +
 *      4 phase-advance / revert. AS.0.5 §3 + AS.0.6 §3 invariant.
 *   4. **15 outcome literals** — 4 verify outcomes + 7 bypass + 4
 *      jsfail flavours. Drives the `auditEvent` lookup table.
 *   5. **Numeric defaults** — `DEFAULT_SCORE_THRESHOLD = 0.5` (AS.0.5
 *      §2.4 + design doc §3.5), `DEFAULT_VERIFY_TIMEOUT_SECONDS = 3.0`,
 *      `TEST_TOKEN_HEADER = "X-OmniSight-Test-Token"`.
 *   6. **Phase-aware classifier behaviour** — same 3-phase fail-open /
 *      fail-closed matrix; same provider-side score calibration
 *      (Turnstile / reCAPTCHA v3 → vendor float, v2 / hCaptcha → 1.0
 *      on success / 0.0 on failure).
 *   7. **Bypass axis precedence** — A (api_key) → C (test_token) →
 *      B (ip_allowlist) → D (path) per AS.0.6 §4.
 *   8. **Same three typed errors** — `BotChallengeError` (base),
 *      `ProviderConfigError`, `InvalidProviderError`.
 *
 * Drift is caught by `backend/tests/test_bot_challenge_shape_drift.py`
 * (AS.1.5 / AS.2.3-style cross-twin parity test, regex-extracted static
 * pins + Node-spawned behavioural parity matrix).
 *
 * Frontend deployment topology
 * ────────────────────────────
 * The four siteverify endpoints take a `secret` query arg that **must
 * never** ship to the browser. Two emission shapes:
 *
 *   * **Server-side TS** (Node SSR / edge worker / `next/server`) —
 *     `verifyProvider` + `verify` are called with the secret loaded
 *     from `process.env`, in the same way the Python lib reads
 *     `OMNISIGHT_TURNSTILE_SECRET` etc. This is the typical generated-
 *     app shape.
 *   * **Pure-browser TS** — the browser captures the widget token then
 *     POSTs it to its own backend `/api/v1/bot-challenge/verify`
 *     endpoint, which calls `verifyProvider` server-side. This module
 *     supplies the contract surface (enums, errors, types) the
 *     fetch-handler can use to type its request / response.
 *
 * The two shapes share the same `BotChallengeResult` envelope so a
 * frontend caller reads `result.allow` to decide 4xx vs continue
 * regardless of which side actually called the vendor.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * No module-level mutable state — only frozen `Set`s, frozen
 *     arrays, frozen object literals, classes, and pure functions.
 *   * The four siteverify URLs live in a `Object.freeze`d map.
 *   * No env reads at module top-level — `isEnabled()` reads
 *     `OMNISIGHT_AS_FRONTEND_ENABLED` lazily on every call. Each
 *     browser tab / Node worker derives the same value from the same
 *     env source — answer #1 of SOP §1 audit (deterministic-by-
 *     construction across workers).
 *   * Importing the module is free of side effects.
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 *   * `isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the
 *     **frontend** twin of the Python `settings.as_enabled` —
 *     deliberately decoupled per AS.0.8 §2.5). Default `true`.
 *   * `verify()` short-circuits with `passthrough()` (i.e.
 *     `outcome="pass"` + `score=1.0` + no audit emit) when knob-off,
 *     matching the Python lib's AS.0.5 §4 precedence axis #2.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — providers, endpoints, envs, defaults
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** The four bot-challenge vendors AS.3 ships support for. Values are
 * the canonical short strings used in audit metadata, config envs, and
 * Python twin enum.
 *
 * Implemented as a frozen literal-typed object rather than a TypeScript
 * `enum` so the twin loads cleanly under Node's `--experimental-strip-
 * types` mode (which the AS.1.5 / AS.2.3 cross-twin parity harness
 * uses; `enum` requires actual transpile, not type-stripping). */
export const Provider = Object.freeze({
  TURNSTILE: "turnstile",
  RECAPTCHA_V2: "recaptcha_v2",
  RECAPTCHA_V3: "recaptcha_v3",
  HCAPTCHA: "hcaptcha",
} as const)
export type Provider = (typeof Provider)[keyof typeof Provider]

/** Canonical siteverify endpoints per provider. Frozen object — assigning
 * to a key on this map at runtime throws `TypeError`. Mirrors the Python
 * `SITEVERIFY_URLS` `MappingProxyType`. */
export const SITEVERIFY_URLS: Readonly<Record<Provider, string>> =
  Object.freeze({
    [Provider.TURNSTILE]:
      "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    [Provider.RECAPTCHA_V2]:
      "https://www.google.com/recaptcha/api/siteverify",
    [Provider.RECAPTCHA_V3]:
      "https://www.google.com/recaptcha/api/siteverify",
    [Provider.HCAPTCHA]: "https://hcaptcha.com/siteverify",
  })

/** Per-provider env-var name carrying the site secret. AS.0.5 §5: each
 * provider has its own env, no env may be reused across providers,
 * **except** reCAPTCHA v2 + v3 share an env (same Google project, two
 * site keys; Google's `/siteverify` dispatches on the secret-key version
 * internally). */
export function secretEnvFor(provider: Provider): string {
  if (provider === Provider.RECAPTCHA_V2 || provider === Provider.RECAPTCHA_V3) {
    return "OMNISIGHT_RECAPTCHA_SECRET"
  }
  if (provider === Provider.TURNSTILE) return "OMNISIGHT_TURNSTILE_SECRET"
  if (provider === Provider.HCAPTCHA) return "OMNISIGHT_HCAPTCHA_SECRET"
  throw new InvalidProviderError(`unknown provider: ${String(provider)}`)
}

/** Default fail-mode score threshold per AS.0.5 §2.4 + design doc §3.5
 * (`score < 0.5` → reject in Phase 3 fail-closed branch). Pinned at
 * 0.5 — design doc §10 explicitly forbids stricter thresholds because
 * vendor calibrations differ; raising it amplifies false-positives. */
export const DEFAULT_SCORE_THRESHOLD = 0.5

/** Default HTTP timeout for siteverify calls, in seconds. 3 s is the
 * upper bound every vendor's SLA covers; longer means we're queueing
 * user-visible latency on a captcha that's almost certainly already
 * mis-configured. */
export const DEFAULT_VERIFY_TIMEOUT_SECONDS = 3.0

/** Test-token header name (AS.0.6 §2.3 invariant). Constant — drift
 * guard test asserts no inline string anywhere in callers. */
export const TEST_TOKEN_HEADER = "X-OmniSight-Test-Token"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Audit event canonical names — AS.0.5 §3 + AS.0.6 §3
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

// Verify-outcome events (8) — emitted from `verify` once classification
// completes. Strings are part of the AS-roadmap contract; tests pin them.
export const EVENT_BOT_CHALLENGE_PASS = "bot_challenge.pass"
export const EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE = "bot_challenge.unverified_lowscore"
export const EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR = "bot_challenge.unverified_servererr"
export const EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE = "bot_challenge.blocked_lowscore"
export const EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA = "bot_challenge.jsfail_fallback_recaptcha"
export const EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA = "bot_challenge.jsfail_fallback_hcaptcha"
export const EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS = "bot_challenge.jsfail_honeypot_pass"
export const EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL = "bot_challenge.jsfail_honeypot_fail"

// Bypass events (7) — emitted from the bypass branch of `verify`.
// AS.0.5 §3 ships 5, AS.0.6 §3 adds 2. Five + two = seven always-on.
export const EVENT_BOT_CHALLENGE_BYPASS_APIKEY = "bot_challenge.bypass_apikey"
export const EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK = "bot_challenge.bypass_webhook"
export const EVENT_BOT_CHALLENGE_BYPASS_CHATOPS = "bot_challenge.bypass_chatops"
export const EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP = "bot_challenge.bypass_bootstrap"
export const EVENT_BOT_CHALLENGE_BYPASS_PROBE = "bot_challenge.bypass_probe"
export const EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST = "bot_challenge.bypass_ip_allowlist"
export const EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN = "bot_challenge.bypass_test_token"

// Phase advance / revert events (4) — AS.5.2 dashboard owns the
// emitter helper; the strings live here because this is the bot-
// challenge family's namespace SoT.
export const EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2 = "bot_challenge.phase_advance_p1_to_p2"
export const EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3 = "bot_challenge.phase_advance_p2_to_p3"
export const EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2 = "bot_challenge.phase_revert_p3_to_p2"
export const EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1 = "bot_challenge.phase_revert_p2_to_p1"

export const ALL_BOT_CHALLENGE_EVENTS: readonly string[] = Object.freeze([
  EVENT_BOT_CHALLENGE_PASS,
  EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
  EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
  EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
  EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA,
  EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA,
  EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS,
  EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL,
  EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
  EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK,
  EVENT_BOT_CHALLENGE_BYPASS_CHATOPS,
  EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP,
  EVENT_BOT_CHALLENGE_BYPASS_PROBE,
  EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST,
  EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN,
  EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2,
  EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3,
  EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2,
  EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1,
])

// Outcome literal vocabulary — each result row carries one of these as
// `BotChallengeResult.outcome`. `eventForOutcome` maps each to one of
// the `EVENT_BOT_CHALLENGE_*` strings above.
export const OUTCOME_PASS = "pass"
export const OUTCOME_UNVERIFIED_LOWSCORE = "unverified_lowscore"
export const OUTCOME_UNVERIFIED_SERVERERR = "unverified_servererr"
export const OUTCOME_BLOCKED_LOWSCORE = "blocked_lowscore"
export const OUTCOME_BYPASS_APIKEY = "bypass_apikey"
export const OUTCOME_BYPASS_WEBHOOK = "bypass_webhook"
export const OUTCOME_BYPASS_CHATOPS = "bypass_chatops"
export const OUTCOME_BYPASS_BOOTSTRAP = "bypass_bootstrap"
export const OUTCOME_BYPASS_PROBE = "bypass_probe"
export const OUTCOME_BYPASS_IP_ALLOWLIST = "bypass_ip_allowlist"
export const OUTCOME_BYPASS_TEST_TOKEN = "bypass_test_token"
export const OUTCOME_JSFAIL_FALLBACK_RECAPTCHA = "jsfail_fallback_recaptcha"
export const OUTCOME_JSFAIL_FALLBACK_HCAPTCHA = "jsfail_fallback_hcaptcha"
export const OUTCOME_JSFAIL_HONEYPOT_PASS = "jsfail_honeypot_pass"
export const OUTCOME_JSFAIL_HONEYPOT_FAIL = "jsfail_honeypot_fail"

export const ALL_OUTCOMES: readonly string[] = Object.freeze([
  OUTCOME_PASS,
  OUTCOME_UNVERIFIED_LOWSCORE,
  OUTCOME_UNVERIFIED_SERVERERR,
  OUTCOME_BLOCKED_LOWSCORE,
  OUTCOME_BYPASS_APIKEY,
  OUTCOME_BYPASS_WEBHOOK,
  OUTCOME_BYPASS_CHATOPS,
  OUTCOME_BYPASS_BOOTSTRAP,
  OUTCOME_BYPASS_PROBE,
  OUTCOME_BYPASS_IP_ALLOWLIST,
  OUTCOME_BYPASS_TEST_TOKEN,
  OUTCOME_JSFAIL_FALLBACK_RECAPTCHA,
  OUTCOME_JSFAIL_FALLBACK_HCAPTCHA,
  OUTCOME_JSFAIL_HONEYPOT_PASS,
  OUTCOME_JSFAIL_HONEYPOT_FAIL,
])

const _OUTCOME_TO_EVENT: Readonly<Record<string, string>> = Object.freeze({
  [OUTCOME_PASS]: EVENT_BOT_CHALLENGE_PASS,
  [OUTCOME_UNVERIFIED_LOWSCORE]: EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
  [OUTCOME_UNVERIFIED_SERVERERR]: EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
  [OUTCOME_BLOCKED_LOWSCORE]: EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
  [OUTCOME_BYPASS_APIKEY]: EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
  [OUTCOME_BYPASS_WEBHOOK]: EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK,
  [OUTCOME_BYPASS_CHATOPS]: EVENT_BOT_CHALLENGE_BYPASS_CHATOPS,
  [OUTCOME_BYPASS_BOOTSTRAP]: EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP,
  [OUTCOME_BYPASS_PROBE]: EVENT_BOT_CHALLENGE_BYPASS_PROBE,
  [OUTCOME_BYPASS_IP_ALLOWLIST]: EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST,
  [OUTCOME_BYPASS_TEST_TOKEN]: EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN,
  [OUTCOME_JSFAIL_FALLBACK_RECAPTCHA]: EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA,
  [OUTCOME_JSFAIL_FALLBACK_HCAPTCHA]: EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA,
  [OUTCOME_JSFAIL_HONEYPOT_PASS]: EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS,
  [OUTCOME_JSFAIL_HONEYPOT_FAIL]: EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL,
})

/** Return the canonical `bot_challenge.*` event string for an outcome
 * literal. Throws `BotChallengeError` on unknown outcome. */
export function eventForOutcome(outcome: string): string {
  const ev = _OUTCOME_TO_EVENT[outcome]
  if (ev === undefined) {
    throw new BotChallengeError(`unknown outcome: ${JSON.stringify(outcome)}`)
  }
  return ev
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Bypass list — AS.0.5 §8.1 + AS.0.6 §2.1 / §2.4
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Path-prefix bypass list (AS.0.1 §4.5 inventory). The drift guard
 * test asserts the same nine prefixes the Python side ships. */
export const BYPASS_PATH_PREFIXES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    "/api/v1/livez",
    "/api/v1/readyz",
    "/api/v1/healthz",
    "/api/v1/bootstrap/",
    "/api/v1/webhooks/",
    "/api/v1/chatops/webhook/",
    "/api/v1/auth/oidc/",
    "/api/v1/auth/mfa/challenge",
    "/api/v1/auth/mfa/webauthn/challenge/",
  ]),
)

/** Caller-kind bypass list (AS.0.5 §8.1 + AS.0.6 §2.1). The audit row
 * carries the granular `caller_kind` so the dashboard can split
 * `bypass_apikey` rows by which key family was used. */
export const BYPASS_CALLER_KINDS: ReadonlySet<string> = Object.freeze(
  new Set<string>(["apikey_omni", "apikey_legacy", "metrics_token"]),
)

/** Path → bypass-outcome dispatch. Order matters: longer prefix wins so
 * `/api/v1/bootstrap/init` routes to `bypass_bootstrap` not via webhooks. */
const _PATH_PREFIX_TO_OUTCOME: ReadonlyArray<readonly [string, string]> =
  Object.freeze([
    ["/api/v1/livez", OUTCOME_BYPASS_PROBE],
    ["/api/v1/readyz", OUTCOME_BYPASS_PROBE],
    ["/api/v1/healthz", OUTCOME_BYPASS_PROBE],
    ["/api/v1/bootstrap/", OUTCOME_BYPASS_BOOTSTRAP],
    ["/api/v1/chatops/webhook/", OUTCOME_BYPASS_CHATOPS],
    ["/api/v1/webhooks/", OUTCOME_BYPASS_WEBHOOK],
    ["/api/v1/auth/oidc/", OUTCOME_BYPASS_PROBE],
    ["/api/v1/auth/mfa/challenge", OUTCOME_BYPASS_PROBE],
    ["/api/v1/auth/mfa/webauthn/challenge/", OUTCOME_BYPASS_PROBE],
  ] as ReadonlyArray<readonly [string, string]>)

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Errors
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Base class for all errors this module raises. Mirrors
 * `backend.security.bot_challenge.BotChallengeError`. */
export class BotChallengeError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "BotChallengeError"
  }
}

/** Site secret env unset / empty when verify was attempted. */
export class ProviderConfigError extends BotChallengeError {
  constructor(message: string) {
    super(message)
    this.name = "ProviderConfigError"
  }
}

/** Caller passed a string that doesn't match any `Provider`. */
export class InvalidProviderError extends BotChallengeError {
  constructor(message: string) {
    super(message)
    this.name = "InvalidProviderError"
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public types
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Normalised raw response from one of the four siteverify endpoints.
 * Vendors return slightly different JSON shapes; this flattens them
 * onto a single contract before classification. Mirrors the Python
 * `ProviderResponse` frozen dataclass. */
export interface ProviderResponse {
  readonly success: boolean
  readonly score: number
  readonly action: string | null
  readonly hostname: string | null
  readonly raw: Readonly<Record<string, unknown>>
  readonly errorCodes: readonly string[]
}

/** A single bypass-axis hit with the metadata the audit row needs.
 * Constructed by `evaluateBypass`. `outcome` is one of the seven
 * `OUTCOME_BYPASS_*` literals; `auditMetadata` is fed verbatim into
 * the audit row's `after` JSON. */
export interface BypassReason {
  readonly outcome: string
  readonly auditMetadata: Readonly<Record<string, unknown>>
  /** Lower-precedence axes that *also* matched on this request — kept
   * for the AS.0.6 §4 `also_matched` audit field. */
  readonly alsoMatched: readonly string[]
}

/** Inputs to `evaluateBypass`. */
export interface BypassContext {
  readonly path?: string | null
  readonly callerKind?: string | null
  readonly apiKeyId?: string | null
  readonly apiKeyPrefix?: string | null
  readonly clientIp?: string | null
  readonly tenantIpAllowlist?: readonly string[]
  readonly testTokenHeaderValue?: string | null
  readonly testTokenExpected?: string | null
  readonly tenantId?: string | null
  readonly widgetAction?: string | null
}

/** Final result returned by `verify` to the caller. The caller reads
 * `allow` to decide whether to continue or 4xx the request, then
 * optionally fans the full result into the audit emitter and metrics
 * widget. */
export interface BotChallengeResult {
  readonly outcome: string
  readonly allow: boolean
  readonly score: number
  readonly provider: Provider | null
  readonly auditEvent: string
  readonly auditMetadata: Readonly<Record<string, unknown>>
  readonly error: string | null
}

/** Inputs to `verify`. */
export interface VerifyContext {
  readonly provider: Provider
  readonly token?: string | null
  readonly secret?: string | null
  readonly phase?: number
  readonly widgetAction?: string | null
  readonly expectedAction?: string | null
  readonly remoteIp?: string | null
  readonly scoreThreshold?: number
  readonly timeoutSeconds?: number
  readonly bypass?: BypassContext
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  is_enabled — AS.0.8 single-knob hook (frontend side)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Whether the AS feature family is enabled per AS.0.8 §3.1 noop matrix.
 *
 * Reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend** twin of the
 * Python `settings.as_enabled` — deliberately decoupled per AS.0.8
 * §2.5). Default `true`.
 *
 * Resolution order:
 *   1. `(globalThis as any).OMNISIGHT_AS_FRONTEND_ENABLED`
 *   2. `process.env.OMNISIGHT_AS_FRONTEND_ENABLED`
 *   3. Default `true`. */
export function isEnabled(): boolean {
  const raw = (globalThis as { OMNISIGHT_AS_FRONTEND_ENABLED?: unknown })
    .OMNISIGHT_AS_FRONTEND_ENABLED
  let str: string | undefined
  if (typeof raw === "boolean") return raw
  if (typeof raw === "string") {
    str = raw
  } else if (
    typeof process !== "undefined" &&
    process.env &&
    typeof process.env.OMNISIGHT_AS_FRONTEND_ENABLED === "string"
  ) {
    str = process.env.OMNISIGHT_AS_FRONTEND_ENABLED
  }
  if (str === undefined) return true
  const lower = str.trim().toLowerCase()
  return !(lower === "false" || lower === "0" || lower === "no" || lower === "off")
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  passthrough — knob-off / dev-mode short-circuit
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Return a permissive result that does NOT write any audit row.
 *
 * Used by the `OMNISIGHT_AS_FRONTEND_ENABLED=false` knob and any caller
 * that has detected dev-mode. Mirrors the Python lib's `passthrough` —
 * `outcome="pass"`, `allow=true`, `score=1.0`, no provider attribution. */
export function passthrough(reason = "knob_off"): BotChallengeResult {
  return Object.freeze({
    outcome: OUTCOME_PASS,
    allow: true,
    score: 1.0,
    provider: null,
    auditEvent: EVENT_BOT_CHALLENGE_PASS,
    auditMetadata: Object.freeze({ passthrough_reason: reason }),
    error: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Bypass evaluation
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function _pathBypass(path: string | null | undefined): [string, string] | null {
  if (!path) return null
  const matches: Array<[string, string]> = []
  for (const [prefix, outcome] of _PATH_PREFIX_TO_OUTCOME) {
    if (path.startsWith(prefix)) matches.push([prefix, outcome])
  }
  if (matches.length === 0) return null
  matches.sort((a, b) => b[0].length - a[0].length)
  return matches[0]
}

/** Parse a CIDR string into `{ prefix: bigint, prefixLen: number,
 * version: 4|6, mask: bigint }` or `null` on parse failure. */
function _parseCidr(cidr: string): {
  prefix: bigint
  prefixLen: number
  version: 4 | 6
  mask: bigint
} | null {
  const [addr, lenStr] = cidr.includes("/") ? cidr.split("/", 2) : [cidr, null]
  const parsed = _parseIp(addr)
  if (!parsed) return null
  const totalBits = parsed.version === 4 ? 32 : 128
  let prefixLen: number
  if (lenStr === null) {
    prefixLen = totalBits
  } else {
    const n = Number(lenStr)
    if (!Number.isInteger(n) || n < 0 || n > totalBits) return null
    prefixLen = n
  }
  const mask =
    prefixLen === 0
      ? 0n
      : ((1n << BigInt(totalBits)) - 1n) ^
        ((1n << BigInt(totalBits - prefixLen)) - 1n)
  return {
    prefix: parsed.value & mask,
    prefixLen,
    version: parsed.version,
    mask,
  }
}

/** Parse an IP literal (v4 or v6) into a `{ value: bigint, version }`
 * shape. Returns `null` on any parse failure. Implements the subset of
 * RFC 4291 / RFC 5952 needed for allowlist matching. */
function _parseIp(s: string): { value: bigint; version: 4 | 6 } | null {
  const stripped = s.trim()
  if (!stripped) return null
  // IPv4 dotted-decimal.
  if (/^\d+\.\d+\.\d+\.\d+$/.test(stripped)) {
    const parts = stripped.split(".").map(Number)
    if (parts.length !== 4) return null
    let v = 0n
    for (const p of parts) {
      if (!Number.isInteger(p) || p < 0 || p > 255) return null
      v = (v << 8n) | BigInt(p)
    }
    return { value: v, version: 4 }
  }
  // IPv6 — handle `::` compression.
  if (stripped.includes(":")) {
    const head = stripped.split("%", 1)[0]
    let parts: string[]
    if (head.includes("::")) {
      const [left, right] = head.split("::", 2)
      const leftParts = left ? left.split(":") : []
      const rightParts = right ? right.split(":") : []
      const fill = 8 - leftParts.length - rightParts.length
      if (fill < 0) return null
      parts = [...leftParts, ...Array(fill).fill("0"), ...rightParts]
    } else {
      parts = head.split(":")
    }
    if (parts.length !== 8) return null
    let v = 0n
    for (const p of parts) {
      if (!/^[0-9a-fA-F]{1,4}$/.test(p)) return null
      v = (v << 16n) | BigInt(parseInt(p, 16))
    }
    return { value: v, version: 6 }
  }
  return null
}

function _ipInAllowlist(
  clientIp: string | null | undefined,
  allowlist: readonly string[],
): string | null {
  if (!clientIp || !allowlist || allowlist.length === 0) return null
  const ip = _parseIp(clientIp)
  if (!ip) return null
  for (const entry of allowlist) {
    const net = _parseCidr(entry)
    if (!net) {
      // Mirror the Python warning behaviour — entry is corrupt, skip.
      // We don't have a console.warn budget in tests, so silently skip.
      continue
    }
    if (ip.version !== net.version) continue
    if ((ip.value & net.mask) === net.prefix) return entry
  }
  return null
}

function _isWideCidr(cidr: string): boolean {
  const net = _parseCidr(cidr)
  if (!net) return false
  if (net.version === 4) return net.prefixLen <= 24
  return net.prefixLen <= 48
}

function _subnetPrefix(clientIp: string): string {
  const ip = _parseIp(clientIp)
  if (!ip) return "invalid"
  if (ip.version === 4) {
    const mask = ((1n << 32n) - 1n) ^ ((1n << 8n) - 1n) // /24
    const network = ip.value & mask
    const a = Number((network >> 24n) & 0xffn)
    const b = Number((network >> 16n) & 0xffn)
    const c = Number((network >> 8n) & 0xffn)
    return `${a}.${b}.${c}.0/24`
  }
  // /64 — top 64 bits, bottom 64 zero. Build 8 groups + canonicalise per
  // RFC 5952 so the output matches Python's `ipaddress.ip_network`.
  const groups: string[] = []
  for (let i = 7; i >= 0; i--) {
    groups.push(Number((ip.value >> BigInt(i * 16)) & 0xffffn).toString(16))
  }
  // /64 zeroes the bottom 4 groups deterministically.
  groups[4] = "0"
  groups[5] = "0"
  groups[6] = "0"
  groups[7] = "0"
  return `${_compressIpv6(groups)}/64`
}

/** RFC 5952 canonicalisation: collapse the longest run of zero groups
 * (length ≥ 2) into `::`. Ties broken leftmost. Mirrors Python's
 * `ipaddress.IPv6Network.__str__`. */
function _compressIpv6(groups: string[]): string {
  let bestStart = -1
  let bestLen = 0
  let curStart = -1
  let curLen = 0
  for (let i = 0; i < groups.length; i++) {
    if (groups[i] === "0") {
      if (curStart === -1) curStart = i
      curLen++
      if (curLen > bestLen) {
        bestLen = curLen
        bestStart = curStart
      }
    } else {
      curStart = -1
      curLen = 0
    }
  }
  if (bestLen < 2) return groups.join(":")
  const head = groups.slice(0, bestStart).join(":")
  const tail = groups.slice(bestStart + bestLen).join(":")
  return `${head}::${tail}`
}

/** Constant-time string compare. Byte-equal length + same bytes. */
function _timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    let _acc = 0
    for (let i = 0; i < a.length; i++) _acc |= a.charCodeAt(i)
    return false
  }
  let acc = 0
  for (let i = 0; i < a.length; i++) {
    acc |= a.charCodeAt(i) ^ b.charCodeAt(i)
  }
  return acc === 0
}

function _testTokenMatches(
  headerValue: string | null | undefined,
  expected: string | null | undefined,
): boolean {
  if (!headerValue || !expected) return false
  if (expected.length < 32) return false
  return _timingSafeEqual(headerValue, expected)
}

/** Last-12-chars SHA-256 fingerprint convention (mirrors AS.1.4
 * `oauth_audit.fingerprint` and `bot_challenge._fingerprint`).
 *
 * Synchronous to match Python's signature so `evaluateBypass` stays
 * sync end-to-end. Implemented via `_fingerprintSync` (pure-JS SHA-256
 * compute, FIPS 180-4). Identical 12-char output to
 * `hashlib.sha256(value.encode()).hexdigest()[:12]`. */
export function fingerprint(value: string): string {
  return _fingerprintSync(value)
}

/** Sync SHA-256 truncated to 12 hex chars. Pure-JS, no Web-Crypto / no
 * `node:crypto` dependency — keeps the twin runnable in plain
 * `--experimental-strip-types` Node and any browser context without an
 * async hop. */
function _fingerprintSync(value: string): string {
  const bytes = _utf8Encode(value)
  const hashBytes = _sha256(bytes)
  let hex = ""
  for (const b of hashBytes) hex += b.toString(16).padStart(2, "0")
  return hex.slice(0, 12)
}

function _utf8Encode(s: string): Uint8Array {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(s)
  // Fallback path for environments without TextEncoder (very rare).
  const out: number[] = []
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i)
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
    else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
  }
  return new Uint8Array(out)
}

const _SHA256_K: ReadonlyArray<number> = Object.freeze([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
])

function _rotr(x: number, n: number): number {
  return ((x >>> n) | (x << (32 - n))) >>> 0
}

function _sha256(msg: Uint8Array): Uint8Array {
  // Padding: append 0x80, pad with 0x00 to len ≡ 56 mod 64, append
  // 64-bit big-endian bit length.
  const bitLen = msg.length * 8
  const padLen = (56 - (msg.length + 1) % 64 + 64) % 64
  const total = msg.length + 1 + padLen + 8
  const buf = new Uint8Array(total)
  buf.set(msg, 0)
  buf[msg.length] = 0x80
  // 64-bit big-endian length — JS numbers safe to ~2^53, fine for
  // realistic token lengths.
  const hi = Math.floor(bitLen / 0x100000000)
  const lo = bitLen >>> 0
  buf[total - 8] = (hi >>> 24) & 0xff
  buf[total - 7] = (hi >>> 16) & 0xff
  buf[total - 6] = (hi >>> 8) & 0xff
  buf[total - 5] = hi & 0xff
  buf[total - 4] = (lo >>> 24) & 0xff
  buf[total - 3] = (lo >>> 16) & 0xff
  buf[total - 2] = (lo >>> 8) & 0xff
  buf[total - 1] = lo & 0xff

  let h0 = 0x6a09e667
  let h1 = 0xbb67ae85
  let h2 = 0x3c6ef372
  let h3 = 0xa54ff53a
  let h4 = 0x510e527f
  let h5 = 0x9b05688c
  let h6 = 0x1f83d9ab
  let h7 = 0x5be0cd19

  const w = new Uint32Array(64)
  for (let i = 0; i < total; i += 64) {
    for (let t = 0; t < 16; t++) {
      const o = i + t * 4
      w[t] =
        ((buf[o] << 24) |
          (buf[o + 1] << 16) |
          (buf[o + 2] << 8) |
          buf[o + 3]) >>>
        0
    }
    for (let t = 16; t < 64; t++) {
      const s0 =
        _rotr(w[t - 15], 7) ^ _rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3)
      const s1 =
        _rotr(w[t - 2], 17) ^ _rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10)
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0
    }
    let a = h0
    let b = h1
    let c = h2
    let d = h3
    let e = h4
    let f = h5
    let g = h6
    let hh = h7
    for (let t = 0; t < 64; t++) {
      const S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
      const ch = (e & f) ^ (~e & g)
      const temp1 = (hh + S1 + ch + _SHA256_K[t] + w[t]) >>> 0
      const S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const temp2 = (S0 + maj) >>> 0
      hh = g
      g = f
      f = e
      e = (d + temp1) >>> 0
      d = c
      c = b
      b = a
      a = (temp1 + temp2) >>> 0
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + b) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
    h5 = (h5 + f) >>> 0
    h6 = (h6 + g) >>> 0
    h7 = (h7 + hh) >>> 0
  }

  const out = new Uint8Array(32)
  const hs = [h0, h1, h2, h3, h4, h5, h6, h7]
  for (let i = 0; i < 8; i++) {
    out[i * 4] = (hs[i] >>> 24) & 0xff
    out[i * 4 + 1] = (hs[i] >>> 16) & 0xff
    out[i * 4 + 2] = (hs[i] >>> 8) & 0xff
    out[i * 4 + 3] = hs[i] & 0xff
  }
  return out
}

/** Walk the AS.0.6 §4 axis-internal precedence and return the
 * highest-precedence bypass match (or `null`).
 *
 * Precedence (highest → lowest):
 *
 *   1. **A — API key auth** (`callerKind` ∈ `BYPASS_CALLER_KINDS`)
 *   2. **C — Test-token header** (header value matches env, ≥32 chars)
 *   3. **B — IP allowlist** (client IP in tenant's CIDR allowlist)
 *   4. **Path bypass** (route in `BYPASS_PATH_PREFIXES`)
 *
 * Multi-axis matches: only the highest-precedence axis emits the bypass
 * row; the others land in `alsoMatched`. */
export function evaluateBypass(ctx: BypassContext): BypassReason | null {
  type Hit = { axis: string; reason: BypassReason }
  const matches: Hit[] = []

  // Axis A — API key auth.
  if (ctx.callerKind && BYPASS_CALLER_KINDS.has(ctx.callerKind)) {
    const meta: Record<string, unknown> = { caller_kind: ctx.callerKind }
    if (ctx.apiKeyId) meta.key_id = ctx.apiKeyId
    if (ctx.apiKeyPrefix) meta.key_prefix = ctx.apiKeyPrefix
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    matches.push({
      axis: "apikey",
      reason: {
        outcome: OUTCOME_BYPASS_APIKEY,
        auditMetadata: meta,
        alsoMatched: [],
      },
    })
  }

  // Axis C — Test-token header.
  if (_testTokenMatches(ctx.testTokenHeaderValue, ctx.testTokenExpected)) {
    const meta: Record<string, unknown> = {
      // `token_fp` key name MUST match the Python side's metadata
      // schema (audit row's `after` JSON). Value is a 12-char hex
      // fingerprint mirroring Python's `_fingerprint` (last 12 chars
      // of SHA-256). Computed synchronously via a pure-JS SHA-256 so
      // `evaluateBypass` keeps the Python-equivalent sync signature.
      token_fp: _fingerprintSync(ctx.testTokenHeaderValue ?? ""),
      tenant_id_or_null: ctx.tenantId ?? null,
    }
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    matches.push({
      axis: "test_token",
      reason: {
        outcome: OUTCOME_BYPASS_TEST_TOKEN,
        auditMetadata: meta,
        alsoMatched: [],
      },
    })
  }

  // Axis B — IP allowlist.
  const allowlist = ctx.tenantIpAllowlist ?? []
  const matchedCidr = _ipInAllowlist(ctx.clientIp, allowlist)
  if (matchedCidr !== null && ctx.clientIp) {
    const meta: Record<string, unknown> = {
      cidr_match: matchedCidr,
      client_ip_subnet: _subnetPrefix(ctx.clientIp),
      wide_cidr: _isWideCidr(matchedCidr),
    }
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    matches.push({
      axis: "ip_allowlist",
      reason: {
        outcome: OUTCOME_BYPASS_IP_ALLOWLIST,
        auditMetadata: meta,
        alsoMatched: [],
      },
    })
  }

  // Axis D — Path prefix.
  const pathHit = _pathBypass(ctx.path)
  if (pathHit !== null) {
    const [prefix, outcome] = pathHit
    const meta: Record<string, unknown> = { matched_prefix: prefix }
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    matches.push({
      axis: "path",
      reason: {
        outcome,
        auditMetadata: meta,
        alsoMatched: [],
      },
    })
  }

  if (matches.length === 0) return null

  const precedence: Record<string, number> = {
    apikey: 0,
    test_token: 1,
    ip_allowlist: 2,
    path: 3,
  }
  matches.sort((a, b) => precedence[a.axis] - precedence[b.axis])
  const head = matches[0]
  const others = matches.slice(1).map((m) => m.axis)
  if (others.length === 0) return head.reason
  const merged = { ...head.reason.auditMetadata, also_matched: others }
  return {
    outcome: head.reason.outcome,
    auditMetadata: merged,
    alsoMatched: others,
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Provider verifiers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Type alias: the HTTP-fetch callable a caller may inject (production
 * = `globalThis.fetch`). Tests inject a fake to avoid network calls. */
export type HttpFetch = (
  url: string,
  init: { method: string; body: string; headers: Record<string, string>; signal?: AbortSignal },
) => Promise<{
  status: number
  json: () => Promise<unknown>
  text: () => Promise<string>
}>

function _normaliseProvider(value: unknown): Provider {
  if (typeof value === "string") {
    const enumValues: ReadonlyArray<Provider> = [
      Provider.TURNSTILE,
      Provider.RECAPTCHA_V2,
      Provider.RECAPTCHA_V3,
      Provider.HCAPTCHA,
    ]
    for (const ev of enumValues) {
      if (ev === value) return ev
    }
    throw new InvalidProviderError(`unknown provider: ${JSON.stringify(value)}`)
  }
  throw new InvalidProviderError(
    `provider must be Provider or string, got ${typeof value}`,
  )
}

function _parseResponse(
  provider: Provider,
  payload: Record<string, unknown>,
): ProviderResponse {
  const success = Boolean(payload.success)
  const rawScore = payload.score
  let score: number
  if (
    (provider === Provider.TURNSTILE || provider === Provider.RECAPTCHA_V3) &&
    typeof rawScore === "number"
  ) {
    score = Math.max(0.0, Math.min(1.0, rawScore))
  } else {
    score = success ? 1.0 : 0.0
  }
  const action =
    typeof payload.action === "string" ? (payload.action as string) : null
  const hostname =
    typeof payload.hostname === "string" ? (payload.hostname as string) : null
  const rawCodesAny =
    payload["error-codes"] ?? (payload as Record<string, unknown>).error_codes ?? []
  const errorCodes: string[] = []
  if (Array.isArray(rawCodesAny)) {
    for (const c of rawCodesAny) {
      if (typeof c === "string") errorCodes.push(c)
    }
  }
  return Object.freeze({
    success,
    score,
    action,
    hostname,
    raw: Object.freeze({ ...payload }),
    errorCodes: Object.freeze(errorCodes) as readonly string[],
  })
}

/** Server-side `siteverify` call against *provider*.
 *
 * Sends `secret` + `response=<token>` (+ optional `remoteip`) to the
 * provider's siteverify endpoint, parses the JSON, and returns a
 * normalised `ProviderResponse`.
 *
 * @throws {ProviderConfigError} If `secret` is empty.
 * @throws {BotChallengeError} On 5xx, non-JSON body, or transport
 * failure (the caller turns these into `OUTCOME_UNVERIFIED_SERVERERR`). */
export async function verifyProvider(opts: {
  provider: Provider
  token: string
  secret: string
  remoteIp?: string | null
  expectedAction?: string | null
  timeoutSeconds?: number
  fetchImpl?: HttpFetch
}): Promise<ProviderResponse> {
  const {
    provider,
    token,
    secret,
    remoteIp = null,
    expectedAction = null,
    timeoutSeconds = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    fetchImpl,
  } = opts
  if (!secret) {
    throw new ProviderConfigError(
      `site secret for ${provider} is empty (env ${secretEnvFor(provider)} unset)`,
    )
  }
  if (!token) {
    return _parseResponse(provider, {
      success: false,
      "error-codes": ["missing-input-response"],
    })
  }
  const url = SITEVERIFY_URLS[provider]
  const params = new URLSearchParams()
  params.set("secret", secret)
  params.set("response", token)
  if (remoteIp) params.set("remoteip", remoteIp)

  const fetcher: HttpFetch = fetchImpl ?? _defaultFetch

  const controller = typeof AbortController !== "undefined" ? new AbortController() : null
  const timer =
    controller && typeof setTimeout !== "undefined"
      ? setTimeout(() => controller.abort(), timeoutSeconds * 1000)
      : null
  let resp: Awaited<ReturnType<HttpFetch>>
  try {
    resp = await fetcher(url, {
      method: "POST",
      body: params.toString(),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      signal: controller?.signal,
    })
  } catch (e) {
    throw new BotChallengeError(
      `siteverify ${provider} transport failure: ${(e as Error).message ?? String(e)}`,
    )
  } finally {
    if (timer !== null && typeof clearTimeout !== "undefined") clearTimeout(timer)
  }

  if (resp.status >= 500) {
    throw new BotChallengeError(`siteverify ${provider} returned ${resp.status}`)
  }
  if (resp.status >= 400) {
    return _parseResponse(provider, {
      success: false,
      "error-codes": [`http-${resp.status}`],
    })
  }
  let payload: unknown
  try {
    payload = await resp.json()
  } catch (e) {
    throw new BotChallengeError(`siteverify ${provider} returned non-JSON body`)
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new BotChallengeError(
      `siteverify ${provider} returned non-object JSON: ${
        Array.isArray(payload) ? "array" : typeof payload
      }`,
    )
  }
  const parsed = _parseResponse(provider, payload as Record<string, unknown>)
  if (
    expectedAction !== null &&
    parsed.action !== null &&
    parsed.action !== expectedAction
  ) {
    return Object.freeze({
      success: false,
      score: 0.0,
      action: parsed.action,
      hostname: parsed.hostname,
      raw: parsed.raw,
      errorCodes: Object.freeze([...parsed.errorCodes, "action-mismatch"]) as readonly string[],
    })
  }
  return parsed
}

async function _defaultFetch(
  url: string,
  init: { method: string; body: string; headers: Record<string, string>; signal?: AbortSignal },
): Promise<{
  status: number
  json: () => Promise<unknown>
  text: () => Promise<string>
}> {
  if (typeof fetch === "undefined") {
    throw new BotChallengeError(
      "global fetch unavailable — provide fetchImpl explicitly",
    )
  }
  const r = await fetch(url, init)
  return {
    status: r.status,
    json: () => r.json(),
    text: () => r.text(),
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Phase-aware classifier (AS.0.5 §2 phase matrix)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Turn a provider response into the final `BotChallengeResult` per
 * AS.0.5 §2 phase matrix.
 *
 * `phase` is `1 / 2 / 3` corresponding to the three live phases.
 *
 * Phase 1 / 2: fail-open everywhere. Low score → `unverified_lowscore`,
 * server error → `unverified_servererr`; both `allow=true`.
 *
 * Phase 3: fail-closed for confirmed low score; server error stays
 * fail-open (our-side fault). */
export function classifyOutcome(
  response: ProviderResponse,
  opts: {
    provider: Provider
    phase: number
    scoreThreshold?: number
    widgetAction?: string | null
  },
): BotChallengeResult {
  const {
    provider,
    phase,
    scoreThreshold = DEFAULT_SCORE_THRESHOLD,
    widgetAction = null,
  } = opts
  if (phase !== 1 && phase !== 2 && phase !== 3) {
    throw new BotChallengeError(`phase must be 1/2/3, got ${JSON.stringify(phase)}`)
  }
  const metadata: Record<string, unknown> = {
    provider,
    score: response.score,
  }
  if (widgetAction !== null) metadata.widget_action = widgetAction

  if (response.success && response.score >= scoreThreshold) {
    return Object.freeze({
      outcome: OUTCOME_PASS,
      allow: true,
      score: response.score,
      provider,
      auditEvent: EVENT_BOT_CHALLENGE_PASS,
      auditMetadata: Object.freeze(metadata),
      error: null,
    })
  }

  if (response.success && response.score < scoreThreshold) {
    if (phase === 3) {
      return Object.freeze({
        outcome: OUTCOME_BLOCKED_LOWSCORE,
        allow: false,
        score: response.score,
        provider,
        auditEvent: EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
        auditMetadata: Object.freeze(metadata),
        error: null,
      })
    }
    return Object.freeze({
      outcome: OUTCOME_UNVERIFIED_LOWSCORE,
      allow: true,
      score: response.score,
      provider,
      auditEvent: EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
      auditMetadata: Object.freeze(metadata),
      error: null,
    })
  }

  // response.success === false — server-side verify error. Fail-open
  // for ALL phases (AS.0.5 §2.4 row 3).
  const errorKind = _classifyErrorKind(response.errorCodes)
  metadata.error_kind = errorKind
  if (response.errorCodes.length > 0) metadata.error_codes = [...response.errorCodes]
  return Object.freeze({
    outcome: OUTCOME_UNVERIFIED_SERVERERR,
    allow: true,
    score: response.score,
    provider,
    auditEvent: EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
    auditMetadata: Object.freeze(metadata),
    error: response.errorCodes.length > 0 ? response.errorCodes.join(", ") : null,
  })
}

function _classifyErrorKind(errorCodes: readonly string[]): string {
  if (errorCodes.length === 0) return "unknown"
  const codes = errorCodes.map((c) => c.toLowerCase())
  if (codes.some((c) => c.includes("timeout") || c.includes("duplicate"))) return "timeout"
  if (codes.some((c) => c.startsWith("http-5"))) return "5xx"
  if (codes.some((c) => c.startsWith("http-4"))) return "4xx_invalid_token"
  if (codes.some((c) => c.includes("invalid"))) return "4xx_invalid_token"
  if (codes.some((c) => c.includes("missing"))) return "4xx_invalid_token"
  return "unknown"
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Top-level verify()
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** End-to-end orchestrator: knob → bypass → provider verify → classify.
 *
 * Returns a `BotChallengeResult` the caller acts on (`allow` decides
 * 4xx vs continue; `auditEvent` + `auditMetadata` feed the audit
 * emitter; `score` + `provider` feed the AS.5.2 dashboard).
 *
 * Order of evaluation (AS.0.5 §4 precedence):
 *   1. Knob off ⇒ `passthrough()`.
 *   2. Bypass list match ⇒ `BypassReason` → result.
 *   3. Provider verify call.
 *   4. Phase-aware classification. */
export async function verify(
  ctx: VerifyContext,
  opts: { fetchImpl?: HttpFetch } = {},
): Promise<BotChallengeResult> {
  if (!isEnabled()) return passthrough("knob_off")

  const bypass = evaluateBypass(ctx.bypass ?? {})
  if (bypass !== null) {
    const meta = { ...bypass.auditMetadata }
    if (ctx.widgetAction && !("widget_action" in meta)) {
      meta.widget_action = ctx.widgetAction
    }
    return Object.freeze({
      outcome: bypass.outcome,
      allow: true,
      score: 1.0,
      provider: null,
      auditEvent: eventForOutcome(bypass.outcome),
      auditMetadata: Object.freeze(meta),
      error: null,
    })
  }

  // Provider verify branch.
  if (ctx.secret === undefined || ctx.secret === null || ctx.secret === "") {
    const meta: Record<string, unknown> = {
      provider: ctx.provider,
      score: 0.0,
      error_kind: "config_missing_secret",
    }
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    return Object.freeze({
      outcome: OUTCOME_UNVERIFIED_SERVERERR,
      allow: true,
      score: 0.0,
      provider: ctx.provider,
      auditEvent: EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
      auditMetadata: Object.freeze(meta),
      error: `${secretEnvFor(ctx.provider)} unset`,
    })
  }

  let response: ProviderResponse
  try {
    response = await verifyProvider({
      provider: ctx.provider,
      token: ctx.token ?? "",
      secret: ctx.secret,
      remoteIp: ctx.remoteIp ?? null,
      expectedAction: ctx.expectedAction ?? null,
      timeoutSeconds: ctx.timeoutSeconds ?? DEFAULT_VERIFY_TIMEOUT_SECONDS,
      fetchImpl: opts.fetchImpl,
    })
  } catch (e) {
    const msg = (e as Error).message ?? String(e)
    const meta: Record<string, unknown> = {
      provider: ctx.provider,
      score: 0.0,
      error_kind: msg.includes("transport failure") ? "5xx" : "5xx",
    }
    if (ctx.widgetAction) meta.widget_action = ctx.widgetAction
    return Object.freeze({
      outcome: OUTCOME_UNVERIFIED_SERVERERR,
      allow: true,
      score: 0.0,
      provider: ctx.provider,
      auditEvent: EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
      auditMetadata: Object.freeze(meta),
      error: (e as Error).name ?? "Error",
    })
  }

  return classifyOutcome(response, {
    provider: ctx.provider,
    phase: ctx.phase ?? 1,
    scoreThreshold: ctx.scoreThreshold ?? DEFAULT_SCORE_THRESHOLD,
    widgetAction: ctx.widgetAction ?? null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Provider selection (AS.3.3 will replace this with the heuristic)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Return the env-default provider.
 *
 * AS.3.2 placeholder — AS.3.3 will replace it with the region- and
 * ecosystem-aware heuristic (GDPR strict region → hCaptcha; existing
 * Google ecosystem → reCAPTCHA v3; default → Turnstile). Callers
 * should call this rather than hard-code `Provider.TURNSTILE` so AS.3.3
 * is a one-line change. */
export function pickProvider(opts: { default?: Provider } = {}): Provider {
  return opts.default ?? Provider.TURNSTILE
}

// Re-export the normaliser so tests can drive the same provider-string
// validation the Python side does via `_normalise_provider`.
export { _normaliseProvider as normaliseProvider }
