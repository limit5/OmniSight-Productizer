# Prod Deploy Orchestrator — Operator Runbook

**Ticket:** OP-881 (D9, META OP-761 Sprint D §Phase 3) • **Status:** active

> **2026-05-18 image-pipeline cutover (OP-1474 / META OP-1468):** the
> canonical container registry for production images is now
> **GitLab Container Registry** (`registry.gitlab.com/omnisight/...`).
> The legacy `ghcr.io/omnisight/...` namespace is frozen as of Phase 5
> cutover and reads only — do not push or pull from it. Rationale and
> tradeoffs in [ADR-0038](../adr/ADR-0038-image-pipeline-on-gitlab.md).

This runbook covers the `POST /api/v1/prod/deploy` endpoint and its CLI
twin `scripts/prod_deploy_runbook.py`. It is the only sanctioned path
for promoting an image from staging to prod once D6/D7/D8 have produced
the prerequisites (image build, prod secrets, smoke suite).

---

## 1. What the orchestrator does

A single call drives four side-effecting steps in order; any failure
aborts the whole attempt and rolls forward to a terminal status. No
partial progress is left visible to clients.

```
            ┌─────────────────────────────────────────────────────┐
            │           POST /api/v1/prod/deploy                  │
            │  body: release_id, image_tag, reason, approval_token│
            │  hdr:  X-Prod-Deploy-Signature: sha256=<hmac>       │
            └────────────────────┬────────────────────────────────┘
                                 │
       ┌─────────────────────────┴───────────────────────────────┐
       │ approval gate                                            │
       │  • verify HMAC-SHA256(body, WEBHOOK_SECRET)              │
       │  • verify Slack DM confirmation token (out-of-band)      │
       └─────────────────────────┬───────────────────────────────┘
                                 │
   ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
   │ 1. image_pull  │→ │ 2. secrets     │→ │ 3. smoke       │→ │ 4. blue-green  │
   │   (D2)         │  │    decrypt     │  │    pre-check   │  │    switch      │
   │                │  │    (D3)        │  │    on staging  │  │    on prod     │
   │                │  │                │  │    mirror (D7) │  │                │
   └────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘
                                                                       │
                                                                       ▼
                                                          status = "completed"
```

The orchestrator records a `running` row in `prod_deploy_audit` keyed
on `release_id` **before** any side effect, then rolls it forward to
the terminal status. The hash-chained compliance log
(`deploy_audit`, OP-779 D18) is also written: one `started` row on
entry, one `succeeded` / `failed` row on exit.

---

## 1a. Required env keys (registry + signature)

Updated for the OP-1474 GitLab CR cutover. The orchestrator host (and
the CLI twin) must have all of the following set before invoking
`POST /api/v1/prod/deploy` or `scripts/prod_deploy_runbook.py`. Missing
keys fail the approval gate (`OperatorApprovalRefused`) before any
side effect.

| Env key                                  | Purpose                                                         | Source                                                          |
| ---------------------------------------- | --------------------------------------------------------------- | --------------------------------------------------------------- |
| `OMNISIGHT_IMAGE_REGISTRY`               | Canonical registry host. **Pin to `registry.gitlab.com`.**       | operator host config; do not fall back to `ghcr.io`             |
| `OMNISIGHT_IMAGE_NAMESPACE`              | `omnisight/productizer` (unchanged across the cutover)           | repo config                                                     |
| `OMNISIGHT_REGISTRY`                     | Compose image prefix for `docker-compose.prod.yml` / staging. Defaults to `ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}` until G5; set to `sora.services:49154/omnisight` for the GitLab CR mirror after G4 dual-publish. | `.env`, `.env.staging`, or operator shell                       |
| `OMNISIGHT_GITLAB_CR_USER`               | GitLab CR pull/push principal (deploy-token user)                | 1Password → `omnisight/gitlab-cr/deploy-token`                  |
| `OMNISIGHT_GITLAB_CR_TOKEN`              | GitLab CR deploy-token secret (read_registry + write_registry)   | same 1Password entry                                            |
| `OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET`   | HMAC-SHA256 key for the `X-Prod-Deploy-Signature` header         | rotated via `scripts/rotate_webhook_secret.sh`                  |
| `OMNISIGHT_COSIGN_PUB`                   | Path to the cosign public key used to verify the pulled digest    | committed at `deploy/cosign/cosign.pub`                         |
| `OMNISIGHT_SORA_SSH_KEY`                 | SSH key used by the release-cut step to push to GitLab over SSH  | `~/.ssh/id_ed25519_sora` (mode 600)                             |

