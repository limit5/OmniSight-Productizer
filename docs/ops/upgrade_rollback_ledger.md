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
* Deploy-time gate: [`../../scripts/check_bluegreen_gate.py`](../../scripts/check_bluegreen_gate.py)
* Auto-label workflow: [`../../.github/workflows/blue-green-gate.yml`](../../.github/workflows/blue-green-gate.yml)
* Fallback SOP (Path C hard rollback): [`fallback_branches.md`](fallback_branches.md)
