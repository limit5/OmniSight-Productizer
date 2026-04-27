# `templates/_shared/oauth-client/` — AS.1.2 TS twin

TypeScript twin of `backend/security/oauth_client.py`. Pure-functional
protocol primitives + a fetch-based auto-refresh wrapper, suitable for
emission into the generated-app workspace.

## Cross-twin contract

The Python and TypeScript sides MUST agree on:

1. **Five canonical OAuth audit event strings** — `oauth.login_init` /
   `oauth.login_callback` / `oauth.refresh` / `oauth.unlink` /
   `oauth.token_rotated` (AS.0.8 §5 audit-behaviour matrix).
2. **Four numeric defaults** — `PKCE_VERIFIER_MIN_LENGTH=43`,
   `PKCE_VERIFIER_MAX_LENGTH=128`, `DEFAULT_STATE_TTL_SECONDS=600`,
   `DEFAULT_REFRESH_SKEW_SECONDS=60`.

Drift between the two sides is caught by the AS.1.5 drift-guard test
family in `backend/tests/test_oauth_client.py`:

* `test_oauth_event_strings_parity_python_ts` — joins the 5 event
  strings and asserts SHA-256 equality.
* `test_oauth_defaults_parity_python_ts_*` — extracts each numeric
  literal from `index.ts` and asserts `==` against the Python module.

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
