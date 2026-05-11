# Deployment Master Runbook

> Ticket: OP-890 (D18, META OP-761 / OP-784 staged filing)
>
> Audience: release operators, incident commanders, and reviewers who
> approve production changes.
>
> Status: canonical operator checklist for milestone -> cut -> deploy
> -> monitor -> rollback.

This runbook is the single deployment path. Operators may open deeper
docs for background, but the deploy dry run and production deploy must
be executable from this page alone. The deeper docs remain the source
for command implementation details and are linked at the step where the
operator needs them.

The required audit command for change review is:

```bash
python3 scripts/deploy_audit_query.py --since=30d
```

The quarterly retrospective template is
`docs/operations/deploy-retro-template.md`.

## 0. Control Contract

### 0.1 Roles

| Role | May approve | May execute | Escalates to |
| --- | --- | --- | --- |
| Release operator | Tier S/M routine releases, release branch cuts, staging deploys | Milestone checks, tag cuts, staging deploys, production deploy request | Incident commander for failed gates |
| Incident commander | Emergency rollback, hotfix deploy, SLO-breach response | Rollback command, traffic drain, deploy freeze | Human operator for customer-impacting changes |
| Human operator | Tier L/X, risk acceptance, force override, production deploy final approval | Any manual override after recording reason | Governance reviewer for policy exceptions |
| AI reviewer | Gerrit +1 only | No production approval | Human reviewer for +2 |
| Security owner | Secret rotation, credential or key-management release approval | Security recovery steps only when explicitly assigned | Human operator |

Approval is always explicit and recorded. An approval without a reason
is invalid even if the deploy command succeeds.

### 0.2 Approval Workflow

1. Release operator verifies the milestone and creates the release
   candidate evidence bundle.
2. Gerrit review lands with the required human +2. AI review may add
   +1 only.
3. Release operator cuts the release branch and tag.
4. Human operator opens the production approval surface, reviews smoke
   results, baseline diff, included tickets, and rollback target.
5. Human operator enters a non-empty reason and approves the deploy.
6. The deploy orchestration writes `deploy_audit` rows for started and
   terminal status.
7. Release operator posts audit row IDs and the deploy result to the
   release ticket.

Escalation paths:

| Condition | Halt point | Escalation |
| --- | --- | --- |
| Missing milestone evidence | Before branch cut | Release operator -> human operator |
| Gerrit human +2 missing | Before branch cut | Reviewer queue |
| Smoke or baseline regression | Before production approval | Incident commander |
| Approval surface unavailable | Before production approval | Incident commander, use DR path only if prod is impaired |
| Rollback target unavailable | Before production approval | Human operator, do not deploy |
| Policy exception requested | Any point | Governance reviewer |

### 0.3 Audit Rules

Every production change requires an audit row with:

- `kind`: `deploy`, `rollback`, `slo_breach`, or `operator_action`
- `status`: `started`, `succeeded`, or `failed`
- `actor`: operator identity
- `reason`: non-empty human-readable reason
- `tag`: release or rollback tag when applicable

Review the last 30 days before and after a deploy window:

```bash
python3 scripts/deploy_audit_query.py --since=30d
```

For large windows, the script paginates in bounded batches. If the
query times out, reduce the window or pass a smaller `--page-size`;
the error catalog name is `AuditQueryTimeout`.

### 0.4 Error Catalog

| Error | Meaning | Operator action |
| --- | --- | --- |
| `RunbookOutOfDate` | This runbook conflicts with a current deeper doc or live system behavior | Halt. Manual review gate only; do not automate around it. File a docs follow-up and continue only after a human decision. |
| `AuditQueryTimeout` | Audit export window is too large or the DB is locked | Rerun with pagination by lowering `--page-size`; if lock persists, wait for the writer and retry once. |

## 1. Pre-Flight

Complete these checks before any milestone, cut, deploy, monitor, or
rollback operation.

| Check | Command / evidence | Pass |
| --- | --- | --- |
| Working copy clean except intended release files | `git status --short` | no unrelated changes |
| Correct release version selected | JIRA fixVersion and planned tag match | exact match |
| Gerrit review state known | Gerrit change shows required human +2 | present |
| Audit log readable | `python3 scripts/deploy_audit_query.py --since=30d --limit=1` | exits 0 |
| Rollback target known | previous production tag recorded | tag exists |
| Quarterly retro template available | `test -f docs/operations/deploy-retro-template.md` | exits 0 |

If any row fails, stop at the current stage and record the blocker on
the release ticket.

## 2. Stage Checklist: Milestone

Goal: prove the fixVersion is ready to cut.

### 2.1 Inputs

- Target fixVersion, for example `vX.Y.Z`
- Release ticket key
- Changelog scope
- Known rollback tag

### 2.2 Steps