Validate before triggering:

```bash
: "${OMNISIGHT_IMAGE_REGISTRY:?}"
: "${OMNISIGHT_IMAGE_NAMESPACE:?}"
: "${OMNISIGHT_REGISTRY:=ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}}"
: "${OMNISIGHT_GITLAB_CR_USER:?}"
: "${OMNISIGHT_GITLAB_CR_TOKEN:?}"
: "${OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET:?}"

# Confirm registry reachability with the deploy-token (no docker pull yet).
echo "$OMNISIGHT_GITLAB_CR_TOKEN" \
  | docker login "$OMNISIGHT_IMAGE_REGISTRY" \
      -u "$OMNISIGHT_GITLAB_CR_USER" --password-stdin
docker manifest inspect \
  "$OMNISIGHT_IMAGE_REGISTRY/$OMNISIGHT_IMAGE_NAMESPACE:$IMAGE_TAG" >/dev/null
```

`OMNISIGHT_IMAGE_REGISTRY` remains the orchestrator's canonical
registry host. `OMNISIGHT_REGISTRY` is the compose-side image prefix
introduced by OP-1487 so the same digest lock can render either
`ghcr.io/your-org/omnisight-*` or
`sora.services:49154/omnisight/omnisight-*` references during the G3-G5
transition.

If `OMNISIGHT_IMAGE_REGISTRY` is still set to `ghcr.io` on the deploy
host, **stop** — that is a stale config from the pre-cutover era. Pull
the latest config from the operator host bootstrap (see
`docs/operations/release-runbook.md` §0 pre-flight) and retry.

---

## 2. How to trigger a deploy

### 2.1 HTTP (preferred — webhook signed)

```bash
RELEASE_ID="2026.05.11-rc1"
IMAGE_TAG="v2026.05.11"
APPROVAL_TOKEN="$(slack-cli get-prod-deploy-token "$RELEASE_ID")"

BODY=$(jq -nc \
  --arg release_id "$RELEASE_ID" \
  --arg image_tag "$IMAGE_TAG" \
  --arg approval_token "$APPROVAL_TOKEN" \
  --arg reason "$1" \
  '{$release_id, $image_tag, $approval_token, $reason}')

SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET" | awk '{print $2}')"

curl -fsSL \
  -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Prod-Deploy-Signature: $SIG" \
  --data-binary "$BODY" \
  https://api.omnisight.internal/api/v1/prod/deploy
```

Exit status:

| HTTP | Meaning                              | Action                                      |
| ---- | ------------------------------------ | ------------------------------------------- |
| 200  | deploy completed (idempotent or new) | tail prod logs; no action                   |
| 403  | `OperatorApprovalRefused`            | re-issue Slack token; re-sign with secret   |
| 409  | concurrent deploy in flight          | wait for the other request to finish        |
| 412  | `PreCheckSmokeFailed`                | inspect staging mirror; **do not retry**    |
| 422  | bad body                             | fix the JSON; do not re-sign garbage        |
| 504  | `DeployTimeoutExceeded`              | rollback already initiated; investigate     |

### 2.2 CLI (sandbox-mirror DoD only)

```bash
python scripts/prod_deploy_runbook.py \
    --release-id 2026.05.11-rc1 \
    --image-tag v2026.05.11 \
    --reason 'sandbox-mirror DoD run' \
    --approval-token "$APPROVAL_TOKEN" \
    --actor "op@example.test"
```

`--insecure-skip-webhook-signature` is reserved for the sandboxed
mirror DoD run and is logged loudly in `prod_deploy_audit.context_json`.
Never pass it against real prod.

---

## 3. Error catalog & rollback paths

The orchestrator's error catalog (`OP-881 D9 §AC`) maps directly to
the `aborted_*` audit statuses and the SSE event payload's
`error_class` field.

### 3.1 `OperatorApprovalRefused` → `aborted_approval_refused`

**Trigger:** webhook signature mismatch OR Slack DM token not in the
allowed set.

**Side effects observed:** none — no docker pull, no secrets decrypt,
no smoke run. The orchestrator aborts before any external command.

**Recovery:** there is nothing to roll back. Re-issue the Slack token
(this is intentionally short-lived) and re-sign with the rotated
`OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET` if a leak is suspected.

### 3.2 `PreCheckSmokeFailed` → `aborted_smoke_failed`

