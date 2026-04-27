# `templates/_shared/oauth-client/` — AS.1.2 + AS.1.3 TS twin

TypeScript twin of `backend/security/oauth_client.py` (AS.1.2) plus
`backend/security/oauth_vendors.py` (AS.1.3). Pure-functional protocol
primitives + a fetch-based auto-refresh wrapper + a frozen catalog of
the 11 shipped OAuth providers, suitable for emission into the
generated-app workspace.

## Files

| File | Side | What it ships |
|---|---|---|
| `index.ts` | AS.1.2 | Protocol primitives (PKCE / state / nonce / token parse / rotation / `AutoRefreshFetch`) |
| `vendors.ts` | AS.1.3 | Frozen `VendorConfig` catalog for the 11 shipped providers + `getVendor` / `buildAuthorizeUrlForVendor` / `beginAuthorizationForVendor` shims |
| `README.md` | doc | This file |

## Cross-twin contract

The Python and TypeScript sides MUST agree on:

1. **Five canonical OAuth audit event strings** (AS.1.2) —
   `oauth.login_init` / `oauth.login_callback` / `oauth.refresh` /
   `oauth.unlink` / `oauth.token_rotated` (AS.0.8 §5 audit-behaviour
   matrix).
2. **Four numeric defaults** (AS.1.2) — `PKCE_VERIFIER_MIN_LENGTH=43`,
   `PKCE_VERIFIER_MAX_LENGTH=128`, `DEFAULT_STATE_TTL_SECONDS=600`,
   `DEFAULT_REFRESH_SKEW_SECONDS=60`.
3. **Eleven vendor catalog entries** (AS.1.3) — every field of every
   `VendorConfig` (provider id / display name / endpoints / scopes /
   OIDC flag / extra params / refresh + PKCE flags) MUST byte-match.

Drift is caught by the AS.1.5 drift-guard tests:

* `backend/tests/test_oauth_client.py`:
  * `test_oauth_event_strings_parity_python_ts` — SHA-256 over joined
    event strings.
  * `test_oauth_defaults_parity_python_ts_*` — each numeric literal
    extracted from `index.ts` and `==`-compared.
* `backend/tests/test_oauth_vendors.py`:
  * `test_vendor_catalog_field_parity_python_ts[<vendor>]` —
    parametrized per-vendor field-by-field check (11 instances).
  * `test_canonical_vendor_id_order_sha256_parity_python_ts` —
    SHA-256 over the catalog order tuple, pins ordering.
  * `test_ts_twin_declares_eleven_export_const_vendors` — sanity
    that every Python vendor has a matching TS `export const`.

If you change one side, you MUST change the other. CI red is the canary.

## Why a TS twin and not just a JSON config?

OmniSight's productizer scaffolds new apps that bring along this lib at
build time. The TS surface lives next to other generated-app primitives
(`password-generator/`, future `token-vault/` / `bot-challenge/` /
`honeypot/` per the AS roadmap) so each generated app can run
client-side OAuth flows without runtime dependence on the OmniSight
backend.

## Public API

```ts
import {
  // Primitives
  generateState,
  generateNonce,
  generatePkce,                     // async — uses Web Crypto SHA-256
  buildAuthorizeUrl,
  beginAuthorization,
  verifyStateAndConsume,
  parseTokenResponse,
  applyRotation,
  needsRefresh,
  // Auto-refresh
  autoRefresh,
  AutoRefreshFetch,
  // Knob hook
  isEnabled,
  // Constants
  PKCE_VERIFIER_MIN_LENGTH,
  PKCE_VERIFIER_MAX_LENGTH,
  DEFAULT_STATE_TTL_SECONDS,
  DEFAULT_REFRESH_SKEW_SECONDS,
  // Audit event strings
  EVENT_OAUTH_LOGIN_INIT,
  EVENT_OAUTH_LOGIN_CALLBACK,
  EVENT_OAUTH_REFRESH,
  EVENT_OAUTH_UNLINK,
  EVENT_OAUTH_TOKEN_ROTATED,
  ALL_OAUTH_EVENTS,
  // Errors
  OAuthClientError,
  StateMismatchError,
  StateExpiredError,
  TokenResponseError,
  TokenRefreshError,
  // Types
  type PkcePair,
  type FlowSession,
  type TokenSet,
  type RefreshFn,
  type RotationHook,
} from "./index"

// Start an authorization-code flow
const { url, flow } = await beginAuthorization({
  provider: "github",
  authorizeEndpoint: "https://github.com/login/oauth/authorize",
  clientId: "Iv1.…",
  redirectUri: "https://app.example/callback",
  scope: ["read:user", "user:email"],
})
sessionStorage.setItem(`oauth:flow:${flow.state}`, JSON.stringify(flow))
window.location.assign(url)

// Callback: verify state then exchange code (caller-provided)
const stored: FlowSession = JSON.parse(
  sessionStorage.getItem(`oauth:flow:${returnedState}`)!,
)
verifyStateAndConsume(stored, returnedState)
sessionStorage.removeItem(`oauth:flow:${returnedState}`)

// Auto-refresh fetch wrapper
const fetcher = new AutoRefreshFetch(token, refreshFn, { onRotated })
const r = await fetcher.fetch("https://api.example/me")
```

