# OmniSight Release Runbook

> **Ticket:** OP-779 (Sprint D D18 — Operator runbook + approval workflow + change audit log)
>
> **Audience:** the on-call operator who pushes the SHIP-IT button,
> rolls back, recovers from disaster, or responds to an SLO breach.
>
> **Status:** canonical operator artifact for production change
> management. Five scenarios — one section each. Every section gives
> trigger, steps, verification, and post-action.

This runbook is the single page an operator should be able to open in
the middle of a 2 a.m. page and execute end-to-end. It does **not**
replace the underlying deeper docs (linked at the bottom of each
section); it is the linear path through them.

Every operator-initiated change documented here lands in the
`deploy_audit` table (see §6) with the operator's reason text. The
table is append-only and immutable — re-walk the chain via
`GET /admin/deploy-audit/verify` after any change-management review.

## 0. Pre-flight contract

Before invoking any of the five scenarios, verify:

| Check | Command | Pass |
|---|---|---|
| Operator identity propagated | `whoami` on the prod host matches the JIRA approver | identical |
| Approval surface up | `curl -fsS https://prod.example.com/admin/release-approval/api` | 200 + JSON |
| Audit log writable | `curl -fsS https://prod.example.com/admin/deploy-audit?limit=1` | 200 + last row |
| Image registry reachable | `docker pull registry.gitlab.com/omnisight/productizer:<latest>` | `Status: Image is up to date` |

If any pre-flight check fails, **halt** and follow §4 (DR) until they
pass.

---

## 1. Normal release flow (milestone → ship)

**Trigger:** OP-770 release-tagged event consumer surfaces a pending
approval for tag `vX.Y.Z`. The operator receives a high-severity
notification with a link to `/admin/release-approval?tag=vX.Y.Z`.

**Steps:**

