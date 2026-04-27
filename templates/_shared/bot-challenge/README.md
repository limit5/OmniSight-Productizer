# `templates/_shared/bot-challenge/` — AS.3.2 TS twin

TypeScript twin of `backend/security/bot_challenge.py`. The provider-
agnostic bot-challenge interface (Turnstile / reCAPTCHA v2 / reCAPTCHA
v3 / hCaptcha) emitted into every generated-app workspace so scaffolded
apps can wire their own forms onto the same `verify()` entry point —
regardless of which captcha vendor sits behind the request.

## Files

| File         | What it ships                                                                                                                                                                                                                                                  |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `index.ts`   | `Provider` enum, 19 `EVENT_BOT_CHALLENGE_*` audit-event strings, 15 `OUTCOME_*` literals, `BotChallengeResult` / `ProviderResponse` / `BypassContext` / `VerifyContext` types, `verifyProvider` + `verify` orchestrators, `evaluateBypass`, `classifyOutcome`, `pickProvider`, `passthrough`, `isEnabled`, `eventForOutcome`, `fingerprint`, plus the three typed errors. |
| `README.md`  | This file.                                                                                                                                                                                                                                                     |

## Cross-twin contract

Eight surfaces stay byte-equal across the Python and TS twin. Drift is
caught by `backend/tests/test_bot_challenge_shape_drift.py` (AS.1.5 /
AS.2.3-style cross-twin parity test, regex-extracted static pins +
Node-spawned behavioural parity matrix):

1. **Provider enum values** — `"turnstile"`, `"recaptcha_v2"`,
   `"recaptcha_v3"`, `"hcaptcha"`. Used in audit metadata + config envs
   on both sides.
2. **Siteverify URLs** — the four vendor `/siteverify` endpoints.
3. **19 audit event strings** — 8 verify-outcome (`pass`,
   `unverified_lowscore`, `unverified_servererr`, `blocked_lowscore`,
   `jsfail_fallback_recaptcha`, `jsfail_fallback_hcaptcha`,
   `jsfail_honeypot_pass`, `jsfail_honeypot_fail`) + 7 bypass
   (`bypass_apikey`, `bypass_webhook`, `bypass_chatops`,
   `bypass_bootstrap`, `bypass_probe`, `bypass_ip_allowlist`,
   `bypass_test_token`) + 4 phase-advance / revert
   (`phase_advance_p1_to_p2`, `phase_advance_p2_to_p3`,
   `phase_revert_p3_to_p2`, `phase_revert_p2_to_p1`).
4. **15 outcome literals** — drives the `auditEvent` lookup table; the
   `eventForOutcome` mapping must be byte-equal across the two sides.
5. **Numeric defaults** — `DEFAULT_SCORE_THRESHOLD = 0.5` (AS.0.5 §2.4
   + design doc §3.5), `DEFAULT_VERIFY_TIMEOUT_SECONDS = 3.0`,
   `TEST_TOKEN_HEADER = "X-OmniSight-Test-Token"`.
6. **Phase-aware classifier behaviour** — same 3-phase fail-open /
   fail-closed matrix; same provider-side score calibration (Turnstile
   / reCAPTCHA v3 → vendor float, v2 / hCaptcha → 1.0 on success / 0.0
   on failure).
7. **Bypass axis precedence** — A (api_key) → C (test_token) → B
   (ip_allowlist) → D (path) per AS.0.6 §4.
8. **Three typed errors** — `BotChallengeError` (base),
   `ProviderConfigError`, `InvalidProviderError`.

If you change one side, you MUST change the other. CI red is the canary.

## Why a TS twin and not just a thin client?

Two emission shapes for the generated app:

* **Server-side TS** (Node SSR / edge worker / `next/server`) —
  `verifyProvider` + `verify` are called with the secret loaded from
  `process.env`, the same way the Python lib reads
  `OMNISIGHT_TURNSTILE_SECRET` etc. This is the typical generated-app
  shape — the secret never leaves the server side, just like in
  OmniSight's own backend.
* **Pure-browser TS** — the browser captures the widget token then
  POSTs it to its own backend `/api/v1/bot-challenge/verify` endpoint,
  which calls `verifyProvider` server-side. This module supplies the
  contract surface (enums, errors, types) the fetch-handler can use to
  type its request / response.

