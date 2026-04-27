# `templates/_shared/honeypot/` — AS.4.1 TS twin

TypeScript twin of `backend/security/honeypot.py`.  Hidden-form-field
generator + bot detector emitted into every generated-app workspace so
scaffolded apps can render a honeypot trap that the OmniSight backend
recognises out of the box.  Pairs with AS.3 (captcha) as a 2-layer
defence: a bot that solves a captcha but auto-fills every form input
still gets caught here, and a bot that pattern-skips honeypots can
still get caught by the captcha — the layers are independent.

## Files

| File        | What it ships                                                                                                                                                                                                                                                            |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `index.ts`  | `FORM_PREFIXES` (4 form paths → 2-letter prefix), `RARE_WORD_POOL` (12 words), `OS_HONEYPOT_CLASS`, `HONEYPOT_HIDE_CSS`, `HONEYPOT_INPUT_ATTRS` (5 + 2 ignore attrs), 3 `EVENT_BOT_CHALLENGE_HONEYPOT_*`, 4 `OUTCOME_HONEYPOT_*`, `HoneypotResult` type, `HoneypotError` + `HoneypotRejected` errors, `honeypotFieldName`, `expectedFieldNames`, `currentEpoch`, `validateHoneypot`, `validateAndEnforce`, `shouldReject`, `eventForHoneypotOutcome`, `isEnabled`, `supportedFormPaths`. |
| `README.md` | This file.                                                                                                                                                                                                                                                                |

## Cross-twin contract

Ten surfaces stay byte-equal across the Python and TS twin.  Drift is
caught by `backend/tests/test_honeypot_shape_drift.py` (regex-extracted
static pins + Node-spawned behavioural parity matrix):

1. **12-word rare pool** — selected per AS.0.7 §2.1: outside the WHATWG
   `autocomplete` value spec, no collision with OmniSight's existing form
   inputs (grep-verified at design-freeze time), plausible enough that a
   naive form-fill bot will populate them.  Frozen tuple — adding /
   removing words requires a new design plan PR.
2. **4 form-prefix entries** — `lg_` / `sg_` / `pr_` / `ct_` for the
   four OmniSight self-form paths (`login` / `signup` /
   `password-reset` / `contact`).  AS.0.7 §4.1 invariant.
3. **CSS class** — `"os-honeypot-field"`.
4. **Hide CSS body** — off-screen positioning only
   (`position:absolute;left:-9999px;...`); `display:none` /
   `visibility:hidden` are forbidden because some headless browsers /
   form-fill bots skip them and defeat the trap.
5. **5 + 2 input attributes** — `tabindex="-1"` (keyboard skip) +
   `autocomplete="off"` (Chrome / Safari autofill skip) +
   `data-1p-ignore="true"` / `data-lpignore="true"` /
   `data-bwignore="true"` (1Password / LastPass / Bitwarden ignore) +
   `aria-hidden="true"` + `aria-label="Do not fill"` (screen-reader
   skip).  AS.0.7 §2.6 invariant — every honeypot input must render
   with all of these.
6. **3 audit event strings** — `bot_challenge.honeypot_pass` /
   `bot_challenge.honeypot_fail` / `bot_challenge.honeypot_form_drift`.
   Distinct from the AS.0.5 §3 `jsfail_honeypot_*` events (those are
   emitted by the AS.3 captcha-fallback chain when the widget JS fails
   and the chain terminates at honeypot; the events here are emitted
   by the *active* honeypot path that runs alongside captcha).
7. **4 outcome literals** — `honeypot_pass` / `honeypot_fail` /
   `honeypot_form_drift` / `honeypot_bypass`.  Drives the audit-event
   lookup table; bypass intentionally maps to `null` (caller emits the
   AS.0.6 `bypass_*` event from its own layer).
8. **30-day rotation period** — `HONEYPOT_ROTATION_PERIOD_SECONDS = 30 * 86400`.
   The validator accepts both the current and the previous epoch's field
   name to absorb NTP clock skew + the 1-request boundary grace.
9. **Reject code + status** — `"bot_challenge_failed"` / `429`.  Same
   surface as AS.3.4 `BOT_CHALLENGE_REJECTED_CODE` /
   `BOT_CHALLENGE_REJECTED_HTTP_STATUS` so the front-end UI keys on a
   single error code regardless of which AS layer caught the bot.
10. **`honeypotFieldName(form, tenant, epoch)` SHA-256 deterministic** —
    same triple → same field name across the two twins, regardless of
    runtime, locale, or platform.

If you change one side, you MUST change the other.  CI red is the canary.

## Why a TS twin and not just a thin client?

Two emission shapes for the generated app:

* **Server-side TS** (Node SSR / edge worker / `next/server`) —
  `validateHoneypot` is called with the parsed form body in the request
  handler, mirroring the Python lib's surface.  The result `allow`
  flag drives the HTTP 4xx vs continue decision.
* **Pure-client TS** — the browser computes `honeypotFieldName(form,
  tenant, currentEpoch())` at form-render time to produce the input's
  `name` attribute; the value is always rendered as an empty string,
  so a legitimate user submits an empty value and the validator passes.

