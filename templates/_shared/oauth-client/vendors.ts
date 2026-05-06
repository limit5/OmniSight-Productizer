/**
 * AS.1.3 — OAuth vendor catalog (TypeScript twin).
 *
 * Behaviourally identical mirror of `backend/security/oauth_vendors.py`.
 * Per-vendor configuration for the 11 OAuth providers OmniSight ships
 * out-of-the-box: GitHub, Google, Microsoft, Apple, GitLab, Bitbucket,
 * Slack, Notion, Salesforce, HubSpot, Discord.
 *
 * Cross-twin contract (enforced by AS.1.5 drift guard)
 * ────────────────────────────────────────────────────
 * For every vendor the following fields MUST be byte-identical
 * between the two twins:
 *
 *   * `providerId`              ↔ `provider_id`
 *   * `displayName`             ↔ `display_name`
 *   * `authorizeEndpoint`       ↔ `authorize_endpoint`
 *   * `tokenEndpoint`           ↔ `token_endpoint`
 *   * `userinfoEndpoint`        ↔ `userinfo_endpoint`
 *   * `revocationEndpoint`      ↔ `revocation_endpoint`
 *   * `defaultScopes`           ↔ `default_scopes`
 *   * `isOidc`                  ↔ `is_oidc`
 *   * `extraAuthorizeParams`    ↔ `extra_authorize_params`
 *   * `supportsRefreshToken`    ↔ `supports_refresh_token`
 *   * `supportsPkce`            ↔ `supports_pkce`
 *
 * The catalog **order** also matters — the AS.1.5 drift guard hashes
 * the canonical order tuple (`ALL_VENDOR_IDS`) and asserts
 * byte-equality against the Python side.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * Every vendor entry is `Object.freeze`-d at construction time.
 *     Because `Object.freeze` is shallow, the inner arrays
 *     (`defaultScopes`, `extraAuthorizeParams`) are **also** frozen
 *     individually — the catalog-builder helper does both passes.
 *   * `ALL_VENDORS`, `ALL_VENDOR_IDS`, and the `VENDORS` map are
 *     frozen / Object.freeze-d at module-load. No mutable
 *     module-level state.
 *   * Importing this module is free of side effects (no fetch, no
 *     localStorage IO, no env reads at top level).
 *
 * AS.0.8 single-knob behaviour
 * ────────────────────────────
 * The catalog itself does NOT consult `isEnabled()` — same invariant
 * as the lib's pure helpers. Turning the AS knob off must not break a
 * client-side script that needs `GITHUB.tokenEndpoint` to revoke a
 * stored token. The HTTP endpoint surface is where the 503 lives.
 */

import {
  buildAuthorizeUrl,
  beginAuthorization,
  DEFAULT_STATE_TTL_SECONDS,
  type FlowSession,
} from "./index"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Exceptions
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Lookup of an unknown `providerId` against the catalog. */
export class VendorNotFoundError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "VendorNotFoundError"
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  VendorConfig — frozen interface
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Protocol-level configuration for a single OAuth provider.
 *
 * All fields are readonly + the runtime instance is `Object.freeze`-d
 * (shallowly, with inner arrays frozen separately by the
 * catalog-builder helper). camelCase is the TS idiom; the Python
 * twin uses snake_case for the same field set.
 */
export interface VendorConfig {
  readonly providerId: string
  readonly displayName: string
  readonly authorizeEndpoint: string
  readonly tokenEndpoint: string
  readonly userinfoEndpoint: string | null
  readonly revocationEndpoint: string | null
  readonly defaultScopes: readonly string[]
  readonly isOidc: boolean
  readonly extraAuthorizeParams: ReadonlyArray<readonly [string, string]>
  readonly supportsRefreshToken: boolean
  readonly supportsPkce: boolean
}

/** Build a `VendorConfig` with all reachable arrays + the entry
 * itself frozen. Avoids accidental mutation by callers + matches
 * the Python frozen-dataclass guarantee. */
