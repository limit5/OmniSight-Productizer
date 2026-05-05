# Major Upgrade + Rollback Ledger (N10)

> One-line record for every **major** dependency upgrade shipped to
> prod, plus every **rollback** triggered within the 24h window. The
> quarterly policy review reads this file to compute the rollback rate
> (see `docs/ops/dependency_upgrade_policy.md` → "Quarterly review").
>
> **Append-only.** Do not edit or delete past rows; if an entry is
> factually wrong, add a correction row below with a `correction →`
> pointer to the original row. Source control preserves the full
> history.

## How to add a row

On **every** successful major cut-over:

1. Add one row to the "Upgrades" table below with the cut-over
   timestamp (UTC), package, version range, operator, PR URL, and the
   final disposition (`shipped` / `rolled-back` / `waived`).
2. If the blue-green gate was **waived** (`deploy/bluegreen-waived`
   label or `OMNISIGHT_BLUEGREEN_OVERRIDE=1`), add a brief rationale
   in the "Notes" column.

On **every** rollback within the 24h hot-window:

1. Update the original upgrade row's disposition to `rolled-back`.
2. Add one row to the "Rollbacks" table with the trigger (SLO that
   tripped), duration (cut-over → rollback, minutes), and a link to
   the incident post-mortem or HANDOFF.md entry.

On **quarterly review** (first working day of new quarter):

1. Compute majors-shipped, rollbacks-triggered, and mean soak time.
2. Append one row to the "Quarterly Summaries" table.
3. File `policy-review` issue only if the thresholds in
   `dependency_upgrade_policy.md` trip (rollback rate > 25 % or mean
   soak < 24h).

On **every quarterly third-party pentest** (KS.4.4):

1. Store the vendor report in the private security evidence vault; do
   not commit the report, screenshots, exploit payloads, or customer
   data.
2. Compute the stored report's SHA-256 fingerprint.
3. Append one row to the "Pentest Reports" table with quarter, vendor,
   test window, report fingerprint, finding counts, remediation tracker,
   disposition, and notes.
4. Follow `quarterly_pentest_sop.md` for vendor contracting, rules of
   engagement, retest, and delayed-quarter handling.

On **every bug bounty program lifecycle change** (KS.4.5):

1. Store the provider comparison, order form, scope profile, safe harbor,
   payout policy, and disclosure policy in the private security evidence
   vault; do not commit platform exports, researcher PII, exploit
   payloads, secrets, or customer data.
2. Append one row to the "Bug Bounty Programs" table when the program is
   planned, launched, paused, publicly launched, closed, or materially
   changed.
3. Append one row to the "Bug Bounty Findings" table for each accepted
   valid finding after managed triage. Record only finding ID, severity,
   fingerprint, ticket link, bounty amount, and disposition.
4. Follow `bug_bounty_program_sop.md` for HackerOne / Bugcrowd selection,
   post-GA launch gates, payout caps, scope boundaries, and triage SLA.

On **every SOC 2 Type II readiness milestone** (KS.4.7):

1. Store the control matrix, GRC platform comparison, auditor scorecard,
   engagement letter, evidence index, and auditor independence
   confirmation in the private security evidence vault; do not commit
   auditor requests, screenshots, raw logs, customer data, secrets, or
   platform exports.
2. Compute a SHA-256 fingerprint for the evidence index when the
   observation window starts, ends, or materially changes.
3. Append one row to the "SOC 2 Readiness" table when the program is
   planned, ready for observation, observation starts, observation ends,
   draft report arrives, final report is issued, delayed, or rescoped.
4. Follow `soc2_type2_readiness_checklist.md` for control mapping,
   evidence collection, GRC platform evaluation, and independent CPA
   firm selection.

On **every GDPR / DSAR request lifecycle change** (KS.4.8):

1. Store raw exports, tenant deletion evidence, DEK purge proof,
   subprocessor receipts, and audit redaction summary in the private
   security evidence vault; do not commit raw exports, customer data,
   secrets, raw audit payloads, plaintext tokens, plaintext DEKs, or
   wrapped DEK material.
2. Compute SHA-256 fingerprints for the stored export, if one exists,
   and for the deletion / redaction evidence bundle.
3. Append one row to the "DSAR Evidence" table when a request is
   received, exported, erased, delayed, legally held, completed, or
   corrected.
4. Follow `gdpr_dsar_alignment_sop.md` for tenant data deletion, DEK
   purge, audit metadata retention, raw payload deletion, and DSAR export
   workflow.

On **every Priority I multi-tenancy readiness sign-off** (KS.DOD):

