/**
 * AS.5.1 — Auth event format (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/security/auth_event.py`.
 * Provides:
 *
 *   * Eight `EVENT_AUTH_*` string constants (login_success / login_fail
 *     / oauth_connect / oauth_revoke / bot_challenge_pass /
 *     bot_challenge_fail / token_refresh / token_rotated) + the
 *     `ALL_AUTH_EVENTS` array.
 *   * Per-event vocabulary sets (auth methods, login fail reasons, bot-
 *     challenge pass kinds + fail reasons, token refresh outcomes,
 *     rotation triggers, oauth-connect outcomes, oauth-revoke
 *     initiators) — frozen `Set<string>`.
 *   * Eight typed `*Context` interfaces matching the Python frozen
 *     dataclasses field-for-field.
 *   * Eight pure async `build*Payload` builders that produce the same
 *     `{action, entityKind, entityId, before, after, actor}` shape the
 *     Python side hands to `audit.log(...)`.
 *   * Eight `emit*` async wrappers that gate on `isEnabled()` and
 *     route through a caller-supplied `AuthAuditSink` (defaults to a
 *     no-op sink) — generated apps inject their own sink (typically a
 *     thin `fetch(POST /audit)` wrapper, sometimes a structured-log
 *     writer for offline contexts).
 *   * `fingerprint(value)` — first-12-chars SHA-256 hex helper, byte-
 *     identical to the Python helper of the same name (and to AS.1.4
 *     `oauth-client/audit.ts::fingerprint`).
 *
 * Cross-twin contract (enforced by AS.5.1 drift guard)
 * ────────────────────────────────────────────────────
 * For every event family the following MUST be byte-identical:
 *
 *   * Eight action strings
 *   * Three `entity_kind` constants (`auth_session` /
 *     `oauth_connection` / `oauth_token`)
 *   * Auth method vocabulary (6 strings)
 *   * Login fail reasons vocabulary (10 strings)
 *   * Bot-challenge pass kinds (4 strings)
 *   * Bot-challenge fail reasons (5 strings)
 *   * Token refresh outcomes (3 strings)
 *   * Token rotation triggers (2 strings)
 *   * OAuth connect outcomes (2 strings)
 *   * OAuth revoke initiators (3 strings)
 *   * Per-event `after` field set (sorted, optional fields enumerated)
 *   * `before` shape per event (null for most; `{provider,
 *     prior_refresh_token_fp}` for `auth.token_rotated`)
 *   * Default actor rules (user_id fallback / "anonymous" fallback)
 *   * Entity-id format (`provider:userId` for oauth-connection +
 *     oauth-token rows; `form_path` for bot-challenge rows; `user_id`
 *     for login_success; fingerprint for login_fail)
 *   * Fingerprint algorithm (SHA-256 hex, slice 0..12)
 *   * `FINGERPRINT_LENGTH` integer
 *
 * The drift guard test (`backend/tests/test_auth_event_shape_drift.py`)
 * regex-extracts the static pins and (optionally, when Node ≥ 22 is
 * available) drives a behavioural fixture matrix through both twins.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * No module-level mutable state — only frozen literals + pure
 *     functions; the sink is per-call (caller-injected) so each
 *     generated app gets its own sink instance.
 *   * No env reads at module top — `isEnabled()` resolves
 *     `OMNISIGHT_AS_FRONTEND_ENABLED` lazily on every call.  Each
 *     browser tab / Node worker derives the same value from the same
 *     env source — answer #1 of SOP §1 audit (deterministic-by-
 *     construction across workers).
 *   * SHA-256 comes from `crypto.subtle.digest("SHA-256", …)` (Web
 *     Crypto).  Same digest the Python side uses (`hashlib.sha256`),
 *     same first-12-hex-char slicing, so fingerprints round-trip
 *     across the two twins.
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 * `emit*` helpers consult `isEnabled()` first and silently no-op
 * (return `null`) if the AS knob is off — mirrors the Python side.
 * The pure `build*Payload` builders deliberately do NOT consult the
 * knob; a script that wants to inspect the canonical payload shape
 * (test harness, doc generator) must work regardless.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Eight canonical event names — AS.5.1 SoT
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const EVENT_AUTH_LOGIN_SUCCESS = "auth.login_success"
export const EVENT_AUTH_LOGIN_FAIL = "auth.login_fail"
export const EVENT_AUTH_OAUTH_CONNECT = "auth.oauth_connect"
export const EVENT_AUTH_OAUTH_REVOKE = "auth.oauth_revoke"
export const EVENT_AUTH_BOT_CHALLENGE_PASS = "auth.bot_challenge_pass"
export const EVENT_AUTH_BOT_CHALLENGE_FAIL = "auth.bot_challenge_fail"
export const EVENT_AUTH_TOKEN_REFRESH = "auth.token_refresh"
export const EVENT_AUTH_TOKEN_ROTATED = "auth.token_rotated"

export const ALL_AUTH_EVENTS: ReadonlyArray<string> = Object.freeze([
  EVENT_AUTH_LOGIN_SUCCESS,
  EVENT_AUTH_LOGIN_FAIL,
  EVENT_AUTH_OAUTH_CONNECT,
  EVENT_AUTH_OAUTH_REVOKE,
  EVENT_AUTH_BOT_CHALLENGE_PASS,
  EVENT_AUTH_BOT_CHALLENGE_FAIL,
  EVENT_AUTH_TOKEN_REFRESH,
  EVENT_AUTH_TOKEN_ROTATED,
])

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  entity_kind constants
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Per-attempt auth events (login_success / login_fail /
 * bot_challenge_pass / bot_challenge_fail). */
