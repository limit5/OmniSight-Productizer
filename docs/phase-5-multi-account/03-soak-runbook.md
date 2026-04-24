# Phase 5-11 — Soak + Security Audit Runbook

This document is the **operator-facing acceptance checklist** for
flipping Phase 5 from `dev-only` to `deployed-active`. It covers the
three verification axes that the automated test suite can't exercise
on its own:

1.  **Real-credential soak** — 3 github.com + 2 gitlab.com + 2 Gerrit
    + 1 JIRA accounts walked through the full forge API surface
    (clone / push / PR / issue / webhook) against live remotes.
2.  **Token rotation drill** — rotate one account's token via the UI,
    confirm no interrupted operations + audit trace.
3.  **Per-tenant isolation drill** — log in as tenant A, create creds,
    log in as tenant B, verify absence on every surface.
4.  **Legacy migration dry-run on staging** — preview what the
    Phase 5-5 auto-migration would do, before enabling it on prod.

Each axis has a corresponding automated test file in
`backend/tests/test_phase5_11_*.py` — **read those first to understand
what the code contract is**. This runbook confirms that the real
world behaves the same way the tests claim.

**You cannot complete this runbook without real credentials for all
four forges.** Stage them in your secrets vault and tick them off at
the prerequisites section before proceeding.

---

## Scope

**In scope**
- Verification drills against the post-Phase-5-9 production stack
  (`backend-a` + `backend-b` rolling-restarted with the new image).
- Acceptance criteria for flipping TODO.md row 5-11 from `[O]` to
  `[D]` (deployed-active) after soak passes.
- A 24 h observation window for flipping row 5-11 from `[D]` to `[V]`
  (deployed-observed).

**Out of scope**
- Phase 5-12 (OAuth flows) — separate follow-up, different runbook.
- Phase 5b (LLM API key persistence) — separate phase.
- Migration from legacy `.env` on prod — Path A in
  [02-migration-runbook.md](./02-migration-runbook.md) covers that;
  this doc only adds the dry-run preview step.

---

## Prerequisites

- [ ] Phase 5-1 → 5-10 all merged (check `TODO.md`; all rows `[x]`).
- [ ] Backend image rebuilt since Phase 5-9 merged:
  ```
  docker compose -f docker-compose.prod.yml build backend-a backend-b
  ```
- [ ] Frontend image rebuilt since Phase 5-9 merged:
  ```
  docker compose -f docker-compose.prod.yml build frontend
  ```
- [ ] Rolling restart completed and `/readyz` reports green from both
      `backend-a` and `backend-b`.
