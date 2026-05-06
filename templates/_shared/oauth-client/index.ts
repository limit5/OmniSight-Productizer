/**
 * AS.1.2 — OAuth 2.0 / OIDC core client library (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/security/oauth_client.py`.
 *
 * Protocol-primitives layer of the AS OAuth shared library — emitted
 * into every generated-app workspace so apps scaffolded by the
 * OmniSight productizer can run client-side OAuth flows (PKCE state /
 * nonce mint, callback verify, token parse + rotation, fetch-based
 * auto-refresh middleware) **without** runtime dependence on the
 * OmniSight backend.
 *
 * Public surface mirrors the Python side:
 *
 *   * `generatePkce`               (PKCE — RFC 7636 §4.1 / §4.2)
 *   * `generateState` / `generateNonce`
 *   * `buildAuthorizeUrl`          (RFC 6749 §4.1.1)
 *   * `beginAuthorization`         (PKCE + state + (OIDC) nonce mint)
 *   * `verifyStateAndConsume`      (constant-time + TTL)
 *   * `parseTokenResponse`         (RFC 6749 §5.1 / §5.2)
 *   * `applyRotation`              (RFC 6749 §10.4 + OAuth 2.1 BCP §4.13)
 *   * `autoRefresh`                (skew-window async helper)
 *   * `AutoRefreshFetch`           (fetch wrapper — TS twin of AutoRefreshAuth)
 *   * `isEnabled`                  (AS.0.8 §3.1 noop hook)
 *
 * Vendor-specific clients (GitHub / Google / Microsoft / Apple / GitLab /
 * Bitbucket / Slack / Notion / Salesforce / HubSpot / Discord) ship in
 * AS.1.3. Token persistence ships in AS.2.x. This module is **provider-
 * and storage-agnostic** — callers wire their own provider config + their
 * own persistence callback.
 *
 * Cross-twin contract (enforced by AS.1.5 drift guard)
 * ────────────────────────────────────────────────────
 * The 5 canonical OAuth audit event strings + the 4 numeric defaults
 * (`PKCE_VERIFIER_MIN_LENGTH`, `PKCE_VERIFIER_MAX_LENGTH`,
 * `DEFAULT_STATE_TTL_SECONDS`, `DEFAULT_REFRESH_SKEW_SECONDS`) MUST
 * match the Python side byte-for-byte. Drift breaks
 * `backend/tests/test_oauth_client.py::test_oauth_event_strings_parity_python_ts`
 * and the four `test_oauth_defaults_parity_python_ts_*` tests.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * All randomness comes from `globalThis.crypto.getRandomValues`
 *     (Web Crypto). Each browser tab / Node worker derives its own
 *     values from the same kernel CSPRNG — answer #1 of SOP §1 audit
 *     (deterministic-by-construction across workers).
 *   * No module-level mutable state — only frozen literals and pure
 *     functions. The fetch-middleware class holds per-instance state
 *     (`this.token`) but no module-level cache.
 *   * Importing the module is free of side effects (no env reads, no
 *     network IO, no localStorage IO at module top level).
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 *   * `isEnabled()` reads the **frontend** AS knob
 *     `OMNISIGHT_AS_FRONTEND_ENABLED` (Python side reads
 *     `settings.as_enabled` — these are the **deliberately decoupled**
 *     pair per AS.0.8 §2.5). Default `true` — the AS feature family
 *     is on unless explicitly disabled.
 *   * The pure helpers (PKCE / state / parsing) deliberately do NOT
 *     consult the knob — turning AS off must not break a script that
 *     parses a stored token (matches the Python lib invariant).
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Numeric constants — must mirror Python side
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** RFC 7636 §4.1 — code_verifier MUST be 43-128 unreserved characters. */
export const PKCE_VERIFIER_MIN_LENGTH = 43
/** RFC 7636 §4.1 — code_verifier upper bound. */
export const PKCE_VERIFIER_MAX_LENGTH = 128

/** Raw bytes used to derive the PKCE verifier. 64 bytes → 86 b64url chars. */
const PKCE_VERIFIER_RAW_BYTES = 64

/** Raw bytes used for state (32 → 43 b64url chars → 256-bit entropy). */
const STATE_RAW_BYTES = 32
/** Raw bytes used for nonce (32 → 43 b64url chars → 256-bit entropy). */
const NONCE_RAW_BYTES = 32

