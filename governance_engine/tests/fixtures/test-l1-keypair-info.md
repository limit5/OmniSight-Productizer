# OP-1096 Test L1 Keypair

This fixture keypair was generated once on 2026-05-14 with:

```sh
gpg --batch --homedir /tmp/op1096-gnupg --passphrase '' \
  --quick-generate-key 'OmniSight OP-1096 Test L1 <op-1096-l1@example.invalid>' \
  ed25519 sign 0
```

Public key fingerprint:

```text
DB05223CD1CECB4DA69C7C02011FEE752BFEDBB3
```

The armored public key is committed as `test-l1-public-key.asc`. Tests import
that public key into an isolated `GNUPGHOME` and use this fingerprint as the
trusted L1 value.

The private key is not committed. It existed only in the temporary generator
keyring and was used once to sign `sample-roster.yaml` into
`sample-roster.yaml.asc`; after signing, the temporary keyring was destroyed.

The detached signature committed in `sample-roster.yaml.asc` is the canonical
test signature. Tests must not regenerate it.

Do not use this keypair in production. Any roster signed by this fingerprint
must be rejected by production runners because the production L1 fingerprint is
different.