The two shapes share the same `BotChallengeResult` envelope so a
frontend caller reads `result.allow` to decide 4xx vs continue
regardless of which side actually called the vendor.

## Public API

```ts
import {
  // contract constants
  Provider,
  SITEVERIFY_URLS,
  DEFAULT_SCORE_THRESHOLD,
  DEFAULT_VERIFY_TIMEOUT_SECONDS,
  TEST_TOKEN_HEADER,
  ALL_BOT_CHALLENGE_EVENTS,
  ALL_OUTCOMES,
  EVENT_BOT_CHALLENGE_PASS,
  /* ... 18 more EVENT_BOT_CHALLENGE_* strings ... */
  OUTCOME_PASS,
  /* ... 14 more OUTCOME_* literals ... */
  BYPASS_PATH_PREFIXES,
  BYPASS_CALLER_KINDS,
  // pure functions
  isEnabled,
  passthrough,
  eventForOutcome,
  evaluateBypass,
  classifyOutcome,
  secretEnvFor,
  pickProvider,
  fingerprint,
  // orchestrators
  verifyProvider,
  verify,
  // types
  type ProviderResponse,
  type BypassReason,
  type BypassContext,
  type BotChallengeResult,
  type VerifyContext,
  type HttpFetch,
  // errors
  BotChallengeError,
  ProviderConfigError,
  InvalidProviderError,
} from "./index"

// Server-side flow (Node SSR / edge handler):
const result = await verify({
  provider: Provider.TURNSTILE,
  token: req.body["cf-turnstile-response"],
  secret: process.env.OMNISIGHT_TURNSTILE_SECRET!,
  phase: 2,
  widgetAction: "login",
  expectedAction: "login",
  remoteIp: req.headers["cf-connecting-ip"] as string,
})
if (!result.allow) {
  return new Response("bot challenge failed", { status: 429 })
}
// Otherwise emit `result.auditEvent` + `result.auditMetadata` to your
// audit pipeline and continue with the underlying action.
```

## AS.0.8 single-knob hook

`isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend**
twin of the Python `settings.as_enabled` — deliberately decoupled per
AS.0.8 §2.5). Default `true`. `verify()` short-circuits with
`passthrough()` when knob-off, matching the Python lib's AS.0.5 §4
precedence axis #2. The pure helpers (`evaluateBypass`,
`classifyOutcome`) deliberately do NOT consult the knob — turning AS
off must not break a script that re-classifies a stored response.

## Module-global state audit (per implement_phase_step.md SOP §1)

* No module-level mutable state — only frozen `Set`s, frozen arrays,
  frozen object literals, classes, and pure functions.
* The four siteverify URLs live in an `Object.freeze`d map; the bypass
  path prefixes / caller-kind sets live in frozen `Set` instances.
* No env reads at module top-level — `isEnabled()` reads
  `OMNISIGHT_AS_FRONTEND_ENABLED` lazily on every call. Each browser
  tab / Node worker derives the same value from the same env source —
  answer #1 of SOP §1 audit (deterministic-by-construction across
  workers).
* Importing the module is free of side effects.

## AS.3.3 provider-selection heuristic

`pickProvider` consumes three optional inputs (`override` / `region` /
`ecosystemHints`) and returns one of the four `Provider` values per the
following precedence (highest first):

1. `override` — caller-supplied force value (e.g. per-tenant admin pin
   loaded from `tenants.auth_features.captcha_provider`). Wins
   unconditionally; lets ops override the heuristic without modifying
   caller code.
2. **GDPR strict region** (`region` ∈ `GDPR_STRICT_REGIONS`) →
   `Provider.HCAPTCHA`. Privacy-first vendor; sidesteps the Cloudflare
   / Google cross-border data-transfer paperwork most EU/EEA/UK/CH
   operators need to file.
3. **Google ecosystem hint** (`"google"` ∈ `ecosystemHints`) →
   `Provider.RECAPTCHA_V3`. UX continuity: principal already accepted
   Google's data-collection terms via OAuth, so routing them through
   reCAPTCHA preserves the same vendor relationship.
4. **Default** → `default` (defaults to `Provider.TURNSTILE`).

`GDPR_STRICT_REGIONS` covers EU 27 + Iceland + Liechtenstein + Norway +
UK + Switzerland (32 ISO 3166-1 alpha-2 codes). The list is mirrored
byte-for-byte by the Python twin and locked by a cross-twin drift guard
(`backend/tests/test_bot_challenge_shape_drift.py::test_ts_gdpr_strict_regions_match_python`).
Region matching is case-insensitive and whitespace-tolerant.

```typescript
import { pickProvider, Provider } from "./index"

