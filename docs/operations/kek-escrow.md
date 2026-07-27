# KEK escrow — making prod backups actually restorable

**Ticket:** OP-2760 · **Blocks:** OP-2731 F1

## The hole

`git_accounts.encrypted_token`, `llm_credentials.encrypted_value` and every
wrapped DEK are decryptable only with `/app/data/.secret_key` — a Fernet key the
app **auto-generates** because `OMNISIGHT_SECRET_KEY` is unset, living on docker
volume `omnisight-productizer_omnisight-data`.

**No backup lane captured it.** All three only `pg_dump` the database. So every
artefact from every lane restored to a database whose every credential was
permanently unreadable — and the DR drill could not tell, because it asserted
table count, `alembic_version` and a non-empty `audit_log`, none of which touch
an encrypted column.

## Why asymmetric

Escrow has a built-in tension: DR wants the key to survive the host;
confidentiality wants whoever can read the backups **not** to be able to read
the key. A symmetric passphrase resolves that only by *asking* an operator to
keep a copy elsewhere — and `backup-dr.env` already shows how that ends: its
header asks exactly that, and nothing verifies it ever happened.

So this host holds **only the public half** (`848A5F172FD988B6`, RSA-4096,
encrypt-only). It can write an escrow artefact and is **structurally incapable
of reading one back**. "Please keep the private key somewhere else" stops being
a request and becomes a property of the system.

The trade, stated plainly: **if the off-host private key is lost, the escrow
artefacts are unrecoverable.** That is why the round trip was proven *before*
the private key was moved, not after.

## Proven, not assumed

| check | result |
|---|---|
| host writes an escrow artefact | 656 bytes, recipient `848A5F172FD988B6` |
| host decrypts its own escrow | **refused** — `decryption failed: No secret key` |
| off-host private key recovers it | recovered KEK **matches the live KEK byte-for-byte** |
| escrow script with a secret key present | refuses to run — that state defeats the design |
| routine check, wrong expected keyid | fails |
| routine check, stale artefact | fails |
| routine check, no artefact at all | fails |

## Routine check vs. proof of recoverability — do not conflate them

`omnisight-kek-escrow-check.sh` runs **without** the private key, by design. It
proves the artefact **exists, is recent, and is addressed to the expected
recipient** — catching the realistic silent failure, escrow quietly stopping.

A routine PASS means *"escrow is running"*. It never means *"the backups are
recoverable"*. Recoverability is proven only by an operator rehearsal with the
off-host private key.

## Operator rehearsal

```sh
# on a machine that holds the private key, NOT this host
gpg --import <private key>
gpg --decrypt kek-escrow-<ts>.gpg > recovered.key
# then, against a restored database:
omnisight-verify-restorable-secrets.py --db <restored-db> \
    --kek-from escrow --escrow recovered.key
```

`omnisight-verify-restorable-secrets.py` deliberately refuses to use the live
container's key for this: decrypting with the running host's KEK would pass every
night while proving nothing, because the disaster being modelled is one where
the host is gone. A drill that borrows live state to verify a backup measures
the wrong system.

The unwrap chain it walks is three layers, and getting it wrong fails
identically to a wrong key — so each layer was verified against the live key
first: `Fernet(KEK)` → JSON `{"ctx","dek_b64"}` → base64 → AES-GCM with a nonce
and an AAD rebuilt from the inner envelope's own `alg`/`dek`/`fmt`/`tid`.

## Schedule

`omnisight-kek-escrow.timer`, daily 02:40 — after lane A's 02:17 so the escrow
sits beside a fresh artefact, clear of the 04:30 drill. The unit runs the escrow
**and** the routine check, so "escrow stopped" and "escrow is addressed to a key
nobody holds" both surface as one unit failure rather than needing a second
thing to be watched. `OnFailure=` routes to the OP-2728 channel.

## Still open

The escrow artefacts are local-only, like lane A's. They travel off-site when
OP-2731 F1 lands. Until then a total host loss still loses them — which is
exactly why F1 is gated on this ticket rather than the other way round.
