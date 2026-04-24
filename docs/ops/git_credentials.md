# Git Forge Credentials — Operator Runbook

> Date: 2026-04-24
> Scope: Phase 5 `git_accounts` — day-to-day operator tasks for
> **GitHub / GitLab / Gerrit / JIRA** credentials. For the one-time
> migration from the legacy `.env` scalar fields to `git_accounts`,
> see [`02-migration-runbook.md`](../phase-5-multi-account/02-migration-runbook.md).

---

## TL;DR

| Task                                                          | Where               |
| ------------------------------------------------------------- | ------------------- |
| Add a new account (PAT / SSH key / webhook secret)            | Modal § Git Accounts tab |
| Rotate a token (operator leaves, compromise suspected, etc.)  | Per-row `ROTATE` button  |
| Temporarily disable an account without deleting               | Per-row `DISABLE` toggle |
| Delete an account                                             | Per-row `DELETE` button  |
| Find which account a repo URL resolves to                     | `POST /api/v1/git-accounts/resolve?url=...` |
| List all accounts for a platform                              | `GET /api/v1/git-accounts?platform=github`  |
| Test a saved account's token without rotating                 | Per-row `TEST` button    |

All operations are **per-tenant**. An admin sees only their tenant's
accounts; tenant isolation is enforced at the PG `WHERE tenant_id =
$1` layer and was drilled in Phase 5-11's soak.

---

## 1. Conceptual model

Phase 5-1 through 5-9 replaced the old `Settings.github_token` /
`github_token_map` / `gerrit_instances` / `notification_jira_*`
scalars with one row per account in `git_accounts`:

```
git_accounts
├── id              # ga-xxxxx (stable; UI uses this for PATCH / DELETE)
├── tenant_id       # t-default / t-customer-N (per-tenant RLS)
├── platform        # 'github' | 'gitlab' | 'gerrit' | 'jira'
├── instance_url    # 'https://github.com' | 'https://git.acme.com' | ...
├── label           # operator-chosen; shown in UI; grep-friendly
├── username        # account identity (optional — display only)
├── encrypted_token # Fernet-ciphertext PAT / API token
├── encrypted_ssh_key    # Fernet-ciphertext private SSH key
├── ssh_host / ssh_port / project   # gerrit-specific
├── encrypted_webhook_secret        # HMAC / Bearer verifier
├── url_patterns    # glob list — 'github.com/acme-corp/*'
├── is_default      # one TRUE per (tenant, platform) via partial-unique index
├── enabled         # FALSE → resolver skips this row
├── last_used_at    # LRU analytics (touched on successful resolve)
└── version         # optimistic-lock guard for PATCH (If-Match)
```

The resolver chain used by clone / fetch / push / webhook / issue
sync / PR / MR is (see `docs/phase-5-multi-account/01-design.md` §3.10):

1. `url_patterns` glob match against the scheme-stripped URL.
2. Exact host match against `instance_url` or `ssh_host`.
3. Substring host match (legacy shim rows).
4. Platform default (`is_default=TRUE`, else first enabled row).

---

## 2. Add a new account

### Via UI (recommended)

1. Open the OmniSight dashboard → gear icon → **SYSTEM INTEGRATIONS**
   → **Git Accounts** tab.
2. Pick the platform tab (**github** / **gitlab** / **gerrit** /
   **jira**).
3. Fill the form:
   - **Label**: operator-visible name (e.g. `acme-corp github PAT`).
   - **Instance URL** (github / gitlab / jira) — `https://github.com`
     for github.com, `https://gitlab.acme.com` for self-hosted.
   - **SSH host / port / project** (gerrit only) — matches what you
     would have put in `gerrit_ssh_host` / `gerrit_ssh_port` /
     `gerrit_project`.
   - **Username** (optional, display only).
   - **Token**: paste the PAT.
   - **SSH key** (optional, github/gitlab/gerrit): paste the private
     key body. Both `encrypted_token` and `encrypted_ssh_key` can
     coexist — the resolver picks one based on URL scheme
     (`https://` → token, `git@`/`ssh://` → SSH key).
   - **Webhook secret** (optional): the HMAC / Bearer secret the
     forge signs incoming webhooks with.
   - **URL patterns**: one glob per line, e.g.
     `github.com/acme-corp/*` + `github.com/acme-internal/*`.
     Empty = account matches the platform default only.
   - **Default for platform**: tick to make this account the
     platform-level fallback when no `url_patterns` match.
