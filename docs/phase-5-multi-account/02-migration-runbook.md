# Phase 5 — Legacy `.env` → `git_accounts` Migration Runbook

> Date: 2026-04-24
> Scope: one-time migration from the pre-Phase-5 `Settings`-scalar +
> JSON-map credential model to the `git_accounts` PostgreSQL table.
> For day-to-day operator tasks (add / rotate / disable / delete)
> see [`../ops/git_credentials.md`](../ops/git_credentials.md).

---

## 0. Prerequisites

- Backend image is built from a commit ≥ Phase 5-9 (the `git_accounts`
  table + CRUD API + UI all present). Check: `git log --oneline |
  grep -i 'phase-5-9'` should show the `AccountManagerSection` row.
- PostgreSQL is reachable; `/readyz` is green; `alembic upgrade head`
  includes the `0027_git_accounts` migration (Phase 5-1).
- You have admin UI access (session cookie with `role=admin`).
- You have a copy of the current `.env` on the production host —
  the migration does not delete `.env` lines, but you will want to
  clean them up at the end.

Estimated maintenance window: **15 minutes** for a deployment with a
handful of credentials; less for a fresh deployment that never set
any legacy scalar.

---

## 1. Decide the migration strategy

Pick **ONE** of three paths before touching anything:

### Path A — Auto-migrate on next backend boot (**recommended**)

Phase 5-5 shipped a lifespan hook (`migrate_legacy_credentials_once`)
that runs on every backend startup. It:

- Checks whether `git_accounts` is empty AND any legacy `.env` field
  is non-empty.
- If both true, reads `github_token` / `github_token_map` /
  `gitlab_token` / `gitlab_token_map` / `gerrit_instances` /
  `notification_jira_*` / `*_webhook_secret` and inserts one
  `ga-legacy-*` row per unique host / platform.
- Writes an `audit_log` row per inserted account with
  `actor=system/migration`.
- Idempotent — runs only once; re-boot is a no-op.

**When to pick Path A**: you want zero manual work and are OK letting
the auto-migration choose labels like `github.com (legacy)`.

### Path B — Manual CRUD (operator-driven)

Skip the auto-migration, add accounts one-by-one via the UI. You get
full control of labels, URL patterns, which account is `is_default`,
which tokens get rotated while you're at it.

**When to pick Path B**: you have ≥ 5 accounts across multiple
tenants, or you want to redesign URL-pattern routing (e.g. split one
`github.com` PAT into per-org patterns) as part of the move.

### Path C — Hybrid (**most common for production**)

Let Path A populate `ga-legacy-*` rows, then touch up labels / patterns
/ default flags in the UI afterwards. Best of both worlds; is what
the Phase 5 authors used during internal soak.

---

## 2. Path A — Auto-migrate walkthrough

### 2.1 Pre-flight — confirm `.env` has the credentials you want

On the production host:

```bash
grep -E '^(OMNISIGHT_(GITHUB|GITLAB|GERRIT|NOTIFICATION_JIRA|JIRA|GIT_SSH)_)' .env \
  | sed 's/=.*$/=***redacted***/'
```

Expected output = every legacy credential field that the migration
will pick up. Cross-check against what you expect; fix `.env` before
rolling.

### 2.2 Ensure kill-switch is NOT set to `skip`

```bash
grep OMNISIGHT_CREDENTIAL_MIGRATE .env
# Expected: either empty or "OMNISIGHT_CREDENTIAL_MIGRATE=" (empty value)
# If it says "=skip", unset it before proceeding.
```

### 2.3 Verify `git_accounts` is empty

```bash
# From the backend container, or any psql-reachable host
PGPASSWORD=... psql -h ... -U ... -d omnisight \
  -c "SELECT COUNT(*) FROM git_accounts;"
# Expected: 0
```

If it's non-zero the auto-migration will skip — decide whether you
want to `TRUNCATE git_accounts` first (destructive, only for
first-time migrations where nothing real has been added yet).

### 2.4 Rebuild + rolling recreate

```bash
docker compose -f docker-compose.prod.yml build backend-a backend-b
docker compose -f docker-compose.prod.yml up -d --no-deps backend-a
# wait for /readyz green
curl -s https://ai.sora-dev.app/api/v1/health | jq .
docker compose -f docker-compose.prod.yml up -d --no-deps backend-b
```

### 2.5 Confirm the migration ran

```bash
docker compose -f docker-compose.prod.yml logs backend-a 2>&1 | grep -i CRED-MIGRATE
```

Expected one of:

- `[CRED-MIGRATE] migrated N legacy credential(s) from Settings into git_accounts`
  — success; N > 0.
- `[CRED-MIGRATE] git_accounts already has rows; skipping` — was
  already populated; rerun aborted.
