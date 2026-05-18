# GitLab Container Registry — Policy & Prod Pull Credentials

> P2 of the GitLab Container Registry migration (META OP-1468). Owner
> deliverable for OP-1470. Companion to `docs/sprint-s12/phase-31f-ticket-spec.md`
> (Phase 31.F — cosign + GHCR → GitLab CR migration) and to the GHCR
> retention job in `.github/workflows/build-images.yml`.
>
> Audience: platform operator standing up GitLab CR for the
> `sora.services:49154` self-hosted instance and provisioning the
> read-only token used by prod hosts.

This document covers four operator-owned procedures:

1. **Enabling** the container registry on the project.
2. **Cleanup policy** that mirrors the GHA `retention` job.
3. **Provisioning a `read_registry` token** for the prod docker daemon.
4. **Rotation** of that token.

The administrative GitLab actions (project setting toggle, cleanup
policy, deploy-token creation, prod-host secret deployment) are NOT
automated — they are operator steps. This runbook is the source of
truth for what those steps are, where the resulting secret lives, and
how to refresh it.

## 1. Verify container registry is enabled on the project

For each project that will host images under `sora.services:49154`
(initially `omnisight/omnisight-productizer`, then `omnisight/sora-bridge`):

1. Sign in to `https://sora.services:49154` as a Maintainer (or
   Owner) on the project.
2. Navigate **Settings → General → Visibility, project features,
   permissions**.
3. Confirm **Container registry** is toggled **ON**.
4. Save.

Verify from a workstation:

```bash
curl -sSf -o /dev/null -w '%{http_code}\n' \
  https://sora.services:49154/v2/
# 401 (auth required) is the healthy response on a registry that is
# enabled. 404 means the registry is not enabled on the instance.
```

Verify the project-scoped registry endpoint responds (after login):

```bash
TOKEN="$(cat ~/.config/omnisight/gitlab-cr-pull-token)"
USER="omnisight-cr-puller"
curl -sSf -u "${USER}:${TOKEN}" \
  https://sora.services:49154/v2/omnisight/omnisight-productizer/tags/list
# Expect HTTP 200 + a JSON tag list (possibly empty during phase 31.F).
```

## 2. Cleanup policy (mirrors the GHA `retention` job)

The GHA workflow at `.github/workflows/build-images.yml` enforces:

- Tagged versions (`v*`, `latest`, any non-empty tag list) kept forever.
- Untagged versions younger than 30 days kept.
- Hard floor of the last 20 untagged versions per package.

GitLab's project-level cleanup policy uses a different but equivalent
shape. For OP-1470 the policy mirrors GHA via:

| GitLab field | Value | Why |
|---|---|---|
| Cleanup policy enabled | `true` | required to schedule any sweep |
| Run cleanup | `Every week` | matches GHA `cron: "17 3 * * 1"` cadence |
| Tags older than | `30 days` | matches GHA `MAX_AGE_DAYS=30` |
| Name regex (tags to remove) | `^(sha-[0-9a-f]+\|[0-9a-f]{7,40})$` | targets SHA-style tags only |
| Name regex (tags to keep) | `^(v.*\|latest)$` | NEVER delete `v*` + `latest` |
| Keep N tags matching `name_regex_delete` | `30` | matches "keep last 30 by SHA" |

Configure via the GitLab UI:

1. **Settings → Packages and registries → Container Registry →
   Clean up image tags**.
2. Toggle **Enable cleanup policies on this project**.
3. Fill the fields per the table above.
4. **Save**.

Or via the API (operator with `api` scope token):

```bash
curl -sSf --request PUT \
  --header "PRIVATE-TOKEN: ${GITLAB_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{
    "container_expiration_policy_attributes": {
      "enabled": true,
      "cadence": "7d",
      "keep_n": 30,
      "older_than": "30d",
      "name_regex_delete": "^(sha-[0-9a-f]+|[0-9a-f]{7,40})$",
      "name_regex_keep": "^(v.*|latest)$"
    }
  }' \
  "https://sora.services:49154/api/v4/projects/omnisight%2Fomnisight-productizer"
```

Verify the policy reads back the values you set:

```bash
curl -sSf \
  --header "PRIVATE-TOKEN: ${GITLAB_ADMIN_TOKEN}" \
  "https://sora.services:49154/api/v4/projects/omnisight%2Fomnisight-productizer" \
  | jq '.container_expiration_policy'
```

Important behavioural notes (carry-over from the GHA design):