export const ENTITY_KIND_AUTH_SESSION = "auth_session"

/** Per-user-per-provider connection (oauth_connect / oauth_revoke).
 * Sibling to AS.1.4 `oauth_token`; chosen to disambiguate "the user-
 * visible connection" from "the stored token blob". */
export const ENTITY_KIND_OAUTH_CONNECTION = "oauth_connection"

/** Token-lifecycle events (token_refresh / token_rotated).  Same string
 * as AS.1.4 `oauth_audit.ENTITY_KIND_TOKEN` so the AS.5.2 dashboard
 * can correlate the rollup family with the forensic family. */
export const ENTITY_KIND_OAUTH_TOKEN = "oauth_token"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Vocabularies — frozen sets the dashboard widget keys on
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const AUTH_METHOD_PASSWORD = "password"
export const AUTH_METHOD_OAUTH = "oauth"
export const AUTH_METHOD_PASSKEY = "passkey"
export const AUTH_METHOD_MFA_TOTP = "mfa_totp"
export const AUTH_METHOD_MFA_WEBAUTHN = "mfa_webauthn"
export const AUTH_METHOD_MAGIC_LINK = "magic_link"

export const AUTH_METHODS: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_PASSKEY,
    AUTH_METHOD_MFA_TOTP,
    AUTH_METHOD_MFA_WEBAUTHN,
    AUTH_METHOD_MAGIC_LINK,
  ]),
)

export const LOGIN_FAIL_BAD_PASSWORD = "bad_password"
export const LOGIN_FAIL_UNKNOWN_USER = "unknown_user"
export const LOGIN_FAIL_ACCOUNT_LOCKED = "account_locked"
export const LOGIN_FAIL_ACCOUNT_DISABLED = "account_disabled"
export const LOGIN_FAIL_MFA_REQUIRED = "mfa_required"
export const LOGIN_FAIL_MFA_FAILED = "mfa_failed"
export const LOGIN_FAIL_RATE_LIMITED = "rate_limited"
export const LOGIN_FAIL_BOT_CHALLENGE_FAILED = "bot_challenge_failed"
export const LOGIN_FAIL_OAUTH_STATE_INVALID = "oauth_state_invalid"
export const LOGIN_FAIL_OAUTH_PROVIDER_ERROR = "oauth_provider_error"

