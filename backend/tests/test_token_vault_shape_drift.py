"""AS.2.3 — Token-vault contract drift guard (Python ↔ TS twin).

Behavioural drift guard between
:mod:`backend.security.token_vault` (Python, KS.1 envelope-based) and
``templates/_shared/token-vault/index.ts`` (TS twin, Web Crypto
AES-GCM-based).

Why this test exists
────────────────────
The two vaults deliberately use different cipher primitives —
KS.1.2 per-tenant envelope encryption on the server, AES-256-GCM on
the client. Server-encrypted ciphertext and client-encrypted
ciphertext are NOT interchangeable; what stays byte-equal is the
**contract surface**:

    * binding-envelope key set ``{fmt, salt, uid, prv, tok}``
    * numeric constants ``KEY_VERSION_CURRENT``, ``BINDING_FORMAT_VERSION``
    * provider whitelist ``SUPPORTED_PROVIDERS``
    * the same five typed errors with the same names + the same input
      shapes that trigger each one
    * the ``fingerprint`` algorithm (``"****"`` ≤8 char, ``"…<last 4>"`` else)

Coverage shape
──────────────
1. **Static parity** (no Node required) — regex-extract numeric
   constants + provider whitelist from the TS source and ``==``-compare
   to the Python side. Catches "someone bumps ``KEY_VERSION_CURRENT``
   on one side only" cleanly without a Node spawn.

2. **Behavioural parity** (Node spawned once per session) — drive a
   matrix of fixtures through both sides and compare the **outcome**
   (envelope key set on success, error class name on failure):

       * round-trip success across all 4 providers + provider case
         normalisation
       * binding mismatch on userId swap
       * binding mismatch on provider swap
       * unsupported provider rejected on encrypt
       * empty plaintext rejected on encrypt
       * unknown keyVersion rejected on decrypt
       * tampered ciphertext rejected
       * envelope shape after own-side encrypt == ``{fmt, salt, uid, prv, tok}``

   We deliberately do NOT compare ciphertext bytes — the two ciphers
   are not interchangeable and that's by design (per AS.0.4 §3.1).
   What matters is that both sides produce envelopes with the same
   key set and react identically to malformed inputs.

How TS execution works
──────────────────────
Same harness as AS.1.5 (`backend/tests/test_oauth_token_shape_drift.py`):
spawn ``node --experimental-strip-types`` to import the TS twin
directly — no transpile step, no node_modules dep. A single
subprocess runs all fixtures and emits one JSON blob; the
session-scoped fixture caches that across the parametrized tests so
the spawn cost amortises to one invocation per pytest session.

The whole family ``pytest.skip``s if Node is unavailable (or below
v22) — this matches the AS.1.2 / AS.1.3 / AS.1.4 / AS.1.5 cross-twin
tests' "skip if TS twin file is absent" gating; CI must have Node.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All fixture data lives in module-level dict literals containing only
  immutable scalars; each pytest worker re-imports them with byte-
  identical content (answer #1 of SOP §1: deterministic-by-construction
  across workers).
* The session-scoped Node-output cache lives on the pytest fixture, not
  module-level — pytest manages its lifecycle per worker.
* No DB, no network IO, no env reads at module import time.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Mapping

import pytest

from backend.security import token_vault as tv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "token-vault"
    / "index.ts"
)


def _node_supports_strip_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        r = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    raw = r.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major >= 22


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Static parity (no Node required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ts_source() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


def test_ts_twin_file_exists() -> None:
    """AS.2.3 deliverable presence — the TS twin file must be on disk
    where the productizer's emit pipeline expects it."""
    assert _TS_TWIN_PATH.exists(), (
        f"AS.2.3 TS twin missing at {_TS_TWIN_PATH}; "
        "the OAuth row in the productizer scaffolds depends on this file."
    )


def test_ts_key_version_current_matches_python() -> None:
    src = _ts_source()
    m = re.search(r"export\s+const\s+KEY_VERSION_CURRENT\s*=\s*(\d+)", src)
    assert m is not None, "TS twin missing KEY_VERSION_CURRENT export"
    assert int(m.group(1)) == tv.KEY_VERSION_CURRENT, (
        f"KEY_VERSION_CURRENT drift: Python={tv.KEY_VERSION_CURRENT}, "
        f"TS={m.group(1)}"
    )


