/**
 * AS.1.4 — OAuth audit event format + unified emit (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/security/oauth_audit.py`.
 * Provides:
 *
 *   * Five typed `OAuth*Context` interfaces matching the Python
 *     `LoginInitContext` / `LoginCallbackContext` / `RefreshContext` /
 *     `UnlinkContext` / `TokenRotatedContext` dataclasses.
 *   * Five `buildOAuth*Payload` pure-functional payload builders that
 *     produce the same `{action, entityKind, entityId, before, after,
 *     actor}` shape the Python side hands to `audit.log(...)`.
 *   * Five `emitOAuth*` async wrappers that route through a caller-
 *     supplied `OAuthAuditSink` (defaults to a no-op sink) — generated
 *     apps inject their own sink (typically a thin `fetch(POST /audit)`
 *     wrapper, sometimes a structured-log writer for offline contexts).
 *   * `fingerprint(value)` — first-12-chars SHA-256 hex helper, byte-
 *     identical to the Python helper of the same name.
 *
 * Cross-twin contract (enforced by AS.1.5 drift guard)
 * ────────────────────────────────────────────────────
 * For every event family the following MUST be byte-identical:
 *
 *   * action string          (already pinned by AS.1.2 — sourced from `./index`)
 *   * entity_kind constant   (`oauth_flow` / `oauth_token`)
 *   * outcome vocabulary     (10 strings — login_callback × 5,
 *                             refresh × 3, unlink × 3, revocation × 3,
 *                             rotation_trigger × 2 — sets overlap on
 *                             `success` / `revocation_failed`)
 *   * `before` field set     (per-event, sorted)
 *   * `after` field set      (per-event, sorted, optional fields
 *                             enumerated; `error` only when present)
 *   * fingerprint algorithm  (SHA-256 hex, slice 0..12)
 *   * entity_id format       (`provider` for flow rows;
 *                             `${provider}:${userId}` for token rows)
 *
 * The drift guard test (`backend/tests/test_oauth_audit.py`) hashes the
 * canonical field-set tuples + outcome sets across both sides and
 * asserts SHA-256 equality, the same pattern AS.1.3 used for vendor
 * catalog parity.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * No module-level mutable state — only frozen literals and pure
 *     functions; the sink is per-call (caller-injected) so each
 *     generated app gets its own sink instance.
 *   * No env reads at module top — the AS knob is read lazily via
 *     `isEnabled()` from `./index` (resolved per call from
 *     `globalThis.OMNISIGHT_AS_FRONTEND_ENABLED` /
 *     `process.env.OMNISIGHT_AS_FRONTEND_ENABLED`).
 *   * SHA-256 comes from `crypto.subtle.digest("SHA-256", …)` (Web
 *     Crypto). Same digest the Python side uses (`hashlib.sha256`),
 *     same first-12-hex-char slicing, so fingerprints round-trip
 *     across the two twins.
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 * `emitOAuth*` helpers consult `isEnabled()` first and silently no-op
 * (return `null`) if the AS knob is off — mirrors the Python side. The
 * pure `buildOAuth*Payload` builders deliberately do NOT consult the
 * knob; a script that wants to inspect the canonical payload shape (for
 * instance, a tests harness) should be able to do so regardless.
 */

import {
  EVENT_OAUTH_LOGIN_INIT,
  EVENT_OAUTH_LOGIN_CALLBACK,
  EVENT_OAUTH_REFRESH,
  EVENT_OAUTH_UNLINK,
  EVENT_OAUTH_TOKEN_ROTATED,
  isEnabled,
} from "./index"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — entity_kind + outcome vocabularies
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Audit-row `entity_kind` for in-flight authorisation rows. */
export const ENTITY_KIND_FLOW = "oauth_flow"
/** Audit-row `entity_kind` for stored-token rows (refresh / unlink / rotate). */
export const ENTITY_KIND_TOKEN = "oauth_token"

