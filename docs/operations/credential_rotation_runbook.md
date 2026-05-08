# Credential Rotation Runbook

> Companion to `configs/credentials.yaml` (inventory) and
> `scripts/credential_expiry_check.py` (daily 30 / 7 / 1-day alert ladder).
>
> Tracking: OP-727 (this runbook is the T7 deliverable referenced from
> the OP-721 META).
>
> Audience: on-call platform operator triaging an expiry alert.

## How to use this runbook

1. The daily cron (`scripts/credential_expiry_check.py`) fires an alert
   tagged with the credential `id` and `type`. The alert body includes
   a link back to this file.
2. Find the `type`-specific section below.
3. Follow the rotation steps for that type.
4. **Update the inventory** (`configs/credentials.yaml`) — bump
   `expiry` to the new policy date and `last_rotated_at` to today.
   Without this step the alert keeps firing.
5. Re-run the check: `python3 scripts/credential_expiry_check.py`.
   Alert MUST clear (the AC2 contract).

If a step fails or the rotation is blocked (e.g. SSO admin unavailable),
post in `#oncall` and snooze the alert by editing `expiry` to a
near-future date with a `notes:` line citing the blocker — do NOT remove
the entry, do NOT set `expiry: never` to silence it.

## Severity ladder

| Days until expiry | Severity   | Exit code | Action                 |
|-------------------|-----------|-----------|------------------------|
| > 30              | ok        | 0         | none                   |
| 8 – 30            | warning   | 1         | schedule rotation      |
| 2 – 7             | urgent    | 2         | rotate this week       |
| 0 – 1             | critical  | 3         | rotate immediately     |
| ≤ 0 (already lapsed) | expired   | 3         | rotate + incident note |

`expiry: never` entries are skipped silently. Do not use `never` to
opt out of alerting on a credential that *does* have a policy expiry
— that hides exactly the failure mode this runbook exists to catch.

## Pre-rotation checklist (all types)

- [ ] You can authenticate to the issuing system as an admin (SSO,
      console root, etc.). If not, escalate before generating a new
      credential.
- [ ] You know the consumers of the credential (env var, vault path,
      DB row). The `secret_ref` field in the inventory is the source
      of truth — if it's stale, fix it.
- [ ] Plan the deploy: most rotations require a service reload to
      pick up the new value. Schedule outside the freeze window when
      possible.

---

## jira-tokens

**What it is**: Atlassian Cloud API token used by runner bots
(`claude-bot`, `codex-bot`) to read/transition tickets and post
comments via `backend/agents/jira_dispatch.py`.

**Policy expiry**: configurable per Atlassian admin; default 12 months.
Set the inventory `expiry` to the value reported by the Atlassian
admin console, not a guess.

**Rotation steps**

1. Sign in to <https://id.atlassian.com/manage-profile/security/api-tokens>
   as the bot's service account.
2. Click **Create API token**, name it `<bot>-rotated-YYYY-MM-DD`.
3. Copy the new token value once — Atlassian will not show it again.
4. Update the storage location in `secret_ref`:
   - `env:JIRA_TOKEN_<BOT>` → update the deployment env file
     (`.env.production`) and reload the affected services.
   - `db:git_accounts.encrypted_token[<bot>]` → use the
     `POST /git-accounts` API to overwrite the value (see
     `backend/security/credential_vault.py`).
5. Verify: run a smoke `GET /myself` against JIRA with the new token.
6. Revoke the old token in the Atlassian console.
7. **Update the inventory**:
   ```yaml
   - id: jira-token-<bot>
     expiry: <today + N months>
     last_rotated_at: <today>
   ```
8. Re-run `scripts/credential_expiry_check.py` and confirm the alert
   clears.

**Common pitfalls**

- Do NOT delete the old token before verifying the new one — runners
  will fail their next pickup loop and bounce the JIRA dispatcher.
- SSO-bound accounts may require an admin to whitelist the new token's
  scopes. Check that first.

---

## gerrit-http-passwords

**What it is**: Self-hosted Gerrit's HTTP password used by the merger
bot, the auto-rebase agent, and the runner's git push path.

**Policy expiry**: 90 days, enforced by the self-hosted Gerrit policy.
This was the root-cause credential that motivated OP-727 — a Gerrit
HTTP password silently expired and we discovered it only when a push
failed in a later session.

**Rotation steps**

1. SSH into the Gerrit host as the bot user.
2. From the Gerrit web UI: User → Settings → HTTP Credentials →
   **Generate New Password**.
3. Copy the new password.
4. Update `db:git_accounts.encrypted_token[<bot>]` via the
   `POST /git-accounts` API (the field is reused for HTTP password on
   gerrit-platform rows). Do NOT edit the database directly.
5. Verify: `git ls-remote https://<bot>:<new-pwd>@<gerrit-host>/<project>`.
6. **Update the inventory**:
   ```yaml
   - id: gerrit-http-<bot>
     expiry: <today + 90 days>
     last_rotated_at: <today>
   ```
7. Re-run `scripts/credential_expiry_check.py`.

**Common pitfalls**

- The merger bot's HTTP password is what backs the `Code-Review +2`
  push for conflict resolution. If it expires, merger workflow stalls
  silently — there is no other gate that surfaces this. Gerrit returns
  401 on the push, the merger logs it, and the queue grows.
- Gerrit HTTP password is NOT the same as the Gerrit SSH key — both
  exist, both have separate inventory entries.

---

## ssh-keys

**What it is**: Per-bot ed25519 SSH private keys used for signed git
push to Gerrit / GitLab and for SSH runner targets.

**Policy expiry**: `never` — ed25519 keys do not have a built-in
expiration. The inventory entry MUST set `expiry: never`.

**When to rotate**