// Default — no hints → Turnstile.
pickProvider() === Provider.TURNSTILE

// EU strict-region request → hCaptcha.
pickProvider({ region: "DE" }) === Provider.HCAPTCHA

// Existing Google OAuth user (vendor continuity) → reCAPTCHA v3.
pickProvider({ ecosystemHints: ["google"] }) === Provider.RECAPTCHA_V3

// Region wins over ecosystem (privacy > UX continuity).
pickProvider({ region: "FR", ecosystemHints: ["google"] }) === Provider.HCAPTCHA

// Per-tenant operator pin overrides everything.
pickProvider({
  override: Provider.TURNSTILE,
  region: "DE",
}) === Provider.TURNSTILE
```

## Out of scope (deferred to follow-up rows in the same epic)

* AS.3.4 — Server-side score-verification + `score < 0.5` reject logic.
  The classifier here already returns `OUTCOME_BLOCKED_LOWSCORE`
  (`allow=false`) on Phase 3 + low score; AS.3.4 wires the audit /
  metric emitters around it.
* AS.3.5 — Fallback chain (primary → secondary → tertiary on jsfail).
  This row exposes the primitives — `verifyProvider` per provider —
  but the orchestrator that chains them on widget JS load failure is
  AS.3.5.

## Shape parity vs the Python side

| Python (`backend.security.bot_challenge`)   | TypeScript (`templates/_shared/bot-challenge/index.ts`) |
| ------------------------------------------- | ------------------------------------------------------- |
| `Provider` (str enum)                       | `Provider` (TS enum, same string values)                |
| `SITEVERIFY_URLS: MappingProxyType`         | `SITEVERIFY_URLS: Object.freeze`                        |
| `secret_env_for(provider)`                  | `secretEnvFor(provider)`                                |
| `DEFAULT_SCORE_THRESHOLD = 0.5`             | `DEFAULT_SCORE_THRESHOLD = 0.5`                         |
| `DEFAULT_VERIFY_TIMEOUT_SECONDS = 3.0`      | `DEFAULT_VERIFY_TIMEOUT_SECONDS = 3.0`                  |
| `TEST_TOKEN_HEADER`                         | `TEST_TOKEN_HEADER`                                     |
| `EVENT_BOT_CHALLENGE_*` (19 strings)        | same names, same string values                          |
| `OUTCOME_*` (15 literals)                   | same names, same string values                          |
| `event_for_outcome(outcome)`                | `eventForOutcome(outcome)`                              |
| `evaluate_bypass(ctx)`                      | `evaluateBypass(ctx)`                                   |
| `classify_outcome(resp, ...)`               | `classifyOutcome(resp, opts)`                           |
| `verify_provider(...)`                      | `verifyProvider(opts)`                                  |
| `verify(ctx)`                               | `verify(ctx, opts)`                                     |
| `pick_provider(default=, region=, ecosystem_hints=, override=)` | `pickProvider({ default, region, ecosystemHints, override })` |
| `GDPR_STRICT_REGIONS: frozenset[str]` (32 codes) | `GDPR_STRICT_REGIONS: ReadonlySet<string>` (32 codes) |
| `ECOSYSTEM_HINT_GOOGLE = "google"`          | `ECOSYSTEM_HINT_GOOGLE = "google"`                      |
| `is_gdpr_strict_region(region)`             | `isGdprStrictRegion(region)`                            |
| `passthrough(reason=...)`                   | `passthrough(reason)`                                   |
| `is_enabled()`                              | `isEnabled()`                                           |
| `BotChallengeError` (base)                  | `BotChallengeError` (base)                              |
| `ProviderConfigError`                       | `ProviderConfigError`                                   |
| `InvalidProviderError`                      | `InvalidProviderError`                                  |

Casing follows each language's idiom; the **string values** of the
`Provider` enum, the 19 audit event names, and the 15 outcome literals
are the byte-identical contract surface.