// Outcome vocabulary — pinned strings the dashboard widget keys on.
// Must stay byte-identical with `backend/security/oauth_audit.py`.

export const OUTCOME_SUCCESS = "success"
export const OUTCOME_STATE_MISMATCH = "state_mismatch"
export const OUTCOME_STATE_EXPIRED = "state_expired"
export const OUTCOME_TOKEN_ERROR = "token_error"
export const OUTCOME_CALLBACK_ERROR = "callback_error"
export const OUTCOME_NO_REFRESH_TOKEN = "no_refresh_token"
export const OUTCOME_PROVIDER_ERROR = "provider_error"
export const OUTCOME_NOT_LINKED = "not_linked"
export const OUTCOME_REVOCATION_FAILED = "revocation_failed"
export const OUTCOME_REVOCATION_SKIPPED = "revocation_skipped"

/** Allowed outcomes for `oauth.login_callback`. */
export const LOGIN_CALLBACK_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    OUTCOME_SUCCESS,
    OUTCOME_STATE_MISMATCH,
    OUTCOME_STATE_EXPIRED,
    OUTCOME_TOKEN_ERROR,
    OUTCOME_CALLBACK_ERROR,
  ]),
)

/** Allowed outcomes for `oauth.refresh`. */
export const REFRESH_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    OUTCOME_SUCCESS,
    OUTCOME_NO_REFRESH_TOKEN,
    OUTCOME_PROVIDER_ERROR,
  ]),
)

/** Allowed outcomes for `oauth.unlink`. */
export const UNLINK_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    OUTCOME_SUCCESS,
    OUTCOME_NOT_LINKED,
    OUTCOME_REVOCATION_FAILED,
  ]),
)

/** Allowed `triggered_by` values for `oauth.token_rotated`. */
export const ROTATION_TRIGGERS: ReadonlySet<string> = Object.freeze(
  new Set<string>(["auto_refresh", "explicit_refresh"]),
)

/** Allowed `revocation_outcome` values when `revocation_attempted=true`. */
export const REVOCATION_OUTCOMES: ReadonlySet<string> = Object.freeze(
  new Set<string>([
    OUTCOME_SUCCESS,
    OUTCOME_REVOCATION_FAILED,
    OUTCOME_REVOCATION_SKIPPED,
  ]),
)

/** First N chars of a SHA-256 hex digest. 12 = 48 bits, plenty for forensic
 * correlation without leaking the underlying secret. Mirrors the Python
 * side's `FINGERPRINT_LENGTH` and AS.0.6 §5 `token_fp last-12` convention. */
export const FINGERPRINT_LENGTH = 12

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Helpers — fingerprint, scope normalise, entity_id compose
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Resolve the platform Web-Crypto impl for SHA-256. Throws typed if absent. */
function getSubtle(): SubtleCrypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || !c.subtle) {
    throw new Error(
      "Web Crypto API not available — SHA-256 is required for OAuth audit fingerprints",
    )
  }
  return c.subtle
}

/** Hex-encode raw bytes (lowercase). */
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
 * Byte-identical contract with `backend/security/oauth_audit.fingerprint`. */
export async function fingerprint(value: string | null | undefined): Promise<string | null> {
  if (value === null || value === undefined || value === "") return null
  const subtle = getSubtle()
  const buf = await subtle.digest("SHA-256", new TextEncoder().encode(value))
  return bytesToHex(buf).slice(0, FINGERPRINT_LENGTH)
}

/** Normalise scope to a string array.
 *
 * Accepts `null` (→ `[]`), a single space- or comma-separated string
 * (→ split + dedupe-empty), or any iterable of strings. Mirrors
 * `_normalize_scope` on the Python side so the audit row's scope
 * shape is stable regardless of caller layer. */
export function normalizeScope(scope: string | readonly string[] | null | undefined): string[] {
  if (scope === null || scope === undefined) return []
  if (typeof scope === "string") {
    return scope
      .replace(/,/g, " ")
      .split(/\s+/)
      .filter((s) => s.length > 0)
  }
  return Array.from(scope, (s) => String(s))
}