/**
 * Default state TTL — 10 minutes per OIDC Core §3.1.2.7. Most providers
 * expire the authorization-code itself within 10 minutes, so a longer
 * state TTL gains nothing.
 */
export const DEFAULT_STATE_TTL_SECONDS = 600

/**
 * Default skew before access-token expiry to trigger a refresh. 60 s
 * buys time for one slow refresh round-trip on a constrained link
 * without being so generous that we refresh tokens with hours of
 * remaining life.
 */
export const DEFAULT_REFRESH_SKEW_SECONDS = 60

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Canonical OAuth audit event strings
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//
// Hard contract — the strings are part of the AS.0.8 §5 truth-table
// cross-reference. Once any caller emits them they MUST NOT change.
// AS.5.1 will move both Python + TS twins to a shared `audit_events`
// surface; until then the two-side parity is enforced by the
// AS.1.5 drift-guard test.

export const EVENT_OAUTH_LOGIN_INIT = "oauth.login_init"
export const EVENT_OAUTH_LOGIN_CALLBACK = "oauth.login_callback"
export const EVENT_OAUTH_REFRESH = "oauth.refresh"
export const EVENT_OAUTH_UNLINK = "oauth.unlink"
export const EVENT_OAUTH_TOKEN_ROTATED = "oauth.token_rotated"

export const ALL_OAUTH_EVENTS: readonly string[] = [
  EVENT_OAUTH_LOGIN_INIT,
  EVENT_OAUTH_LOGIN_CALLBACK,
  EVENT_OAUTH_REFRESH,
  EVENT_OAUTH_UNLINK,
  EVENT_OAUTH_TOKEN_ROTATED,
]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Exceptions
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Base class for all errors this module raises. Callers can catch
 * once and not enumerate. */
export class OAuthClientError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "OAuthClientError"
  }
}

/** Returned `state` does not match the stored value (CSRF suspicion —
 * RFC 6749 §10.12). */
export class StateMismatchError extends OAuthClientError {
  constructor(message = "oauth state mismatch") {
    super(message)
    this.name = "StateMismatchError"
  }
}

/** Stored state TTL has elapsed; user must restart the flow. */
export class StateExpiredError extends OAuthClientError {
  constructor(message: string) {
    super(message)
    this.name = "StateExpiredError"
  }
}

/** Provider returned a malformed or error-shaped token response
 * (RFC 6749 §5.2 `error` payload, or missing `access_token`). */
export class TokenResponseError extends OAuthClientError {
  constructor(message: string) {
    super(message)
    this.name = "TokenResponseError"
  }
}

/** Refresh attempt failed — either we have no refresh_token, or the
 * provider rejected ours (typically because rotation already consumed
 * it on a previous call). */