1. Open `/admin/release-approval?tag=vX.Y.Z`. Review:
   - Smoke results (must be all green)
   - Baseline diff (no negative regressions)
   - Change list (every JIRA ticket fixVersion'd to this tag)
2. Type the **operator reason** in the SHIP-IT form.
   - Required to be non-empty (the form rejects empty submission with
     HTTP 400).
   - Recommended format: `<short why>; OP-<linked ticket>; risk:
     <low|med|high>`. Example: `D-sprint final ship; OP-770 milestone;
     risk: low`.
3. Click **SHIP IT**. The handler calls
   `production_release.approve_and_ship` which:
   - writes a `release.ship_approved` row in the per-tenant audit
     chain (existing OP-770 behaviour)
   - appends a `kind=deploy, status=started` row to `deploy_audit`
   - executes the blue-green ship sequence (alembic upgrade, then
     `backend-a` → readyz → `backend-b` → readyz → frontend)
   - on success appends `kind=deploy, status=succeeded,
     elapsed_seconds=<n>` to `deploy_audit`
4. Wait for the redirect to the approval page. Status should flip to
   `shipped`.

**Verification:**

```bash
# 1) Approval record persisted
curl -fsS https://prod.example.com/admin/release-approval/api?tag=vX.Y.Z \
  | jq '{tag, status, approved_by, deploy_elapsed_seconds}'

# 2) Two deploy_audit rows written (started + succeeded)
curl -fsS https://prod.example.com/admin/deploy-audit?kind=deploy \
  | jq '.rows[-2:]'

# 3) Hash chain still verifies
curl -fsS https://prod.example.com/admin/deploy-audit/verify
# {"ok": true, "rows": <N>}
```

**Post-action:**

- Comment on the JIRA milestone ticket with the deploy_audit row ids.
- If `deploy_elapsed_seconds > 600`, file a follow-up under
  `meta:deploy-slow` (the 10-minute target lives in
  `ProductionDeployOrchestrator.deploy_timeout_seconds`).

**Deeper docs:**

- `docs/operations/release-discipline.md` — SemVer rules
- `docs/operations/release-image-pipeline.md` — image build chain
- `backend/production_release.py` — orchestrator implementation

---

## 2. Hotfix flow (urgent fix on prod)

**Trigger:** a JIRA ticket labelled `hotfix:vX.Y.Z` and `tier:S` has
been merged to `release/vX.Y` and the operator is paged to ship it
out-of-band.

**Steps:**

1. Cherry-pick the fix onto the active release branch:

   ```bash
   scripts/cherry_pick_hotfix.py <commit-sha> --to release/vX.Y
   ```

   The script creates the next patch tag (e.g. `v1.0.0` → `v1.0.1`).

2. Run the hotfix planner:

   ```bash
   scripts/hotfix_pipeline.py \
     --ticket OP-XYZ \
     --label hotfix:v1.0.1 \
     --label tier:S \
     --smoke-status green \
     --critical-slo-status green \
     --operator-approved
   ```

3. The planner emits a `release_tagged` event for the patch tag, which
   surfaces a fresh approval page at
   `/admin/release-approval?tag=v1.0.1`.
4. Follow §1 from step 1, with the **reason** explicitly identifying
   the hotfix bug:
   `hotfix v1.0.1; OP-XYZ <one-line>; severity: <SEV-1|SEV-2>`.

**Verification:**

```bash
# Hotfix tag is reachable in registry
docker pull registry.gitlab.com/omnisight/productizer:v1.0.1

# Hotfix recorded in deploy_audit
curl -fsS 'https://prod.example.com/admin/deploy-audit?kind=deploy' \
  | jq '.rows[] | select(.tag=="v1.0.1")'
```

**Post-action:**

- Backport the cherry-pick to `develop` if not already present.
- Update the user-facing changelog with a **Hotfix** marker.
- File `lessons/L-OP-XYZ-*.md` if a generalisable failure mode emerged.

**Deeper docs:**

- `docs/runbook/hotfix-workflow.md` — full hotfix SOP
- `scripts/cherry_pick_hotfix.py` — tag math
- `scripts/hotfix_pipeline.py` — planner + approval emission

---

## 3. Rollback flow (failed deploy)

**Trigger:** one of:

- The blue-green ship sequence in §1 failed mid-rollout
  (`approve_and_ship` raised); the orchestrator's `_rollback` already
  ran `scripts/deploy.sh --rollback` automatically.
- The operator wants to revert a previously-shipped release that is
  now showing user-impacting issues but has **not** breached an SLO
  (otherwise see §5 — auto-rollback covers that path).

**Steps:**

1. Confirm the prior tag is in registry:

   ```bash
   docker manifest inspect registry.gitlab.com/omnisight/productizer:<previous-tag>
   ```

   If missing, halt and escalate — manual rollback is unsafe without
   the prior image.
2. Trigger the operator-driven rollback:

   ```bash
   OMNISIGHT_IMAGE_TAG=<previous-tag> \
   docker compose -f docker-compose.prod.yml up -d --no-deps \
     backend-a backend-b frontend
   ```

3. Within 30 seconds, post the operator-action audit row:

   ```bash
   curl -fsS -X POST https://prod.example.com/admin/release-approval/cancel \
     -d "tag=<bad-tag>" \
     -d "reason=manual rollback; OP-XYZ regression; rolled to <previous-tag>"
   ```

   That handler appends `kind=operator_action, status=succeeded` to
   `deploy_audit` with the reason text — the human "why" required for
   compliance.
4. Verify health:

   ```bash
   for port in 8000 8001 8002; do
     curl -fsS http://localhost:$port/readyz
   done
   ```

**Verification:**

```bash
# 1) Audit row landed
curl -fsS https://prod.example.com/admin/deploy-audit?kind=rollback \
  | jq '.rows[-1]'

# 2) Chain still verifies
curl -fsS https://prod.example.com/admin/deploy-audit/verify

# 3) Frontend serving previous tag
curl -fsS https://prod.example.com/api/v1/health | jq .release_tag
```

**Post-action:**

- Re-open the JIRA ticket that introduced the regression with a
  `rollback:<bad-tag>` label.
- Add the rollback row id to the ticket comment so post-mortem can
  find the audit trail.
- If automatic rollback fired (case 1), confirm the
  `kind=rollback, status=succeeded` row exists; if not, file an
  incident — the orchestrator should always record both ends.

**Deeper docs:**

- `docs/operations/slo-monitor-rollback.md` — automatic rollback
- `scripts/deploy.sh --rollback` — rollback primitive
- `backend/production_release.py::ProductionDeployOrchestrator._rollback`

---

## 4. DR flow (full system loss)

**Trigger:** total loss of a production host or its Postgres data
volume — the SHIP-IT page is unreachable, the audit endpoints return
5xx, or `/readyz` fails for both backends for >5 minutes with no
auto-recovery.

**Steps:**

1. **Stop the bleeding.** Drain traffic to the failed host (DNS
   weighting → 0, or pull the host out of the load balancer).
2. **Restore Postgres.** Follow `docs/operations/disaster-recovery.md`:

   ```bash
   bash scripts/postgres_restore.sh \
     --backup-id <latest-daily> \
     --wal-since <last-hour-marker>
   ```

   Targets are RTO ≤ 1h and RPO ≤ 1h.
3. **Re-deploy the app stack** from a known-good tag:

   ```bash
   OMNISIGHT_IMAGE_TAG=<last-shipped> \
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml run --rm \
     -w /app/backend backend-a python -m alembic upgrade head
   ```

4. **Re-arm the audit chain.** After restore, run the chain verifier
   once and compare against the pre-incident hash captured by the
   nightly compliance export:

   ```bash
   curl -fsS https://prod.example.com/admin/deploy-audit/verify
   # Expected: ok=true, rows >= last-known
   ```

5. **Record the DR action.** Post an `operator_action` row with the
   restore details:

   ```bash
   curl -fsS -X POST .../deploy-audit/manual-record \
     -d kind=operator_action -d status=succeeded \
     -d reason="DR restore from backup-<id>; OP-XYZ incident"
   ```

   (For now this is recorded by the operator running
   `python -m backend.deploy_audit record …` directly on the host;
   the manual-record HTTP endpoint is tracked under D19 follow-up.)

**Verification:**

```bash
# 1) Both backends ready
for port in 8000 8001; do curl -fsS http://localhost:$port/readyz; done

# 2) Last 30 days of audit rows still present after restore
curl -fsS 'https://prod.example.com/admin/deploy-audit?since=2026-04-08T00:00:00+00:00' \
  | jq '.count'

# 3) Hash chain intact
curl -fsS https://prod.example.com/admin/deploy-audit/verify
```

**Post-action:**

- File a retrospective under `docs/retrospectives/YYYY-MM-DD-dr-<slug>.md`.
- Export the deploy_audit window covering the incident:
  `GET /admin/deploy-audit.csv?since=…&until=…` and attach to the
  retro.
- Schedule a DR drill the same week (RTO/RPO regression check).

**Deeper docs:**

- `docs/operations/disaster-recovery.md` — Postgres restore (canonical)
- `docs/runbook/post-deploy-recovery.md` — five gate-driven recoveries
- `.github/workflows/postgres-backup-dr.yml` — backup cadence

---

## 5. SLO breach response

**Trigger:** `backend.orchestrator.slo_monitor` posts a critical
operator notification after the configured sustained-breach window
elapses. The monitor then attempts **automatic** rollback through the
OP-883 orchestrator path; while progressive canary is HOLD'd and
`canary_state.json` is absent, it defaults to full production rollback.

**Steps (operator-side, while the monitor is acting):**

1. Open the SLO dashboard tile: `/admin/slo-monitor`. Confirm the
   breach is real (not a Prometheus scrape glitch).
2. Watch the auto-rollback. The monitor emits `slo.breach` with the
   selected rollback mode and then invokes the OP-883 rollback trigger.
3. If the monitor reports rollback failure, pivot to §3 manual rollback.

**Verification:**

```bash
# 1) Breach + rollback both audited
curl -fsS 'https://prod.example.com/admin/deploy-audit?kind=slo_breach' \
  | jq '.rows[-1]'
curl -fsS 'https://prod.example.com/admin/deploy-audit?kind=rollback' \
  | jq '.rows[-1]'

# 2) p95 + error-rate back under threshold
# (open Grafana SLO dashboard and confirm)

# 3) Chain still verifies
curl -fsS https://prod.example.com/admin/deploy-audit/verify
```

**Post-action:**

- File an incident ticket linked to the breaching tag.
- Run the "what changed" diff between the bad tag and the prior tag,
  paste it into the incident as the candidate root cause.
- Re-run the canary on the next attempt at the bad tag (per
  OP-771/canary).

**Deeper docs:**

- `docs/operations/slo-monitor-rollback.md` — monitor + auto-rollback
  contract
- `config/slo_thresholds.yaml` — thresholds (error rate < 1 %, p95 < 500 ms)
- `backend/orchestrator/slo_monitor.py` — implementation

---

## 6. Audit log + compliance export

Every operator action above lands in the `deploy_audit` table (see
the alembic 0204 migration `backend/alembic/versions/0204_deploy_audit.py`).
Properties:

- **Immutable.** No UPDATE/DELETE API; tampering is detected by
  re-walking the SHA-256 hash chain via
  `GET /admin/deploy-audit/verify`.
- **Queryable for 1y.** `GET /admin/deploy-audit?since=…&until=…&kind=…&limit=…`
  defaults to a 1y rolling window per ticket spec.
- **CSV-exportable.** `GET /admin/deploy-audit.csv?…` returns the
  same window as `text/csv` with the canonical
  `id,ts,kind,tag,actor,reason,status,elapsed_seconds,context,prev_hash,curr_hash`
  header order.

For monthly compliance review:

```bash
# Export the full month as CSV
curl -fsS \
  'https://prod.example.com/admin/deploy-audit.csv?since=2026-04-01T00:00:00+00:00&until=2026-05-01T00:00:00+00:00' \
  -o deploy_audit-2026-04.csv

# Verify chain integrity before sending to compliance
curl -fsS https://prod.example.com/admin/deploy-audit/verify | jq .
```

---

## 7. Operator dry-run drill

This runbook is signed off only after each of the five scenarios has
been walked through against the staging stack with screenshots and
audit-row evidence captured. Each operator should:

| # | Scenario | Drill target | Sign-off |
|---|---|---|---|
| 1 | Normal release | staging → tag `vSTAGING.<n>` | screenshot of approval page + 2 deploy_audit rows |
| 2 | Hotfix | staging hotfix tag `vSTAGING.<n>.1` | row with `kind=deploy` and reason starting `hotfix` |
| 3 | Rollback | manual roll from staging+1 → staging | row with `kind=operator_action` and reason starting `manual rollback` |
| 4 | DR | restore staging Postgres from backup | `verify_chain` returns ok=true after restore |
| 5 | SLO breach | inject synthetic slowdown via test fault | rows for `slo_breach` + auto `rollback` |

The drill log is captured under `docs/operations/release-runbook-drill-log.md`
(per-operator) and the operator signs the OP-779 ticket comment with
the drill-log Gerrit Change-Id.

---

## 8. Cross-references

- Audit table schema: `backend/alembic/versions/0204_deploy_audit.py`
- Audit module: `backend/deploy_audit.py`
- Approval flow: `backend/production_release.py::approve_and_ship`
- Approval router: `backend/routers/release_approval.py`
- Audit query/CSV router: `backend/routers/deploy_audit.py`
- Lesson: `docs/sop/lessons/L-OP-779-audit-trail-design-needs-its-own-table.md`