- The "keep last 30" floor only protects matches of
  `name_regex_delete`. Anything matched by `name_regex_keep` is
  unconditionally retained — `v*` releases are NEVER pruned.
- GitLab's cleanup runs on a project worker. If the policy fails
  (timeout, registry GC contention), GitLab surfaces it under
  **Settings → Packages and registries → Cleanup policy → Last run**.
  Treat a stuck policy the same as the GHA `retention` job failing —
  page the on-call platform operator.

Repeat the policy configuration for every project added to GitLab CR.
There is no instance-wide default; the policy is per-project.

## 3. Provision the prod pull token

The prod docker daemon needs to pull images from
`sora.services:49154`. It only needs **read** access. The credential
used is a project-scoped **deploy token** with the `read_registry`
scope.

Why a deploy token (not a personal access token, not a project access
token):

- Deploy tokens are decoupled from any human user — they survive
  staff turnover, and they cannot be promoted to write scopes by
  accident.
- They are project-scoped, so the blast radius of a leak is one
  project's registry.
- The token has its own username — prod docker login uses that
  username, not a human's.

### 3.1 Generate the deploy token

1. Sign in as an Owner on the project (or as a Maintainer with deploy
   token rights enabled).
2. Navigate **Settings → Repository → Deploy tokens**.
3. Click **Add token**.
4. Fill:
   - **Name**: `prod-docker-pull` (this is also the displayed token
     username if you leave `username` blank — but set it explicitly).
   - **Username**: `omnisight-cr-puller`
   - **Expiration date**: today + 6 months (matches the rotation
     cadence in `configs/credentials.yaml`; see §4 below).
   - **Scopes**: `read_registry` (and ONLY `read_registry` — do NOT
     grant `read_repository`, `write_registry`, or `api`).
5. **Create deploy token**.
6. Copy the token value — GitLab will not show it again.

Repeat for each project the prod host needs to pull from.

### 3.2 Where the token lives on prod

Canonical path on the prod host:

```
~/.config/omnisight/gitlab-cr-pull-token
```

(Parallel to the existing `~/.config/omnisight/ghcr-pull-token` that
Phase 31.A introduced for GHCR.)

File permissions and ownership:

```bash
mkdir -p ~/.config/omnisight
chmod 700 ~/.config/omnisight

# Write the token without it landing in shell history:
printf '%s' '<paste-token-here>' > ~/.config/omnisight/gitlab-cr-pull-token
# (Or pipe from a password manager.)

chmod 600 ~/.config/omnisight/gitlab-cr-pull-token
```

The file contains **only the token** — no surrounding JSON, no
trailing newline preference required (`docker login --password-stdin`
strips one trailing newline, so either form works). Do not commit
this file. Do not log it. Do not include it in CI artifacts.

The deploy-token username (`omnisight-cr-puller` from §3.1) is NOT
secret. It can live alongside the token in a sibling file if scripts
need it:

```bash
printf 'omnisight-cr-puller\n' > ~/.config/omnisight/gitlab-cr-pull-user
chmod 644 ~/.config/omnisight/gitlab-cr-pull-user
```

### 3.3 `docker login` recipe

The exact command on the prod host:

```bash
docker login sora.services:49154 \
  -u "$(cat ~/.config/omnisight/gitlab-cr-pull-user)" \
  --password-stdin \
  < ~/.config/omnisight/gitlab-cr-pull-token
```

- The token is piped on stdin via `--password-stdin`. **Never** pass
  it with `-p <token>` — that puts the secret in `ps` output and in
  the shell history.
- `docker login` writes an entry under
  `~/.docker/config.json` (`auths."sora.services:49154"`). That file
  is base64-encoded credentials, not encrypted — protect the home
  directory accordingly (`chmod 700 ~`).
- Successful output looks like:
  ```
  Login Succeeded
  ```

Smoke test the credentials:

```bash
# Anonymous probe — should 401:
curl -sSf -o /dev/null -w '%{http_code}\n' \
  https://sora.services:49154/v2/omnisight/omnisight-productizer/tags/list
# 401

# Authenticated probe — should 200 (even on an empty tag list):
curl -sSf -u "$(cat ~/.config/omnisight/gitlab-cr-pull-user):$(cat ~/.config/omnisight/gitlab-cr-pull-token)" \
  https://sora.services:49154/v2/omnisight/omnisight-productizer/tags/list
# {"name":"omnisight/omnisight-productizer","tags":[...]}
```