## AS.1.3 vendor catalog (`vendors.ts`)

The 11 shipped vendors as frozen `VendorConfig` objects:

| Slug | Display name | OIDC | Refresh | PKCE | Notes |
|---|---|:-:|:-:|:-:|---|
| `github` | GitHub | — | yes | yes | Modern GitHub Apps issue refresh tokens |
| `google` | Google | yes | yes | yes | Needs `access_type=offline` + `prompt=consent` (catalog default) |
| `microsoft` | Microsoft | yes | yes | yes | `offline_access` scope drives refresh (catalog default) |
| `apple` | Apple | yes | yes | yes | `response_mode=form_post` required for `name` scope |
| `gitlab` | GitLab | yes | yes | yes | OIDC active when `openid` is in scope |
| `bitbucket` | Bitbucket | — | yes | yes | Cloud only — self-hosted overrides at use site |
| `slack` | Slack | — | yes | yes | "Sign in with Slack" OIDC is a different endpoint |
| `notion` | Notion | — | — | — | Long-lived token, no refresh, no scopes, no PKCE |
| `salesforce` | Salesforce | yes | yes | yes | Caller adds `refresh_token` scope for refresh tokens |
| `hubspot` | HubSpot | — | yes | — | Authorize on `app.hubspot.com`, token on `api.hubapi.com` |
| `discord` | Discord | — | yes | yes | RFC 7009 revocation supported |

```ts
import { GITHUB, getVendor, beginAuthorizationForVendor } from "./vendors"

// Direct constant
const { url, flow } = await beginAuthorizationForVendor(GITHUB, {
  clientId: "Iv1.…",
  redirectUri: "https://app.example/callback",
})

// Or look up by slug (e.g. from a URL path segment)
const vendor = getVendor(slugFromUrl)  // throws VendorNotFoundError if unknown
const { url, flow } = await beginAuthorizationForVendor(vendor, {
  clientId,
  redirectUri,
  scope: ["openid", "email"],         // override catalog default
  extraAuthorizeParams: { hd: "tenant.example" },  // merged on top of catalog's
})
```

## Randomness + crypto source

Uses `globalThis.crypto.getRandomValues` (Web Crypto API) for state /
nonce / PKCE verifier, and `globalThis.crypto.subtle.digest("SHA-256",…)`
for the PKCE challenge derivation. Throws if no Web Crypto is available
(e.g. legacy server runtime without `globalThis.crypto`).

`generatePkce()` is async because Web Crypto's `subtle.digest` is async.
All other primitives are synchronous.

## AS.0.8 single-knob hook

`isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend**
twin of the Python `settings.as_enabled` — deliberately decoupled per
AS.0.8 §2.5 so the frontend can be flipped independently from the
backend). Default `true`. Resolution order:

1. `globalThis.OMNISIGHT_AS_FRONTEND_ENABLED` (boolean or string)
2. `process.env.OMNISIGHT_AS_FRONTEND_ENABLED` (string)
3. Default `true`

The pure helpers (`generatePkce` / `generateState` / `parseTokenResponse`
/ `applyRotation`) deliberately do NOT consult the knob — turning AS off
must not break a script that parses an already-stored token (matches
the Python lib invariant per AS.0.8 §3.1 row "AS.1 OAuth client").

## Shape parity vs the Python side

| Python | TypeScript |
|---|---|
| `PkcePair.code_verifier` | `PkcePair.codeVerifier` |
| `FlowSession.created_at` | `FlowSession.createdAt` |
| `TokenSet.access_token` | `TokenSet.accessToken` |
| `AutoRefreshAuth(httpx.Auth)` | `AutoRefreshFetch` (fetch wrapper) |
| `apply_rotation()` returns `(new, rotated)` | `applyRotation()` returns `[new, rotated]` |

Casing is the canonical idiom of each language; the audit event strings
and numeric defaults are the **byte-identical** contract surface.
