/**
 * AS.2.3 — OAuth token vault (TypeScript twin, libsodium-equivalent).
 *
 * Behaviourally aligned with `backend/security/token_vault.py`. Provides
 * per-user / per-provider at-rest encryption for OAuth credentials in the
 * generated-app workspace (where the OmniSight backend's Fernet master
 * key is **not** available — each generated app manages its own master
 * key).
 *
 * Cryptographic shape
 * ───────────────────
 * The TS twin wraps each plaintext in the same JSON binding envelope
 * the Python side uses::
 *
 *     {
 *       "fmt": 1,
 *       "salt": "<b64 16 bytes>",
 *       "uid":  "<owner user_id>",
 *       "prv":  "<provider slug>",
 *       "tok":  "<plaintext token>"
 *     }
 *
 * before handing it to AES-256-GCM (Web Crypto `subtle.encrypt`). AES-GCM
 * is the AEAD primitive equivalent to libsodium's `crypto_secretbox_xchacha20poly1305`
 * — both bind a random nonce to authenticated ciphertext + tag in a
 * single call. Server-side ciphertext (Fernet) and client-side
 * ciphertext (AES-GCM) are **deliberately not interchangeable**: each
 * lives behind its own master key and never round-trips across the
 * server / client boundary. The contract surface (envelope shape,
 * binding-format version, supported providers, key-version reservation)
 * is what stays byte-equal between the two twins — that is what the
 * AS.1.5-style drift guard locks.
 *
 * Why a binding envelope and not a derived sub-key
 * ────────────────────────────────────────────────
 * AS.0.4 §3.1 hard invariant: a single master key per side, no per-row
 * sub-key derivation. The salt lives **inside** the AEAD-authenticated
 * envelope, so a DB-level row swap (attacker copies user-A's
 * `access_token_enc` row into user-B's row) decrypts but fails the
 * `uid` / `prv` binding check and raises `BindingMismatchError`.
 *
 * `keyVersion` reservation
 * ────────────────────────
 * `KEY_VERSION_CURRENT = 1`. The first KMS migration row will introduce
 * a v2 branch + dual-read fallback; until then any non-1 value on
 * decrypt raises `UnknownKeyVersionError` rather than silently
 * degrading.
 *
 * Provider whitelist
 * ──────────────────
 * `SUPPORTED_PROVIDERS` MUST equal the Python side's
 * `backend.security.token_vault.SUPPORTED_PROVIDERS` byte-for-byte.
 * Drift is caught by the AS.1.5-style cross-twin parity test in
 * `backend/tests/test_token_vault_shape_drift.py`.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1)
 * ──────────────────────────────────────────────────────────────
 *   * No module-level mutable state — only frozen literals + classes.
 *   * All randomness comes from `globalThis.crypto.getRandomValues`
 *     (Web Crypto, kernel CSPRNG). Each browser tab / Node worker
 *     derives its own values from the same kernel source — answer #1
 *     of SOP §1 audit (deterministic-by-construction across workers).
 *   * The master `CryptoKey` is owned by the caller (constructor arg
 *     to `TokenVault`); the module never holds it at module scope and
 *     never reads it from env / storage. First-boot key generation +
 *     persistence is the generated app's responsibility (typically
 *     IndexedDB / a vendor-specific keystore).
 *   * Importing the module is free of side effects.
 *
 * AS.0.8 single-knob hook
 * ───────────────────────
 * `isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend**
 * twin of the Python `settings.as_enabled` — deliberately decoupled per
 * AS.0.8 §2.5). Default `true`. The pure encrypt / decrypt helpers do
 * NOT auto-gate on the knob: per AS.0.4 §6.2 a backfill / DSAR /
 * key-rotation script must remain able to read existing ciphertext
 * even when OAuth login is feature-flagged off.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants — must mirror Python side byte-for-byte
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Active key version. AS.0.4 §3.1 reserves the column for future
 * KMS migrations; the first KMS-rotation row will introduce a v2
 * branch and the dual-read fallback. */
export const KEY_VERSION_CURRENT = 1