The shared `HoneypotResult` envelope means a frontend that calls its
own backend `/api/.../validate-honeypot` endpoint reads `result.allow`
to decide 4xx vs continue, the same way OmniSight's own forms do.

## Public API

```ts
import {
  // contract constants
  FORM_PREFIXES,
  RARE_WORD_POOL,
  OS_HONEYPOT_CLASS,
  HONEYPOT_HIDE_CSS,
  HONEYPOT_INPUT_ATTRS,
  HONEYPOT_ROTATION_PERIOD_SECONDS,
  HONEYPOT_REJECTED_CODE,
  HONEYPOT_REJECTED_HTTP_STATUS,
  // event vocabulary
  EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
  EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
  EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
  ALL_HONEYPOT_EVENTS,
  // outcome vocabulary
  OUTCOME_HONEYPOT_PASS,
  OUTCOME_HONEYPOT_FAIL,
  OUTCOME_HONEYPOT_FORM_DRIFT,
  OUTCOME_HONEYPOT_BYPASS,
  ALL_HONEYPOT_OUTCOMES,
  // failure-reason vocabulary
  FAILURE_REASON_FIELD_FILLED,
  FAILURE_REASON_FIELD_MISSING_IN_FORM,
  FAILURE_REASON_FORM_PATH_UNKNOWN,
  // bypass-kind vocabulary
  BYPASS_KIND_API_KEY,
  BYPASS_KIND_TEST_TOKEN,
  BYPASS_KIND_IP_ALLOWLIST,
  BYPASS_KIND_KNOB_OFF,
  BYPASS_KIND_TENANT_DISABLED,
  ALL_BYPASS_KINDS,
  // result + errors
  type HoneypotResult,
  HoneypotError,
  HoneypotRejected,
  // helpers
  isEnabled,
  supportedFormPaths,
  currentEpoch,
  honeypotFieldName,
  expectedFieldNames,
  validateHoneypot,
  validateAndEnforce,
  shouldReject,
  eventForHoneypotOutcome,
} from "./index"
```

## Wiring example — server side

```ts
import { validateAndEnforce, HoneypotRejected } from "./_shared/honeypot"
import { verifyAndEnforce, BotChallengeRejected } from "./_shared/bot-challenge"

export async function loginHandler(req: Request, body: FormBody) {
  // Belt + suspenders: AS.3 captcha + AS.4 honeypot run independently.
  // Either one rejecting kills the request; both passing means the
  // request is "almost certainly human + not bot-auto-filled".
  try {
    validateAndEnforce("/api/v1/auth/login", body.tenantId, body, {
      bypassKind: req.headers.get("x-api-key") ? "apikey" : null,
    })
    await verifyAndEnforce({
      provider: pickProvider({ region: body.region }),
      token: body.captchaToken,
      secret: process.env.OMNISIGHT_TURNSTILE_SECRET!,
    })
  } catch (e) {
    if (e instanceof HoneypotRejected || e instanceof BotChallengeRejected) {
      // Same response shape regardless of which layer caught the bot.
      return new Response(
        JSON.stringify({ error: e.code }),
        { status: e.httpStatus, headers: { "content-type": "application/json" } },
      )
    }
    throw e
  }
  // ...continue with normal login path...
}
```

## Wiring example — form rendering (React JSX)

The actual `<HoneypotField>` JSX component lands per-app in AS.7.x —
this lib ships only the data primitives (field-name generator + the 5
required HTML attribute keys + the CSS class string) that the JSX
component consumes.  A typical render looks like:

```tsx
import {
  honeypotFieldName,
  currentEpoch,
  OS_HONEYPOT_CLASS,
  HONEYPOT_INPUT_ATTRS,
} from "./_shared/honeypot"

export function HoneypotField({
  formPath,
  tenantId,
}: {
  formPath: string
  tenantId: string
}) {
  const epoch = currentEpoch()
  const name = honeypotFieldName(formPath, tenantId, epoch)
  return (
    <input
      type="text"
      name={name}
      className={OS_HONEYPOT_CLASS}
      {...HONEYPOT_INPUT_ATTRS}
      defaultValue=""
    />
  )
}
```

The critical-CSS bundle in the generated app must include the rule
that `OS_HONEYPOT_CLASS` keys on, with the body of `HONEYPOT_HIDE_CSS`:

```css
.os-honeypot-field {
  position: absolute;
  left: -9999px;
  top: auto;
  width: 1px;
  height: 1px;
  overflow: hidden;
}
```

(Inline this rule into critical CSS so it survives external CSS load
failure — a stylesheet 404 must not strip the hide and reveal the
field to the user, per AS.0.7 §2.2 build invariant.)

## AS.0.8 single-knob

`isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` lazily from
`process.env`; default is `true`.  When the env is set to `"false"` /
`"0"`, `validateHoneypot` short-circuits with a bypass-shape result
(`outcome="honeypot_bypass"`, `bypassKind="knob_off"`,
`auditEvent=null`).  Mirrors the Python lib's
`settings.as_enabled = false` behaviour.