- `[CRED-MIGRATE] OMNISIGHT_CREDENTIAL_MIGRATE=skip — hook disabled`
  — kill-switch active; unset and re-boot if that was unintended.
- `[CRED-MIGRATE] nothing to migrate` — legacy `.env` fields were all
  empty; no-op.

### 2.6 Spot-check the rows in UI

1. Open the dashboard → SYSTEM INTEGRATIONS → **Git Accounts** tab.
2. You should see one `ga-legacy-*` row per unique (platform, host)
   pair found in `.env`.
3. Labels are `github.com (legacy)` / `gitlab.com (legacy)` / etc.
   Rename them later if you want nicer labels — that's a PATCH,
   not a re-migration.

### 2.7 Verify resolver behaviour is unchanged

Pick one repo URL that the deployment actually uses (e.g. the repo
configured for CI auto-push) and hit the resolve probe:

```bash
curl -X POST -H "Cookie: ..." \
  "https://ai.sora-dev.app/api/v1/git-accounts/resolve?url=https://github.com/acme/app"
```

Response must show `account_id=ga-legacy-github-github-com` (or
similar) and `token_fingerprint` matching the `.env` PAT's last 4
characters. If it returns `null` / `MissingCredentialError`, the
migration missed the field — see §5 Troubleshooting.

---

## 3. Path B — Manual CRUD walkthrough

1. Ensure `git_accounts` is empty (or that you're comfortable with
   existing rows). Auto-migration is still safe but will populate
   extras under `ga-legacy-*` — unset it with
   `OMNISIGHT_CREDENTIAL_MIGRATE=skip` if you want a clean slate.
2. Add accounts one at a time via the UI (see `../ops/git_credentials.md` §2).
3. Test each account (TEST BEFORE SAVE per-row, then per-row TEST
   after save) before removing the matching `.env` line.
4. Mark the intended platform default via `★ SET`.
5. Remove `.env` lines per §4 once the UI shows the full complement.

---

## 4. Post-migration — remove legacy `.env` lines

Once `git_accounts` has all the rows the resolver needs, the
`.env` scalar fields are redundant. The legacy shim
(`backend.git_credentials._build_registry`) still synthesises
virtual rows from them, but with `git_accounts` populated, the real
rows win (ordering: real rows from PG → shim synthesis → empty).

**Removing the `.env` lines eliminates the yellow "Legacy" banner
in the UI** (Phase 5-9's `AccountManagerSection` only shows it when
the scalar `*_token_map` / `notification_jira_*` fields are
non-empty).

### 4.1 Before touching `.env`, verify parity

```bash
# From backend container
curl -s -H "Cookie: ..." https://ai.sora-dev.app/api/v1/git-accounts \
  | jq '.accounts | length'
# Expected: matches count from §2.1
```

### 4.2 Backup `.env`

```bash
cp .env .env.pre-phase-5-10.$(date +%Y%m%d-%H%M%S)
```

### 4.3 Remove the legacy credential lines

Fields to delete (Phase 5-10 registry — see
`backend.config.LEGACY_CREDENTIAL_FIELDS`):

```
OMNISIGHT_GITHUB_TOKEN=
OMNISIGHT_GITHUB_TOKEN_MAP=
OMNISIGHT_GITHUB_WEBHOOK_SECRET=
OMNISIGHT_GITLAB_TOKEN=
OMNISIGHT_GITLAB_URL=
OMNISIGHT_GITLAB_TOKEN_MAP=
OMNISIGHT_GITLAB_WEBHOOK_SECRET=
OMNISIGHT_GERRIT_URL=
OMNISIGHT_GERRIT_SSH_HOST=
OMNISIGHT_GERRIT_SSH_PORT=
OMNISIGHT_GERRIT_PROJECT=
OMNISIGHT_GERRIT_INSTANCES=
OMNISIGHT_GERRIT_WEBHOOK_SECRET=
OMNISIGHT_NOTIFICATION_JIRA_URL=
OMNISIGHT_NOTIFICATION_JIRA_TOKEN=
OMNISIGHT_NOTIFICATION_JIRA_PROJECT=
OMNISIGHT_JIRA_WEBHOOK_SECRET=
OMNISIGHT_GIT_SSH_KEY_PATH=
OMNISIGHT_GIT_SSH_KEY_MAP=
```

Fields to KEEP (NOT credentials, even though they look adjacent):

- `OMNISIGHT_GERRIT_ENABLED` — master switch.
- `OMNISIGHT_GERRIT_REPLICATION_TARGETS` — post-merge push
  destinations.
- `OMNISIGHT_JIRA_INTAKE_LABEL` / `OMNISIGHT_JIRA_DONE_STATUSES`
  — routing knobs.
