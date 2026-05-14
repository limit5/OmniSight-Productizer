# OP-1096 test L1 keypair

The test L1 keypair was generated once by `gpg --batch --gen-key` on
2026-05-14 with these parameters:

```text
%no-protection
Key-Type: eddsa
Key-Curve: ed25519
Key-Usage: sign
Name-Real: OmniSight OP-1096 Test L1
Name-Comment: canonical roster fixture signing key
Name-Email: test-l1-op-1096@example.invalid
Expire-Date: 0
%commit
```

Public key fingerprint:

```text
F995558736203E70E98F29175464E20846F314B2
```

The private key is not committed. It existed only in the original
generator's temporary keyring and was used once to sign
`sample-roster.yaml` into `sample-roster.yaml.asc`. After that one signing
event, the temporary keyring was destroyed.

The signature in this repository is the canonical test signature. Tests
must not regenerate it; they verify against the committed `.asc` file
using the committed public key.

Do not use this keypair in production. Any roster signed by this
fingerprint must be rejected by production runners because the production
L1 fingerprint differs.