1. Store the KS.1 test transcript, KMS evidence, production image import
   smoke, runtime env snapshot, tenant isolation smoke, and 24h
   observation packet in the private security evidence vault; do not
   commit raw logs, customer data, secrets, plaintext tokens, plaintext
   DEKs, or wrapped DEK material.
2. Compute SHA-256 fingerprints for the KS.1 evidence packet, KMS
   evidence packet, and tenant isolation smoke packet.
3. Append one row to the "Priority I Readiness" table before Priority I
   starts. Use `Disposition = ready-to-start` only when every gate in
   `priority_i_multi_tenancy_readiness.md` is green.
4. Follow `priority_i_multi_tenancy_readiness.md` for KS.1 evidence,
   production image/env proof, multi-tenant isolation smoke, legacy
   Fernet deprecation proof, and 24h observation requirements.

On **every KS KMS control review** (R46):

1. Store the KEK rotation transcript, least-privilege IAM / Vault policy
   snapshot, two-person KMS admin approval, and KMS audit export in the
   private security evidence vault; do not commit raw CloudTrail, Cloud
   Audit Logs, Vault audit payloads, principals, customer data, secrets,
   plaintext DEKs, or wrapped DEK material.
2. Compute SHA-256 fingerprints for the policy snapshot, admin approval,
   and KMS audit export.
3. Append one row to the "KS KMS Control Reviews" table before
   Priority I starts and then once per quarter. Use `Disposition =
   accepted` only when rotation cadence, least-privilege policy,
   dual-control approval, and KMS audit evidence are all present.
4. Follow `ks_cross_cutting_evidence.md` and `cmek_siem_ingest.md` for
   the R46 mitigation evidence boundary.

On **every incident response tabletop drill or real security incident**
(KS.4.6):

1. Store the timeline, responder roster, redacted command transcript,
   customer notification decision, evidence inventory, and corrective
   action tracker in the private security evidence vault; do not commit
   raw logs, customer data, secrets, exploit payloads, screenshots, or
   full incident artefacts.
2. Compute SHA-256 fingerprints for the evidence bundle and postmortem
   packet when one exists.
3. Append one row to the "Incident Response Drills" table after each
   tabletop drill or real incident closure. Use `Disposition = passed`
   only when the 24-hour SOP was exercised, evidence handling was clean,
   and corrective actions have owners.
4. Follow `../security/incident-response-runbook.md` for severity,
   role assignment, containment, rotation, notification, forensics, and
   blameless postmortem requirements.

On **every WP.3 diff-validation cascade match**:

1. Append one row to the "Diff Validation Confidence" table with the
   UTC timestamp, repository path, patch kind, selected cascade layer,
   confidence score, disposition, and non-sensitive notes.
2. Do not store SEARCH / REPLACE payload bytes, file contents, secrets,
   customer data, or model prompts in this ledger.

On **every quarterly feature flag review** (WP.7.6):

1. Export the `feature_flags` registry snapshot, compute its SHA-256
   fingerprint, and collect owner dispositions for every active flag.
2. Query `audit_log` for the latest `entity_kind="feature_flag"`
   mutation per flag, using `feature_flags.created_at` as the fallback
   when no mutation exists.
3. Append one row to the "Feature Flag Quarterly Reviews" table with
   the quarter, review timestamp, registry snapshot fingerprint, review
   packet fingerprint, reviewed count, stale-alert count, disposition,
   and cleanup tracker notes.
4. Append one row to the "Stale Feature Flag Alerts" table for every
   active flag with no mutation for 90 calendar days or more at review
   close. Escalate flags at 180 calendar days to the platform owner.
5. Follow `feature_flag_review_sop.md` for owner acknowledgement,
   disposition vocabulary, long-untouched flag alert thresholds, and
   private evidence handling.

## Upgrades

| Cut-over (UTC) | Package | From → To | PR | Operator | Disposition | Notes |
|---|---|---|---|---|---|---|
| _(no majors shipped yet — N10 policy effective 2026-04-16)_ | | | | | | |

## Rollbacks

| Cut-over (UTC) | Rollback (UTC) | Duration (min) | Package | Trigger | Incident / HANDOFF |
|---|---|---|---|---|---|
| _(no rollbacks logged yet)_ | | | | | |

## Quarterly Summaries

| Quarter | Majors shipped | Rollbacks | Rollback rate | Mean soak (h) | Waivers | Action |
|---|---|---|---|---|---|---|
| _Q2 2026 (Apr–Jun)_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _review scheduled 2026-07-01_ |

