# Prod Cutover Checklist — `OMNISIGHT_REGISTRY` → GitLab CR

**Ticket:** OP-1473 (Phase 5, META OP-1468) • **Status:** active •
**Author:** runner / claude-bot, 2026-05-18

This checklist drives the production cutover from `ghcr.io` to GitLab
Container Registry (`registry.sora.services/...`). It assumes Phase 4
(OP-1472) closed with ≥ 14 days of dual-publish stability and that the
decision record at `docs/decisions/2026-05-18-ghcr-decommission-or-backup.md`
has been read and accepted.

**Cutover model:** in-place `.env` edit + rolling restart with `/readyz`
gate. No DB schema change, no image rebuild, no Gerrit-side code change.

**Estimated wall-time:** 25–40 minutes end-to-end, of which 10–15
minutes is the rolling restart and `/readyz` settle.

**Operator authority:** L2 operator on the prod-on-call rotation.
Phase 5 explicitly does NOT delegate this cutover to a runner — the
`.env` lives on the prod host, outside the repo, and the workflow is
operator-driven.

---

## 0. Pre-flight (T-15 min)

- [ ] **Maintenance window declared** in `#ops` Slack channel with
      start + expected-end timestamps. The operator-window
      announcement template is in `docs/sop/jira-ticket-conventions.md`
      §17.
- [ ] **No open INCIDENT-class JIRA tickets** referencing the
      registry, image pipeline, or `prod_deploy_audit` table.
      Verify: `jira search "project = OP AND priority in (P0, P1)
      AND status != Done AND component in (INFRA-MIGRATION,
      DEPLOY)"`.
- [ ] **GitLab CR readiness check.** From the prod host (NOT a laptop):
      ```bash
      docker login registry.sora.services
      docker pull registry.sora.services/omnisight/omnisight-backend:develop-latest
      docker image rm  registry.sora.services/omnisight/omnisight-backend:develop-latest
      ```
      All three must exit 0. A failure here aborts the cutover — do
      NOT proceed.
- [ ] **Signature verification check.** Using the same digest the
      cutover will pull, run the verifier:
      ```bash
      digest=$(docker buildx imagetools inspect \
          registry.sora.services/omnisight/omnisight-backend:develop-latest \
          --format '{{.Manifest.Digest}}')
      scripts/verify_image_signature.sh \
          registry.sora.services/omnisight/omnisight-backend@${digest}
      ```
      Expect `OK` on stdout, exit 0. Otherwise abort — a GitLab-CR
      image that doesn't verify is a stop-the-line condition.
- [ ] **Cold-standby (ghcr.io) sanity check.** Same three commands
      against `ghcr.io/<owner>/omnisight-backend:develop-latest`. We
      are NOT decommissioning ghcr.io in this ticket
      (per the Option A recommendation in the decision doc) — the
      ghcr.io path remains the fallback if the GitLab CR cutover has
      to be reverted.

---

## 1. Snapshot — pg_dump before the flip (T-5 min)

Per Rule 5 of `docs/operations/prod-deploy-runbook.md`: every prod
mutation that could in principle affect persisted state must be
preceded by a `pg_dump` and followed by a `/readyz` gate. The registry
flip itself is config-only, but the rolling restart it triggers
exercises the same code paths a real deploy would — so the snapshot
rule applies in full.

- [ ] **pg_dump to the cutover snapshot:**
      ```bash
      ts=$(date -u +%Y%m%dT%H%M%SZ)
      mkdir -p /var/backups/omnisight/cutover
      pg_dump \
          --host=$OMNISIGHT_DB_HOST \
          --port=$OMNISIGHT_DB_PORT \
          --username=$OMNISIGHT_DB_USER \
          --format=custom \
          --compress=9 \
          --file=/var/backups/omnisight/cutover/op1473-pre-cutover-${ts}.dump \
          omnisight_prod
      ```
- [ ] **Verify dump integrity** before relying on it:
      ```bash
      pg_restore --list \
          /var/backups/omnisight/cutover/op1473-pre-cutover-${ts}.dump \
          | head -20
      ```
      Expect a non-empty table-of-contents listing the core schemas
      (`public`, `audit`, `governance`). An empty or truncated TOC
      means the dump is unusable — abort, investigate the dump,
      do NOT proceed to step 2.
- [ ] **Record the dump path** in the cutover JIRA comment thread so
      the rollback path (step 5) can find it without guessing.