- All `OMNISIGHT_*_API_KEY` LLM provider keys — Phase 5b migrates
  these separately.

### 4.4 Rolling recreate

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps backend-a
# /readyz green → next replica
docker compose -f docker-compose.prod.yml up -d --no-deps backend-b
```

### 4.5 Confirm banner is gone + resolver still works

1. Open **Git Accounts** tab — yellow "Legacy (will auto-migrate on
   next login)" banner should have disappeared.
2. Trigger one forge action end-to-end (PR comment, issue sync,
   clone). Must still succeed.

---

## 5. Troubleshooting

### "After the migration, resolver returns `MissingCredentialError` for a URL that used to work"

- The auto-migration indexes by (platform, host). If your `.env`
  set, say, a self-hosted GitLab URL but your repo URL is a
  different host (mixing `gitlab.com` tokens into `gitlab.acme.com`
  is a common mistake), the `ga-legacy-gitlab-gitlab-com` row will
  not match `gitlab.acme.com`.
- Fix: add a manual row via UI (§ Path B) with the correct
  `instance_url` and `url_patterns`.

### "The migration ran but did not create a Gerrit row"

- `gerrit_instances` is a JSON list — the migration parses it; if it's
  malformed JSON it silently falls through. Check
  `backend/legacy_credential_migration.py::_parse_gerrit_instances`
  exit path in the `[CRED-MIGRATE]` log.
- Fallback: the scalar `gerrit_url` / `gerrit_ssh_host` /
  `gerrit_project` group synthesises ONE `ga-legacy-gerrit-*` row.
  If both the list and the scalars were set, the list wins.

### "`audit_log` rows reference an actor I don't recognise"

- Auto-migration writes `actor=system/migration`. That is intentional
  — the admin who ran the deploy is recorded in the `deploy` /
  `docker compose` logs, not the audit chain.
- Phase 5-10 write-path deprecation warns (PUT `/runtime/settings`)
  use `actor=<user id>` — those are the rows that identify the
  human kept using the legacy UI.

### "The yellow banner is still there after I removed `.env` lines"

- The banner reads `github_token_map` / `gitlab_token_map` /
  `notification_jira_*` from the CURRENT worker's in-memory `Settings`.
  If you removed `.env` lines but did NOT rolling-recreate, workers
  are still holding the pre-remove values.
- Fix: complete step 4.4 (rolling recreate).

### "PATCH `/git-accounts/{id}` returns 409 after the auto-migration"

- The auto-migration creates rows with `version=0`. A stale PATCH
  body carrying an older `If-Match` will 409. Re-GET the row,
  carry the fresh `version`, retry. Optimistic lock is working as
  designed.

### "I want to roll back to the pre-Phase-5 state"

- The auto-migration is idempotent in the forward direction; the
  reverse is destructive. If you need to undo:
  1. `TRUNCATE git_accounts;` (PG — will cascade-delete if you have
     FK-dependent rows, which you should not at this Phase).
  2. Re-add the `.env` lines you deleted in §4.3.
  3. `docker compose up -d --no-deps backend-a backend-b` rolling.
- After rollback, the legacy shim resumes synthesising virtual rows
  from `.env` and the resolver's behaviour is identical to the
  pre-Phase-5 state.

---

## 6. Module-global state / read-after-write audit

- **Cross-worker coherence** (SOP Step 1, qualified answer #2): the
  migration writes to `git_accounts` via PG INSERTs; every worker
  reads the same table. The lifespan hook uses `ON CONFLICT (id)
  DO NOTHING` so two workers racing at startup collapse into a
  single committed row.
- **Read-after-write timing** (SOP Step 1): the migration runs
  inside the uvicorn lifespan startup hook — before any HTTP
  listener opens — so no request handler can observe a half-
  migrated state.

## 7. Next-gate

- **dev-only** until the operator completes the steps above against
  a live deployment and §2.5 shows a clean `[CRED-MIGRATE] migrated
  N …` line.
- **deployed-active** requires steps §4.5 (banner gone, resolver
  still works) on a deployment with ≥ 1 real forge account.
- **deployed-verified (24h observation)** — Phase 5 overall flips
  to `[V]` only after row 5-11 soak (multi-credential rotation +
  tenant isolation drill) completes.

---

## 8. See also

- [`01-design.md`](./01-design.md) — schema + resolver contract.
- [`../ops/git_credentials.md`](../ops/git_credentials.md) —
  day-to-day operator runbook.
- `backend/legacy_credential_migration.py` — source code of the
  migration hook.
- `backend/config.py::LEGACY_CREDENTIAL_FIELDS` — authoritative
  registry of deprecated fields.
