# GPG Release-Signer Setup (FX.9.8)

**Audience:** the operator who runs `scripts/deploy-prod.sh` against the
production WSL host.

**Why this exists:** FX.7.9 landed the deploy-ref allowlist + GPG signer
gate, but `deploy/prod-deploy-signers.txt` shipped with zero entries —
so every deploy required `--insecure-skip-verify` (a deliberately loud
escape hatch). FX.9.8 provisions the first real release-signing key,
adds its fingerprint to the signers file, and signs `main` so future
deploys go through real verification.

This runbook covers (a) first-time setup, (b) re-key / rotation, and
(c) revocation.

---

## 1. First-time setup

### 1.1 Generate the operator key

A passphrase-less ed25519 sign-only key, 2-year expiry. Passphrase-less
because the deploy host signs and verifies on the same machine; the
threat model is "compromised host = compromised signing capability"
either way, and an interactive passphrase blocks unattended deploys.

Use the helper script (idempotent — refuses to overwrite an existing
release-signer key):

```bash
./scripts/setup_release_signer.sh \
    --name "Your Name" \
    --email "you@example.com"
```

Or, equivalent manual sequence:

```bash
cat > /tmp/keygen.txt <<EOF
%no-protection
Key-Type: EDDSA
Key-Curve: ed25519
Key-Usage: sign
Name-Real: Your Name
Name-Email: you@example.com
Name-Comment: OmniSight prod deploy release-signer (FX.9.8)
Expire-Date: 2y
%commit
EOF
gpg --batch --pinentry-mode loopback --generate-key /tmp/keygen.txt
shred -u /tmp/keygen.txt
```

### 1.2 Capture the fingerprint

```bash
gpg --list-secret-keys --with-colons you@example.com \
    | awk -F: '/^fpr:/ {print $10; exit}'
# → 40 hex chars, e.g. 50245609D5BF1E14CA7AD5AF18BC2AB5FDDD932A
```

### 1.3 Add to the signers list (committed)

Append the fingerprint as a new line in
`deploy/prod-deploy-signers.txt`. Keep the comment block above
`=== RELEASE SIGNERS ===` intact.

Also export the public key into the bundled keyring file
`deploy/release-signers.asc` so other operators (or a future cold-spare
prod host) can import it:

```bash
gpg --armor --export <FPR> >> deploy/release-signers.asc
```

If `release-signers.asc` already exists, regenerate it from scratch
including all current signers' public keys (otherwise the export will
concatenate, which gpg accepts but gets messy):

```bash
gpg --armor --export $(awk '!/^#/ && NF {print $1}' \
    deploy/prod-deploy-signers.txt) > deploy/release-signers.asc
```

### 1.4 Configure git to sign by default

```bash
git config --global user.signingkey <FPR>
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

The `commit.gpgsign=true` global means *every* commit you make in *any*
repo is signed. That's intentional: the gate is "is this signer
trusted", and every commit by this operator should be signed even
outside this repo.

### 1.5 Sign `main` tip

The deploy gate verifies the signature on `origin/main`'s tip
commit. The tip becomes signed automatically the next time you commit
on main with `commit.gpgsign=true` set. To force-sign immediately
without a content change, use an empty commit:

```bash
git commit --allow-empty -S -m "chore(release): sign main tip (FX.9.8)"
git push origin main
```

In the FX.9.8 change itself the very commit that adds the fingerprint
to `prod-deploy-signers.txt` is signed and becomes the new main tip,
so this step is implicit on first setup.

### 1.6 Verify the deploy gate accepts without `--insecure-skip-verify`

Pre-push self-test (local):

```bash
git verify-commit main
# expect: "Good signature from <your name+email>"
```

After pushing, full end-to-end verify against origin:

```bash
./scripts/check_deploy_ref.sh --kind branch --ref main
# expect:
#   ✅ Layer 1: ref 'branch:main' matched allowlist
#   ✅ Layer 2: 'branch:origin/main' signed by trusted fingerprint <FPR>
```

Then run a full dry-run deploy:

```bash
./scripts/deploy-prod.sh --dry-run
# Should NOT print the loud "OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY=1"
# warning. (--dry-run only runs Layer 1 of the verifier — for full
# Layer 2 verification, run check_deploy_ref.sh directly as above.)
```

### 1.7 Revoke the old escape-hatch habit

Remove any operator-side aliases / shell history shortcuts that pass
`--insecure-skip-verify`. From this point onwards, the flag is
reserved for genuine emergencies (key rotation gap, lost passphrase,
prod host rebuild without keyring restore) — every use should be
audit-trailed in the post-deploy review.

---

## 2. Re-key / rotation

The operator key has a 2-year expiry. About 3 months before expiry:

1. Generate the new key with `./scripts/setup_release_signer.sh
   --name "..." --email "..." --allow-existing` (the `--allow-existing`
   flag bypasses the safety check).
2. Append the new fingerprint to `deploy/prod-deploy-signers.txt` —
   *do not remove the old fingerprint yet*. During the overlap window
   commits signed by either key are accepted.
3. Re-export `deploy/release-signers.asc` to include both keys (see
   §1.3 above).
4. Update `git config --global user.signingkey <NEW_FPR>` so new
   commits use the new key.
5. After 1 week of overlap and a successful deploy on the new key,
   remove the old fingerprint from `prod-deploy-signers.txt` and from
   `release-signers.asc`. Run `gpg --delete-secret-keys <OLD_FPR>` and
   `gpg --delete-keys <OLD_FPR>` to purge from the local keyring.

## 3. Revocation (key compromised)

If the operator key is suspected compromised:

1. Immediately remove the fingerprint from
   `deploy/prod-deploy-signers.txt` (PR + merge ASAP — every minute
   the line stays, an attacker with the leaked key can sign a tag and
   deploy).
2. Generate the revocation certificate (auto-saved by gpg at key gen
   time under `~/.gnupg/openpgp-revocs.d/<FPR>.rev`):
   ```bash
   gpg --import ~/.gnupg/openpgp-revocs.d/<FPR>.rev
   ```
3. Push the revoked public key to any keyserver where it was uploaded
   (skip if you never uploaded — the FX.9.8 setup does not upload).
4. Generate a fresh release-signer key per §1, deploy a signed empty
   commit to main, verify deploys gate correctly with new key.

## 4. Cold-spare prod host bring-up

The signing key must NOT travel — only public keys. On a fresh prod
WSL:

```bash
git clone <repo>
cd OmniSight-Productizer
gpg --import deploy/release-signers.asc
./scripts/check_deploy_ref.sh --kind branch --ref main
# Layer 2 should pass: the spare host now trusts the same operator key
# without holding the private half.
```

The spare host can VERIFY but not SIGN. Signing remains exclusive to
the operator's primary host (the one holding `~/.gnupg/private-keys-v1.d/`).