function makeVendor(v: VendorConfig): VendorConfig {
  // Freeze the inner arrays first so the outer freeze can't be
  // bypassed via `entry.defaultScopes.push(...)`.
  Object.freeze(v.defaultScopes)
  for (const pair of v.extraAuthorizeParams) Object.freeze(pair)
  Object.freeze(v.extraAuthorizeParams)
  return Object.freeze(v)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  The 11 shipped vendors
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//
// Each entry's URL set / scope set / flag set MUST be byte-identical
// to the Python side `oauth_vendors.py`. AS.1.5 drift-guard hashes
// every entry per-field; CI red is the canary.

/** GitHub OAuth Apps + GitHub Apps share the authorize/token URLs.
 * Modern GitHub Apps with "expiring user-to-server tokens" issue
 * refresh_tokens; classic OAuth Apps don't — we flag refresh as
 * supported (the recommended modern integration shape). */
export const GITHUB: VendorConfig = makeVendor({
  providerId: "github",
  displayName: "GitHub",
  authorizeEndpoint: "https://github.com/login/oauth/authorize",
  tokenEndpoint: "https://github.com/login/oauth/access_token",
  userinfoEndpoint: "https://api.github.com/user",
  revocationEndpoint: null, // application/{client_id}/token DELETE — non-RFC-7009 shape
  defaultScopes: ["read:user", "user:email"],
  isOidc: false,
  extraAuthorizeParams: [["allow_signup", "true"]],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** Google OIDC. `access_type=offline` + `prompt=consent` are
 * required to receive a refresh_token. PKCE is now mandatory for
 * new clients (was optional pre-2022). */
export const GOOGLE: VendorConfig = makeVendor({
  providerId: "google",
  displayName: "Google",
  authorizeEndpoint: "https://accounts.google.com/o/oauth2/v2/auth",
  tokenEndpoint: "https://oauth2.googleapis.com/token",
  userinfoEndpoint: "https://openidconnect.googleapis.com/v1/userinfo",
  revocationEndpoint: "https://oauth2.googleapis.com/revoke",
  defaultScopes: ["openid", "email", "profile"],
  isOidc: true,
  extraAuthorizeParams: [
    ["access_type", "offline"],
    ["prompt", "consent"],
  ],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** Microsoft Identity Platform / Entra ID v2.0. The "common" tenant
 * accepts both work/school + personal accounts. `offline_access`
 * **scope** drives refresh_token issuance (different idiom from
 * Google's `access_type=offline` query param). */
export const MICROSOFT: VendorConfig = makeVendor({
  providerId: "microsoft",
  displayName: "Microsoft",
  authorizeEndpoint:
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
  tokenEndpoint:
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
  userinfoEndpoint: "https://graph.microsoft.com/oidc/userinfo",
  revocationEndpoint: null, // MS does not expose a public RFC-7009 endpoint
  defaultScopes: ["openid", "email", "profile", "offline_access"],
  isOidc: true,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** Sign in with Apple. `response_mode=form_post` is **required**
 * whenever the `name` scope is requested — Apple posts the user's
 * name back via form body (only chance to capture it is on the very
 * first auth, never again). The caller's callback handler MUST
 * accept POST + parse the form body. */
export const APPLE: VendorConfig = makeVendor({
  providerId: "apple",
  displayName: "Apple",
  authorizeEndpoint: "https://appleid.apple.com/auth/authorize",
  tokenEndpoint: "https://appleid.apple.com/auth/token",
  userinfoEndpoint: null, // No userinfo endpoint — id_token claims are the source of truth
  revocationEndpoint: "https://appleid.apple.com/auth/revoke",
  defaultScopes: ["name", "email"],
  isOidc: true,
  extraAuthorizeParams: [["response_mode", "form_post"]],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** GitLab.com SaaS. Self-hosted instances override at use site
 * (`https://gitlab.example.com/oauth/...`). FX2.D9.7.6 uses the
 * OIDC userinfo flow, so default scopes include `openid`. */
export const GITLAB: VendorConfig = makeVendor({
  providerId: "gitlab",
  displayName: "GitLab",
  authorizeEndpoint: "https://gitlab.com/oauth/authorize",
  tokenEndpoint: "https://gitlab.com/oauth/token",
  userinfoEndpoint: "https://gitlab.com/oauth/userinfo",
  revocationEndpoint: "https://gitlab.com/oauth/revoke",
  defaultScopes: ["read_user", "openid", "email", "profile"],
  isOidc: true,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** Bitbucket Cloud. Self-hosted Bitbucket Server / Data Center
 * uses different endpoints; operators override at use site. */
export const BITBUCKET: VendorConfig = makeVendor({
  providerId: "bitbucket",
  displayName: "Bitbucket",
  authorizeEndpoint: "https://bitbucket.org/site/oauth2/authorize",
  tokenEndpoint: "https://bitbucket.org/site/oauth2/access_token",
  userinfoEndpoint: "https://api.bitbucket.org/2.0/user",
  revocationEndpoint: null, // Bitbucket Cloud has no public revocation endpoint
  defaultScopes: ["account", "email"],
  isOidc: false,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** Sign in with Slack OIDC. Slack's userInfo endpoint is the
 * vendor-shaped openid.connect.userInfo method. */
export const SLACK: VendorConfig = makeVendor({
  providerId: "slack",
  displayName: "Slack",
  authorizeEndpoint: "https://slack.com/openid/connect/authorize",
  tokenEndpoint: "https://slack.com/api/openid.connect.token",
  userinfoEndpoint: "https://slack.com/api/openid.connect.userInfo",
  revocationEndpoint: "https://slack.com/api/auth.revoke",
  defaultScopes: ["openid", "email", "profile"],
  isOidc: true,
  extraAuthorizeParams: [],
  supportsRefreshToken: false,
  supportsPkce: true,
})

/** Notion's OAuth is workspace-scoped not user-scoped. Tokens don't
 * expire, no refresh grant, no scopes (workspaces are the
 * permission unit), no PKCE in vendor docs as of 2026-04. */
export const NOTION: VendorConfig = makeVendor({
  providerId: "notion",
  displayName: "Notion",
  authorizeEndpoint: "https://api.notion.com/v1/oauth/authorize",
  tokenEndpoint: "https://api.notion.com/v1/oauth/token",
  userinfoEndpoint: null,
  revocationEndpoint: null,
  defaultScopes: [],
  isOidc: false,
  extraAuthorizeParams: [["owner", "user"]],
  supportsRefreshToken: false,
  supportsPkce: false,
})

/** Salesforce Identity. `login.salesforce.com` is production
 * multi-tenant; sandboxes use `test.salesforce.com`. Caller must
 * add `refresh_token` scope to receive a refresh_token. */
export const SALESFORCE: VendorConfig = makeVendor({
  providerId: "salesforce",
  displayName: "Salesforce",
  authorizeEndpoint:
    "https://login.salesforce.com/services/oauth2/authorize",
  tokenEndpoint: "https://login.salesforce.com/services/oauth2/token",
  userinfoEndpoint: "https://login.salesforce.com/services/oauth2/userinfo",
  revocationEndpoint: "https://login.salesforce.com/services/oauth2/revoke",
  defaultScopes: ["openid", "email", "profile"],
  isOidc: true,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: true,
})

/** HubSpot OAuth 2.0. Authorize on app.hubspot.com, token on
 * api.hubapi.com (vendor-canonical asymmetry). PKCE not documented;
 * conservatively flagged false. Revocation is non-RFC-7009 shape
 * (DELETE /oauth/v1/refresh-tokens/{token}) — handle in AS.2.5. */
export const HUBSPOT: VendorConfig = makeVendor({
  providerId: "hubspot",
  displayName: "HubSpot",
  authorizeEndpoint: "https://app.hubspot.com/oauth/authorize",
  tokenEndpoint: "https://api.hubapi.com/oauth/v1/token",
  userinfoEndpoint: null, // No canonical userinfo
  revocationEndpoint: null, // DELETE /oauth/v1/refresh-tokens/{token} — non-RFC, handle in AS.2.5
  defaultScopes: ["oauth"],
  isOidc: false,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: false,
})

/** Discord OAuth 2.0. `identify` returns user id + username + avatar,
 * `email` adds verified email. RFC 7009 revocation. */
export const DISCORD: VendorConfig = makeVendor({
  providerId: "discord",
  displayName: "Discord",
  authorizeEndpoint: "https://discord.com/oauth2/authorize",
  tokenEndpoint: "https://discord.com/api/oauth2/token",
  userinfoEndpoint: "https://discord.com/api/users/@me",
  revocationEndpoint: "https://discord.com/api/oauth2/token/revoke",
  defaultScopes: ["identify", "email"],
  isOidc: false,
  extraAuthorizeParams: [],
  supportsRefreshToken: true,
  supportsPkce: true,
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Catalog — ordered tuple + read-only mapping
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//
// Order matters — AS.1.5 SHA-256 drift guard hashes the joined
// providerId sequence and asserts byte-identity against Python.

/** All 11 vendors in canonical declaration order. Frozen. */
export const ALL_VENDORS: readonly VendorConfig[] = Object.freeze([
  GITHUB,
  GOOGLE,
  MICROSOFT,
  APPLE,
  GITLAB,
  BITBUCKET,
  SLACK,
  NOTION,
  SALESFORCE,
  HUBSPOT,
  DISCORD,
])

/** Provider IDs in canonical order. Frozen. */
export const ALL_VENDOR_IDS: readonly string[] = Object.freeze(
  ALL_VENDORS.map((v) => v.providerId),
)

/** Read-only lookup: providerId → VendorConfig. Frozen. */
export const VENDORS: Readonly<Record<string, VendorConfig>> = Object.freeze(
  Object.fromEntries(ALL_VENDORS.map((v) => [v.providerId, v])),
)

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Lookup + integration helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Return the `VendorConfig` for *providerId*.
 *
 * Throws `VendorNotFoundError` if the slug is not in the catalog.
 * Caller-provided slugs from URL paths / form posts MUST be
 * validated against this lookup before driving an authorize
 * redirect — otherwise an attacker-controlled slug could route the
 * flow at an unintended vendor (open-redirect family). */
export function getVendor(providerId: string): VendorConfig {
  const v = VENDORS[providerId]
  if (!v) {
    throw new VendorNotFoundError(
      `unknown OAuth provider ${JSON.stringify(providerId)}; ` +
        `known: ${ALL_VENDOR_IDS.join(", ")}`,
    )
  }
  return v
}

export interface BuildAuthorizeUrlForVendorOptions {
  clientId: string
  redirectUri: string
  state: string
  codeChallenge: string
  scope?: readonly string[]
  nonce?: string | null
  extraParams?: Readonly<Record<string, string>>
}

/** Pre-fill `buildAuthorizeUrl` from the catalog entry —
 * `authorizeEndpoint` and the vendor's `extraAuthorizeParams` are
 * sourced from *vendor*; the caller only supplies what's flow-
 * specific.
 *
 * `scope` defaults to `vendor.defaultScopes` if omitted.
 * `extraParams` is **merged** onto `vendor.extraAuthorizeParams`
 * (caller keys override catalog keys). Collisions with OAuth core
 * keys are still rejected by the underlying core lib.
 */
export function buildAuthorizeUrlForVendor(
  vendor: VendorConfig,
  opts: BuildAuthorizeUrlForVendorOptions,
): string {
  const merged: Record<string, string> = {}
  for (const [k, v] of vendor.extraAuthorizeParams) merged[k] = v
  if (opts.extraParams) {
    for (const [k, v] of Object.entries(opts.extraParams)) merged[k] = v
  }
  return buildAuthorizeUrl({
    authorizeEndpoint: vendor.authorizeEndpoint,
    clientId: opts.clientId,
    redirectUri: opts.redirectUri,
    scope: opts.scope ?? vendor.defaultScopes,
    state: opts.state,
    codeChallenge: opts.codeChallenge,
    nonce: opts.nonce ?? null,
    extraParams: Object.keys(merged).length > 0 ? merged : undefined,
  })
}

export interface BeginAuthorizationForVendorOptions {
  clientId: string
  redirectUri: string
  scope?: readonly string[]
  extra?: Readonly<Record<string, string>>
  extraAuthorizeParams?: Readonly<Record<string, string>>
  stateTtlSeconds?: number
  now?: number
}

/** Catalog-aware `beginAuthorization` shim — pulls
 * `authorizeEndpoint`, `defaultScopes`, `isOidc` (drives
 * `useOidcNonce`), and the vendor's `extraAuthorizeParams` from
 * *vendor*. Returns the same `{ url, flow }` shape as the underlying
 * lib.
 *
 * `extraAuthorizeParams` — caller overrides on top of the catalog's
 * static params. Useful for runtime knobs (per-tenant `hd=...` for
 * Google workspace restriction, `login_hint` for prefilled email).
 */
export async function beginAuthorizationForVendor(
  vendor: VendorConfig,
  opts: BeginAuthorizationForVendorOptions,
): Promise<{ url: string; flow: FlowSession }> {
  const mergedExtraParams: Record<string, string> = {}
  for (const [k, v] of vendor.extraAuthorizeParams) {
    mergedExtraParams[k] = v
  }
  if (opts.extraAuthorizeParams) {
    for (const [k, v] of Object.entries(opts.extraAuthorizeParams)) {
      mergedExtraParams[k] = v
    }
  }
  return beginAuthorization({
    provider: vendor.providerId,
    authorizeEndpoint: vendor.authorizeEndpoint,
    clientId: opts.clientId,
    redirectUri: opts.redirectUri,
    scope: opts.scope ?? vendor.defaultScopes,
    useOidcNonce: vendor.isOidc,
    stateTtlSeconds: opts.stateTtlSeconds ?? DEFAULT_STATE_TTL_SECONDS,
    extraAuthorizeParams:
      Object.keys(mergedExtraParams).length > 0
        ? mergedExtraParams
        : undefined,
    extra: opts.extra,
    now: opts.now,
  })
}