End-to-end pull (placeholder image is fine during Phase 31.F before
real images land):

```bash
docker pull sora.services:49154/omnisight/omnisight-productizer:placeholder
# Pulls; "image not found" is the expected response if no
# :placeholder tag has been pushed yet AND the registry honored the
# credentials (vs returning 401).
```

If the pull returns `unauthorized` after a fresh login, the
deploy-token scope is wrong (probably missing `read_registry`) — go
back to §3.1.

## 4. Rotation

### 4.1 Cadence

Default rotation: **every 6 months**. Track in
`configs/credentials.yaml` (see `docs/operations/credential_rotation_runbook.md`
for the inventory contract). The corresponding inventory entry has:

```yaml
- id: gitlab-cr-pull-token
  type: gitlab-deploy-tokens
  expiry: <today + 6 months>
  last_rotated_at: <today>
  secret_ref: file:~/.config/omnisight/gitlab-cr-pull-token@prod
  notes: |
    Project-scoped deploy token, scope=read_registry. Username =
    omnisight-cr-puller. Used by prod docker daemon to pull from
    sora.services:49154. See docs/operations/gitlab-cr-pull-credentials.md
    §3 for the full provisioning recipe.
```

Rotate ahead of expiry on the **30-day warning** band, not at expiry
— the rotation requires a docker daemon re-login and a prod-host
permission to write the credential file. Both want a planned window.

### 4.2 Rotation steps

1. **Generate the new token** in GitLab (project **Settings →
   Repository → Deploy tokens → Add token**) with the same
   username (`omnisight-cr-puller`), same scope
   (`read_registry`), and the next 6-month expiry. Click
   **Create deploy token**. Copy the token.

   - GitLab's API alternative:
     ```bash
     curl -sSf --request POST \
       --header "PRIVATE-TOKEN: ${GITLAB_ADMIN_TOKEN}" \
       --data "name=prod-docker-pull-rotated-$(date +%Y%m%d)" \
       --data "username=omnisight-cr-puller" \
       --data "scopes[]=read_registry" \
       --data "expires_at=$(date -u -d '+6 months' +%Y-%m-%d)" \
       "https://sora.services:49154/api/v4/projects/omnisight%2Fomnisight-productizer/deploy_tokens"
     ```
     The response includes `token` — that is the only time you can
     read it.

2. **Stage the new token alongside the old one** on the prod host.
   Do NOT overwrite the live file until the new token has been
   verified — a failed rotation that already overwrote the live
   secret leaves prod with no working credential.

   ```bash
   printf '%s' '<new-token>' > ~/.config/omnisight/gitlab-cr-pull-token.new
   chmod 600 ~/.config/omnisight/gitlab-cr-pull-token.new
   ```

3. **Verify the new token** out-of-band against the registry:

   ```bash
   curl -sSf \
     -u "omnisight-cr-puller:$(cat ~/.config/omnisight/gitlab-cr-pull-token.new)" \
     https://sora.services:49154/v2/omnisight/omnisight-productizer/tags/list \
     | jq -r '.name'
   # Expect: omnisight/omnisight-productizer
   ```

4. **Atomically swap** the new token into place and re-login docker.
   `mv` on the same filesystem is atomic — a concurrent reader sees
   either the old or the new file, never a half-written one:

   ```bash
   mv ~/.config/omnisight/gitlab-cr-pull-token.new \
      ~/.config/omnisight/gitlab-cr-pull-token

   docker login sora.services:49154 \
     -u "$(cat ~/.config/omnisight/gitlab-cr-pull-user)" \
     --password-stdin \
     < ~/.config/omnisight/gitlab-cr-pull-token
   # Expect: Login Succeeded
   ```

5. **Smoke a pull**:

   ```bash
   docker pull sora.services:49154/omnisight/omnisight-productizer:placeholder \
     || true  # "manifest unknown" is OK during phase 31.F pre-publish
   ```

   If docker reports `unauthorized: HTTP Basic: Access denied`, the
   new token is wrong / not yet active — restore from the
   `~/.config/omnisight/gitlab-cr-pull-token.new` backup (kept until
   the next rotation), revoke the new token in GitLab, and re-run §1.

6. **Revoke the old token** in GitLab (**Settings → Repository →
   Deploy tokens → Revoke** on the previous row). Do not revoke
   before the new token is live and verified.