def test_ts_binding_format_version_matches_python() -> None:
    src = _ts_source()
    m = re.search(r"export\s+const\s+BINDING_FORMAT_VERSION\s*=\s*(\d+)", src)
    assert m is not None, "TS twin missing BINDING_FORMAT_VERSION export"
    assert int(m.group(1)) == tv.BINDING_FORMAT_VERSION, (
        f"BINDING_FORMAT_VERSION drift: Python={tv.BINDING_FORMAT_VERSION}, "
        f"TS={m.group(1)}"
    )


def test_ts_supported_providers_matches_python() -> None:
    """Cross-twin AS.0.4 §5.2 invariant: the provider whitelist
    must agree byte-for-byte."""
    src = _ts_source()
    # Match the inline `new Set<string>([...])` literal regardless of
    # whitespace.
    m = re.search(
        r"SUPPORTED_PROVIDERS[^=]*=\s*Object\.freeze\(\s*new\s+Set<string>\(\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing SUPPORTED_PROVIDERS Object.freeze literal"
    raw_entries = re.findall(r'"([^"]+)"', m.group(1))
    ts_set = frozenset(raw_entries)
    assert ts_set == tv.SUPPORTED_PROVIDERS, (
        f"SUPPORTED_PROVIDERS drift: Python={sorted(tv.SUPPORTED_PROVIDERS)}, "
        f"TS={sorted(ts_set)}"
    )


def test_ts_declares_five_typed_errors() -> None:
    """All five vault error classes must be declared on the TS side
    with the same names. Catches partial-port regressions where someone
    exports the function but forgets the typed error.
    """
    src = _ts_source()
    expected_classes = [
        "TokenVaultError",
        "UnsupportedProviderError",
        "UnknownKeyVersionError",
        "BindingMismatchError",
        "CiphertextCorruptedError",
    ]
    for cls in expected_classes:
        # Match `export class X extends ...` with any base.
        assert re.search(rf"export\s+class\s+{cls}\b", src), (
            f"TS twin missing typed error: {cls!r}"
        )


def test_ts_uses_web_crypto_not_math_random() -> None:
    """Same RNG-provenance pin AS.1.2 enforces — the TS twin must use
    Web Crypto's `getRandomValues`, never `Math.random`."""
    src = _ts_source()
    assert "getRandomValues" in src, (
        "TS twin must use crypto.getRandomValues for salts + IVs"
    )
    assert "Math.random" not in src, (
        "TS twin must not use Math.random — use Web Crypto for cryptographic randomness"
    )


def test_ts_uses_aes_gcm() -> None:
    """The libsodium-equivalent AEAD primitive on the TS side is
    AES-256-GCM. Pin it by source grep so a future contributor can't
    silently swap it for an unauthenticated cipher.
    """
    src = _ts_source()
    assert "AES-GCM" in src, "TS twin must use AES-GCM AEAD primitive"


def test_ts_envelope_keys_match_python() -> None:
    """The TS twin's envelope literal must enumerate the same five
    keys the Python side writes — `{fmt, salt, uid, prv, tok}`. This
    is the wire-shape contract that round-trips through the
    `oauth_tokens.access_token_enc` column.
    """
    src = _ts_source()
    # Extract the encryptForUser envelope literal block:
    #   const envelope = { fmt: ..., salt: ..., uid, prv: p, tok }
    m = re.search(r"const\s+envelope\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m is not None, "TS twin missing envelope literal"
    body = m.group(1)
    # All five keys must appear (order tolerated; canonicalJsonStringify
    # sorts them at serialisation time).
    for key in ("fmt", "salt", "uid", "prv", "tok"):
        assert re.search(rf"\b{key}\b", body), (
            f"TS twin envelope missing {key!r} field"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Behavioural parity via Node subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Each fixture: { kind: "encrypt"|"decrypt"|"envelope_shape",
#                 inputs: ..., expect: "ok"|"<ErrorName>" }
#
# `envelope_shape` runs encrypt then unwraps via internal helper to
# verify the JSON envelope has the right key set.
BEHAVIOUR_FIXTURES: Mapping[str, dict[str, Any]] = {
    "round_trip_google": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "google",
        "plaintext": "ya29.googletoken-abc-12345-xyz",
        "expect": "ok",
    },
    "round_trip_github": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "github",
        "plaintext": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "expect": "ok",
    },
    "round_trip_apple": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "apple",
        "plaintext": "a.aapl.access.token.value",
        "expect": "ok",
    },
    "round_trip_microsoft": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "microsoft",
        "plaintext": "EwBwA0X2A.ms-token-value",
        "expect": "ok",
    },
    "round_trip_discord": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "discord",
        "plaintext": "discord-access-token-80351110224678912",
        "expect": "ok",
    },
    "round_trip_gitlab": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "gitlab",
        "plaintext": "gitlab-access-token-glpat-like-value",
        "expect": "ok",
    },
    "round_trip_bitbucket": {
        "kind": "round_trip",
        "userId": "user-1",
        "provider": "bitbucket",
        "plaintext": "bitbucket-access-token-app-password-like-value",
        "expect": "ok",
    },
    "provider_case_normalised": {
        "kind": "round_trip",
        "userId": "u1",
        "provider": " GitHub ",
        "decryptProvider": "github",
        "plaintext": "ghp_case_norm_value",
        "expect": "ok",
    },
    "binding_mismatch_user": {
        "kind": "decrypt_with_swap",
        "encryptUserId": "user-a",
        "decryptUserId": "user-b",
        "provider": "google",
        "plaintext": "secret-a",
        "expect": "BindingMismatchError",
    },
    "binding_mismatch_provider": {
        "kind": "decrypt_with_swap",
        "encryptUserId": "user-a",
        "decryptUserId": "user-a",
        "encryptProvider": "google",
        "decryptProvider": "github",
        "plaintext": "tok",
        "expect": "BindingMismatchError",
    },
    "unsupported_provider_encrypt": {
        "kind": "encrypt",
        "userId": "u1",
        "provider": "facebook",
        "plaintext": "tok",
        "expect": "UnsupportedProviderError",
    },
    "unsupported_provider_decrypt": {
        "kind": "decrypt_after_encrypt",
        "userId": "u1",
        "encryptProvider": "google",
        "decryptProvider": "facebook",
        "plaintext": "tok",
        "expect": "UnsupportedProviderError",
    },
    "empty_provider_encrypt": {
        "kind": "encrypt",
        "userId": "u1",
        "provider": "",
        "plaintext": "tok",
        "expect": "UnsupportedProviderError",
    },
    "empty_plaintext": {
        "kind": "encrypt",
        "userId": "u1",
        "provider": "google",
        "plaintext": "",
        "expect": "TokenVaultError",
    },
    "empty_user_id": {
        "kind": "encrypt",
        "userId": "",
        "provider": "google",
        "plaintext": "tok",
        "expect": "TokenVaultError",
    },
    "unknown_key_version_decrypt": {
        "kind": "decrypt_bad_key_version",
        "userId": "u1",
        "provider": "google",
        "plaintext": "tok",
        "fakeKeyVersion": 2,
        "expect": "UnknownKeyVersionError",
    },
    "zero_key_version_decrypt": {
        "kind": "decrypt_bad_key_version",
        "userId": "u1",
        "provider": "google",
        "plaintext": "tok",
        "fakeKeyVersion": 0,
        "expect": "UnknownKeyVersionError",
    },
    "tampered_ciphertext": {
        "kind": "decrypt_tampered",
        "userId": "u1",
        "provider": "google",
        "plaintext": "tok-12345",
        "expect": "CiphertextCorruptedError",
    },
    "envelope_shape": {
        "kind": "envelope_shape",
        "userId": "user-shape",
        "provider": "google",
        "plaintext": "tok-shape-test",
        "expect": "ok",
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Python-side driver — exercises the same fixtures against the Python vault
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _python_run_fixture(fx: dict[str, Any]) -> dict[str, Any]:
    kind = fx["kind"]
    try:
        if kind == "round_trip":
            enc = tv.encrypt_for_user(
                fx["userId"], fx["provider"], fx["plaintext"]
            )
            dec_provider = fx.get("decryptProvider", fx["provider"])
            dec = tv.decrypt_for_user(fx["userId"], dec_provider, enc)
            return {
                "ok": True,
                "matches": dec == fx["plaintext"],
                "envelopeKeys": ["ciphertext", "keyVersion"],
            }
        if kind == "encrypt":
            tv.encrypt_for_user(fx["userId"], fx["provider"], fx["plaintext"])
            return {"ok": True}
        if kind == "decrypt_with_swap":
            enc_user = fx["encryptUserId"]
            dec_user = fx["decryptUserId"]
            enc_prv = fx.get("encryptProvider", fx.get("provider"))
            dec_prv = fx.get("decryptProvider", fx.get("provider"))
            enc = tv.encrypt_for_user(enc_user, enc_prv, fx["plaintext"])
            tv.decrypt_for_user(dec_user, dec_prv, enc)
            return {"ok": True}
        if kind == "decrypt_after_encrypt":
            enc = tv.encrypt_for_user(
                fx["userId"], fx["encryptProvider"], fx["plaintext"]
            )
            tv.decrypt_for_user(fx["userId"], fx["decryptProvider"], enc)
            return {"ok": True}
        if kind == "decrypt_bad_key_version":
            enc = tv.encrypt_for_user(
                fx["userId"], fx["provider"], fx["plaintext"]
            )
            fake = tv.EncryptedToken(
                ciphertext=enc.ciphertext, key_version=fx["fakeKeyVersion"]
            )
            tv.decrypt_for_user(fx["userId"], fx["provider"], fake)
            return {"ok": True}
        if kind == "decrypt_tampered":
            enc = tv.encrypt_for_user(
                fx["userId"], fx["provider"], fx["plaintext"]
            )
            # Flip the last two chars to break the authenticated payload.
            tail = "AA" if not enc.ciphertext.endswith("AA") else "BB"
            tampered = tv.EncryptedToken(
                ciphertext=enc.ciphertext[:-2] + tail, key_version=enc.key_version
            )
            tv.decrypt_for_user(fx["userId"], fx["provider"], tampered)
            return {"ok": True}
        if kind == "envelope_shape":
            from backend.security import envelope as tenant_envelope

            enc = tv.encrypt_for_user(
                fx["userId"], fx["provider"], fx["plaintext"]
            )
            outer = json.loads(enc.ciphertext)
            envelope = json.loads(
                tenant_envelope.decrypt(
                    outer["ciphertext"],
                    tenant_envelope.TenantDEKRef.from_dict(outer["dek_ref"]),
                )
            )
            return {
                "ok": True,
                "envelopeKeys": sorted(envelope.keys()),
                "fmt": envelope.get("fmt"),
                "uid": envelope.get("uid"),
                "prv": envelope.get("prv"),
                "tok": envelope.get("tok"),
                "saltLen": (
                    len(envelope["salt"])
                    if isinstance(envelope.get("salt"), str)
                    else None
                ),
            }
        raise AssertionError(f"unknown fixture kind: {kind!r}")
    except tv.TokenVaultError as e:
        return {"ok": False, "errorName": type(e).__name__}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node driver — runs every fixture against the TS vault, returns JSON
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_NODE_DRIVER = """
import {
  TokenVault,
  generateMasterKey,
} from __TWIN_PATH__
import { readFileSync } from "node:fs"

const fixtures = JSON.parse(readFileSync(0, "utf-8"))
const out = {}

const masterKey = await generateMasterKey()
const vault = new TokenVault(masterKey)

// canonicalJsonStringify is a private helper inside the module — we
// reproduce the envelope-shape decoder here by decrypting the ciphertext
// directly through Web Crypto with the same key + IV slice. The TS
// twin's envelope MUST be a JSON object and have the same key set.
async function decryptRawEnvelope(ciphertext) {
  const std = ciphertext.replace(/-/g, "+").replace(/_/g, "/")
  const padded = std + "=".repeat((4 - (std.length % 4)) % 4)
  const bin = Buffer.from(padded, "base64")
  const iv = bin.subarray(0, 12)
  const enc = bin.subarray(12)
  const exported = await crypto.subtle.exportKey("raw", masterKey)
  const k = await crypto.subtle.importKey(
    "raw", exported, { name: "AES-GCM", length: 256 }, true, ["decrypt"],
  )
  const buf = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, k, enc)
  return JSON.parse(new TextDecoder().decode(buf))
}

for (const [key, fx] of Object.entries(fixtures)) {
  try {
    const kind = fx.kind
    if (kind === "round_trip") {
      const enc = await vault.encryptForUser(fx.userId, fx.provider, fx.plaintext)
      const decProvider = fx.decryptProvider ?? fx.provider
      const dec = await vault.decryptForUser(fx.userId, decProvider, enc)
      out[key] = {
        ok: true,
        matches: dec === fx.plaintext,
        envelopeKeys: ["ciphertext", "keyVersion"],
      }
    } else if (kind === "encrypt") {
      await vault.encryptForUser(fx.userId, fx.provider, fx.plaintext)
      out[key] = { ok: true }
    } else if (kind === "decrypt_with_swap") {
      const encPrv = fx.encryptProvider ?? fx.provider
      const decPrv = fx.decryptProvider ?? fx.provider
      const enc = await vault.encryptForUser(fx.encryptUserId, encPrv, fx.plaintext)
      await vault.decryptForUser(fx.decryptUserId, decPrv, enc)
      out[key] = { ok: true }
    } else if (kind === "decrypt_after_encrypt") {
      const enc = await vault.encryptForUser(fx.userId, fx.encryptProvider, fx.plaintext)
      await vault.decryptForUser(fx.userId, fx.decryptProvider, enc)
      out[key] = { ok: true }
    } else if (kind === "decrypt_bad_key_version") {
      const enc = await vault.encryptForUser(fx.userId, fx.provider, fx.plaintext)
      const fake = { ciphertext: enc.ciphertext, keyVersion: fx.fakeKeyVersion }
      await vault.decryptForUser(fx.userId, fx.provider, fake)
      out[key] = { ok: true }
    } else if (kind === "decrypt_tampered") {
      const enc = await vault.encryptForUser(fx.userId, fx.provider, fx.plaintext)
      const tail = enc.ciphertext.endsWith("AA") ? "BB" : "AA"
      const tampered = { ciphertext: enc.ciphertext.slice(0, -2) + tail, keyVersion: enc.keyVersion }
      await vault.decryptForUser(fx.userId, fx.provider, tampered)
      out[key] = { ok: true }
    } else if (kind === "envelope_shape") {
      const enc = await vault.encryptForUser(fx.userId, fx.provider, fx.plaintext)
      const envelope = await decryptRawEnvelope(enc.ciphertext)
      out[key] = {
        ok: true,
        envelopeKeys: Object.keys(envelope).sort(),
        fmt: envelope.fmt,
        uid: envelope.uid,
        prv: envelope.prv,
        tok: envelope.tok,
        saltLen: typeof envelope.salt === "string" ? envelope.salt.length : null,
      }
    } else {
      out[key] = { ok: false, errorName: "Error", message: `unknown kind ${kind}` }
    }
  } catch (e) {
    out[key] = {
      ok: false,
      errorName: (e && e.constructor && e.constructor.name) || "Error",
      message: String((e && e.message) || e),
    }
  }
}

process.stdout.write(JSON.stringify(out))
"""


def _run_ts_driver(fixtures: Mapping[str, Any]) -> dict[str, Any]:
    twin_path_literal = json.dumps(str(_TS_TWIN_PATH))
    # Use sentinel substitution rather than %-format because the driver's
    # JS body contains `%` (modulo operator) that would clash with
    # Python's printf-style placeholders.
    driver_src = _NODE_DRIVER.replace("__TWIN_PATH__", twin_path_literal)

    cmd = [
        "node",
        "--no-warnings",
        "--experimental-strip-types",
        "--input-type=module",
        "--eval",
        driver_src,
    ]

    payload = json.dumps(dict(fixtures))
    env = dict(os.environ)
    env["TZ"] = "UTC"

    r = subprocess.run(
        cmd,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Node TS driver exited {r.returncode}\n"
            f"stdout={r.stdout!r}\n"
            f"stderr={r.stderr!r}"
        )
    return json.loads(r.stdout)


@pytest.fixture(scope="session")
def ts_vault_results() -> dict[str, Any]:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    if not _node_supports_strip_types():
        pytest.skip(
            "node ≥22 with --experimental-strip-types not available; "
            "TS twin behaviour cannot be exercised"
        )
    return _run_ts_driver(BEHAVIOUR_FIXTURES)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2a — Per-fixture behavioural parity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "name", sorted(BEHAVIOUR_FIXTURES.keys()), ids=lambda n: n
)
def test_behaviour_parity_python_ts(
    name: str, ts_vault_results: dict[str, Any]
) -> None:
    """For every fixture, both vaults must produce the same outcome.

    On success: both ``ok=True`` and (where applicable)
    ``envelopeKeys`` agree.
    On failure: both raise the same typed error class.
    """
    fx = BEHAVIOUR_FIXTURES[name]
    py = _python_run_fixture(fx)
    ts = ts_vault_results[name]

    if fx["expect"] == "ok":
        assert py.get("ok"), f"Python failed unexpectedly on {name!r}: {py}"
        assert ts.get("ok"), f"TS failed unexpectedly on {name!r}: {ts}"
        if fx["kind"] == "round_trip":
            assert py.get("matches") is True, (
                f"Python round_trip mismatch on {name!r}: {py}"
            )
            assert ts.get("matches") is True, (
                f"TS round_trip mismatch on {name!r}: {ts}"
            )
        if fx["kind"] == "envelope_shape":
            # Both sides emit the exact same envelope key set.
            assert py["envelopeKeys"] == ["fmt", "prv", "salt", "tok", "uid"], (
                f"Python envelope keys drifted on {name!r}: {py['envelopeKeys']}"
            )
            assert ts["envelopeKeys"] == py["envelopeKeys"], (
                f"envelope-key drift on {name!r}: "
                f"Python={py['envelopeKeys']}, TS={ts['envelopeKeys']}"
            )
            # Identity fields round-trip identically on both sides.
            assert py["fmt"] == ts["fmt"] == tv.BINDING_FORMAT_VERSION
            assert py["uid"] == ts["uid"] == fx["userId"]
            assert py["prv"] == ts["prv"] == fx["provider"]
            assert py["tok"] == ts["tok"] == fx["plaintext"]
            # Salt is a base64-encoded 16-byte value → 24 chars
            # standard b64 (with `=` padding) on both sides.
            assert py["saltLen"] == ts["saltLen"] == 24, (
                f"salt length drift on {name!r}: "
                f"Python={py['saltLen']}, TS={ts['saltLen']}"
            )
    else:
        # Failure case — both must raise, both with the same class name.
        assert not py.get("ok"), (
            f"Python failed to raise on {name!r}; expected {fx['expect']!r}"
        )
        assert not ts.get("ok"), (
            f"TS failed to raise on {name!r}; expected {fx['expect']!r}, got {ts}"
        )
        assert py["errorName"] == fx["expect"], (
            f"Python raised {py['errorName']!r} on {name!r}, "
            f"expected {fx['expect']!r}"
        )
        assert ts["errorName"] == fx["expect"], (
            f"TS raised {ts['errorName']!r} on {name!r}, "
            f"expected {fx['expect']!r}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2b — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalize_outcome(d: Mapping[str, Any]) -> dict[str, Any]:
    """Project each fixture result to the comparable shape (drop fields
    that legitimately differ — e.g. salt bytes, error message strings).
    """
    out: dict[str, Any] = {"ok": d.get("ok", False)}
    if d.get("ok"):
        if "matches" in d:
            out["matches"] = d["matches"]
        if "envelopeKeys" in d:
            out["envelopeKeys"] = list(d["envelopeKeys"])
        for k in ("fmt", "uid", "prv", "tok", "saltLen"):
            if k in d:
                out[k] = d[k]
    else:
        out["errorName"] = d.get("errorName")
    return out


def test_aggregate_sha256_parity_python_ts(
    ts_vault_results: dict[str, Any]
) -> None:
    """One hash over every fixture's normalised outcome.

    Catches the "many-tiny-drifts" failure mode that per-fixture tests
    can't summarise — if 3 fixtures drift by 1 character each, this
    oracle yields a single short error message that's easier to triage
    than 3 long ``==`` diffs.
    """
    import hashlib

    py_blob: dict[str, Any] = {}
    ts_blob: dict[str, Any] = {}
    for key in sorted(BEHAVIOUR_FIXTURES.keys()):
        py_blob[key] = _normalize_outcome(_python_run_fixture(BEHAVIOUR_FIXTURES[key]))
        ts_blob[key] = _normalize_outcome(ts_vault_results[key])

    py_hash = hashlib.sha256(
        json.dumps(py_blob, sort_keys=True).encode("utf-8")
    ).hexdigest()
    ts_hash = hashlib.sha256(
        json.dumps(ts_blob, sort_keys=True).encode("utf-8")
    ).hexdigest()

    assert py_hash == ts_hash, (
        "aggregate token-vault behaviour drift between Python and TS twin\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}\n"
        "  (run per-fixture tests for which scenario drifted)"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Coverage guard (each provider exercised)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_supported_provider_has_round_trip_fixture() -> None:
    """Every provider in the whitelist must have a parity fixture.
    Catches "added a provider but forgot the parity check" the same
    way AS.1.5 catches "added a 12th vendor but forgot the fixture".
    """
    covered = {
        fx["provider"].strip().lower()
        for fx in BEHAVIOUR_FIXTURES.values()
        if fx["kind"] == "round_trip"
    }
    missing = tv.SUPPORTED_PROVIDERS - covered
    assert not missing, (
        f"AS.2.3 drift guard missing round-trip fixture for: {sorted(missing)}"
    )