1. Confirm the release ticket lists the intended fixVersion.
2. Run the milestone checker:

   ```bash
   python3 scripts/milestone_check.py \
     --version vX.Y.Z \
     --out /tmp/milestone-vX.Y.Z.json
   ```

3. Confirm all included tickets are `Published` or `Archived`.
4. Confirm no open blocker-priority tickets remain in the same
   fixVersion.
5. Confirm merged commits have the required Gerrit verification.
6. Attach or paste the checker JSON summary to the release ticket.

### 2.3 Verification

| Evidence | Pass |
| --- | --- |
| `/tmp/milestone-vX.Y.Z.json` exists | file present |
| Checker exit code | `0` |
| FixVersion ticket count | matches planned scope |
| Blocker count | `0` |

### 2.4 Rollback From This Stage

No production state has changed. If the milestone is not ready:

1. Leave the release ticket in its current state.
2. Comment with the failed milestone checker reason.
3. Do not cut a release branch.

Deeper doc: `docs/operations/milestone-runbook.md`.

## 3. Stage Checklist: Cut

Goal: create the release branch and annotated tag.

### 3.1 Inputs

- Clean `main`
- Target tag `vX.Y.Z`
- Milestone checker evidence from section 2
- Operator approval for tag creation

### 3.2 Steps

1. Refresh `main`:

   ```bash
   git checkout main
   git pull --ff-only
   ```

2. Dry-run the cut:

   ```bash
   scripts/cut_release_branch.sh --version vX.Y.Z --dry-run
   ```

3. Run the real cut:

   ```bash
   scripts/cut_release_branch.sh --version vX.Y.Z --changelog-jira
   ```

4. When prompted, approve only if the version and changelog match the
   release ticket.
5. Push evidence to the release ticket: branch ref, tag ref, and
   changelog summary.

### 3.3 Verification

```bash
git ls-remote gerrit refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git ls-remote origin refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git show --stat vX.Y.Z
```

Expected result: each remote has one release branch ref and one tag
ref; the tag points at the release branch tip.

### 3.4 Rollback From This Stage

If the script fails before pushing:

```bash
git checkout main
git branch -D release/vX.Y.Z
git tag -d vX.Y.Z
```

If either remote push succeeded, do not delete remote refs silently.
Comment on the release ticket and ask whether to keep the refs, replace
them with the script's override path, or cut a new patch version.

Deeper doc: `docs/operations/release-cut-runbook.md`.

## 4. Stage Checklist: Deploy

Goal: promote the release image to production through the approval gate.

### 4.1 Inputs

- Release ID, for example `2026.05.11-rc1`
- Image tag, for example `v2026.05.11`
- Slack or equivalent approval token
- Production deploy webhook secret
- Human approval reason

### 4.2 Steps

1. Confirm the image exists in the registry.
2. Confirm the previous production tag still exists for rollback.
3. Retrieve the approval token out of band.
4. Build and sign the request body:

   ```bash
   RELEASE_ID="2026.05.11-rc1"
   IMAGE_TAG="v2026.05.11"
   APPROVAL_TOKEN="$(slack-cli get-prod-deploy-token "$RELEASE_ID")"

   BODY=$(jq -nc \
     --arg release_id "$RELEASE_ID" \
     --arg image_tag "$IMAGE_TAG" \
     --arg approval_token "$APPROVAL_TOKEN" \
     --arg reason "release vX.Y.Z; OP-890 dry-run evidence; risk: low" \
     '{$release_id, $image_tag, $approval_token, $reason}')

   SIG="sha256=$(printf '%s' "$BODY" \
     | openssl dgst -sha256 -hmac "$OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET" \
     | awk '{print $2}')"
   ```

5. Submit the deploy:

   ```bash
   curl -fsSL \
     -H "Authorization: Bearer $ADMIN_BEARER" \
     -H "Content-Type: application/json" \
     -H "X-Prod-Deploy-Signature: $SIG" \
     --data-binary "$BODY" \
     https://api.omnisight.internal/api/v1/prod/deploy
   ```

6. Watch deploy events until terminal status.
7. Record the terminal status and audit row IDs on the release ticket.

### 4.3 Verification

| Evidence | Command / location |
| --- | --- |
| Deploy orchestration completed | deploy response JSON status |
| Hash-chained audit rows exist | `python3 scripts/deploy_audit_query.py --since=30d --kind=deploy` |
| Production health is green | `/readyz` for both backends and frontend health endpoint |
| Release dashboard updated | deploy lane shows the new tag |

### 4.4 Rollback From This Stage

If the deploy fails before traffic changes, the orchestrator records
the failed terminal state. Do not retry with the same `release_id`.

If traffic changed and health fails:

1. Pivot to section 6 rollback.
2. Keep the release ticket open.
3. Preserve deploy response JSON and audit rows.

Deeper doc: `docs/operations/prod-deploy-runbook.md`.

## 5. Stage Checklist: Monitor

Goal: decide whether to keep, advance, or roll back the release.

### 5.1 Inputs

- Release ID
- Stable tag
- Canary tag, if canary is in use
- Baseline p95 latency
- Error-rate threshold

### 5.2 Steps

1. Watch the global deploy event stream.
2. Open the release dashboard and confirm the deployed tag.
3. If canary is active, observe each stage for the required window.
4. Confirm p95 latency remains within the rollout bound.
5. Confirm error rate remains below the rollout bound.
6. Query audit rows for unexpected operator actions:

   ```bash
   python3 scripts/deploy_audit_query.py --since=30d --format=table
   ```

7. If all signals stay green through the observation window, mark the
   release ticket as deployed and link the audit evidence.

### 5.3 Verification

| Evidence | Pass |
| --- | --- |
| SLO dashboard p95 | within bound |
| Error rate | below threshold |
| Audit query | no unexpected failed rows |
| Release dashboard | terminal deploy state |

### 5.4 Rollback From This Stage

If any signal breaches:

1. Do not advance canary.
2. If auto-rollback fires, wait for `rollback` succeeded audit row.
3. If auto-rollback cannot act, run section 6 manually.
4. File or reopen the regression ticket with the bad tag.

Deeper docs:

- `docs/operations/canary-runbook.md`
- `docs/operations/slo-monitor-rollback.md`
- `docs/operations/slo-runbook.md`

## 6. Stage Checklist: Rollback

Goal: restore the prior known-good release and preserve the audit trail.

### 6.1 Inputs

- Bad tag
- Previous known-good tag
- Incident or release ticket key
- Human rollback reason

### 6.2 Steps

1. Confirm the previous tag exists:

   ```bash
   docker manifest inspect ghcr.io/omnisight/productizer:<previous-tag>
   ```

2. Trigger rollback with the sanctioned rollback primitive for the
   current deployment path:

   ```bash
   OMNISIGHT_IMAGE_TAG=<previous-tag> \
   docker compose -f docker-compose.prod.yml up -d --no-deps \
     backend-a backend-b frontend
   ```

3. Record the operator action using the production audit surface
   documented in the deploy runbook.
4. Verify both backends and the frontend.
5. Query the audit log:

   ```bash
   python3 scripts/deploy_audit_query.py --since=30d --kind=rollback
   ```

6. Reopen the ticket that introduced the regression and add
   `rollback:<bad-tag>`.

### 6.3 Verification

| Evidence | Pass |
| --- | --- |
| Prior image exists | manifest command exits 0 |
| Health checks pass | all endpoints return success |
| Rollback audit row exists | `kind=rollback`, `status=succeeded` |
| Release dashboard shows prior tag | exact tag match |

### 6.4 Rollback From This Stage

Rollback failure is an incident. If rollback cannot restore the prior
tag:

1. Drain traffic from the impaired host.
2. Follow `docs/operations/disaster-recovery.md`.
3. Restore from the latest valid backup and WAL window.
4. Verify the audit chain after restore.

## 7. Dry-Run Promote Checklist

Use this checklist for the DoD dry run. The operator may use staging or
a sandbox mirror, but must not rely on any doc other than this runbook.

| Step | Evidence |
| --- | --- |
| Milestone checked | milestone checker JSON path |
| Cut dry-run completed | command output or terminal capture |
| Deploy request prepared | signed body redacted evidence |
| Deploy dry run executed | deploy response JSON |
| Monitor window observed | dashboard or metric capture |
| Rollback path rehearsed | rollback target identified and audit query output |
| Audit export captured | `scripts/deploy_audit_query.py --since=30d` output |
| Retro template linked | `docs/operations/deploy-retro-template.md` cited in release ticket |

## 8. Quarterly Retrospective

Each quarter, open a retrospective from
`docs/operations/deploy-retro-template.md` and attach:

- Release count
- Hotfix count
- Rollback count
- SLO breach count
- Median deploy duration
- `AuditQueryTimeout` or `RunbookOutOfDate` occurrences
- Top three runbook edits needed

The retrospective owner is the human operator unless the release
operator is explicitly assigned. Keep the retrospective separate from
incident postmortems; incident details should be linked, not copied.

## 9. References

- `docs/operations/milestone-runbook.md`
- `docs/operations/release-cut-runbook.md`
- `docs/operations/prod-deploy-runbook.md`
- `docs/operations/canary-runbook.md`
- `docs/operations/slo-monitor-rollback.md`
- `docs/operations/disaster-recovery.md`
- `docs/operations/deploy-retro-template.md`
- `scripts/deploy_audit_query.py`