4. Click **TEST BEFORE SAVE** — hits the forge's `/user` or
   `/users/self` endpoint and reports OK or failure. GitHub / GitLab
   / Gerrit paths are pre-save; JIRA short-circuits ("save then
   TEST" — JIRA's Basic auth expects the stored secret).
5. Click **SAVE**. Row appears in the list immediately.

### Via REST API

```bash
curl -X POST https://ai.sora-dev.app/api/v1/git-accounts \
  -H "Content-Type: application/json" \
  -H "Cookie: ..." \
  -d '{
    "platform":     "github",
    "label":        "acme-corp github PAT",
    "instance_url": "https://github.com",
    "username":     "acme-bot",
    "token":        "ghp_xxxxxxxxxxxxxxxxxxxx",
    "url_patterns": ["github.com/acme-corp/*"],
    "is_default":   false
  }'
```

Response contains only fingerprints (`…last4`) — the plaintext
never round-trips. `audit_log` records `action=git_account.create`
with the operator's user id.

---

## 3. Rotate a token

Tokens rotate when: the operator leaves, the forge marks the PAT
as compromised, a quarterly rotation policy fires, a webhook secret
leaks.

### Via UI

1. Open **Git Accounts** tab, locate the row.
2. Click `ROTATE` (or `PATCH` → paste new token → SAVE).
3. Click `TEST` to confirm the new token is accepted by the forge.

### Via REST API

```bash
curl -X PATCH https://ai.sora-dev.app/api/v1/git-accounts/ga-xxxxx \
  -H "Content-Type: application/json" \
  -H "If-Match: 42" \
  -d '{"token": "ghp_newvalue_xxxxxx"}'
```

- `If-Match` header carries the `version` from the GET (optimistic
  lock — a concurrent edit from another browser tab will 409).
- Empty string `""` clears the token field.
- `null` / missing leaves the field untouched (partial update).

`version` auto-bumps on each successful PATCH. `last_used_at` does
NOT reset — rotation keeps LRU ordering stable.

### What to test after a rotation

- Trigger one action the account is used for (PR comment, issue
  sync, clone). Success = rotation cleanly replaced the credential
  across the 4 workers (each worker's next resolver call reads the
  new row from PG; no process restart needed).
- If the action fails with 401, check `backend` logs for
  `pick_account_for_url` miss — the new token might belong to a
  different account than the one the URL pattern resolves to.
  Re-run the resolve probe (§6) to confirm.

---

## 4. Temporarily disable an account (without deleting)

Use case: an operator is on holiday, their account's PAT is still
valid but you want clone / push to prefer a different account until
they're back.

### Via UI

1. **Git Accounts** tab → toggle the row's `ENABLED` switch off.
2. The resolver skips `enabled=FALSE` rows (Phase 5-3 resolver
   filter). If this was the platform default, resolver falls
   through to the next enabled row; if it was also the ONLY
   enabled row for that platform, callers get `MissingCredentialError`
   → HTTP 503 on the dependent endpoint.

### Via REST API

```bash
curl -X PATCH https://ai.sora-dev.app/api/v1/git-accounts/ga-xxxxx \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Re-enable by `PATCH {enabled: true}`.

---

## 5. Delete an account

### Via UI

1. Click row's `DELETE` button.
2. Confirm dialog — if the row has `url_patterns` or `is_default`,
   the UI warns about dependencies.
3. If `is_default` and `auto_elect_new_default=true` (default), the
   backend picks the next-LRU account of the same platform as the
   new default inside the same transaction — the partial-unique
   index never observes a 2-TRUE intermediate state.

### Via REST API

```bash
# Refuse without replacement (safer default for scripted deletion)
curl -X DELETE "https://ai.sora-dev.app/api/v1/git-accounts/ga-xxxxx"

# Explicitly allow auto-elect (UI's default behaviour)
curl -X DELETE "https://ai.sora-dev.app/api/v1/git-accounts/ga-xxxxx?auto_elect_new_default=true"
```

- Deleting the sole account for a platform is allowed (auto-elect
  finds nothing to promote — the platform simply has no default).
- `audit_log` records `action=git_account.delete` with the full
  `before` snapshot (minus the plaintext token — fingerprint only).

---

## 6. Find which account a repo URL resolves to

When a clone / push fails with 401 and you want to know "which
credential was the resolver about to use?":

### Via UI

Not directly surfaced — use the REST API or look at the backend log
line `pick_account_for_url(...) → ga-xxxxx`.

### Via REST API

```bash
curl -X POST "https://ai.sora-dev.app/api/v1/git-accounts/resolve?url=https://github.com/acme-corp/app"
```

Response:

```json
{
  "url": "https://github.com/acme-corp/app",
  "platform": "github",
  "match_type": "url_pattern",
  "account_id": "ga-acme-corp",
  "label": "acme-corp github PAT",
  "instance_url": "https://github.com",
  "url_patterns": ["github.com/acme-corp/*"],
  "is_default": false,
  "enabled": true,
  "token_fingerprint": "ghp_…abc4"
}
```

- `match_type` is one of `url_pattern` / `exact_host` /
  `platform_default` / `fallback` — the step of the resolver chain
  that matched.
- `account_id` = the `ga-xxxxx` handle usable in PATCH / DELETE.
- This endpoint does NOT touch `last_used_at` (debug-only; no LRU
  pollution).

---

## 7. List all accounts for a platform

```bash
curl "https://ai.sora-dev.app/api/v1/git-accounts?platform=github&enabled_only=true"
```

Returns the same shape as §6 for every row, fingerprint-only, sorted
by `is_default DESC, last_used_at DESC NULLS LAST` (the same order
the resolver walks).

---

## 8. Troubleshooting

### "After rotation, clone still uses the old token"

- `git_credentials.py` has a best-effort per-worker cache
  (`_CREDENTIALS_CACHE`) that ages out on config change.
  `docker compose -f docker-compose.prod.yml restart backend-a backend-b`
  force-clears; the cache invariants mean this should rarely be
  needed.
- More likely: the clone URL matches a different `url_patterns`
  entry than you expected. Run the resolve probe (§6) with the
  exact URL.

### "Dashboard shows the account but a webhook POST gets 401"

- Webhook verification reads `encrypted_webhook_secret` via
  `get_webhook_secret_for_host_async` — platform-filtered (Phase
  5-8). If you rotated the token but not the webhook secret, the
  inbound signature check still uses the old secret. Rotate the
  webhook secret separately.
- GitHub / GitLab webhooks sign the body with HMAC-SHA256; Gerrit
  signs with HMAC-SHA256 as well; JIRA sends a Bearer token in
  `Authorization`. If the forge dashboard shows "webhook delivery
  401", the OmniSight side has the wrong secret.

### "Legacy `.env` settings still show in the UI banner after migration"

- The Phase 5-5 lifespan auto-migration fires on backend boot when
  `git_accounts` is empty AND any legacy `.env` field is non-empty.
  If you added rows via UI first, the auto-migration sees
  `git_accounts` is non-empty and skips — the banner remains until
  you either delete the legacy `.env` lines or explicitly clear them
  through `PUT /runtime/settings` (which now emits a deprecation
  audit row per Phase 5-10).
- Kill-switch: `OMNISIGHT_CREDENTIAL_MIGRATE=skip` disables
  the hook entirely (useful for dry-run staging).

### "A PATCH returned 409 — operator concurrent edit?"

- The `version` column is an optimistic lock. Re-fetch the row via
  GET (new `version`), merge your change, retry PATCH with the new
  `If-Match`.
- Two operators editing the same row from different devices hit
  this — the 409 is intentional. Neither lost-write wins silently.

### "Resolver returns a different account for HTTPS vs SSH URL"

- The resolver normalises scheme before matching (`ssh://git@host/org/repo`
  → `host/org/repo`), so one `url_patterns` entry covers both. But
  if an operator wrote two separate patterns (one HTTPS, one SSH),
  both can match and first-match-wins per the resolver ordering.
  Check the `url_patterns` list in the CRUD response.