**Trigger:** `backend.staging_validation.run_smoke_suite` returns one
or more failed synthetic transactions against the staging mirror.

**Side effects observed:**
* image pulled (step 1)
* secrets decrypted into memory (step 2)
* smoke run (step 3, failed)
* **blue-green switch NEVER fired** — prod is untouched.

**Recovery:** inspect the failures field in the SSE
`prod.deploy.aborted` payload (or `prod_deploy_audit.error_message`),
fix the root cause on staging, redeploy to staging mirror, then
retry with a **new** `release_id` (the old one is now in
`aborted_smoke_failed` and the idempotence key would refuse a retry
with the same id).

### 3.3 `DeployTimeoutExceeded` → `aborted_timeout`

**Trigger:** elapsed time (monotonic) crosses
`deploy_timeout_seconds` (default 1800s / 30 min). Checked at the
start of every step AND after the blue-green switch returns.

**Side effects observed:** depends on which guard tripped. The
`prod_deploy_audit.last_step` column tells you exactly which step
ran last — recover from that step using the per-step rollback
playbook:

| `last_step`         | Rollback action                                       |
| ------------------- | ----------------------------------------------------- |
| `approval`          | none required                                         |
| `image_pull`        | `docker image rm` the pulled tag                      |
| `secrets_decrypt`   | none — decrypted blob is in memory only               |
| `smoke_pre_check`   | none — staging mirror is the side-effect domain       |
| `blue_green_switch` | `scripts/deploy.sh --rollback` (legacy runbook §4.2)  |
| `completed`         | the timeout fired *after* success — investigate clock |

---

## 4. Idempotence contract (AC #4)

A `release_id` is the deduplication key for the whole pipeline. The
orchestrator guarantees:

1. A duplicate POST with `release_id = X` whose ledger row is
   `status='completed'` returns HTTP 200 with
   `idempotent_replay=true` and runs **zero** side effects (no
   docker pull, no SSE re-emit, no audit chain entry).
2. A duplicate POST whose ledger row is `status='running'` returns
   HTTP 409 (`ConcurrentDeployInFlight`). The race-loser's POST
   never publishes `prod.deploy.started`.
3. A duplicate POST whose row is `aborted_*` is treated as a fresh
   attempt **only if you supply a new `release_id`**. Reusing an
   `aborted_*` release_id will fail the UNIQUE constraint.

Rationale for §3: an `aborted_*` outcome means we have not validated
the prerequisites, so we don't want a quick retry under the same id
to skip the smoke step.

---

## 5. SSE event contract (AC #5)

Three events on the global SSE bus
(`prod.deploy.{started,completed,aborted}`):

```json
// prod.deploy.started
{"release_id": "...", "image_tag": "...", "actor": "...", "timestamp": "..."}

// prod.deploy.completed
{"release_id": "...", "image_tag": "...", "elapsed_seconds": 412.7, "timestamp": "..."}

// prod.deploy.aborted
{
  "release_id": "...",
  "image_tag": "...",
  "status": "aborted_smoke_failed",     // one of the aborted_* terminal states
  "error_class": "PreCheckSmokeFailed", // matches the §AC error catalog
  "error_message": "staging-mirror smoke failed: auth_smoke,checkout_smoke",
  "timestamp": "..."
}
```

The frontend release dashboard (OP-771 / D17) subscribes via SSE and
updates the deploy lane in real time. The events are NOT replayed by
an idempotent re-run — that's deliberate, so the dashboard's
"deploys today" counter stays accurate.

---

## 6. DoD checklist

Before closing the ticket:

- [ ] `pytest backend/tests/test_prod_deploy.py` green (6 cases + helpers)
- [ ] Sandboxed mirror dry run completed via
      `scripts/prod_deploy_runbook.py --insecure-skip-webhook-signature ...`
      and the resulting `prod_deploy_audit` row inspected
      (`status='completed'`, `last_step='completed'`,
      `elapsed_seconds` populated, `context_json` carries the insecure
      flag).
- [ ] Hash-chained compliance log (`deploy_audit`) shows the matching
      `started` + `succeeded` pair with `context` mentioning the
      `release_id`.

---

## 7. References

* OP-779 D18 — `deploy_audit` change-management compliance log
* OP-770 — production release approval (legacy, two-step)
* OP-771 / D17 — canary rollout dashboard
* `backend/orchestrator/prod_deploy.py` — implementation
* `backend/alembic/versions/0224_prod_deploy_audit.py` — schema