/** Binding envelope format. Bumped only when the wrapper shape itself
 * changes (e.g. add a field). Changing this requires a dual-read
 * phase; do NOT alter casually. */
export const BINDING_FORMAT_VERSION = 1

/** Provider whitelist — MUST equal the Python side's
 * `SUPPORTED_PROVIDERS`. Drift is caught by the AS.1.5-style cross-twin
 * parity test. */
export const SUPPORTED_PROVIDERS: ReadonlySet<string> = Object.freeze(
  new Set<string>(["apple", "github", "google", "microsoft"]),
)

/** Per-row salt size (bytes). 16 bytes / 128 bits matches the GUID /
 * nonce convention elsewhere in the codebase + the Python side's
 * `_SALT_RAW_BYTES`. */
const SALT_RAW_BYTES = 16

/** AES-GCM IV size — 12 bytes per NIST SP 800-38D §5.2.1.1
 * recommendation. Smaller than libsodium-XSalsa20's 24-byte nonce, but
 * AES-GCM is the WebCrypto default AEAD; the random IV per encryption
 * means the 96-bit space is comfortably above any realistic OmniSight-
 * scale row count. */
const AES_GCM_IV_BYTES = 12

/** Master-key raw byte size — 32 bytes for AES-256-GCM. Same magnitude
 * as libsodium-secretbox's 32-byte key. */
export const MASTER_KEY_RAW_BYTES = 32

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Errors
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Base class for all errors this module raises. Callers can catch
 * once and not enumerate. Mirrors `backend.security.token_vault.TokenVaultError`. */
export class TokenVaultError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "TokenVaultError"
  }
}

/** `provider` is not in `SUPPORTED_PROVIDERS`. */
export class UnsupportedProviderError extends TokenVaultError {
  constructor(message: string) {
    super(message)
    this.name = "UnsupportedProviderError"
  }
}

/** `keyVersion` on a stored row is not `KEY_VERSION_CURRENT`. The
 * first KMS migration will replace this with a multi-version dispatch;
 * until then any unknown value is treated as corruption. */
export class UnknownKeyVersionError extends TokenVaultError {
  constructor(message: string) {
    super(message)
    this.name = "UnknownKeyVersionError"
  }
}

/** Decrypted binding envelope's `uid` / `prv` did not match the values
 * the caller claimed the row belongs to. Indicates either a DB-level
 * row shuffle or a caller bug. */
export class BindingMismatchError extends TokenVaultError {
  constructor(message: string) {
    super(message)
    this.name = "BindingMismatchError"
  }
}

/** Ciphertext could not be decrypted (AES-GCM auth failed) or the
 * inner JSON envelope was malformed. */
