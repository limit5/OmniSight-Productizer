# `templates/_shared/token-vault/` — AS.2.3 TS twin

TypeScript twin of `backend/security/token_vault.py`. A libsodium-
equivalent (Web Crypto AES-256-GCM) per-user / per-provider OAuth
credential vault, suitable for emission into the generated-app workspace
where the OmniSight backend's Fernet master key is **not** available.

## Files

| File         | What it ships                                                                                                                                              |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `index.ts`   | `TokenVault` class + key helpers (`generateMasterKey` / `importMasterKey` / `exportMasterKey`) + the same five typed errors + `fingerprint` + `isEnabled`. |
| `README.md`  | This file.                                                                                                                                                 |

## Cross-twin contract

Server-side ciphertext (Python Fernet) and client-side ciphertext
(TypeScript AES-GCM) are **deliberately not interchangeable**: each side
holds its own master key and never exposes it across the boundary. What
stays byte-equal across the two twins is the **contract surface** —
that's what the AS.1.5-style drift guard
(`backend/tests/test_token_vault_shape_drift.py`) locks:

1. **Binding envelope shape** — `{fmt, salt, uid, prv, tok}` keys (the
   wire JSON layout inside the AEAD-authenticated ciphertext).
2. **Numeric version constants** — `KEY_VERSION_CURRENT = 1`,
   `BINDING_FORMAT_VERSION = 1`.
3. **Supported provider whitelist** — `SUPPORTED_PROVIDERS` byte-equal
   the Python `frozenset({"apple", "github", "google", "microsoft"})`
   (which itself is byte-equal `account_linking._AS1_OAUTH_PROVIDERS`
   per AS.0.4 §5.2).
4. **Error semantics** — same five typed errors with the same names:
   `TokenVaultError` (base), `UnsupportedProviderError`,
   `UnknownKeyVersionError`, `BindingMismatchError`,
   `CiphertextCorruptedError`. Both sides raise the same class on the
   same input shape (binding mismatch, unknown key version, malformed
   envelope, etc.).
5. **Fingerprint algorithm** — `fingerprint(token)` returns `"****"`
   for tokens of length ≤ 8 and `"…<last 4 chars>"` otherwise, matching
   `backend.secret_store.fingerprint` byte-for-byte.

If you change one side, you MUST change the other. CI red is the canary.

## Cipher choice — why AES-GCM

The TODO row name is "libsodium 等價物" — we reach for AES-256-GCM via
Web Crypto rather than bundling `libsodium-wrappers`:

* **Zero dependencies** — Web Crypto ships natively in every browser
  and Node ≥ 15. The other TS twins in this repo
  (`oauth-client/`, `password-generator/`) follow the same
  no-`node_modules` discipline so the productizer's emit pipeline can
  drop the file in without churning the generated app's `package.json`.
* **AEAD-equivalent guarantees** — AES-GCM is the NIST-blessed AEAD
  primitive; libsodium's `crypto_secretbox_xchacha20poly1305` is the
  similarly-blessed AEAD primitive on the libsodium side. Both bind
  authenticated ciphertext + tag to a per-call random nonce; both
  resist the same threat model.
* **96-bit IV space** — AES-GCM's 12-byte IV is smaller than libsodium
  XChaCha20's 24-byte nonce, but at OmniSight scale the random IV per
  encryption is comfortably above the collision floor. The vault
  encrypts a few rows per user per provider; not millions per second.

## Why not share the master key with the server?

AS.0.4 §3 hard invariant — *each side* holds *one* master key. The
generated app **never** receives the server's `secret_store._fernet`
key (that key fingerprint is never exposed beyond the backend). The
generated app generates its own AES-GCM master key on first boot and
persists it in its own keystore (typically IndexedDB; vendor-specific
secure-storage in mobile shells). Server-encrypted token rows
(`oauth_tokens.access_token_enc`) live behind the server vault; any
client-side mirror lives behind the client vault. The two never touch.

## Public API

```ts
import {
  TokenVault,
  generateMasterKey,
  importMasterKey,
  exportMasterKey,
  fingerprint,
  isEnabled,
  KEY_VERSION_CURRENT,
  BINDING_FORMAT_VERSION,
  SUPPORTED_PROVIDERS,
  MASTER_KEY_RAW_BYTES,
  TokenVaultError,
  UnsupportedProviderError,
  UnknownKeyVersionError,
  BindingMismatchError,
  CiphertextCorruptedError,
  type EncryptedToken,
} from "./index"

// First-boot path — generate + persist
const key = await generateMasterKey()
await keystore.put("oauth-vault-master-key", await exportMasterKey(key))

// Subsequent loads — restore
const raw = await keystore.get("oauth-vault-master-key")
const restored = await importMasterKey(raw)

// Use it
const vault = new TokenVault(restored)
const enc = await vault.encryptForUser("user-42", "github", "ghp_secret_xyz")
// Persist enc.ciphertext + enc.keyVersion to your local-app oauth_tokens row.
const tok = await vault.decryptForUser("user-42", "github", enc)
```

All `encryptForUser` / `decryptForUser` calls are async (Web Crypto's
`subtle.encrypt` / `subtle.decrypt` are async).

## AS.0.8 single-knob hook

`isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` (the **frontend**
twin of the Python `settings.as_enabled` — deliberately decoupled per
AS.0.8 §2.5). Default `true`. The pure encrypt / decrypt helpers
deliberately do NOT consult the knob — turning AS off must not break a
backfill / DSAR / key-rotation script.

## Module-global state audit (per implement_phase_step.md SOP §1)

* No module-level mutable state — only frozen `Set` + frozen number
  literals + classes.
* All randomness comes from `globalThis.crypto.getRandomValues`. Each
  browser tab / Node worker derives its own values from the same
  kernel CSPRNG (answer #1 of SOP §1: deterministic-by-construction
  across workers).
* The master `CryptoKey` is owned by the `TokenVault` instance, not
  the module — caller-injected at construction time. The module never
  reads it from `process.env` / `localStorage` / `window.*`.
* No DB, no network, no env reads at module top level.

## Shape parity vs the Python side

| Python                                  | TypeScript                              |
| --------------------------------------- | --------------------------------------- |
| `EncryptedToken.ciphertext`             | `EncryptedToken.ciphertext`             |
| `EncryptedToken.key_version`            | `EncryptedToken.keyVersion`             |
| `encrypt_for_user(user_id, prv, plain)` | `vault.encryptForUser(userId, prv, p)`  |
| `decrypt_for_user(user_id, prv, tok)`   | `vault.decryptForUser(userId, prv, t)`  |
| `secret_store._fernet` (server-owned)   | `TokenVault.masterKey` (caller-owned)   |
| `Fernet` cipher                         | AES-256-GCM (Web Crypto)                |
| `BindingMismatchError`                  | `BindingMismatchError`                  |
| `UnknownKeyVersionError`                | `UnknownKeyVersionError`                |
| `CiphertextCorruptedError`              | `CiphertextCorruptedError`              |

Casing is the canonical idiom of each language; the **envelope keys**
(`fmt` / `salt` / `uid` / `prv` / `tok`) and the **numeric constants**
are the byte-identical contract surface.