ed25519 SSH keys are rotated only on:

- Suspected compromise (operator laptop lost, key leaked to a log,
  bot account compromised).
- Bot retirement (account decommissioned).
- Hardware migration (key was bound to a now-defunct host).

Routine calendar-based rotation of healthy ed25519 keys is **not**
required and adds risk (every rotation is an opportunity to break
something) without security benefit.

**Rotation steps (when triggered)**

1. Generate new key on a trusted host:
   ```bash
   ssh-keygen -t ed25519 -C "<bot>@omnisight" -f ~/.ssh/id_ed25519_<bot>_new
   ```
2. Add the public key to the bot's account on every relevant forge
   (Gerrit, GitLab, GitHub).
3. Test against each forge:
   ```bash
   ssh -i ~/.ssh/id_ed25519_<bot>_new -p 29418 <bot>@<gerrit-host>
   ```
4. Replace the key file referenced by `secret_ref`.
5. Remove the old public key from the forges after a 24 h soak.
6. **Update the inventory** — even though `expiry: never` doesn't
   change, bump `last_rotated_at` for audit:
   ```yaml
   - id: ssh-key-<bot>
     expiry: never
     last_rotated_at: <today>
   ```

---

## api-keys

**What it is**: Third-party provider keys (Anthropic, OpenAI, etc.)
used by the LLM gateway and the agent runtime.

**Policy expiry**: provider-dependent. Anthropic and OpenAI consoles
let you set an explicit expiry; if you didn't, the key is "never" by
provider policy but you should still set a calendar-based rotation
(every 6–12 months) and capture that as `expiry`.

**Rotation steps**

1. Sign in to the provider console.
2. Create a new key, scope it to the same uses as the old one.
3. Update `env:<PROVIDER>_API_KEY` in the deployment env file.
4. Reload the gateway / backend services that consume it.
5. Smoke-test: send one prompt through the gateway and confirm
   200 + token usage on the new key.
6. Revoke the old key in the provider console.
7. **Update the inventory**:
   ```yaml
   - id: api-key-<provider>-<env>
     expiry: <today + 12 months>
     last_rotated_at: <today>
   ```

**Common pitfalls**

- Provider keys are often bound to a specific organization / project.
  A new key generated in the wrong workspace will authenticate but
  bill / log to the wrong place. Verify the org_id in `metadata`
  matches before flipping the env var.

---

## encryption-keys

**What it is**: Application-level encryption keys (Fernet, AES) used
by the credential vault to encrypt the `encrypted_*` columns in the
`git_accounts` and `llm_credentials` tables.

**Policy expiry**: annual rotation. Rotation requires re-encryption
of every dependent row — this is a migration, not a swap.

**Rotation steps**

1. Schedule a maintenance window. Re-encryption holds a row-level
   write lock per credential during migration; on a busy system this
   can serialize behind the credential dispatcher.
2. Generate a new Fernet key:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. Stage the new key as the *secondary* in the vault (vault supports
   dual-key decrypt during rotation).
4. Run the re-encryption migration (see the corresponding alembic
   step — typically authored alongside this rotation).
5. Promote the new key to primary, demote the old key to secondary.
6. After one full backup cycle has completed under the new primary,
   remove the old key from the vault.
7. **Update the inventory**:
   ```yaml
   - id: encryption-key-credential-vault
     expiry: <today + 12 months>
     last_rotated_at: <today>
   ```

**Common pitfalls**

- Do NOT remove the old key before all rows have been re-encrypted
  AND a known-good backup exists under the new primary. Losing both
  keys means losing every encrypted credential in the vault.

---

## After every rotation: verify the alert clears

```bash
python3 scripts/credential_expiry_check.py --inventory configs/credentials.yaml
```

Expected: the rotated credential moves from the `ALERTS` section to
`OK`. If the alert still fires after a rotation, the most likely cause
is that `expiry` in `configs/credentials.yaml` was not updated — the
rotation is incomplete until the inventory reflects it.

This is the AC2 contract from OP-727 and is exercised by
`tests/test_credential_expiry_check.py::test_ac2_alert_clears_after_rotation`.

---

## Scheduling — running the check daily

The systemd units in `deploy/systemd/` schedule the daily run:

- `omnisight-credential-expiry-check.service` — oneshot, runs the script.
- `omnisight-credential-expiry-check.timer`   — calls the service at 09:00
  local with `Persistent=true` so a missed day still fires.

Install:

```bash
sudo cp deploy/systemd/omnisight-credential-expiry-check.{service,timer} \
    /etc/systemd/system/
# Edit USER_HOME / USERNAME placeholders in both files.
sudo systemctl daemon-reload
sudo systemctl enable --now omnisight-credential-expiry-check.timer
```

Verify:

```bash
systemctl list-timers | grep credential-expiry
sudo systemctl start omnisight-credential-expiry-check.service
journalctl -u omnisight-credential-expiry-check.service -n 50
```

Exit codes (so the alerter / T1 dispatcher can route by severity):

| Code | Meaning            |
|------|--------------------|
| 0    | no alerts          |
| 1    | warning band (≤ 30d) |
| 2    | urgent band (≤ 7d)   |
| 3    | critical / expired (≤ 1d) |
| 64   | usage error          |
| 65   | inventory invalid    |

Hooking up alerts (T1): point a sibling `OnFailure=` unit at your
notification dispatcher, or pipe `--format json` into the Slack /
PagerDuty bridge of your choice. The script is intentionally
side-effect-free — it prints, it exits with a code, and it leaves
the routing to the deployment-specific alerter.

If you don't yet have a `configs/credentials.yaml`, copy the example
template:

```bash
cp configs/credentials.example.yaml configs/credentials.yaml
# Then prune to your real credentials and update expiry / owner fields.
```
