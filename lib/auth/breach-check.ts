/**
 * AS.7.2 — HaveIBeenPwned k-anonymity breach check.
 *
 * Implements the [HIBP Pwned Passwords v3 API][1] k-anonymity flow:
 *
 *   1. SHA-1 the password client-side (Web Crypto `subtle.digest`)
 *   2. Send the **first 5 hex chars** of the digest as the URL
 *      suffix — `https://api.pwnedpasswords.com/range/<prefix5>`
 *   3. The response is a `\r\n`-separated list of `<35-char-suffix>:<count>`
 *      rows for every breached hash starting with the prefix
 *   4. The browser scans the list locally for the matching suffix
 *      and returns the count (or 0)
 *
 * The plaintext password never leaves the browser; the prefix has
 * ≥ 16² × 16 = 4 096 candidates per request, well within HIBP's
 * privacy bound. Cloudflare-fronted, no API key, free, the
 * canonical industry pattern (1Password, Firefox Monitor, etc.).
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are pure functions or `as const` constants. No
 *     module-level mutable container.
 *   - `breachCount(password)` calls Web Crypto + fetch — both are
 *     deterministic with respect to (password, network state). For
 *     the same password and a healthy network, the same result is
 *     returned across workers / tabs (Answer #1 of the SOP §1 audit).
 *   - Network failure is handled as a soft "unknown" outcome rather
 *     than an exception so the signup page can still submit; the
 *     UI surfaces a non-blocking warning.
 *
 * Read-after-write timing audit: N/A — read-only network call.
 *
 * [1]: https://haveibeenpwned.com/API/v3#PwnedPasswords
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Base URL of the HIBP range endpoint. Pinned by tests so a typo
 *  is a CI red. */
export const HIBP_RANGE_BASE = "https://api.pwnedpasswords.com/range"

/** Number of hex characters from the SHA-1 prefix sent to HIBP.
 *  RFC: HIBP returns up to ~600 entries per prefix (1 in 16⁵
 *  hashes); the suffix in the response is therefore 35 chars. */
export const HIBP_PREFIX_LENGTH = 5
export const HIBP_SUFFIX_LENGTH = 40 - HIBP_PREFIX_LENGTH  // 35

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Web Crypto helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Resolve the platform `crypto.subtle` instance. Lazy-throwing so
 *  the module imports cleanly even on runtimes without Web Crypto;
 *  only the `breachCheck` callsite eats the error. */
function _resolveSubtle(): SubtleCrypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || !c.subtle) {
    throw new Error(
      "AS.7.2 breach-check: SubtleCrypto unavailable (need Web Crypto " +
        "API — modern browser or Node ≥ 16).",
    )
  }
  return c.subtle
}

/** SHA-1 a UTF-8 string and return the **uppercase** hex digest
 *  (40 chars). Uppercase because HIBP responds with uppercase
 *  hashes; matching case-sensitively avoids a `.toUpperCase()` on
 *  every response row. */
export async function sha1HexUpper(seed: string): Promise<string> {
  const subtle = _resolveSubtle()
  const data = new TextEncoder().encode(seed)
  const buf = await subtle.digest("SHA-1", data)
  const bytes = new Uint8Array(buf)
  let hex = ""
  for (let i = 0; i < bytes.length; i += 1) {
    hex += bytes[i].toString(16).padStart(2, "0")
  }
  return hex.toUpperCase()
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Range response parser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Parse the HIBP range body for a specific 35-char suffix and
 *  return its breach count, or 0 if absent. Body shape per row:
 *
 *      ABCDEF…XYZ:42
 *
 *  Splits on `\r\n` (Cloudflare) but tolerates `\n` (e.g. test
 *  fixtures). Skips malformed rows silently — that's also how
 *  every reference HIBP client behaves. */
export function parseHibpRangeBody(
  body: string,
  suffixUpper: string,
): number {
  if (!body) return 0
  const rows = body.split(/\r?\n/)
  for (const row of rows) {
    const colon = row.indexOf(":")
    if (colon < 0) continue
    const suffix = row.slice(0, colon).trim()
    if (suffix === suffixUpper) {
      const count = Number.parseInt(row.slice(colon + 1).trim(), 10)
      if (Number.isFinite(count) && count >= 0) return count
      return 0
    }
  }
  return 0
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public outcome type
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type BreachStatus = "ok" | "breached" | "unknown" | "skipped"

export interface BreachResult {
  /** Lookup outcome:
   *  - `ok` — HIBP returned 200 + count = 0
   *  - `breached` — HIBP returned 200 + count ≥ 1
   *  - `unknown` — network / fetch failure (soft fail)
   *  - `skipped` — caller passed empty / sentinel input */
  readonly status: BreachStatus
  /** Number of breaches the password appears in (≥ 1 implies
   *  `breached`; 0 implies `ok`). `null` for `unknown` / `skipped`. */
  readonly count: number | null
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public API
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

interface BreachCheckOptions {
  /** Override `fetch` — primarily for tests. Defaults to the global
   *  fetch implementation. */
  readonly fetchImpl?: typeof fetch
  /** Optional AbortSignal so the caller can cancel a pending lookup
   *  (e.g. when the user types another character before the previous
   *  fetch resolved). */
  readonly signal?: AbortSignal
}

/** Look up the password in HIBP and return its breach status.
 *  Never throws — soft-fails to `unknown` so the signup form can
 *  always submit. */
export async function breachCount(
  password: string,
  opts: BreachCheckOptions = {},
): Promise<BreachResult> {
  if (!password) {
    return Object.freeze({ status: "skipped", count: null })
  }
  let prefix: string
  let suffix: string
  try {
    const digest = await sha1HexUpper(password)
    prefix = digest.slice(0, HIBP_PREFIX_LENGTH)
    suffix = digest.slice(HIBP_PREFIX_LENGTH)
  } catch {
    return Object.freeze({ status: "unknown", count: null })
  }

  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  if (typeof fetchImpl !== "function") {
    return Object.freeze({ status: "unknown", count: null })
  }

  let body: string
  try {
    const res = await fetchImpl(`${HIBP_RANGE_BASE}/${prefix}`, {
      method: "GET",
      // HIBP recommends this header to opt into the augmented (NTLM)
      // padding that hides the exact response size. Either header
      // value is accepted. Using `0` (off) so the test fixtures
      // remain stable across mocked runs.
      headers: { "Add-Padding": "false" },
      signal: opts.signal,
    })
    if (!res.ok) {
      return Object.freeze({ status: "unknown", count: null })
    }
    body = await res.text()
  } catch {
    return Object.freeze({ status: "unknown", count: null })
  }

  const count = parseHibpRangeBody(body, suffix)
  return Object.freeze({
    status: count > 0 ? "breached" : "ok",
    count,
  })
}