## Pentest Reports

| Quarter | Vendor | Test window (UTC) | Report SHA-256 | Findings C/H/M/L | Remediation tracker | Disposition | Notes |
|---|---|---|---|---|---|---|---|
| _Q2 2026 (Apr–Jun)_ | _pending vendor contract_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _KS.4.4 policy effective 2026-05-03; first row due by 2026-06-30_ |

Pentest rows are append-only. If a row is wrong, add a correction row
with `correction -> <quarter/vendor/report-sha256>` in Notes.

## Bug Bounty Programs

| Quarter | Provider | Mode | Disposition | Reward pool USD | Scope SHA-256 | Remediation tracker | Notes |
|---|---|---|---|---:|---|---|---|
| _Q3 2026 (post-GA target)_ | _pending HackerOne/Bugcrowd decision_ | _private managed_ | _planned_ | _pending_ | _pending_ | _pending_ | _KS.4.5 policy effective 2026-05-03; launch only after GA gates pass_ |

Bug bounty program rows are append-only. If a row is wrong, add a
correction row with `correction -> <quarter/provider/scope-sha256>` in
Notes.

## Bug Bounty Findings

| Validated (UTC) | Platform finding ID | Severity | Finding SHA-256 | Remediation ticket | Bounty USD | Disposition | Notes |
|---|---|---|---|---|---:|---|---|
| _(no accepted findings yet — KS.4.5 policy effective 2026-05-03)_ | | | | | | | |

Bug bounty finding rows are append-only. Do not store exploit payloads,
researcher PII, secrets, screenshots, or customer data in this ledger.
Use `correction -> <platform-finding-id>` in Notes to correct a prior
row.

## SOC 2 Readiness

| Quarter | GRC platform | Auditor | Criteria | Observation window (UTC) | Evidence index SHA-256 | Disposition | Notes |
|---|---|---|---|---|---|---|---|
| _Q3 2026 target_ | _pending Vanta/Drata/Secureframe decision_ | _pending independent CPA firm_ | _Security/Availability/Confidentiality target_ | _pending_ | _pending_ | _planned_ | _KS.4.7 policy effective 2026-05-03; observation window cannot start until readiness gates pass_ |

SOC 2 readiness rows are append-only. Do not store auditor requests,
raw evidence, screenshots, customer data, secrets, or platform exports
in this ledger. Use `correction -> <quarter/auditor/evidence-index-sha256>`
in Notes to correct a prior row.

## DSAR Evidence

| Completed (UTC) | Request ID | Request type | Subject scope | Export SHA-256 | Evidence SHA-256 | DEKs purged | Audit rows redacted | Disposition | Notes |
|---|---|---|---|---|---|---:|---:|---|---|
| _(no DSAR evidence logged yet - KS.4.8 policy effective 2026-05-03)_ | | | | | | | | | |

DSAR evidence rows are append-only. Do not store raw exports, customer
data, raw audit payloads, secrets, plaintext tokens, plaintext DEKs, or
wrapped DEK material in this ledger. Use
`correction -> <request-id/evidence-sha256>` in Notes to correct a prior
row.

## Priority I Readiness

| Signed off (UTC) | Commit | Backend image digest | KS.1 evidence SHA-256 | KMS evidence SHA-256 | Tenant smoke SHA-256 | Observation window | Disposition | Notes |
|---|---|---|---|---|---|---|---|---|
| _(pending Priority I start)_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _KS.DOD policy effective 2026-05-06; use ready-to-start only after every gate in priority_i_multi_tenancy_readiness.md passes_ |

Priority I readiness rows are append-only. Do not store raw logs,
customer data, secrets, plaintext tokens, plaintext DEKs, or wrapped DEK
material in this ledger. Use
`correction -> <commit/evidence-sha256>` in Notes to correct a prior row.

## KS KMS Control Reviews

| Reviewed (UTC) | Quarter | Provider scope | KEK rotation SHA-256 | Policy snapshot SHA-256 | Dual-control approval SHA-256 | KMS audit export SHA-256 | Disposition | Notes |
|---|---|---|---|---|---|---|---|---|
| _Q2 2026 initial_ | _pending_ | _AWS/GCP/Vault/LocalFernet_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _R46 mitigation ledger effective 2026-05-06; first accepted row required before Priority I start_ |

KS KMS control review rows are append-only. Do not store raw CloudTrail,
Cloud Audit Logs, Vault audit payloads, principals, customer data,
secrets, plaintext DEKs, or wrapped DEK material in this ledger. Use
`correction -> <quarter/provider-scope/evidence-sha256>` in Notes to
correct a prior row.