export class CiphertextCorruptedError extends TokenVaultError {
  constructor(message: string) {
    super(message)
    this.name = "CiphertextCorruptedError"
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public types
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Persistent shape for one stored OAuth credential.
 *
 * Round-tripped to / from the `oauth_tokens` row's `access_token_enc`
 * (TEXT) + `key_version` (INTEGER) columns. The `ciphertext` is the
 * base64url-encoded `iv || aesGcm(plaintext, iv, masterKey)` byte
 * sequence; `keyVersion` MUST be `KEY_VERSION_CURRENT` for this
 * release. The salt is intentionally NOT a public attribute — it lives
 * inside the AEAD-authenticated envelope, not in a separate column. */
export interface EncryptedToken {
  readonly ciphertext: string
  readonly keyVersion: number
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  AS.0.8 single-knob hook (frontend side)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Whether the AS feature family is enabled per AS.0.8 §3.1 noop matrix.
 *
 * Reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend** twin of the
 * Python `settings.as_enabled` — deliberately decoupled per AS.0.8
 * §2.5 so the frontend can be flipped independently from the backend).
 * Default `true`.
 *
 * Resolution order:
 *   1. `(globalThis as any).OMNISIGHT_AS_FRONTEND_ENABLED`
 *   2. `process.env.OMNISIGHT_AS_FRONTEND_ENABLED`
 *   3. Default `true`.
 *
 * The pure encrypt / decrypt helpers deliberately do NOT call this —
 * turning the knob off must not break a backfill / DSAR / key-rotation
 * script (matches the Python lib invariant per AS.0.4 §6.2). */
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
//  Crypto helpers (Web Crypto)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Resolve the platform Web-Crypto impl, throwing typed if absent. */
function getCrypto(): Crypto {
  const c = (globalThis as { crypto?: Crypto }).crypto
  if (!c || typeof c.getRandomValues !== "function" || !c.subtle) {
    throw new Error(
      "Web Crypto API not available — secure random + AES-GCM are required",
    )
  }
  return c
}

/** Constant-time string equality. JS strings are UTF-16 — for our use
 * (ASCII slugs / user_ids) charCode comparison is byte comparison. */
function timingSafeEqual(a: string, b: string): boolean {
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

/** Base64-encode raw bytes (standard alphabet, padded — what the
 * Python side emits for the `salt` field via `base64.b64encode`). */
function b64Standard(raw: Uint8Array): string {
  let bin = ""
  for (let i = 0; i < raw.length; i++) bin += String.fromCharCode(raw[i])
  return btoa(bin)
}

/** Base64url-encode raw bytes (no padding) — used for the wire ciphertext. */
function b64urlNoPad(raw: Uint8Array): string {
  let bin = ""
  for (let i = 0; i < raw.length; i++) bin += String.fromCharCode(raw[i])
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** Inverse of `b64urlNoPad`. */
function b64urlDecode(s: string): Uint8Array {
  let std = s.replace(/-/g, "+").replace(/_/g, "/")
  while (std.length % 4 !== 0) std += "="
  const bin = atob(std)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** Concatenate two `Uint8Array`s. */
function concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length)
  out.set(a, 0)
  out.set(b, a.length)
  return out
}

/** Canonical JSON.stringify with sorted keys + no spaces — byte-equal
 * to Python `json.dumps(obj, sort_keys=True, separators=(",", ":"))`
 * for shallow dicts of scalar values. */
function canonicalJsonStringify(obj: Record<string, unknown>): string {
  const sortedKeys = Object.keys(obj).sort()
  const parts: string[] = []
  for (const k of sortedKeys) {
    parts.push(`${JSON.stringify(k)}:${JSON.stringify(obj[k])}`)
  }
  return "{" + parts.join(",") + "}"
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Master-key helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Generate a fresh 256-bit AES-GCM master key. The caller is
 * responsible for persisting the export of this key in a per-app
 * keystore (typically IndexedDB or a vendor-specific secure-storage
 * binding). Equivalent to the Python side's first-boot
 * `Fernet.generate_key()` flow that `secret_store` already owns. */
export async function generateMasterKey(): Promise<CryptoKey> {
  const c = getCrypto()
  return await c.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    true,
    ["encrypt", "decrypt"],
  )
}

/** Import a previously-exported 32-byte raw key as an AES-GCM
 * `CryptoKey`. Use this on every page load after fetching the raw key
 * bytes from your keystore. */
export async function importMasterKey(raw: Uint8Array): Promise<CryptoKey> {
  if (raw.length !== MASTER_KEY_RAW_BYTES) {
    throw new TokenVaultError(
      `master key must be exactly ${MASTER_KEY_RAW_BYTES} bytes, got ${raw.length}`,
    )
  }
  const c = getCrypto()
  return await c.subtle.importKey(
    "raw",
    raw,
    { name: "AES-GCM", length: 256 },
    true,
    ["encrypt", "decrypt"],
  )
}

/** Export an AES-GCM `CryptoKey` as 32 raw bytes for keystore
 * persistence. Pair with `importMasterKey` on subsequent page loads. */
export async function exportMasterKey(key: CryptoKey): Promise<Uint8Array> {
  const c = getCrypto()
  const buf = await c.subtle.exportKey("raw", key)
  return new Uint8Array(buf)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Internal helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function checkProvider(provider: string): string {
  if (typeof provider !== "string" || provider.length === 0) {
    throw new UnsupportedProviderError(
      `provider must be a non-empty string, got ${typeof provider}`,
    )
  }
  const p = provider.trim().toLowerCase()
  if (!SUPPORTED_PROVIDERS.has(p)) {
    throw new UnsupportedProviderError(
      `unsupported OAuth provider: ${JSON.stringify(provider)} ` +
        `(expected one of [${[...SUPPORTED_PROVIDERS].sort().join(", ")}])`,
    )
  }
  return p
}

function checkUserId(userId: string): string {
  if (typeof userId !== "string" || userId.length === 0) {
    throw new TokenVaultError(
      `userId must be a non-empty string, got ${typeof userId}`,
    )
  }
  return userId
}

function checkPlaintext(plaintext: string): string {
  if (typeof plaintext !== "string") {
    throw new TokenVaultError(
      `plaintext must be a string, got ${typeof plaintext}`,
    )
  }
  if (plaintext.length === 0) {
    throw new TokenVaultError("plaintext must not be empty")
  }
  return plaintext
}

function checkEncryptedToken(token: unknown): asserts token is EncryptedToken {
  if (
    !token ||
    typeof token !== "object" ||
    typeof (token as EncryptedToken).ciphertext !== "string" ||
    typeof (token as EncryptedToken).keyVersion !== "number"
  ) {
    throw new TokenVaultError(
      "token must be an EncryptedToken { ciphertext, keyVersion }",
    )
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Public API — TokenVault class
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Per-user / per-provider OAuth token vault.
 *
 * Construct once per app load with the master `CryptoKey` (typically
 * imported from a keystore via `importMasterKey`). The same instance
 * can encrypt + decrypt many tokens for many users / providers; the
 * binding envelope inside each ciphertext keeps rows tied to their
 * owning `(userId, provider)` pair so a DB-level row shuffle is
 * caught at decrypt time.
 *
 * TS twin of `backend.security.token_vault` — the public surface
 * mirrors the Python module: `encryptForUser` ↔ `encrypt_for_user`,
 * `decryptForUser` ↔ `decrypt_for_user`, `EncryptedToken` ↔
 * `EncryptedToken`, plus the same five typed errors. */
export class TokenVault {
  private readonly masterKey: CryptoKey

  constructor(masterKey: CryptoKey) {
    if (
      !masterKey ||
      typeof masterKey !== "object" ||
      (masterKey as CryptoKey).type !== "secret"
    ) {
      throw new TokenVaultError(
        "masterKey must be a CryptoKey of type 'secret' " +
          "(use generateMasterKey or importMasterKey)",
      )
    }
    this.masterKey = masterKey
  }

  /** Encrypt *plaintext* (an OAuth access_token / refresh_token) for
   * storage in the `oauth_tokens` row owned by *userId* + *provider*.
   *
   * The plaintext is wrapped in a binding envelope (see module
   * docstring) before being handed to AES-256-GCM, so the resulting
   * ciphertext is bound to this *(userId, provider)* pair: a row swap
   * in the database will be caught by `decryptForUser`. */
  async encryptForUser(
    userId: string,
    provider: string,
    plaintext: string,
  ): Promise<EncryptedToken> {
    const p = checkProvider(provider)
    const uid = checkUserId(userId)
    const tok = checkPlaintext(plaintext)

    const c = getCrypto()
    const saltBytes = new Uint8Array(SALT_RAW_BYTES)
    c.getRandomValues(saltBytes)

    const envelope = {
      fmt: BINDING_FORMAT_VERSION,
      salt: b64Standard(saltBytes),
      uid,
      prv: p,
      tok,
    }
    const payload = canonicalJsonStringify(envelope)

    const iv = new Uint8Array(AES_GCM_IV_BYTES)
    c.getRandomValues(iv)
    const enc = await c.subtle.encrypt(
      { name: "AES-GCM", iv },
      this.masterKey,
      new TextEncoder().encode(payload),
    )
    const wire = concatBytes(iv, new Uint8Array(enc))
    return {
      ciphertext: b64urlNoPad(wire),
      keyVersion: KEY_VERSION_CURRENT,
    }
  }

  /** Decrypt *token* and return its plaintext.
   *
   * Verifies that the binding envelope inside the ciphertext matches
   * the *(userId, provider)* the caller is claiming the row belongs
   * to. A mismatch (DB-level row shuffle, or caller bug) raises
   * `BindingMismatchError`. */
  async decryptForUser(
    userId: string,
    provider: string,
    token: EncryptedToken,
  ): Promise<string> {
    const p = checkProvider(provider)
    const uid = checkUserId(userId)
    checkEncryptedToken(token)
    if (token.keyVersion !== KEY_VERSION_CURRENT) {
      throw new UnknownKeyVersionError(
        `unknown keyVersion=${token.keyVersion} ` +
          `(this release supports only ${KEY_VERSION_CURRENT})`,
      )
    }

    const c = getCrypto()
    let wire: Uint8Array
    try {
      wire = b64urlDecode(token.ciphertext)
    } catch (e) {
      throw new CiphertextCorruptedError("ciphertext is not valid base64url")
    }
    if (wire.length < AES_GCM_IV_BYTES + 16) {
      // 16 bytes is the AES-GCM auth tag; below this nothing useful
      // can decode.
      throw new CiphertextCorruptedError(
        "ciphertext too short to contain iv + auth tag",
      )
    }
    const iv = wire.subarray(0, AES_GCM_IV_BYTES)
    const enc = wire.subarray(AES_GCM_IV_BYTES)

    let payloadBuf: ArrayBuffer
    try {
      payloadBuf = await c.subtle.decrypt(
        { name: "AES-GCM", iv },
        this.masterKey,
        enc,
      )
    } catch (e) {
      throw new CiphertextCorruptedError("ciphertext failed AES-GCM authentication")
    }
    const payloadStr = new TextDecoder().decode(payloadBuf)

    let envelope: unknown
    try {
      envelope = JSON.parse(payloadStr)
    } catch (e) {
      throw new CiphertextCorruptedError("inner envelope is not valid JSON")
    }
    if (
      !envelope ||
      typeof envelope !== "object" ||
      Array.isArray(envelope)
    ) {
      throw new CiphertextCorruptedError(
        `inner envelope must be an object, got ${
          Array.isArray(envelope) ? "array" : typeof envelope
        }`,
      )
    }
    const env = envelope as Record<string, unknown>

    if (env.fmt !== BINDING_FORMAT_VERSION) {
      // Treat as binding mismatch rather than corruption — the
      // ciphertext decoded fine (AES-GCM auth passed), it just carries
      // a shape this release doesn't understand.
      throw new BindingMismatchError(
        `unknown binding format version: ${JSON.stringify(env.fmt)} ` +
          `(this release supports only ${BINDING_FORMAT_VERSION})`,
      )
    }

    const storedUid = env.uid
    const storedPrv = env.prv
    if (typeof storedUid !== "string" || typeof storedPrv !== "string") {
      throw new CiphertextCorruptedError(
        "envelope missing 'uid' / 'prv' string fields",
      )
    }
    if (!timingSafeEqual(storedUid, uid)) {
      throw new BindingMismatchError("ciphertext bound to a different userId")
    }
    if (!timingSafeEqual(storedPrv, p)) {
      throw new BindingMismatchError("ciphertext bound to a different provider")
    }

    const tok = env.tok
    if (typeof tok !== "string") {
      throw new CiphertextCorruptedError(
        "envelope 'tok' field missing or not a string",
      )
    }
    return tok
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Fingerprint helper (mirrors backend.secret_store.fingerprint)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Last-4-character fingerprint for UI / log surfacing.
 *
 * Byte-equal to the Python side's `backend.secret_store.fingerprint`:
 * tokens of length ≤ 8 collapse to `"****"`; longer tokens render as
 * `"…<last 4 chars>"`. Used by `oauth-client/audit.ts` callers that
 * want to attach a short ciphertext-side hint to a structured log
 * line without leaking the rest of the value. */
export function fingerprint(token: string | null | undefined): string {
  const t = token ?? ""
  if (t.length <= 8) return "****"
  return "…" + t.slice(-4)
}