export class TokenRefreshError extends OAuthClientError {
  constructor(message: string) {
    super(message)
    this.name = "TokenRefreshError"
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public types — frozen dataclasses (interfaces with readonly)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** RFC 7636 §4.1 / §4.2 verifier + S256 challenge pair.
 *
 * `codeVerifier` MUST stay client-side until the token-exchange POST.
 * `codeChallenge` is what travels with the authorize redirect.
 */
export interface PkcePair {
  readonly codeVerifier: string
  readonly codeChallenge: string
  readonly codeChallengeMethod: "S256"
}

/** In-flight authorization context.
 *
 * Created by `beginAuthorization()` and persisted by the caller (cookie
 * / sessionStorage / IndexedDB / Redis-via-server) keyed by something
 * derived from `state`. Looked up by `state` on the callback and
 * validated with `verifyStateAndConsume`.
 */
export interface FlowSession {
  readonly provider: string
  readonly state: string
  readonly codeVerifier: string
  readonly nonce: string | null
  readonly redirectUri: string
  readonly scope: readonly string[]
  readonly createdAt: number
  readonly expiresAt: number
  /** Caller-supplied opaque metadata that round-trips with the flow. */
  readonly extra: ReadonlyArray<readonly [string, string]>
}

/** Parsed RFC 6749 §5.1 token response.
 *
 * `expiresAt` is **absolute** (epoch seconds) — computed at parse time
 * from relative `expires_in` plus `now`, so storage round-trips don't
 * have to re-evaluate clock skew.
 */
export interface TokenSet {
  readonly accessToken: string
  readonly refreshToken: string | null
  readonly tokenType: string
  readonly expiresAt: number | null
  readonly scope: readonly string[]
  readonly idToken: string | null
  readonly raw: Readonly<Record<string, unknown>>
}

/** Whether the token is within `skewSeconds` of expiry (or past it).
 * Returns false if the token has no expiry hint — caller must rely on
 * a 401 response from the provider in that case. */
export function needsRefresh(
  token: TokenSet,
  opts: { skewSeconds?: number; now?: number } = {},
): boolean {
  if (token.expiresAt === null) return false
  const skew = opts.skewSeconds ?? DEFAULT_REFRESH_SKEW_SECONDS
  const now = opts.now ?? nowSeconds()
  return now >= token.expiresAt - skew
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  AS.0.8 single-knob hook (frontend side)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Whether the AS feature family is enabled per AS.0.8 §3.1 noop matrix.
 *
 * Reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend** twin of the
 * Python `settings.as_enabled` — deliberately decoupled per
 * AS.0.8 §2.5 so the frontend can be flipped independently from the
 * backend). Default `true`.
 *
 * Resolution order:
 *   1. `(globalThis as any).OMNISIGHT_AS_FRONTEND_ENABLED` — runtime
 *      injection (e.g. Vite `define`, server-rendered window var).
 *   2. `process.env.OMNISIGHT_AS_FRONTEND_ENABLED` — Node / SSR.
 *   3. Default `true`.
 *
 * The pure helpers (PKCE / state / parsing) deliberately do NOT call
 * this — turning the knob off must not break a script that parses an
 * already-stored token (matches the Python lib invariant).
 */
export function isEnabled(): boolean {
  const raw =
    (globalThis as { OMNISIGHT_AS_FRONTEND_ENABLED?: unknown })
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
//  Crypto helpers (Web Crypto)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Resolve the platform Web-Crypto impl, throwing typed if absent. */
function getCrypto(): Crypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || typeof c.getRandomValues !== "function" || !c.subtle) {
    throw new Error(
      "Web Crypto API not available — secure random + SHA-256 are required",
    )
  }
  return c
}

/** Current epoch in seconds (float). Mirrors Python `time.time()`. */
function nowSeconds(): number {
  return Date.now() / 1000
}

/** Base64url encode raw bytes (no padding) per RFC 4648 §5. */
function b64urlNoPad(raw: Uint8Array): string {
  // Encode to standard base64 then translate alphabet + strip padding.
  let bin = ""
  for (let i = 0; i < raw.length; i++) bin += String.fromCharCode(raw[i])
  const std = btoa(bin)
  return std.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** `b64urlNoPad(getRandomValues(new Uint8Array(rawBytes)))`. */
function b64urlToken(rawBytes: number): string {
  const buf = new Uint8Array(rawBytes)
  getCrypto().getRandomValues(buf)
  return b64urlNoPad(buf)
}

/** Constant-time string equality. JS strings are UTF-16 — for our use
 * (urlsafe-b64 ASCII) charCode comparison is byte comparison. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    // Compare against `a` to keep work proportional to `a.length`,
    // then unconditionally return false.
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

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Random helpers (urlsafe base64)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Generate a fresh CSRF `state` (≥256 bits of entropy). */
export function generateState(): string {
  return b64urlToken(STATE_RAW_BYTES)
}

/** Generate a fresh OIDC `nonce` (≥256 bits of entropy). */
export function generateNonce(): string {
  return b64urlToken(NONCE_RAW_BYTES)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  PKCE (RFC 7636)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Generate a fresh PKCE verifier + S256 challenge.
 *
 * Async because Web Crypto's `subtle.digest` is async (browser SHA-256
 * runs off-main-thread). The verifier is 86 chars (64 random bytes →
 * urlsafe-base64 no-pad), well within the RFC 7636 §4.1 [43, 128]
 * window; the challenge is SHA-256 of the verifier ASCII bytes,
 * urlsafe-base64 no-pad.
 */
export async function generatePkce(): Promise<PkcePair> {
  const verifier = b64urlToken(PKCE_VERIFIER_RAW_BYTES)
  if (
    verifier.length < PKCE_VERIFIER_MIN_LENGTH ||
    verifier.length > PKCE_VERIFIER_MAX_LENGTH
  ) {
    throw new Error(
      `verifier length ${verifier.length} out of RFC 7636 §4.1 range ` +
        `[${PKCE_VERIFIER_MIN_LENGTH}, ${PKCE_VERIFIER_MAX_LENGTH}]`,
    )
  }
  const ascii = new Uint8Array(verifier.length)
  for (let i = 0; i < verifier.length; i++) ascii[i] = verifier.charCodeAt(i)
  const digest = await getCrypto().subtle.digest("SHA-256", ascii)
  const challenge = b64urlNoPad(new Uint8Array(digest))
  return Object.freeze({
    codeVerifier: verifier,
    codeChallenge: challenge,
    codeChallengeMethod: "S256",
  }) as PkcePair
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Authorization-URL builder
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface BuildAuthorizeUrlOptions {
  authorizeEndpoint: string
  clientId: string
  redirectUri: string
  scope: readonly string[]
  state: string
  codeChallenge: string
  codeChallengeMethod?: string
  nonce?: string | null
  extraParams?: Readonly<Record<string, string>>
}

/** Assemble the user-agent redirect URL (RFC 6749 §4.1.1).
 *
 * `extraParams` lets vendor adapters add provider-specific knobs
 * (`access_type=offline` for Google, `prompt=consent` for forced
 * refresh-token issuance, `allow_signup=false` for GitHub, …)
 * without bloating the core signature.
 *
 * `nonce` is appended only for OIDC providers (caller decides).
 * `response_type` is hard-coded to `code` — implicit and hybrid flows
 * are out of scope for this lib (and discouraged by OAuth 2.1).
 */
export function buildAuthorizeUrl(opts: BuildAuthorizeUrlOptions): string {
  const {
    authorizeEndpoint,
    clientId,
    redirectUri,
    scope,
    state,
    codeChallenge,
    codeChallengeMethod = "S256",
    nonce,
    extraParams,
  } = opts

  if (!authorizeEndpoint) throw new Error("authorizeEndpoint is required")
  if (!clientId) throw new Error("clientId is required")
  if (!redirectUri) throw new Error("redirectUri is required")
  if (!state) throw new Error("state is required")
  if (!codeChallenge) throw new Error("codeChallenge is required")

  const params: Array<[string, string]> = [
    ["response_type", "code"],
    ["client_id", clientId],
    ["redirect_uri", redirectUri],
    ["state", state],
    ["code_challenge", codeChallenge],
    ["code_challenge_method", codeChallengeMethod],
  ]
  if (scope.length > 0) {
    params.push(["scope", scope.join(" ")])
  }
  if (nonce !== undefined && nonce !== null) {
    params.push(["nonce", nonce])
  }
  if (extraParams) {
    const coreKeys = new Set(params.map(([k]) => k))
    for (const [k, v] of Object.entries(extraParams)) {
      if (coreKeys.has(k)) {
        throw new Error(
          `extraParams key ${JSON.stringify(k)} collides with core OAuth param`,
        )
      }
      params.push([k, v])
    }
  }

  const sep = authorizeEndpoint.includes("?") ? "&" : "?"
  const qs = params
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&")
  return authorizeEndpoint + sep + qs
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  FlowSession lifecycle
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface BeginAuthorizationOptions {
  provider: string
  authorizeEndpoint: string
  clientId: string
  redirectUri: string
  scope: readonly string[]
  useOidcNonce?: boolean
  stateTtlSeconds?: number
  extraAuthorizeParams?: Readonly<Record<string, string>>
  extra?: Readonly<Record<string, string>>
  now?: number
}

/** Start an authorization-code flow.
 *
 * Returns `{ url, flow }`. The caller MUST persist `flow` (e.g.
 * sessionStorage under `oauth:flow:${state}`), then redirect the
 * user-agent to `url`.
 *
 * On the callback the caller fetches the persisted FlowSession by the
 * returned `state` query param, calls `verifyStateAndConsume`, then
 * exchanges the code via the provider's token endpoint with the same
 * `codeVerifier`.
 */
export async function beginAuthorization(
  opts: BeginAuthorizationOptions,
): Promise<{ url: string; flow: FlowSession }> {
  const ts = opts.now ?? nowSeconds()
  const state = generateState()
  const nonce = opts.useOidcNonce ? generateNonce() : null
  const pkce = await generatePkce()
  const stateTtlSeconds = opts.stateTtlSeconds ?? DEFAULT_STATE_TTL_SECONDS

  const extraEntries: Array<readonly [string, string]> = opts.extra
    ? Object.entries(opts.extra)
        .slice()
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => [k, v] as const)
    : []

  const flow: FlowSession = Object.freeze({
    provider: opts.provider,
    state,
    codeVerifier: pkce.codeVerifier,
    nonce,
    redirectUri: opts.redirectUri,
    scope: Object.freeze([...opts.scope]) as readonly string[],
    createdAt: ts,
    expiresAt: ts + Math.max(0, stateTtlSeconds),
    extra: Object.freeze(extraEntries) as ReadonlyArray<readonly [string, string]>,
  })

  const url = buildAuthorizeUrl({
    authorizeEndpoint: opts.authorizeEndpoint,
    clientId: opts.clientId,
    redirectUri: opts.redirectUri,
    scope: flow.scope,
    state,
    codeChallenge: pkce.codeChallenge,
    nonce,
    extraParams: opts.extraAuthorizeParams,
  })
  return { url, flow }
}

/** Validate the callback's `state` against the stored FlowSession.
 *
 * Throws `StateMismatchError` on any difference (constant-time compare),
 * or `StateExpiredError` if the FlowSession's TTL has elapsed.
 *
 * The "consume" in the name signals the caller's responsibility: after
 * a successful verify, **delete the stored FlowSession** so it cannot
 * be replayed. We don't delete it ourselves because the storage backend
 * is the caller's choice.
 */
export function verifyStateAndConsume(
  stored: FlowSession,
  returnedState: string | null | undefined,
  opts: { now?: number } = {},
): void {
  const ts = opts.now ?? nowSeconds()
  if (ts >= stored.expiresAt) {
    throw new StateExpiredError(
      `oauth flow expired at ${stored.expiresAt.toFixed(0)} (now ${ts.toFixed(0)})`,
    )
  }
  if (!timingSafeEqual(stored.state, returnedState ?? "")) {
    throw new StateMismatchError()
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Token-response parser (RFC 6749 §5.1 / §5.2)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Turn the provider's token-endpoint JSON into a typed `TokenSet`.
 *
 * Handles the success shape (RFC 6749 §5.1) and the error shape
 * (RFC 6749 §5.2 — raised as `TokenResponseError` with the provider
 * message preserved). The returned `expiresAt` is **absolute** (epoch
 * seconds), deliberately decoupling the caller from clock-skew bugs
 * that come from storing relative durations.
 */
export function parseTokenResponse(
  payload: unknown,
  opts: { now?: number } = {},
): TokenSet {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TokenResponseError(
      `token payload must be a mapping, got ${typeof payload}`,
    )
  }
  const obj = payload as Record<string, unknown>

  if ("error" in obj) {
    const err = String(obj.error ?? "unknown_error")
    const desc = obj.error_description
    let msg = `token endpoint returned error=${err}`
    if (desc) msg += ` description=${JSON.stringify(desc)}`
    throw new TokenResponseError(msg)
  }

  const access = obj.access_token
  if (typeof access !== "string" || !access) {
    throw new TokenResponseError("token response missing access_token")
  }

  const tokenTypeRaw = obj.token_type
  let tokenType: string
  if (tokenTypeRaw === undefined || tokenTypeRaw === null) {
    tokenType = "Bearer"
  } else if (typeof tokenTypeRaw !== "string") {
    throw new TokenResponseError("token_type must be a string")
  } else {
    tokenType = tokenTypeRaw
  }

  const refreshRaw = obj.refresh_token
  let refresh: string | null
  if (refreshRaw === undefined || refreshRaw === null) {
    refresh = null
  } else if (typeof refreshRaw !== "string") {
    throw new TokenResponseError("refresh_token must be a string when present")
  } else {
    refresh = refreshRaw
  }

  const expiresInRaw = obj.expires_in
  let expiresAt: number | null
  if (expiresInRaw === undefined || expiresInRaw === null) {
    expiresAt = null
  } else {
    let expiresInF: number
    if (typeof expiresInRaw === "number") {
      expiresInF = expiresInRaw
    } else if (typeof expiresInRaw === "string" && expiresInRaw.trim() !== "") {
      const parsed = Number(expiresInRaw)
      if (!Number.isFinite(parsed)) {
        throw new TokenResponseError(
          `expires_in not a number: ${JSON.stringify(expiresInRaw)}`,
        )
      }
      expiresInF = parsed
    } else {
      throw new TokenResponseError(
        `expires_in not a number: ${JSON.stringify(expiresInRaw)}`,
      )
    }
    if (!Number.isFinite(expiresInF)) {
      throw new TokenResponseError(
        `expires_in not a number: ${JSON.stringify(expiresInRaw)}`,
      )
    }
    if (expiresInF < 0) {
      throw new TokenResponseError(`expires_in negative: ${expiresInF}`)
    }
    const ts = opts.now ?? nowSeconds()
    expiresAt = ts + expiresInF
  }

  const rawScope = obj.scope
  let scope: readonly string[]
  if (rawScope === undefined || rawScope === null) {
    scope = []
  } else if (typeof rawScope === "string") {
    const seen = new Set<string>()
    const out: string[] = []
    for (const tok of rawScope.replace(/,/g, " ").split(/\s+/)) {
      if (tok && !seen.has(tok)) {
        seen.add(tok)
        out.push(tok)
      }
    }
    scope = out
  } else if (Array.isArray(rawScope)) {
    scope = rawScope.map((s) => String(s))
  } else {
    throw new TokenResponseError(`scope has unsupported type ${typeof rawScope}`)
  }

  const idTokenRaw = obj.id_token
  let idToken: string | null
  if (idTokenRaw === undefined || idTokenRaw === null) {
    idToken = null
  } else if (typeof idTokenRaw !== "string") {
    throw new TokenResponseError("id_token must be a string when present")
  } else {
    idToken = idTokenRaw
  }

  return Object.freeze({
    accessToken: access,
    refreshToken: refresh,
    tokenType,
    expiresAt,
    scope: Object.freeze([...scope]) as readonly string[],
    idToken,
    raw: Object.freeze({ ...obj }),
  }) as TokenSet
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Refresh-token rotation
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Apply RFC 6749 §6 + §10.4 refresh-token rotation.
 *
 * Parses `refreshedPayload` (the JSON returned by the provider's
 * `grant_type=refresh_token` POST) and merges it onto `previous`:
 *
 *   * `accessToken` and `expiresAt` always replaced.
 *   * `refreshToken`: per RFC 6749 §10.4 / OAuth 2.1 BCP §4.13, providers
 *     SHOULD issue a new refresh_token on every refresh; if they do, the
 *     old one MUST be considered consumed. If the provider omits it (some
 *     explicitly opt out of rotation), keep the previous one.
 *   * `scope` — replaced if the response includes a fresh value, else kept.
 *   * `idToken` — providers may or may not re-issue; replaced if present.
 *
 * Returns `[newToken, rotated]` where `rotated` is true iff the provider
 * actually rotated the refresh_token (i.e. the new one differs from the
 * old). Callers persist the new TokenSet and, if `rotated`, MUST emit
 * an `EVENT_OAUTH_TOKEN_ROTATED` audit row + delete the old refresh_token
 * from any cache.
 */
export function applyRotation(
  previous: TokenSet,
  refreshedPayload: unknown,
  opts: { now?: number } = {},
): [TokenSet, boolean] {
  const fresh = parseTokenResponse(refreshedPayload, opts)
  const newRefresh =
    fresh.refreshToken !== null ? fresh.refreshToken : previous.refreshToken
  const rotated =
    fresh.refreshToken !== null &&
    previous.refreshToken !== null &&
    fresh.refreshToken !== previous.refreshToken
  const merged: TokenSet = Object.freeze({
    accessToken: fresh.accessToken,
    refreshToken: newRefresh,
    tokenType: fresh.tokenType || previous.tokenType,
    expiresAt: fresh.expiresAt,
    scope:
      fresh.scope.length > 0
        ? fresh.scope
        : previous.scope,
    idToken: fresh.idToken !== null ? fresh.idToken : previous.idToken,
    raw: fresh.raw,
  }) as TokenSet
  return [merged, rotated]
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Auto-refresh middleware
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Caller-implemented refresh callback — performs the actual provider
 * POST (token endpoint URL + auth + content-type vary by vendor). */
export type RefreshFn = (refreshToken: string) => Promise<unknown>

/** Caller-implemented rotation hook — fires after every refresh so the
 * caller can persist the new access_token + decide whether to invalidate
 * the old refresh_token from any cache (rotated=true). */
export type RotationHook = (
  oldToken: TokenSet,
  newToken: TokenSet,
  rotated: boolean,
) => Promise<void> | void

export interface AutoRefreshOptions {
  skewSeconds?: number
  onRotated?: RotationHook
  now?: number
}

/** Return a TokenSet that is fresh for at least `skewSeconds` more.
 *
 * If `current` has not yet entered the skew window, returns it
 * unchanged. Otherwise:
 *
 *   1. Calls `refreshFn(current.refreshToken)` to obtain the provider's
 *      new token response.
 *   2. Merges via `applyRotation`.
 *   3. Awaits `onRotated(old, new, rotated)` so the caller can persist +
 *      audit (always fires, even when rotated=false — the caller still
 *      needs to persist the new accessToken).
 *   4. Returns the merged TokenSet.
 *
 * Throws `TokenRefreshError` if the current token has no refresh_token.
 */
export async function autoRefresh(
  current: TokenSet,
  refreshFn: RefreshFn,
  opts: AutoRefreshOptions = {},
): Promise<TokenSet> {
  const skew = opts.skewSeconds ?? DEFAULT_REFRESH_SKEW_SECONDS
  if (!needsRefresh(current, { skewSeconds: skew, now: opts.now })) {
    return current
  }
  if (!current.refreshToken) {
    throw new TokenRefreshError(
      "current token has no refresh_token; user must re-authenticate",
    )
  }
  const payload = await refreshFn(current.refreshToken)
  const [newToken, rotated] = applyRotation(current, payload, { now: opts.now })
  if (opts.onRotated) {
    await opts.onRotated(current, newToken, rotated)
  }
  return newToken
}

/** Fetch wrapper that auto-refreshes the access token before each request
 * and sets the `Authorization` header.
 *
 * TS twin of the Python `AutoRefreshAuth(httpx.Auth)` middleware. Usage:
 *
 *   const fetcher = new AutoRefreshFetch(token, refreshFn, { onRotated })
 *   const r = await fetcher.fetch("https://api.example/me")
 *
 * The instance mutates its own `token` field so subsequent requests
 * within the same lifetime reuse the freshly-rotated value without
 * re-fetching from storage. Cross-tab / cross-page reuse is the caller's
 * responsibility (via `onRotated`).
 */
export class AutoRefreshFetch {
  token: TokenSet
  private readonly refreshFn: RefreshFn
  private readonly skewSeconds: number
  private readonly onRotated: RotationHook | undefined
  private readonly fetchImpl: typeof fetch

  constructor(
    token: TokenSet,
    refreshFn: RefreshFn,
    opts: {
      skewSeconds?: number
      onRotated?: RotationHook
      fetchImpl?: typeof fetch
    } = {},
  ) {
    if (!token || typeof token !== "object" || typeof (token as TokenSet).accessToken !== "string") {
      throw new TypeError("token must be a TokenSet")
    }
    this.token = token
    this.refreshFn = refreshFn
    this.skewSeconds = opts.skewSeconds ?? DEFAULT_REFRESH_SKEW_SECONDS
    this.onRotated = opts.onRotated
    this.fetchImpl =
      opts.fetchImpl ??
      ((globalThis as { fetch?: typeof fetch }).fetch as typeof fetch)
    if (typeof this.fetchImpl !== "function") {
      throw new Error(
        "no fetch implementation available — pass `fetchImpl` explicitly",
      )
    }
  }

  async fetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
    this.token = await autoRefresh(this.token, this.refreshFn, {
      skewSeconds: this.skewSeconds,
      onRotated: this.onRotated,
    })
    const headers = new Headers(init.headers ?? {})
    headers.set(
      "Authorization",
      `${this.token.tokenType || "Bearer"} ${this.token.accessToken}`,
    )
    return this.fetchImpl(input, { ...init, headers })
  }
}