export const LOGIN_FAIL_REASONS: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    LOGIN_FAIL_BAD_PASSWORD,
    LOGIN_FAIL_UNKNOWN_USER,
    LOGIN_FAIL_ACCOUNT_LOCKED,
    LOGIN_FAIL_ACCOUNT_DISABLED,
    LOGIN_FAIL_MFA_REQUIRED,
    LOGIN_FAIL_MFA_FAILED,
    LOGIN_FAIL_RATE_LIMITED,
    LOGIN_FAIL_BOT_CHALLENGE_FAILED,
    LOGIN_FAIL_OAUTH_STATE_INVALID,
    LOGIN_FAIL_OAUTH_PROVIDER_ERROR,
  ]),
)

export const BOT_CHALLENGE_PASS_VERIFIED = "verified"
export const BOT_CHALLENGE_PASS_BYPASS_APIKEY = "bypass_apikey"
export const BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST = "bypass_ip_allowlist"
export const BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN = "bypass_test_token"

export const BOT_CHALLENGE_PASS_KINDS: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    BOT_CHALLENGE_PASS_VERIFIED,
    BOT_CHALLENGE_PASS_BYPASS_APIKEY,
    BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST,
    BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN,
  ]),
)

export const BOT_CHALLENGE_FAIL_LOWSCORE = "lowscore"
export const BOT_CHALLENGE_FAIL_UNVERIFIED = "unverified"
export const BOT_CHALLENGE_FAIL_HONEYPOT = "honeypot"
export const BOT_CHALLENGE_FAIL_JSFAIL = "jsfail"
export const BOT_CHALLENGE_FAIL_SERVER_ERROR = "server_error"

export const BOT_CHALLENGE_FAIL_REASONS: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    BOT_CHALLENGE_FAIL_LOWSCORE,
    BOT_CHALLENGE_FAIL_UNVERIFIED,
    BOT_CHALLENGE_FAIL_HONEYPOT,
    BOT_CHALLENGE_FAIL_JSFAIL,
    BOT_CHALLENGE_FAIL_SERVER_ERROR,
  ]),
)

export const TOKEN_REFRESH_SUCCESS = "success"
export const TOKEN_REFRESH_NO_REFRESH_TOKEN = "no_refresh_token"
export const TOKEN_REFRESH_PROVIDER_ERROR = "provider_error"

export const TOKEN_REFRESH_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    TOKEN_REFRESH_SUCCESS,
    TOKEN_REFRESH_NO_REFRESH_TOKEN,
    TOKEN_REFRESH_PROVIDER_ERROR,
  ]),
)

export const TOKEN_ROTATION_TRIGGER_AUTO = "auto_refresh"
export const TOKEN_ROTATION_TRIGGER_EXPLICIT = "explicit_refresh"

export const TOKEN_ROTATION_TRIGGERS: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    TOKEN_ROTATION_TRIGGER_AUTO,
    TOKEN_ROTATION_TRIGGER_EXPLICIT,
  ]),
)

export const OAUTH_CONNECT_CONNECTED = "connected"
export const OAUTH_CONNECT_RELINKED = "relinked"

export const OAUTH_CONNECT_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([OAUTH_CONNECT_CONNECTED, OAUTH_CONNECT_RELINKED]),
)

export const OAUTH_REVOKE_USER = "user"
export const OAUTH_REVOKE_ADMIN = "admin"
export const OAUTH_REVOKE_DSAR = "dsar"

export const OAUTH_REVOKE_INITIATORS: ReadonlySet<string> = Object.freeze(
  new Set<string>([OAUTH_REVOKE_USER, OAUTH_REVOKE_ADMIN, OAUTH_REVOKE_DSAR]),
)

/** First N chars of a SHA-256 hex digest. 12 = 48 bits, plenty for
 * forensic correlation without leaking the underlying secret. Mirrors
 * AS.1.4 `oauth-client/audit.ts::FINGERPRINT_LENGTH` byte-for-byte. */