---

## 2. Flip `OMNISIGHT_REGISTRY` (T-0)

The `.env` lives at `/etc/omnisight/prod.env` on each prod host. The
relevant line, before the flip:

```
OMNISIGHT_REGISTRY=ghcr.io/<owner>
```

After the flip:

```
OMNISIGHT_REGISTRY=registry.sora.services/omnisight
```

- [ ] **Edit the `.env` on host 1** (typically `prod-app-01`):
      ```bash
      sudo cp /etc/omnisight/prod.env /etc/omnisight/prod.env.bak.op1473
      sudo sed -i \
          's|^OMNISIGHT_REGISTRY=.*|OMNISIGHT_REGISTRY=registry.sora.services/omnisight|' \
          /etc/omnisight/prod.env
      ```
- [ ] **Diff the file** to confirm exactly one line changed and the
      new value is correct:
      ```bash
      sudo diff /etc/omnisight/prod.env.bak.op1473 /etc/omnisight/prod.env
      ```
- [ ] **Repeat on remaining prod hosts** (`prod-app-02`, `prod-app-03`,
      bridge hosts). The `.env` is host-local; there is no central
      config server distributing it.

The cutover is non-atomic across hosts by design — the rolling restart
in step 3 picks up each host's new value in sequence.

---

## 3. Rolling restart with `/readyz` gate (T+0 → T+15)

Per Rule 5 of `prod-deploy-procedure.md`: restart one host at a time,
wait for that host's `/readyz` to return 200 before moving to the next.

For each host in the rotation:

- [ ] **Drain traffic from this host** — remove from the upstream LB
      pool:
      ```bash
      curl -X POST -H "Authorization: Bearer $LB_ADMIN_TOKEN" \
          "https://lb-admin.internal/pool/omnisight-app/host/${HOST}/drain"
      ```