- [ ] **Real credentials staged** (pull from your secrets vault or
      create fresh at each provider's token-management page):

  | Platform      | How many | Suggested labels                                 | Required scopes                                  |
  |---------------|----------|---------------------------------------------------|---------------------------------------------------|
  | github.com    | 3        | `personal`, `company`, `opensource`               | `repo` (classic PAT) or `contents:rw`+`pull_requests:rw`+`issues:rw` (fine-grained) |
  | gitlab.com    | 2        | `client-a`, `client-b`                            | `api` (full) — minimum `read_repository`+`write_repository`+`api` |
  | Gerrit        | 2        | `gerrit-primary`, `gerrit-secondary` (SSH host)   | SSH keypair added to the Gerrit user's `Settings → SSH Keys` |
  | JIRA          | 1        | `jira-prod`                                       | `read:jira-work`+`write:jira-work` via Atlassian API token |

- [ ] Throwaway test repos for `clone/push/PR` side of the soak.
      Create one repo per account (or reuse existing if you own it);
      **do NOT run the push/PR test against a repo where a false
      positive would be disruptive.**
- [ ] Two operator users belonging to **different tenants**, each
      with `admin` role (UI → `Security → Users`). Only tenant-admin
      users can CRUD `git_accounts`.
- [ ] `psql` or equivalent PG client pointed at the production PG
      pair (read-only role sufficient; you're only querying
      `audit_log` during rotation drill).

---

## Soak matrix — per-platform scenarios

Each row is a discrete test. Tick `[x]` inline as you walk through;
report failures immediately and do **not** proceed to the rotation
drill until the matrix is green.

### S1. github.com — 3 accounts with `url_patterns` routing

Goal: prove that the resolver picks the right account per repo URL
even when three accounts share the same host.

**Preparation**
1. [ ] Log in as tenant-admin. Open `Settings → Integrations → GIT ACCOUNTS`.
2. [ ] **Add `personal` account**:
   - platform = `github`, label = `personal`, instance URL = `https://github.com`
   - token = PAT for your personal account
   - url patterns = `github.com/<your-user>/*`
   - `is_default` = **unchecked**
   - Click **TEST BEFORE SAVE** → expect green `{ok: true, username: "<your-user>"}`
   - SAVE.
3. [ ] **Add `company` account**:
   - platform = `github`, label = `company`, instance URL = `https://github.com`
   - token = PAT for company-org account
   - url patterns = `github.com/<company-org>/*`
   - `is_default` = **unchecked**
   - TEST BEFORE SAVE → green.
   - SAVE.
4. [ ] **Add `opensource` account**:
   - platform = `github`, label = `opensource`, instance URL = `https://github.com`
   - token = PAT for your OSS identity
   - url patterns = (leave empty)
   - `is_default` = **checked** (this is the fallback)
   - TEST BEFORE SAVE → green.
   - SAVE.

**Resolver verification (use the `resolve` probe endpoint)**
5. [ ] `POST /api/v1/git-accounts/resolve?url=https://github.com/<your-user>/repo`
   → expect `{account: {label: "personal", ...}, match_type: "url_pattern"}`
6. [ ] `POST /api/v1/git-accounts/resolve?url=https://github.com/<company-org>/repo`
   → expect `{account: {label: "company", ...}, match_type: "url_pattern"}`
7. [ ] `POST /api/v1/git-accounts/resolve?url=https://github.com/some/other-repo`
   → expect `{account: {label: "opensource", ...}, match_type: "platform_default"}`

**End-to-end ops (exercise the call-site sweep from rows 5-6/7/8)**
8. [ ] Trigger a **clone** of a personal repo via the orchestrator
       or manual workspace setup — backend log should show
       `[git-credentials] resolved url=https://github.com/<your-user>/... → label=personal`
       (or the equivalent per-caller format). Exit 0, no 401.
9. [ ] Repeat the clone for a company repo → `label=company`.
10. [ ] Trigger a **PR creation** against the personal repo
       (orchestrator → "create PR" flow, or `POST /api/v1/invoke/create-change`
       if wired). Expect 201 with PR URL; GitHub shows the PR opened
       by your personal account (check the avatar in the GitHub UI).
11. [ ] Issue-tracker sync: generate a debug-finding on a repo whose
        URL matches the `company` pattern → expect the GitHub issue
        opens under the company PAT. Check both the backend log's
        `[issue_tracker] resolved ... label=company` line AND the
        GitHub issue's author field.
12. [ ] Inbound **webhook** HMAC: configure the personal repo's
        webhook URL to `https://<your-domain>/api/v1/webhooks/github`
        with the `webhook_secret` you set in the `personal` row;
        trigger a push event from GitHub; backend returns `200 OK`
        and writes the event to `audit_log`. If you get 401, the
        per-account secret wiring broke — investigate before moving on.

### S2. gitlab.com — 2 accounts + self-hosted instance_url

Goal: prove gitlab token_map legacy shim migrated correctly + the
`instance_url` per-row field routes self-hosted GitLab traffic.

13. [ ] Add `client-a` (gitlab.com, default).
14. [ ] Add `client-b` (gitlab.com, url_patterns = `gitlab.com/<client-b-group>/*`).
15. [ ] (Optional) Add a `self-hosted` entry with `instance_url = https://gitlab.internal.corp.com`
        — if you have one. Skip if not.
16. [ ] Resolver probe for both clients → matches correct row.
17. [ ] Clone + push to a client-b repo → log shows `label=client-b`.
18. [ ] MR creation → check MR author on gitlab.com UI.
19. [ ] Webhook HMAC round-trip (same as S1 #12 but gitlab) → 200.

### S3. Gerrit — 2 instances (different SSH hosts)

Goal: prove `_resolve_account(project)` picks the right Gerrit per
project and that SSH-key decryption works round-trip.

20. [ ] Add `gerrit-primary`: platform = `gerrit`,
        ssh_host = `gerrit.primary.internal`, ssh_port = 29418,
        project = `platform/core`, ssh_key = paste-the-private-key,
        is_default = checked.
21. [ ] Add `gerrit-secondary`: ssh_host = `gerrit.secondary.internal`,
        project = `apps/mobile`, ssh_key = its-private-key, is_default = unchecked.
22. [ ] `POST /api/v1/git-accounts/resolve?url=ssh://gerrit.primary.internal:29418/platform/core`
        → resolves to `gerrit-primary`.
23. [ ] Trigger a **Gerrit change creation** via the invoke flow
        against `platform/core` — backend issues
        `gerrit-primary`'s `ssh -i <decrypted_key> ...` command.
        The new change appears in Gerrit under the `gerrit-primary`
        user's identity.
24. [ ] Same against `apps/mobile` → resolves to `gerrit-secondary`.
        Change appears under the secondary user.
25. [ ] `set-reviewer` SSH op (row 5-7 added this) against
        `platform/core`: `POST /api/v1/gerrit/set-reviewer` (or however
        your orchestrator exposes it). Expect 200, Gerrit review has
        the reviewer attached, SSH session used `gerrit-primary`'s
        key.
26. [ ] Gerrit **webhook** HMAC: configure the Gerrit server's
        webhook plugin with the `webhook_secret` you set in the
        `gerrit-primary` row. Trigger a `patchset-created` event.
        Backend returns 200 and logs
        `[webhook] gerrit HMAC verified for instance gerrit.primary.internal`.
        If 401, row 5-7's `get_webhook_secret_for_host_async` is
        not wired correctly.

### S4. JIRA — 1 account + status transition + comment

Goal: prove JIRA credential resolves via `pick_default("jira")` +
instance_url override + webhook secret.

27. [ ] Add `jira-prod`: platform = `jira`,
        instance_url = `https://<your-tenant>.atlassian.net`,
        token = paste-the-API-token, is_default = checked,
        webhook_secret = (generate a shared secret, put it in JIRA's
        webhook configuration too).
28. [ ] Create a debug-finding that triggers JIRA sync:
        backend calls `_sync_jira()` → expect a new JIRA ticket
        appears in the project, issue type `Bug` (or whatever your
        issue_tracker config says), authored by the token owner.
29. [ ] Status transition: trigger a finding resolution → backend
        does `POST /rest/api/3/issue/{key}/transitions` via the
        resolved account's token. Check JIRA shows the status moved.
30. [ ] Comment sync: trigger a comment-update event → `_comment_jira`
        posts a new comment. Check the JIRA ticket has it.
31. [ ] JIRA **webhook**: configure the JIRA project's webhook URL
        to `https://<your-domain>/api/v1/webhooks/jira` with the
        shared secret. Move a ticket's status in JIRA. Backend
        returns 200 and backend log shows HMAC verified.

### S5. Cross-platform soak

32. [ ] Confirm `GET /api/v1/git-accounts` lists all 8 rows
        (3 github + 2 gitlab + 2 gerrit + 1 jira) with correct
        `is_default` flags. Fingerprints show the expected last-4 of
        each PAT/secret.
33. [ ] Run `python3 scripts/migrate_legacy_credentials_dryrun.py --probe-db`
        and confirm the probe reports `git_accounts has 8 row(s);
        migration hook will skip`. This proves idempotency is
        working (the Phase-5-5 lifespan hook won't try to re-seed).

---

## Token rotation drill

Goal: prove a production token rotation takes effect immediately + is
traceable in audit without an outage.

### Rotation flow (use the `company` github account as the canary)

1. [ ] Note the **current `token_fingerprint`** for the `company` row
       in the UI — e.g. `…abc1`.
2. [ ] **Generate a new PAT** on github.com for that account. Keep
       both old + new tokens in your vault temporarily; the drill will
       validate that the old one is de-privileged.
3. [ ] Open the `company` row in `GIT ACCOUNTS`, click the **rotate
       token** affordance (UI design per row 5-9), paste the new PAT,
       click SAVE.
4. [ ] Verify the UI now shows the new token's last-4 fingerprint
       (`…wxyz`). The `version` column (if visible in the UI; else
       via REST) bumped by 1.
5. [ ] Immediately trigger a company-repo clone / PR operation. It
       **must succeed on the new token**. If it fails with 401, the
       Redis / SharedKV / pool read-after-write guarantee is broken
       and rotation isn't propagating — **escalate, do not continue**.
6. [ ] **De-privilege the OLD PAT**: revoke it on github.com. Wait
       30 s (GitHub's edge cache window), then trigger another
       company-repo clone. It must still succeed (because the backend
       is using the NEW token, not the old). If this fails, the
       backend retained the old token somewhere.

### Audit trace verification

7. [ ] Query audit_log for the rotation row. From `psql` against the
       production PG:
   ```sql
   SELECT id, ts, actor, action, entity_id, before_json, after_json
     FROM audit_log
    WHERE tenant_id = '<your-tenant-id>'
      AND entity_kind = 'git_account'
      AND action = 'git_account.update'
    ORDER BY id DESC LIMIT 5;
   ```
8. [ ] Confirm the most recent row has:
   - `entity_id` = the `company` account's id (e.g. `ga-xxxxxxxxxxxx`)
   - `after_json` mentions `token_fingerprint` with the new last-4
   - **`before_json` and `after_json` do NOT contain either the old
     or new plaintext PAT** — greppable rule:
     ```bash
     grep -E "ghp_<old-pat-first-10-chars>|ghp_<new-pat-first-10-chars>" audit_dump.txt
     ```
     should return zero matches. **If greppable, it's a critical
     leak** — rotation itself triggered the exposure it was meant to
     prevent.
9. [ ] Confirm the chain is intact post-rotation:
   ```python
   # In a python3 shell on the backend host:
   import asyncio
   from backend import audit
   from backend.db_context import set_tenant_id
   set_tenant_id("<your-tenant-id>")
   asyncio.run(audit.verify_chain())
   ```
   Expected: `(True, None)`. Anything else means the rotation broke
   hash chaining.

---

## Per-tenant isolation drill

Goal: confirm that tenant A's credentials are invisible to tenant B
on every UI and REST surface, **beyond** what the automated
`test_phase5_11_tenant_isolation.py` suite exercises.

1. [ ] Log in as `admin@tenant-a.example`. Note what rows
       `GIT ACCOUNTS` panel shows (from the soak, at least 8 rows
       for `t-default`; or re-seed a smaller set for tenant A).
2. [ ] Log out. Log in as `admin@tenant-b.example` in a different
       browser (or incognito).
3. [ ] Navigate to `Settings → Integrations → GIT ACCOUNTS`.
       **Expected: empty list** (or whatever B independently set up
       — but ZERO rows from tenant A).
4. [ ] Try the REST surface directly from tenant B's session:
   - [ ] `GET /api/v1/git-accounts` → empty or only B's rows.
   - [ ] `GET /api/v1/git-accounts/<id-from-tenant-A>` → HTTP 404.
   - [ ] `PATCH /api/v1/git-accounts/<id-from-tenant-A>`
         with body `{"label": "hijack"}` → HTTP 404 (not 403 — we
         don't acknowledge the row exists at all).
   - [ ] `DELETE /api/v1/git-accounts/<id-from-tenant-A>` → HTTP 404.
   - [ ] `POST /api/v1/git-accounts/<id-from-tenant-A>/test` → 404.
   - [ ] `POST /api/v1/git-accounts/resolve?url=https://github.com/<tenant-a-org>/repo`
         → **either** HTTP 404 (no match in B's scope) **or** 200
         with B's own matching row. **Never** returns tenant A's row.
5. [ ] Switch back to tenant A; confirm all original rows are intact
       and unchanged (no side effect from B's probing).

---

## Legacy migration dry-run on staging

**If** your staging environment still has legacy credentials in
`.env` (`OMNISIGHT_GITHUB_TOKEN` / `OMNISIGHT_GITLAB_TOKEN_MAP` /
`OMNISIGHT_GERRIT_INSTANCES` / `OMNISIGHT_NOTIFICATION_JIRA_*`),
preview what the Phase-5-5 auto-migration would do before running
it for real.

1. [ ] On the staging backend host:
   ```bash
   cd /opt/omnisight-productizer   # or wherever the code lives
   OMNISIGHT_CREDENTIAL_MIGRATE=  # ensure kill-switch NOT set to 'skip'
   python3 scripts/migrate_legacy_credentials_dryrun.py
   ```
2. [ ] Review the candidate-row list. Expect one row per:
   - scalar `github_token` (`is_default=true`, id
     `ga-legacy-github-github-com`)
   - each entry in `github_token_map` (non-default)
   - scalar `gitlab_token` (default)
   - each entry in `gitlab_token_map`
   - each entry in `gerrit_instances`
   - scalar gerrit iff `gerrit_enabled=true` and not already in
     `gerrit_instances`
   - single JIRA row iff both URL + token set.
3. [ ] Run with probe:
   ```bash
   python3 scripts/migrate_legacy_credentials_dryrun.py --probe-db
   ```
   - Expected on a **clean** staging DB: `git_accounts is EMPTY —
     the next backend boot will insert the rows listed above.`
   - Expected on a **re-run** (rows already migrated): `git_accounts
     already has N row(s). The migration hook will SKIP —
     operator-managed table.`
4. [ ] Sign off: save the dry-run output
   (`--json > migration-preview-$(date +%F).json`) as a CI
   attestation / audit-trail artifact. This is the pre-state you
   can point back to if you later need to prove "the migration
   produced exactly what was planned."
5. [ ] When ready to actually migrate, follow
   [`02-migration-runbook.md`](./02-migration-runbook.md) Path A.
   The dry-run is a preview, not the migration itself.

---

## Acceptance criteria

Required for flipping TODO.md row 5-11 from `[O]` to `[D]`:

- [ ] Sections S1 – S4 all green (31/31 checkboxes).
- [ ] Token rotation drill green (9/9 checkboxes including chain
      verify).
- [ ] Per-tenant isolation drill green (5/5 checkboxes).
- [ ] Legacy dry-run completed on staging; output archived.

Required for flipping row 5-11 from `[D]` to `[V]`:

- [ ] 24 h observation window **after** the soak completed, with:
  - Zero 401 / 403 errors in backend log for `git_*` / `webhook` /
    `issue_tracker` paths.
  - Zero `MissingCredentialError` logs (if one fires, a call site
    lost its credential resolution chain).
  - Zero `git_account.*` entries in audit_log not explained by
    legitimate operator activity.
  - Zero chain-verify failures via
    `scripts/audit_archive.py verify --tenant=<tid>` (if that tool
    exists; else a manual `verify_chain` call per tenant).

---

## If something fails

1.  **Identify which layer failed** — match the failing checkbox
    against the automated test that covers the same contract:

    | Soak section                | Automated test                                  |
    |-----------------------------|-------------------------------------------------|
    | S1 resolver probe (4-7)     | `test_phase5_11_tenant_isolation::test_pick_account_for_url_*` (isolation negative) + `test_git_accounts_crud::test_three_github_accounts_resolve_by_url_pattern` (positive) |
    | S1 webhook HMAC (#12)       | `test_phase5_7_call_site_sweep::test_github_webhook_hmac_*` |
    | S3 SSH-key decrypt (23-25)  | `test_phase5_7_call_site_sweep::test_ssh_args_for_account_uses_per_account_key` |
    | S4 JIRA (27-31)             | `test_phase5_8_call_site_sweep::test_sync_jira_*` |
    | Rotation drill (7-9)        | `test_phase5_11_rotation_drill::test_rotation_writes_audit_row_without_plaintext` + `test_audit_chain_intact_after_rotation` |
    | Per-tenant isolation        | `test_phase5_11_tenant_isolation::*` (whole file) |

    If the automated test passes but the soak fails, the bug is
    between the code contract and the production deployment —
    usually a missing env var, an unwired router, or an image-rebuild
    lag. Re-check prerequisites.

2.  **Rollback is compose-scoped, not schema-scoped**:
    - Revert the Phase-5-9 frontend commit → rebuild `frontend` image
      → rolling restart. The `GIT ACCOUNTS` UI disappears; the
      backend `/api/v1/git-accounts` REST routes remain live but have
      no consumer.
    - Set `OMNISIGHT_CREDENTIAL_MIGRATE=skip` to suppress the
      lifespan auto-migration on subsequent boots.
    - Do **NOT** drop the `git_accounts` table — it's shared with
      rows 5-1 through 5-10 and dropping it on prod would require a
      restore-from-backup to recover.
    - The legacy `.env` knobs still work (the
      `_build_registry` shim falls back to them when `git_accounts`
      is empty), so a full rollback leaves you where you were pre-
      Phase-5.

3.  **Audit-chain break** is the worst outcome — it means rotation
    corrupted the tamper-evidence guarantee. Do NOT continue other
    ops until fixed:
    - Snapshot the audit_log table immediately
      (`pg_dump -t audit_log`).
    - Identify the first bad row from `verify_chain`'s `bad` return
      value.
    - Examine rows before/after to understand the gap.
    - File a P0 bug; do not roll the chain forward by inserting
      synthetic rows — that makes the break undetectable later.

---

## Appendix — commands cheat sheet

```bash
# Rebuild backend + frontend images after merging Phase 5-9:
docker compose -f docker-compose.prod.yml build backend-a backend-b frontend

# Rolling recreate (drain → recreate → healthy):
docker compose -f docker-compose.prod.yml up -d --no-deps backend-a
# wait for healthy
docker compose -f docker-compose.prod.yml up -d --no-deps backend-b
# wait for healthy
docker compose -f docker-compose.prod.yml up -d --no-deps frontend

# Dry-run preview:
python3 scripts/migrate_legacy_credentials_dryrun.py --probe-db

# Chain verify (single tenant) — run from a python3 shell on backend host:
python3 -c "
import asyncio
from backend.db_pool import init_pool, close_pool
from backend import audit
from backend.db_context import set_tenant_id
async def main():
    await init_pool()
    try:
        set_tenant_id('t-default')
        ok, bad = await audit.verify_chain()
        print('chain ok:' if ok else 'chain BROKEN at id:', bad)
    finally:
        await close_pool()
asyncio.run(main())
"

# Audit query for rotation trace:
psql postgresql://$DB_USER:$DB_PASS@pg-primary:5432/omnisight -c "
  SELECT id, ts, actor, action, entity_id,
         substring(after_json FROM 'token_fingerprint\":\"([^\"]+)') AS new_fp
    FROM audit_log
   WHERE tenant_id = 't-default'
     AND entity_kind = 'git_account'
     AND action = 'git_account.update'
   ORDER BY id DESC LIMIT 10;
"
```

---

## Related documents

- [01-design.md](./01-design.md) — git_accounts schema + resolver decisions
- [02-migration-runbook.md](./02-migration-runbook.md) — legacy → git_accounts migration SOP
- [docs/ops/git_credentials.md](../ops/git_credentials.md) — ongoing operator
  runbook (add / rotate / delete / resolve)
- `backend/tests/test_phase5_11_tenant_isolation.py` — automated isolation contract
- `backend/tests/test_phase5_11_rotation_drill.py` — automated rotation contract
- `backend/tests/test_phase5_11_dryrun_script.py` — dry-run script contract
- `scripts/migrate_legacy_credentials_dryrun.py` — dry-run tool
