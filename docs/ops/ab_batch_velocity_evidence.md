# AB Batch Velocity Evidence

> Status: evidence index
> Scope: AB Definition of Done row for seven batch-accelerated task families.
> Source ADR:
> [`anthropic-api-migration-and-batch-mode.md`](../operations/anthropic-api-migration-and-batch-mode.md)

This index records the repository evidence that the seven intended
high-volume OmniSight development task families are routed through the
Anthropic Batch lane, and defines the velocity measurements operators
must record after production dogfood.

## 1. Scope boundary

This row covers only the seven batch-accelerated development task
families:

- HD.1 EDA parse tasks
- HD.4 reference design diff tasks
- HD.5.13 datasheet vision extraction tasks
- HD.18.6 CVE impact backfill tasks
- L4.1 determinism regression tasks
- L4.3 adversarial CI tasks
- TODO routine checkbox-processing tasks

It does not claim the one-week API-mode dogfood, first 100-task batch
cost-vs-estimate result, 30-day subscription fallback disable, full ADR
completion, or operator runbook completion rows. Those are separate AB
Definition-of-Done rows.

Current status is `dev-only`. The routing table, cost model, and
operator measurement template are in the repository; production velocity
uplift still requires operator-run batches against the real Anthropic
workspace.

## 2. Seven-family routing matrix

| DoD family | Batch task kind(s) | Priority | Auto-batch threshold | Runtime evidence | Cost evidence |
|---|---|---:|---:|---|---|
| HD.1 | `hd_parse_kicad`, `hd_parse_altium`, `hd_parse_odb`, `hd_parse_eagle` | P2 | 10 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `HD.1 schematic parse` |
| HD.4 | `hd_diff_reference` | P2 | 5 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `HD.4 reference diff` |
| HD.5.13 | `hd_sensor_kb_extract` | P3 | 20 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `HD.5.13 datasheet vision` |
| HD.18.6 | `hd_cve_impact` | P3 | 20 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `HD.18.6 CVE impact` |
| L4.1 | `l4_determinism_regression` | P3 | 50 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `L4.1 determinism regression` |
| L4.3 | `l4_adversarial_ci` | P3 | 30 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `L4.3 adversarial CI` |
| TODO routine | `todo_routine` | P3 | 10 | `backend/agents/batch_eligibility.py::DEFAULT_ROUTING` | ADR section 6.3: `TODO \`[ ]\` routine 任務 batch` |

The drift guard for this matrix is
`backend/tests/test_ab_batch_velocity_evidence.py`. It checks that all
seven families remain batch-eligible, have non-empty auto-batch
thresholds, and still appear in the ADR/runbook evidence.

Cost label parity with the ADR is intentional: TODO `[ ]` routine 任務 batch remains the canonical label for the TODO routine cost row.

## 3. Velocity measurement contract

Operators record one row per dogfood week after API mode is active:

| Field | Definition | Pass threshold |
|---|---|---:|
| `period_start` / `period_end` | ISO dates for the measurement window | 7 calendar days |
| `batch_tasks_submitted` | Count of submitted tasks across the seven families | >= 100 for the first full run |
| `batch_tasks_succeeded` | Count of tasks with succeeded batch results | >= 95% of submitted |
| `subscription_baseline_tasks_per_day` | Median completed routine tasks/day from the subscription-era baseline week | Recorded before comparison |
| `api_batch_tasks_per_day` | Completed seven-family tasks/day during API+Batch week | >= 2x baseline |
| `wall_clock_hours_saved` | Baseline serial effort minus API+Batch elapsed effort for same task count | > 0 |
| `estimated_cost_usd` / `actual_cost_usd` | Cost guard estimate and Anthropic observed spend | Actual within 10% of estimate |
| `batch_discount_observed_pct` | `(realtime_estimate - actual_batch_cost) / realtime_estimate` | >= 45% |
| `p95_batch_completion_hours` | P95 elapsed time from submit to result ingestion | <= 24h |
| `dlq_rate_pct` | DLQ entries divided by submitted tasks | < 2% |

This is intentionally operational evidence, not a new runtime code path.
The current repository proves the routing and measurement contract; the
operator flips the production status after a real dogfood week supplies
the ledger row.

## 4. Production status

This evidence index does not deploy production infrastructure by itself.

**Production status:** dev-only
**Next gate:** deployed-active - operator submits the seven-family dogfood batch set, records the weekly velocity ledger row above, confirms >= 2x tasks/day versus the subscription-era baseline, verifies actual cost is within 10% of estimate, and attaches Anthropic usage export plus batch result IDs.