/** Compose the `entity_id` for `oauth_token`-kind rows.
 * `${provider}:${userId}` — same key as the AS.2.x token vault row. */
export function entityIdToken(provider: string, userId: string): string {
  return `${provider}:${userId}`
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Context interfaces — caller-built, builder-consumed
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Inputs for `oauth.login_init`. */
export interface LoginInitContext {
  readonly provider: string
  readonly state: string
  readonly scope: readonly string[]
  readonly redirectUri: string
  readonly useOidcNonce: boolean
  readonly stateTtlSeconds: number
  /** Defaults to `"anonymous"` when omitted. */
  readonly actor?: string
}

/** Inputs for `oauth.login_callback`. */
export interface LoginCallbackContext {
  readonly provider: string
  readonly state: string
  readonly outcome: string
  readonly actor?: string
  readonly grantedScope?: readonly string[]
  readonly hasRefreshToken?: boolean
  readonly expiresInSeconds?: number | null
  readonly isOidc?: boolean
  readonly error?: string | null
}

/** Inputs for `oauth.refresh`. */
export interface RefreshContext {
  readonly provider: string
  readonly userId: string
  readonly outcome: string
  readonly previousExpiresAt?: number | null
  readonly newExpiresInSeconds?: number | null
  readonly grantedScope?: readonly string[]
  readonly error?: string | null
  /** Defaults to `userId` when omitted. */
  readonly actor?: string
}

/** Inputs for `oauth.unlink`. */
export interface UnlinkContext {
  readonly provider: string
  readonly userId: string
  readonly outcome: string
  readonly revocationAttempted?: boolean
  readonly revocationOutcome?: string | null
  readonly actor?: string
}

/** Inputs for `oauth.token_rotated`. */
export interface TokenRotatedContext {
  readonly provider: string
  readonly userId: string
  readonly previousRefreshToken: string
  readonly newRefreshToken: string
  readonly triggeredBy: string
  readonly actor?: string
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Audit-row payload (the shape the sink receives)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** A canonical OAuth audit row, ready to be handed to a sink. The
 * shape mirrors the positional + keyword args of
 * `backend.audit.log(action=, entity_kind=, entity_id=, before=, after=, actor=)`. */
export interface OAuthAuditPayload {
  readonly action: string
  readonly entityKind: string
  readonly entityId: string
  readonly before: Record<string, unknown> | null
  readonly after: Record<string, unknown>
  readonly actor: string
}

/** Caller-supplied audit sink. Receives a fully-built payload and
 * forwards it to whatever transport the generated app uses
 * (typically `fetch("POST /audit", ...)` against the OmniSight
 * backend, or a structured-log writer for offline contexts).
 *
 * Sinks SHOULD NOT throw on transient transport failures — mirror the
 * Python `audit.log` policy of "log a warning, never raise". The
 * default sink (`noopSink`) discards the payload silently and is
 * intended for tests + the no-audit-needed path. */
export type OAuthAuditSink = (payload: OAuthAuditPayload) => Promise<void>

/** Default sink — discards the payload silently. Use this in tests +
 * any code path where audit is intentionally not wired. */
export const noopSink: OAuthAuditSink = async (_payload) => {
  /* intentionally empty */
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Pure builders — no IO, no knob check
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Build the canonical `oauth.login_init` payload.
 *
 * Pure async (the only async work is hashing `state` for the
 * fingerprint via Web Crypto). Does NOT consult `isEnabled()` —
 * callers that need the knob gate should use `emitLoginInit`. */
export async function buildLoginInitPayload(
  ctx: LoginInitContext,
): Promise<OAuthAuditPayload> {
  const stateFp = await fingerprint(ctx.state)
  return {
    action: EVENT_OAUTH_LOGIN_INIT,
    entityKind: ENTITY_KIND_FLOW,
    entityId: ctx.state,
    before: null,
    after: {
      provider: ctx.provider,
      state_fp: stateFp,
      scope: Array.from(ctx.scope),
      redirect_uri: ctx.redirectUri,
      use_oidc_nonce: Boolean(ctx.useOidcNonce),
      state_ttl_seconds: Math.trunc(ctx.stateTtlSeconds),
    },
    actor: ctx.actor ?? "anonymous",
  }
}

/** Build the canonical `oauth.login_callback` payload.
 *
 * Validates the outcome against `LOGIN_CALLBACK_OUTCOMES` so a typo
 * cannot sneak into the audit chain. */
export async function buildLoginCallbackPayload(
  ctx: LoginCallbackContext,
): Promise<OAuthAuditPayload> {
  if (!LOGIN_CALLBACK_OUTCOMES.has(ctx.outcome)) {
    throw new Error(
      `oauth.login_callback outcome ${JSON.stringify(ctx.outcome)} not in ` +
        `[${[...LOGIN_CALLBACK_OUTCOMES].sort().join(", ")}]`,
    )
  }
  const stateFp = await fingerprint(ctx.state)
  const after: Record<string, unknown> = {
    provider: ctx.provider,
    state_fp: stateFp,
    outcome: ctx.outcome,
    granted_scope: Array.from(ctx.grantedScope ?? []),
    has_refresh_token: Boolean(ctx.hasRefreshToken),
    expires_in_seconds:
      ctx.expiresInSeconds === undefined || ctx.expiresInSeconds === null
        ? null
        : Math.trunc(ctx.expiresInSeconds),
    is_oidc: Boolean(ctx.isOidc),
  }
  if (ctx.error) {
    after.error = String(ctx.error)
  }
  return {
    action: EVENT_OAUTH_LOGIN_CALLBACK,
    entityKind: ENTITY_KIND_FLOW,
    entityId: ctx.state,
    before: { provider: ctx.provider, state_fp: stateFp },
    after,
    actor: ctx.actor ?? "anonymous",
  }
}

/** Build the canonical `oauth.refresh` payload.
 *
 * Validates the outcome against `REFRESH_OUTCOMES`. The complementary
 * `oauth.token_rotated` payload (if rotation actually happened) is
 * built by `buildTokenRotatedPayload` and emitted separately. */
export async function buildRefreshPayload(
  ctx: RefreshContext,
): Promise<OAuthAuditPayload> {
  if (!REFRESH_OUTCOMES.has(ctx.outcome)) {
    throw new Error(
      `oauth.refresh outcome ${JSON.stringify(ctx.outcome)} not in ` +
        `[${[...REFRESH_OUTCOMES].sort().join(", ")}]`,
    )
  }
  const after: Record<string, unknown> = {
    provider: ctx.provider,
    outcome: ctx.outcome,
    new_expires_in_seconds:
      ctx.newExpiresInSeconds === undefined ||
      ctx.newExpiresInSeconds === null
        ? null
        : Math.trunc(ctx.newExpiresInSeconds),
    granted_scope: Array.from(ctx.grantedScope ?? []),
  }
  if (ctx.error) {
    after.error = String(ctx.error)
  }
  return {
    action: EVENT_OAUTH_REFRESH,
    entityKind: ENTITY_KIND_TOKEN,
    entityId: entityIdToken(ctx.provider, ctx.userId),
    before: {
      provider: ctx.provider,
      previous_expires_at:
        ctx.previousExpiresAt === undefined ||
        ctx.previousExpiresAt === null
          ? null
          : Number(ctx.previousExpiresAt),
    },
    after,
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `oauth.unlink` payload.
 *
 * Validates `outcome` and (when `revocationAttempted=true`) the
 * `revocationOutcome`. When revocation was not attempted, the field
 * is forced to `null` so the row's shape stays consistent. */
export async function buildUnlinkPayload(
  ctx: UnlinkContext,
): Promise<OAuthAuditPayload> {
  if (!UNLINK_OUTCOMES.has(ctx.outcome)) {
    throw new Error(
      `oauth.unlink outcome ${JSON.stringify(ctx.outcome)} not in ` +
        `[${[...UNLINK_OUTCOMES].sort().join(", ")}]`,
    )
  }
  let revocationOutcome = ctx.revocationOutcome ?? null
  if (ctx.revocationAttempted) {
    if (revocationOutcome === null || !REVOCATION_OUTCOMES.has(revocationOutcome)) {
      throw new Error(
        "oauth.unlink revocation_outcome must be one of " +
          `[${[...REVOCATION_OUTCOMES].sort().join(", ")}] when revocationAttempted=true`,
      )
    }
  } else {
    revocationOutcome = null
  }
  return {
    action: EVENT_OAUTH_UNLINK,
    entityKind: ENTITY_KIND_TOKEN,
    entityId: entityIdToken(ctx.provider, ctx.userId),
    before: { provider: ctx.provider },
    after: {
      provider: ctx.provider,
      outcome: ctx.outcome,
      revocation_attempted: Boolean(ctx.revocationAttempted),
      revocation_outcome: revocationOutcome,
    },
    actor: ctx.actor ?? ctx.userId,
  }
}

/** Build the canonical `oauth.token_rotated` payload.
 *
 * Stores both old and new refresh_tokens as 12-char SHA-256
 * fingerprints — raw values would be credentials, never written. */
export async function buildTokenRotatedPayload(
  ctx: TokenRotatedContext,
): Promise<OAuthAuditPayload> {
  if (!ROTATION_TRIGGERS.has(ctx.triggeredBy)) {
    throw new Error(
      `oauth.token_rotated triggered_by ${JSON.stringify(ctx.triggeredBy)} not in ` +
        `[${[...ROTATION_TRIGGERS].sort().join(", ")}]`,
    )
  }
  const [priorFp, newFp] = await Promise.all([
    fingerprint(ctx.previousRefreshToken),
    fingerprint(ctx.newRefreshToken),
  ])
  return {
    action: EVENT_OAUTH_TOKEN_ROTATED,
    entityKind: ENTITY_KIND_TOKEN,
    entityId: entityIdToken(ctx.provider, ctx.userId),
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

/** Build the `oauth.login_init` payload, then forward it to the sink
 * (subject to the AS knob).
 *
 * Returns the built payload on emit, or `null` if the AS knob is off. */
export async function emitLoginInit(
  ctx: LoginInitContext,
  sink: OAuthAuditSink = noopSink,
): Promise<OAuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildLoginInitPayload(ctx)
  await sink(payload)
  return payload
}

/** Build + emit `oauth.login_callback`. */
export async function emitLoginCallback(
  ctx: LoginCallbackContext,
  sink: OAuthAuditSink = noopSink,
): Promise<OAuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildLoginCallbackPayload(ctx)
  await sink(payload)
  return payload
}

/** Build + emit `oauth.refresh`. */
export async function emitRefresh(
  ctx: RefreshContext,
  sink: OAuthAuditSink = noopSink,
): Promise<OAuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildRefreshPayload(ctx)
  await sink(payload)
  return payload
}

/** Build + emit `oauth.unlink`. */
export async function emitUnlink(
  ctx: UnlinkContext,
  sink: OAuthAuditSink = noopSink,
): Promise<OAuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildUnlinkPayload(ctx)
  await sink(payload)
  return payload
}

/** Build + emit `oauth.token_rotated`. */
export async function emitTokenRotated(
  ctx: TokenRotatedContext,
  sink: OAuthAuditSink = noopSink,
): Promise<OAuthAuditPayload | null> {
  if (!isEnabled()) return null
  const payload = await buildTokenRotatedPayload(ctx)
  await sink(payload)
  return payload
}