- [ ] **Wait for in-flight requests** to settle (60 s — match the
      LB's `connection_drain_timeout_s`):
      ```bash
      sleep 60
      ```
- [ ] **Restart the omnisight stack on the host:**
      ```bash
      ssh ${HOST} 'sudo systemctl restart omnisight-backend omnisight-frontend'
      ```
- [ ] **Wait for `/readyz`** on the host (max 5 min; if it fails to
      become ready, halt the rolling restart and proceed to step 5
      rollback):
      ```bash
      timeout 300 bash -c "
        until curl -fsS http://${HOST}:8080/readyz | grep -q '\"ready\":true'; do
          sleep 5
        done
      "
      ```
- [ ] **Verify the running image came from GitLab CR** (not ghcr.io):
      ```bash
      ssh ${HOST} 'docker ps --format "{{.Image}}" | grep omnisight'
      ```
      Expected: every line starts with `registry.sora.services/...`.
      Any `ghcr.io/...` line means the `.env` edit didn't take —
      abort the rest of the rolling restart on that host, investigate
      `/etc/omnisight/prod.env` and the systemd unit's
      `EnvironmentFile=` directive.
- [ ] **Re-enable traffic** to this host on the LB:
      ```bash
      curl -X POST -H "Authorization: Bearer $LB_ADMIN_TOKEN" \
          "https://lb-admin.internal/pool/omnisight-app/host/${HOST}/enable"
      ```
- [ ] **Smoke test against this host directly** before moving on:
      ```bash
      curl -fsS http://${HOST}:8080/healthz
      curl -fsS http://${HOST}:8080/api/v1/version | jq '.image_source'
      ```
      The `image_source` field should now report `gitlab-cr` (the
      backend reads `OMNISIGHT_REGISTRY` and exposes a normalised
      identifier; if absent in the version payload, fall back to the
      `docker ps` check above).

Repeat for each host in `prod-app-01`, `prod-app-02`, `prod-app-03`,
then the bridge hosts (`prod-bridge-01`, etc.) in declared order.

---

## 4. Post-cutover verification (T+15 → T+30)

- [ ] **`deploy-audit.sh` reports GitLab CR as image source** for the
      latest deploy row. Per AC #3:
      ```bash
      scripts/deploy_audit_query.py --since=1d --kind=deploy --format=json \
          | jq '.[0].image_source'
      ```
      Expected: `"gitlab-cr"`. The `image_source` column was
      populated during the Phase 3 dual-publish work — if it returns
      `null`, the audit row was written by an older backend version
      that predated the column; in that case verify directly via the
      running container image (step 3's `docker ps` check) and
      annotate the JIRA ticket.
- [ ] **All hosts running `registry.sora.services/...`** — one-shot
      audit across the fleet:
      ```bash
      for h in prod-app-01 prod-app-02 prod-app-03 \
               prod-bridge-01 prod-bridge-02; do
          echo "=== ${h} ==="
          ssh ${h} 'docker ps --format "{{.Names}} {{.Image}}"' \
              | grep -E 'omnisight' \
              | awk '{print $2}' \
              | sort -u
      done
      ```
      Expect every line to start with `registry.sora.services/`.
- [ ] **`/readyz` green fleet-wide:**
      ```bash
      for h in prod-app-01 prod-app-02 prod-app-03; do
          curl -fsS http://${h}:8080/readyz | jq -c '{host:"'${h}'", ready, components}'
      done
      ```
      Every row must show `"ready": true`. Any host short of ready
      after 10 minutes settle is an incident — page the on-call,
      consider rollback (step 5).
- [ ] **No registry-related errors in the prod log stream for 30
      minutes after the last host returns ready:**
      ```bash
      grep -iE 'registry|ghcr|image.*pull' /var/log/omnisight/app.log \
          | tail -200
      ```
      Expected: zero pull-failure entries against the new
      `registry.sora.services` host. Stale ghcr.io references in
      cache-warming code paths are acceptable for the first hour
      (image-pull cache backs off lazily); persistent failures past
      hour 1 are not.
- [ ] **Update JIRA ticket OP-1473** with the timestamps of:
      `T-0` (`.env` flip), `T+15` (last host ready), `T+30`
      (verification complete).

---

## 5. Rollback path

If any host fails to come back ready within the 5-minute `/readyz`
timeout, or if the post-cutover error stream shows registry-pull
failures, abort and roll back:

- [ ] **Re-flip `.env` on every host back to ghcr.io:**
      ```bash
      sudo cp /etc/omnisight/prod.env.bak.op1473 /etc/omnisight/prod.env
      sudo systemctl restart omnisight-backend omnisight-frontend
      ```
      The `.env.bak.op1473` file is the snapshot taken in step 2 —
      keep it on each host for at least 14 days post-cutover.
- [ ] **Wait for `/readyz` to come back** on every host (same
      timeout as step 3).
- [ ] **Verify image source reverted to ghcr.io:**
      ```bash
      ssh ${HOST} 'docker ps --format "{{.Image}}" | grep omnisight'
      ```
- [ ] **DO NOT** restore from the pg_dump unless persisted state has
      visibly drifted — a config-flip rollback should never have
      affected the database. The dump exists only as defense in depth
      against an unforeseen migration side-effect.
- [ ] **File a P1 INCIDENT JIRA** with affected_subsystems including
      `registry`, `image-pull`, `deploy`. Link to OP-1473 and to the
      JIRA cutover thread for context.

---

## 6. AC reference

Acceptance criteria from OP-1473 and where they get exercised in
this checklist:

| AC # | Criterion                                                | Where exercised in this checklist                   |
|------|----------------------------------------------------------|-----------------------------------------------------|
| 1    | Decision doc committed; if decommission, GHA removed     | Sibling deliverable — `docs/decisions/2026-05-18-ghcr-decommission-or-backup.md` |
| 2    | Prod runs on GitLab CR for ≥ 7 days without registry incident | Verified by operator 7 days post-cutover, recorded as JIRA comment |
| 3    | `deploy-audit.sh` confirms image source is GitLab CR     | Step 4, first checkbox                              |
| 4    | ≥ 1 release-cut + deploy through GitLab-only flow, no operator intervention | Verified at the next `v*` release after this cutover; recorded as JIRA comment on the release ticket and cross-linked from OP-1473 |

## 7. References

- `docs/decisions/2026-05-18-ghcr-decommission-or-backup.md` — Phase 5 decision record
- `docs/operations/prod-deploy-runbook.md` — Rule 5 (`/readyz` gate)
- `docs/operations/release-image-pipeline.md` — OP-763 image pipeline (ghcr.io path, still live as cold-standby)
- `scripts/verify_image_signature.sh` — cosign signature verifier (works for both ghcr.io and GitLab CR images)
- `scripts/deploy_audit_query.py` — `deploy_audit` query helper used in step 4
- ADR-0002 — GitLab primary, GitHub mirror