7. **Update the inventory** (`configs/credentials.yaml`):

   ```yaml
   - id: gitlab-cr-pull-token
     expiry: <today + 6 months>
     last_rotated_at: <today>
   ```

8. **Re-run the expiry check** to confirm the alert clears:

   ```bash
   python3 scripts/credential_expiry_check.py --inventory configs/credentials.yaml
   ```

   The rotation is complete only when the daily credential-expiry
   check moves the row out of the `ALERTS` band — same contract as
   every other rotation in `docs/operations/credential_rotation_runbook.md`.

### 4.3 Emergency rotation (suspected compromise)

If the token may have leaked (committed to source, surfaced in a CI
artifact, posted in chat):

1. **Revoke first**, verify later. In GitLab: **Settings → Repository →
   Deploy tokens → Revoke**. Prod will stop being able to pull on the
   next docker login attempt — that is the desired state.
2. File an incident ticket linked to this runbook §4.3 (label
   `incident:credential-leak`).
3. Generate the replacement per §4.2 steps 1–7. Skip the
   "keep both alive" overlap from step 2 — the revoked-first approach
   trades a brief prod-pull outage for guaranteed exposure stop.
4. Scan the GitLab CR audit log for pulls/pushes by the compromised
   token between the leak time and the revoke time. Capture in the
   incident ticket.
5. Update the inventory with the post-incident `last_rotated_at`
   and a `notes:` line citing the incident ticket.

## 5. Common pitfalls

- **Wrong scope on the token.** A token with only `read_repository`
  will authenticate at the GitLab API but fail at the container
  registry with `unauthorized`. Verify `scope = read_registry` in
  the deploy-token row.
- **Forgetting the port.** `sora.services` (no port) is the Gerrit
  HTTPS endpoint via the reverse proxy. `sora.services:49154` is the
  container registry. `docker login sora.services` will go to the
  wrong host. Always include `:49154`.
- **`docker login -p`.** Never. Use `--password-stdin`. This is also
  enforced by the Phase 31.E `ci-yml-validator` for any
  `.gitlab-ci.yml` (rejects `docker login -p`). The same hygiene
  applies on prod hosts.
- **Cleanup policy not per-project.** GitLab does not have an
  instance-wide cleanup policy default. Every project added to
  GitLab CR must have §2 applied independently. A new project with
  no policy will accumulate untagged versions forever.
- **Confusing the deploy token with the cosign key.** The deploy
  token controls *pulling* from GitLab CR (this doc). The cosign
  keypair (Phase 31.F-1bc / 31.F-6a) controls *signing* — those are
  separate credentials with separate rotation. A rotation of one
  does NOT rotate the other.
- **Pull works locally, fails on prod.** Almost always the prod
  host's docker daemon hasn't picked up the new credential. Re-run
  the `docker login` from §3.3 on the prod host — `~/.docker/config.json`
  is per-user, and the docker daemon's pull path reads from the
  uid that invoked it (or the daemon's own config if pulled by
  systemd unit). Confirm which user the pull runs as.

## 6. Out of scope

Per the OP-1470 ticket non-goals:

- **No compose-file changes.** docker-compose.prod.yml is updated in
  Phase 3 of the migration (a separate ticket). Until then, prod
  continues to pull from GHCR by default — this token exists so the
  Phase 3 flip is a config swap, not a credential scramble.
- **No image publishing.** Pushing real images to GitLab CR is
  Phase 4 (and is gated on cosign sign + verify roundtrip from
  Phase 31.F-SignSmoke). A `:placeholder` tag is sufficient to
  exercise this runbook end-to-end.

## 7. Where this fits in the bigger picture

| Phase | Status after OP-1470 |
|---|---|
| Phase 1 — GitLab project + registry exists | DONE before this ticket |
| **Phase 2 — Registry policy + pull credentials (OP-1470)** | **this ticket** |
| Phase 3 — Compose flip to pull from GitLab CR | follow-up ticket |
| Phase 4 — Publish real images (post 31.F-SignSmoke) | follow-up ticket |

Cross-references:

- META: OP-1468 (GitLab Container Registry migration).
- Phase 31.F spec (cosign + CR migration coupling):
  `docs/sprint-s12/phase-31f-ticket-spec.md`.
- GHCR retention reference implementation:
  `.github/workflows/build-images.yml` (`retention` job) + the policy
  comment block at lines 269–284.
- Credential rotation contract (inventory + daily expiry check):
  `docs/operations/credential_rotation_runbook.md`.