## Incident Response Drills

| Completed (UTC) | Scenario / Incident ID | Severity | Evidence SHA-256 | Postmortem SHA-256 | Corrective actions | Disposition | Notes |
|---|---|---|---|---|---|---|---|
| _Q2 2026 tabletop target_ | _pending_ | _SEV-2 exercise_ | _pending_ | _pending_ | _pending_ | _pending_ | _KS.4.6 runbook ship effective 2026-05-06; first passed tabletop required before marking deployed-active_ |

Incident response drill rows are append-only. Do not store raw incident
artefacts, customer data, secrets, exploit payloads, screenshots, or full
log exports in this ledger. Use
`correction -> <scenario-or-incident-id/evidence-sha256>` in Notes to
correct a prior row.

## Diff Validation Confidence

| Applied (UTC) | Path | Patch kind | Layer | Confidence | Disposition | Notes |
|---|---|---|---:|---:|---|---|
| _(runtime rows appended by WP.3 patcher; no raw patch payloads stored)_ | | | | | | |

Diff-validation confidence rows are append-only. Do not store raw
SEARCH / REPLACE payloads, file contents, model prompts, secrets, or
customer data in this ledger. Use
`correction -> <applied-utc/path/layer>` in Notes to correct a prior row.

## Feature Flag Quarterly Reviews

| Quarter | Reviewed (UTC) | Registry snapshot SHA-256 | Review SHA-256 | Flags reviewed | Stale alerts | Disposition | Notes |
|---|---|---|---|---:|---:|---|---|
| _Q2 2026 (Apr-Jun)_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _planned_ | _WP.7.6 policy effective 2026-05-05; first review due first working week of Q3 2026_ |

Feature flag review rows are append-only. Store no customer data,
secrets, raw audit payloads, runtime user preferences, or owner-private
comments in this ledger. Use
`correction -> <quarter/review-sha256>` in Notes to correct a prior row.

## Stale Feature Flag Alerts

| Alerted (UTC) | Flag name | Tier | Owner | Last mutation (UTC) | Age days | Alert SHA-256 | Disposition | Notes |
|---|---|---|---|---|---:|---|---|---|
| _(no stale flag alert rows yet - WP.7.6 policy effective 2026-05-05)_ | | | | | | | | |

Stale flag alert rows are append-only. Store no customer data,
secrets, raw audit payloads, runtime user preferences, or alert payload
exports in this ledger. Use
`correction -> <flag-name/alert-sha256>` in Notes to correct a prior row.

## Trigger vocabulary (Rollbacks)

Use one of these standard strings in the Rollbacks "Trigger" column so
the quarterly review can tally by cause:

* `slo/error-rate` — 5xx or domain error rate crossed the N6 runbook's
  Phase-3 threshold.
* `slo/latency-p99` — request latency p99 regressed > 20 % vs baseline.
* `slo/memory` — backend RSS or frontend heap crossed the N6 ceiling.
* `slo/domain` — domain-specific SLO (DAG completion, SSE reconnect,
  auth success rate, smoke test re-run).
* `operator/manual` — operator flipped back before SLOs tripped
  (e.g. visible regression in a dashboard, customer report).
* `ceremony/smoke-fail` — smoke test failed on standby, cut-over
  aborted (this is actually *better* than a rolled-back cut-over —
  the gate did its job).

## Related

* Policy: [`dependency_upgrade_policy.md`](dependency_upgrade_policy.md)
* Runbook: [`dependency_upgrade_runbook.md`](dependency_upgrade_runbook.md)
* Quarterly pentest SOP: [`quarterly_pentest_sop.md`](quarterly_pentest_sop.md)
* Bug bounty SOP: [`bug_bounty_program_sop.md`](bug_bounty_program_sop.md)
* SOC 2 Type II readiness checklist: [`soc2_type2_readiness_checklist.md`](soc2_type2_readiness_checklist.md)
* GDPR / DSAR alignment SOP: [`gdpr_dsar_alignment_sop.md`](gdpr_dsar_alignment_sop.md)
* Quarterly feature flag review SOP: [`feature_flag_review_sop.md`](feature_flag_review_sop.md)
* Deploy-time gate: [`../../scripts/check_bluegreen_gate.py`](../../scripts/check_bluegreen_gate.py)
* Auto-label workflow: [`../../.github/workflows/blue-green-gate.yml`](../../.github/workflows/blue-green-gate.yml)
* Fallback SOP (Path C hard rollback): [`fallback_branches.md`](fallback_branches.md)
