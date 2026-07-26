#!/usr/bin/env python3
"""Assert a restored database's encrypted columns can actually be decrypted (OP-2760).

The hole this exists to close
-----------------------------
`git_accounts.encrypted_token` and `llm_credentials.encrypted_value` are envelope
ciphertext: `{fmt, ciphertext, dek_ref}`, where the DEK inside `dek_ref` is
wrapped by a KEK. That KEK is `/app/data/.secret_key` — auto-generated because
`OMNISIGHT_SECRET_KEY` is unset — and it lives on a docker volume that **no
backup lane captures**. So every artefact from every lane restores to a database
whose every credential is permanently unreadable, and the existing DR drill
cannot notice: it asserts table count, `alembic_version` and a non-empty
`audit_log`, none of which touch an encrypted column.

Why this must NOT use the live key
----------------------------------
The obvious implementation — decrypt using the running container's
`/app/data/.secret_key` — would pass every night and prove nothing, because in
the disaster this exists to model **the host is gone**. A drill that borrows
live state to verify a backup is measuring the wrong system. So the KEK must
come from the *backup set* (`--kek-from escrow`), and `--kek-from live` exists
only to demonstrate the difference and is refused in the drill.

Exit codes: 0 decryptable, 1 not decryptable / escrow missing, 2 usage error.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys

PG_CONTAINER = os.environ.get("PG_CONTAINER", "omnisight-pg-primary")
PG_USER = os.environ.get("PG_USER", "omnisight")


def die(msg: str, code: int = 1) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(code)


def psql(db: str, sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db, "-tAc", sql],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if out.returncode != 0:
        die(f"psql failed on {db}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def load_kek(source: str, escrow_path: str | None) -> bytes:
    if source == "live":
        # Deliberately available, deliberately refused by the drill: see module
        # docstring. Present so the difference can be demonstrated, not relied on.
        out = subprocess.run(
            ["docker", "exec", "omnisight-productizer-backend-a-1",
             "cat", "/app/data/.secret_key"],
            capture_output=True, timeout=30, check=False,
        )
        if out.returncode != 0:
            die("cannot read the live KEK (this is the DR condition, by the way)")
        return out.stdout.strip()

    if not escrow_path:
        die("--escrow is required with --kek-from escrow", 2)
    if not os.path.exists(escrow_path):
        die(f"no KEK escrow artefact at {escrow_path}. The backup set does not "
            "contain the key that decrypts its own credential columns, so this "
            "restore is NOT fully restorable. This is the OP-2760 condition.")
    try:
        with open(escrow_path, "rb") as fh:
            data = fh.read().strip()
    except OSError as exc:
        die(f"cannot read escrow artefact {escrow_path}: {exc}")
    if not data:
        die(f"escrow artefact {escrow_path} is empty")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="restored database name")
    ap.add_argument("--kek-from", choices=["escrow", "live"], default="escrow")
    ap.add_argument("--escrow", default=os.environ.get("OMNISIGHT_KEK_ESCROW_PATH"))
    args = ap.parse_args()

    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        die("python cryptography is unavailable; cannot verify decryptability", 2)

    # Pick a real envelope from the RESTORED database.
    row = psql(args.db,
               "select encrypted_token from git_accounts "
               "where encrypted_token like '{%' limit 1;")
    if not row:
        row = psql(args.db,
                   "select encrypted_value from llm_credentials "
                   "where encrypted_value <> '' limit 1;")
    if not row:
        print("SKIP-CLEAN: restored DB holds no encrypted credential rows; "
              "nothing to prove here (this is not a pass, it is an empty set)")
        return 0

    try:
        env = json.loads(row)
    except json.JSONDecodeError:
        die("credential column is not the expected envelope JSON")

    dek_ref = env.get("dek_ref") or {}
    wrapped = dek_ref.get("wrapped_dek_b64")
    ciphertext = env.get("ciphertext")
    if not wrapped or not ciphertext:
        die(f"envelope missing wrapped_dek_b64/ciphertext (keys: {sorted(env)})")

    kek = load_kek(args.kek_from, args.escrow)
    # Unwrapping is three layers, not one: Fernet(KEK) yields a JSON payload
    # {"ctx", "dek_b64"} (kms_adapters.LocalFernetKMSAdapter.wrap_dek), and the
    # AES key is the base64-decoded dek_b64 inside it. Decrypting to "a 172-byte
    # blob starting with {" is the signature of stopping one layer early.
    try:
        payload = json.loads(Fernet(kek).decrypt(base64.b64decode(wrapped)))
        dek = base64.b64decode(payload["dek_b64"].encode("ascii"))
    except (InvalidToken, ValueError, TypeError, KeyError) as exc:
        die(f"KEK from {args.kek_from!r} cannot unwrap the DEK ({type(exc).__name__}) "
            "— the restored credentials are unreadable")
    # The ciphertext field is itself an envelope: AES-GCM with a nonce and AAD,
    # not a bare Fernet token. The AAD is rebuilt from the inner envelope's own
    # alg/dek/fmt/tid, matching backend/security/envelope.py::_aad exactly — an
    # AAD mismatch fails identically to a wrong key, so getting this wrong would
    # have produced an assertion that could never pass.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        die("cryptography AESGCM unavailable", 2)
    try:
        inner = json.loads(ciphertext)
        aad = json.dumps({"alg": inner["alg"], "dek": inner["dek"],
                          "fmt": inner["fmt"], "tid": inner["tid"]},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
        AESGCM(dek).decrypt(base64.b64decode(inner["nonce_b64"]),
                            base64.b64decode(inner["ciphertext_b64"]), aad)
    except KeyError as exc:
        die(f"inner ciphertext envelope missing {exc}")
    except Exception as exc:  # noqa: BLE001
        die(f"DEK unwrapped but the ciphertext will not decrypt ({type(exc).__name__})")

    print(f"OK: a real credential envelope in {args.db} decrypts end-to-end "
          f"using the KEK from {args.kek_from!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