export const FINGERPRINT_LENGTH = 12

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Helpers — fingerprint, AS.0.8 knob gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function getSubtle(): SubtleCrypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || !c.subtle) {
    throw new Error(
      "Web Crypto API not available — SHA-256 is required for AS.5.1 auth event fingerprints",
    )
  }
  return c.subtle
}

function bytesToHex(buf: ArrayBuffer): string {
  const view = new Uint8Array(buf)
  let out = ""
  for (let i = 0; i < view.length; i++) {
    out += view[i].toString(16).padStart(2, "0")
  }
  return out
}

/** Stable first-12-chars SHA-256 fingerprint of *value*.
 *
 * Returns `null` for `null` / `undefined` / empty string so the JSON
 * column round-trips a typed null instead of an empty string ("").
 * Byte-identical contract with `backend/security/auth_event.py
 * .fingerprint` and `oauth-client/audit.ts::fingerprint`. */
export async function fingerprint(
  value: string | null | undefined,
): Promise<string | null> {
  if (value === null || value === undefined || value === "") return null
  const subtle = getSubtle()
  const buf = await subtle.digest("SHA-256", new TextEncoder().encode(value))
  return bytesToHex(buf).slice(0, FINGERPRINT_LENGTH)
}

/** Whether the AS family is enabled per AS.0.8 §3.1 noop matrix.
 *
 * Reads `OMNISIGHT_AS_FRONTEND_ENABLED` lazily — defaults to `true`
 * (forward-promotion guard).  Mirrors the Python lib's `is_enabled()`. */
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
//  Context interfaces — caller-built, builder-consumed
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Inputs for `auth.login_success`. */
export interface LoginSuccessContext {
  readonly userId: string
  readonly authMethod: string
  readonly provider?: string | null
  readonly mfaSatisfied?: boolean
  readonly ip?: string | null
  readonly userAgent?: string | null
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.login_fail`. */
export interface LoginFailContext {
  readonly attemptedUser: string
  readonly authMethod: string
  readonly failReason: string
  readonly provider?: string | null
  readonly ip?: string | null
  readonly userAgent?: string | null
  /** Defaults to `"anonymous"` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.oauth_connect`. */
export interface OAuthConnectContext {
  readonly userId: string
  readonly provider: string
  readonly outcome: string
  readonly scope?: readonly string[]
  readonly isAccountLink?: boolean
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.oauth_revoke`. */
export interface OAuthRevokeContext {
  readonly userId: string
  readonly provider: string
  readonly initiator: string
  readonly revocationSucceeded?: boolean
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.bot_challenge_pass`. */
export interface BotChallengePassContext {
  readonly formPath: string
  readonly kind: string
  readonly provider?: string | null
  readonly score?: number | null
  /** Defaults to `"anonymous"` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.bot_challenge_fail`. */
export interface BotChallengeFailContext {
  readonly formPath: string
  readonly reason: string
  readonly provider?: string | null
  readonly score?: number | null
  /** Defaults to `"anonymous"` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.token_refresh`. */
export interface TokenRefreshContext {
  readonly userId: string
  readonly provider: string
  readonly outcome: string
  readonly newExpiresInSeconds?: number | null
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

/** Inputs for `auth.token_rotated`. */
export interface TokenRotatedContext {
  readonly userId: string
  readonly provider: string
  readonly previousRefreshToken: string
  readonly newRefreshToken: string
  readonly triggeredBy: string
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Audit-row payload (the shape the sink receives)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** A canonical AS.5.1 audit row, ready to be handed to a sink.  The
 * shape mirrors `backend.audit.log(action=, entity_kind=, entity_id=,
 * before=, after=, actor=)`. */
export interface AuthAuditPayload {
  readonly action: string
  readonly entityKind: string
  readonly entityId: string
  readonly before: Record<string, unknown> | null
  readonly after: Record<string, unknown>
  readonly actor: string
}

/** Caller-supplied audit sink.  Receives a fully-built payload and
 * forwards it to whatever transport the generated app uses (typically
 * `fetch("POST /audit", ...)` against the OmniSight backend, or a
 * structured-log writer for offline contexts).
 *
 * Sinks SHOULD NOT throw on transient transport failures — mirror the
 * Python `audit.log` policy of "log a warning, never raise".  The
 * default sink (`noopSink`) discards the payload silently and is
 * intended for tests + the no-audit-needed path. */
export type AuthAuditSink = (payload: AuthAuditPayload) => Promise<void>

/** Default sink — discards the payload silently. */
export const noopSink: AuthAuditSink = async (_payload) => {
  /* intentionally empty */
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Pure builders — no IO, no knob check
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function entityIdOAuthConnection(provider: string, userId: string): string {
  return `${provider}:${userId}`
}

/** Build the canonical `auth.login_success` payload.  Validates
 * `authMethod` against `AUTH_METHODS`. */
export async function buildLoginSuccessPayload(
  ctx: LoginSuccessContext,
): Promise<AuthAuditPayload> {
  if (!AUTH_METHODS.has(ctx.authMethod)) {
    throw new Error(
      `auth.login_success auth_method ${JSON.stringify(ctx.authMethod)} not in ` +
        `[${[...AUTH_METHODS].sort().join(", ")}]`,
    )
  }
  const [ipFp, uaFp] = await Promise.all([
    fingerprint(ctx.ip ?? null),
    fingerprint(ctx.userAgent ?? null),
  ])
  return {
    action: EVENT_AUTH_LOGIN_SUCCESS,
    entityKind: ENTITY_KIND_AUTH_SESSION,
    entityId: ctx.userId,
    before: null,
    after: {
      auth_method: ctx.authMethod,
      provider: ctx.provider ?? null,
      mfa_satisfied: Boolean(ctx.mfaSatisfied),
      ip_fp: ipFp,
      user_agent_fp: uaFp,
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `auth.login_fail` payload.  Validates
 * `authMethod` and `failReason`.  `attemptedUser` is fingerprinted; the
 * raw value never lands in the chain. */
export async function buildLoginFailPayload(
  ctx: LoginFailContext,
): Promise<AuthAuditPayload> {
  if (!AUTH_METHODS.has(ctx.authMethod)) {
    throw new Error(
      `auth.login_fail auth_method ${JSON.stringify(ctx.authMethod)} not in ` +
        `[${[...AUTH_METHODS].sort().join(", ")}]`,
    )
  }
  if (!LOGIN_FAIL_REASONS.has(ctx.failReason)) {
    throw new Error(
      `auth.login_fail fail_reason ${JSON.stringify(ctx.failReason)} not in ` +
        `[${[...LOGIN_FAIL_REASONS].sort().join(", ")}]`,
    )
  }
  const [attemptedFp, ipFp, uaFp] = await Promise.all([
    fingerprint(ctx.attemptedUser),
    fingerprint(ctx.ip ?? null),
    fingerprint(ctx.userAgent ?? null),
  ])
  return {
    action: EVENT_AUTH_LOGIN_FAIL,
    entityKind: ENTITY_KIND_AUTH_SESSION,
    entityId: attemptedFp ?? "anonymous",
    before: null,
    after: {
      auth_method: ctx.authMethod,
      fail_reason: ctx.failReason,
      provider: ctx.provider ?? null,
      attempted_user_fp: attemptedFp,
      ip_fp: ipFp,
      user_agent_fp: uaFp,
    },
    actor: ctx.actor ?? "anonymous",
  }
}

/** Build the canonical `auth.oauth_connect` payload.  Validates
 * `outcome` against `OAUTH_CONNECT_OUTCOMES`. */
export async function buildOAuthConnectPayload(
  ctx: OAuthConnectContext,
): Promise<AuthAuditPayload> {
  if (!OAUTH_CONNECT_OUTCOMES.has(ctx.outcome)) {
    throw new Error(
      `auth.oauth_connect outcome ${JSON.stringify(ctx.outcome)} not in ` +
        `[${[...OAUTH_CONNECT_OUTCOMES].sort().join(", ")}]`,
    )
  }
  return {
    action: EVENT_AUTH_OAUTH_CONNECT,
    entityKind: ENTITY_KIND_OAUTH_CONNECTION,
    entityId: entityIdOAuthConnection(ctx.provider, ctx.userId),
    before: null,
    after: {
      provider: ctx.provider,
      outcome: ctx.outcome,
      scope: Array.from(ctx.scope ?? []),
      is_account_link: Boolean(ctx.isAccountLink),
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `auth.oauth_revoke` payload.  Validates
 * `initiator` against `OAUTH_REVOKE_INITIATORS`. */
export async function buildOAuthRevokePayload(
  ctx: OAuthRevokeContext,
): Promise<AuthAuditPayload> {
  if (!OAUTH_REVOKE_INITIATORS.has(ctx.initiator)) {
    throw new Error(
      `auth.oauth_revoke initiator ${JSON.stringify(ctx.initiator)} not in ` +
        `[${[...OAUTH_REVOKE_INITIATORS].sort().join(", ")}]`,
    )
  }
  return {
    action: EVENT_AUTH_OAUTH_REVOKE,
    entityKind: ENTITY_KIND_OAUTH_CONNECTION,
    entityId: entityIdOAuthConnection(ctx.provider, ctx.userId),
    before: null,
    after: {
      provider: ctx.provider,
      initiator: ctx.initiator,
      revocation_succeeded: Boolean(ctx.revocationSucceeded),
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `auth.bot_challenge_pass` payload.  Validates
 * `kind` against `BOT_CHALLENGE_PASS_KINDS`.  `score` is required when
 * `kind="verified"`, must be `null` for bypass kinds. */
export async function buildBotChallengePassPayload(
  ctx: BotChallengePassContext,
): Promise<AuthAuditPayload> {
  if (!BOT_CHALLENGE_PASS_KINDS.has(ctx.kind)) {
    throw new Error(
      `auth.bot_challenge_pass kind ${JSON.stringify(ctx.kind)} not in ` +
        `[${[...BOT_CHALLENGE_PASS_KINDS].sort().join(", ")}]`,
    )
  }
  const score = ctx.score ?? null
  if (ctx.kind === BOT_CHALLENGE_PASS_VERIFIED && score === null) {
    throw new Error(
      "auth.bot_challenge_pass kind='verified' requires score",
    )
  }
  if (ctx.kind !== BOT_CHALLENGE_PASS_VERIFIED && score !== null) {
    throw new Error(
      `auth.bot_challenge_pass kind=${JSON.stringify(ctx.kind)} must have score=null ` +
        "(no challenge ran)",
    )
  }
  return {
    action: EVENT_AUTH_BOT_CHALLENGE_PASS,
    entityKind: ENTITY_KIND_AUTH_SESSION,
    entityId: ctx.formPath,
    before: null,
    after: {
      form_path: ctx.formPath,
      kind: ctx.kind,
      provider: ctx.provider ?? null,
      score: score === null ? null : Number(score),
    },
    actor: ctx.actor ?? "anonymous",
  }
}

/** Build the canonical `auth.bot_challenge_fail` payload.  Validates
 * `reason` against `BOT_CHALLENGE_FAIL_REASONS`. */
export async function buildBotChallengeFailPayload(
  ctx: BotChallengeFailContext,
): Promise<AuthAuditPayload> {
  if (!BOT_CHALLENGE_FAIL_REASONS.has(ctx.reason)) {
    throw new Error(
      `auth.bot_challenge_fail reason ${JSON.stringify(ctx.reason)} not in ` +
        `[${[...BOT_CHALLENGE_FAIL_REASONS].sort().join(", ")}]`,
    )
  }
  return {
    action: EVENT_AUTH_BOT_CHALLENGE_FAIL,
    entityKind: ENTITY_KIND_AUTH_SESSION,
    entityId: ctx.formPath,
    before: null,
    after: {
      form_path: ctx.formPath,
      reason: ctx.reason,
      provider: ctx.provider ?? null,
      score:
        ctx.score === null || ctx.score === undefined ? null : Number(ctx.score),
    },
    actor: ctx.actor ?? "anonymous",
  }
}

/** Build the canonical `auth.token_refresh` payload.  Validates
 * `outcome` against `TOKEN_REFRESH_OUTCOMES`. */
export async function buildTokenRefreshPayload(
  ctx: TokenRefreshContext,
): Promise<AuthAuditPayload> {
  if (!TOKEN_REFRESH_OUTCOMES.has(ctx.outcome)) {
    throw new Error(
      `auth.token_refresh outcome ${JSON.stringify(ctx.outcome)} not in ` +
        `[${[...TOKEN_REFRESH_OUTCOMES].sort().join(", ")}]`,
    )
  }
  return {
    action: EVENT_AUTH_TOKEN_REFRESH,
    entityKind: ENTITY_KIND_OAUTH_TOKEN,
    entityId: entityIdOAuthConnection(ctx.provider, ctx.userId),
    before: null,
    after: {
      provider: ctx.provider,
      outcome: ctx.outcome,
      new_expires_in_seconds:
        ctx.newExpiresInSeconds === undefined ||
        ctx.newExpiresInSeconds === null
          ? null
          : Math.trunc(ctx.newExpiresInSeconds),
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `auth.token_rotated` payload.  Validates
 * `triggeredBy` against `TOKEN_ROTATION_TRIGGERS`.  Both refresh tokens
 * are stored as 12-char SHA-256 fingerprints — raw values are
 * credentials and never written. */
export async function buildTokenRotatedPayload(
  ctx: TokenRotatedContext,
): Promise<AuthAuditPayload> {
  if (!TOKEN_ROTATION_TRIGGERS.has(ctx.triggeredBy)) {
    throw new Error(
      `auth.token_rotated triggered_by ${JSON.stringify(ctx.triggeredBy)} not in ` +
        `[${[...TOKEN_ROTATION_TRIGGERS].sort().join(", ")}]`,
    )
  }
  const [priorFp, newFp] = await Promise.all([
    fingerprint(ctx.previousRefreshToken),
    fingerprint(ctx.newRefreshToken),
  ])
  return {
    action: EVENT_AUTH_TOKEN_ROTATED,
    entityKind: ENTITY_KIND_OAUTH_TOKEN,
    entityId: entityIdOAuthConnection(ctx.provider, ctx.userId),
    before: { provider: ctx.provider, prior_refresh_token_fp: priorFp },
    after: {
      provider: ctx.provider,
      new_refresh_token_fp: newFp,
      triggered_by: ctx.triggeredBy,
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Sink-fanout emitters — gate on isEnabled(), then forward
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export async function emitLoginSuccess(
  ctx: LoginSuccessContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildLoginSuccessPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitLoginFail(
  ctx: LoginFailContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildLoginFailPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitOAuthConnect(
  ctx: OAuthConnectContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildOAuthConnectPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitOAuthRevoke(
  ctx: OAuthRevokeContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildOAuthRevokePayload(ctx)
  await sink(payload)
  return payload
}

export async function emitBotChallengePass(
  ctx: BotChallengePassContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildBotChallengePassPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitBotChallengeFail(
  ctx: BotChallengeFailContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildBotChallengeFailPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitTokenRefresh(
  ctx: TokenRefreshContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildTokenRefreshPayload(ctx)
  await sink(payload)
  return payload
}

export async function emitTokenRotated(
  ctx: TokenRotatedContext,
  sink: AuthAuditSink = noopSink,
): Promise<AuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildTokenRotatedPayload(ctx)
  await sink(payload)
  return payload
}
