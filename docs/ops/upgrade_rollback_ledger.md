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
* Deploy-time gate: [`../../scripts/check_bluegreen_gate.py`](../../scripts/check_bluegreen_gate.py)
* Auto-label workflow: [`../../.github/workflows/blue-green-gate.yml`](../../.github/workflows/blue-green-gate.yml)
* Fallback SOP (Path C hard rollback): [`fallback_branches.md`](fallback_branches.md)