---

## 9. Audit trail

Every account mutation writes an `audit_log` row:

| Action                               | Fired when                                           |
| ------------------------------------ | ---------------------------------------------------- |
| `git_account.create`                 | `POST /git-accounts`                                 |
| `git_account.update`                 | `PATCH /git-accounts/{id}`                           |
| `git_account.delete`                 | `DELETE /git-accounts/{id}`                          |
| `settings.legacy_credential_write`   | Phase 5-10 — `PUT /runtime/settings` wrote a legacy scalar (github_token / notification_jira_token / ...) |

Query the chain:

```bash
curl "https://ai.sora-dev.app/api/v1/audit/query?action=git_account.update&limit=50"
```

The audit chain is hash-linked (`prev_hash` → `curr_hash`); any
post-write tampering breaks `audit.verify_chain`.

---

## 10. Legacy `.env` fields — still read, write is deprecated

Phase 5-10 marked the following `Settings` fields as deprecated.
They are still **readable** (`backend.git_credentials._build_registry`
synthesises a virtual `git_accounts` row from any set scalar, so
deployments that never migrated keep working), but **writing** them
via `PUT /runtime/settings` now emits:

1. A `logger.warning(...)` line: `Phase-5-10 deprecated-write: settings.<field>`
2. An `audit_log` row: `action=settings.legacy_credential_write, entity_id=<field>`
3. A `deprecations` block in the PUT response so the UI can surface
   a yellow "move to Git Accounts" banner.

Deprecated fields: see `backend.config.LEGACY_CREDENTIAL_FIELDS`
(registry of truth; dict mapping each legacy field to its
replacement hint).

---

## 11. See also

- [`02-migration-runbook.md`](../phase-5-multi-account/02-migration-runbook.md)
  — first-time migration from `.env` to `git_accounts`.
- [`01-design.md`](../phase-5-multi-account/01-design.md) — schema
  + resolver contract + rationale.
- `backend/git_credentials.py` — resolver source code.
- `backend/routers/git_accounts.py` — CRUD + test + resolve
  endpoints.
