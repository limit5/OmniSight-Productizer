# OmniSight Disaster Recovery — RTO / RPO Objectives (G6 / HA-06)

> G6 #2 (TODO row 1380). Second deliverable of the G6 HA-06 bucket —
> makes the recovery-time and recovery-point targets **explicit and
> contract-pinned** so every other G6 row (daily drill, manual
> failover runbook, annual drill checklist, bundle-closure script)
> has the same numeric floor to plan against.

This document is the **canonical source of truth** for the two
disaster-recovery budgets every OmniSight operator must treat as a
hard contract:

| Objective | Definition | OmniSight target | Charter literal |
|---|---|---|---|
| **RTO** (Recovery Time Objective) | Max wall-clock time from incident declaration to service being back up. | **≤ 15 min** | TODO row 1380 |
| **RPO** (Recovery Point Objective) | Max acceptable data loss window (oldest durable on-disk write that can be lost without manual reconciliation). | **≤ 5 min** | TODO row 1380 |

Every other artefact in the G6 bucket is bounded by these two numbers:

* G6 #1 (row 1379) — daily DR drill must **finish inside the RTO**
  (≤ 15 min end-to-end across `primary-backup` → `secondary-restore`
  → `smoke-subset` → `report`; the four job `timeout-minutes` sum is
  intentionally ≤ 40 min but the drill is expected to *land* well
  inside 15 min on a warm CI runner).
* G6 #3 (row 1381) — primary-DB manual-switch runbook must be writable
  for "failover-in-15-min" (pg promote + service re-point + smoke
  inside the window — the runbook does NOT get to spend ≥ 15 min
  explaining what to do).
* G6 #4 (row 1382) — annual DR drill checklist measures the *achieved*
  RTO/RPO against these targets and flags any drift.
* G6 #5 (row 1383) — `scripts/dr_drill.sh` + `docs/ops/dr_runbook.md`
  close the bucket; both MUST reference this doc by path so the
  targets stay in one place.

---

## 1. Why these two numbers — and not a different pair

OmniSight is a **dev command-center for embedded AI cameras**, not a
payment rail. The failure mode the product actually cares about is:

1. A camera-firmware developer is 3 hours into a debug session,
   orchestrator + episodic memory + audit log are live, and the
   backend host dies (hardware / kernel / containerd / disk-full).

2. The developer needs the system back **before the next stand-up**
   (15 min is the operational floor — anything longer means the
   standup becomes "the tool was down, no status today").

3. Losing ≥ 5 min of episodic memory / audit-log rows is the point
   at which the developer has to **manually reconstruct** what they
   were doing (re-prompt, re-run commands, re-answer DE proposals);
   below 5 min a single verbal debrief covers the gap.

The **15 / 5** pair comes from those two product constraints, not
from a compliance template. If the product changes (e.g. becomes a
billing system, multi-tenant SaaS, or has a regulatory audit chain
with legal retention SLAs), **revisit both numbers here first** —
don't drift G6 artefacts silently.

### 1.1 Explicit non-goals

* **Not a 99.99 % availability SLA.** Availability is a separate
  production concern — see G7 (row 1385–1388) for the observability
  dashboard that *measures* availability; this doc only bounds
  recovery speed once an incident has been declared.
* **Not a data-integrity SLA.** Phase 53 audit-log hash chain is the
  integrity contract (`scripts/backup_selftest.py` verifies it every
  restore); RPO is a *durability* bound, not an *integrity* bound.
* **Not a cost ceiling.** The backup / retention / replication cost
  to hold RPO ≤ 5 min is accepted as the price of the product
  promise — cost optimisation belongs in a separate review.

---

## 2. RTO — Recovery Time Objective (≤ 15 min)

### 2.1 Definition

**RTO** is the wall-clock time from:

* **t₀** — operator declares the incident (page received, host
  reported dead, or backend health-check 5xx sustained > 1 min).

to:

* **t₁** — service is back up and the `/readyz` probe returns 200
  from the recovery target (new primary, standby-promoted, rebuilt
  host — whatever path the runbook chose).

**OmniSight target: `t₁ − t₀ ≤ 15 min`**, measured on the operator's
wall clock (not the CI runner's, not the metric scraper's — the
operator's, because that's who decides "we're back").

### 2.2 How the 15-min budget splits

| Phase | Budget | What happens |
|---|---|---|
| **Detect + declare** | 0–2 min | Grafana alert fires (G7 row 1388 — 5xx > 1 % for 2 min / instance-down), oncall acknowledges, declares incident. |
| **Diagnose + decide** | 2–5 min | Operator reads `docs/ops/db_failover.md` §6 "Primary won't come back in bounded time" or the G6 #3 proxy-fallback runbook, chooses path A (promote standby) vs B (rebuild primary). |
| **Execute** | 5–12 min | `pg_ctl promote` (≤ 30 s) + service re-point (Caddy reload, ≤ 10 s) + smoke subset (`pytest backend/tests/test_prod_smoke_test_subset_cli.py`, ≤ 60 s on warm VM) + latency budget for human typing / misreading / retry. |
| **Verify + ack** | 12–15 min | Operator confirms `/readyz` 200, posts "we're back", updates incident ticket. |

If any single phase blows budget, the runbook is broken — file a G6
post-mortem and tighten the offender (not the budget).

### 2.3 RTO is **asymmetric** — *declaration* matters more than *execution*

The 15 min budget assumes the operator is **awake, at a keyboard, and
has the runbook open**. It does **not** include:

* The time between "host actually dies" and "page arrives" (covered
  by G7 alert rules — monitor-side, not recovery-side).
* The time between "page arrives" and "operator is at keyboard"
  (oncall pager SLA, not this doc's scope).
* The time to write a public incident post-mortem (post-recovery).

This asymmetry is why G6 #1's daily drill runs unattended on CI — it
measures the *execution* budget in isolation, surfaces drift fast,
and gives the operator a high-confidence floor for the other phases.

### 2.4 What counts as "service is back"

`/readyz` 200 is the single canonical signal:

* backend container responsive (G1 liveness satisfied),
* DB reachable (G4 Postgres primary alive),
* migrations current (G4 #1 Alembic contract),
* smoke-subset passes (G6 #1 artefact).

A backend that returns 200 on `/livez` but 503 on `/readyz` is **not
recovered** for RTO purposes — the readiness probe is what Caddy and
the K8s HPA use to route traffic (G5 #4 row 1372).

---

## 3. RPO — Recovery Point Objective (≤ 5 min)

### 3.1 Definition

**RPO** is the maximum age of the oldest durable write that can be
lost during recovery:

* **t₀** — incident occurs (host dies / DB corrupts / disk fills).
* **t_last-safe** — timestamp of the most recent write that survives
  into the recovered primary.

**OmniSight target: `t₀ − t_last-safe ≤ 5 min`**, averaged over any
7-day window (a single spike from a paused WAL shipper is tolerable
if the 7-day average holds; a sustained breach is an incident).

### 3.2 How the 5-min budget is achieved

| Data surface | Mechanism | Typical lag |
|---|---|---|
| **Postgres WAL shipping** | Async streaming replication to `pg-standby` (see `docs/ops/db_failover.md` §3). | ≤ 1 s steady-state, ≤ 60 s during primary restart. |
| **SQLite audit-log (dev mode)** | `sqlite3.Connection.backup()` WAL-safe online backup, invoked every 5 min via `scripts/backup_selftest.py` seam. | ≤ 5 min by construction. |
| **Agent episodic memory (SQLite)** | Same 5-min backup seam as audit-log. | ≤ 5 min. |
| **Object artefacts (screenshots / LLM responses / exports)** | Re-derivable from inputs — not in RPO scope. | N/A. |

The **tightest** constraint is the SQLite 5-min backup cadence; the
Postgres path is typically 1–60 s. When the Postgres path is the
only durable store (prod-mode deploys), RPO is effectively
**sub-minute** and this doc's 5-min target is the *upper bound*,
not the expected value.

### 3.3 RPO is **bounded by what the daily drill exercises**

G6 #1's daily drill (`.github/workflows/dr-drill-daily.yml`) runs:

1. `primary-backup` job seeds a synthetic DB + runs
   `sqlite3.Connection.backup()` → uploads artefact.
2. `secondary-restore` job downloads artefact → runs
   `scripts/backup_selftest.py` → verifies integrity + 6 required
   tables + Phase 53 hash chain.

If the drill goes **red** for > 1 day, the RPO claim is **suspended**
— operators must assume current RPO = "whatever the last green
drill's backup was", and treat any outage as potential data loss.
The G6 #4 annual checklist explicitly reconciles drill-green-days vs
observed-RPO over the preceding 12 months.

### 3.4 RPO is **not** zero

5 min is a deliberate trade — zero RPO would require synchronous
replication on every write, which costs:

* 2× write latency on the hot path (every commit waits for ack from
  standby).
* Availability loss if standby is down (writes block).
* Operational complexity (fsync tuning, replication slots, back-
  pressure).

The product does not justify that cost. If it ever does (e.g. a
regulated tenant arrives), revisit §1 here, not the budget.

---

## 4. How this doc stays honest

The numbers above are **contract-pinned**:

* `backend/tests/test_dr_rto_rpo_g6_2.py` asserts the literal
  `RTO ≤ 15 min` and `RPO ≤ 5 min` appear in this file's text, so a
  silent drift of either number in this file RED-flags CI.
* TODO row 1380 headline duplicates the same targets; a mismatch
  between the headline and this doc's targets also reds the
  contract.
* The G6 #1 daily drill's `timeout-minutes` sum is an upper bound,
  not the target — if the drill runtime creeps close to 15 min,
  that's the "execution" phase of §2.2 running hot and should be a
  G6 post-mortem trigger.

Any operator or contributor changing these numbers must:

1. Update this doc (§ header table + §2 / §3 budget sections).
2. Update TODO row 1380 headline.
3. Update the contract test's pinned literals.
4. Post a rationale in HANDOFF.md.

A PR that changes the numbers in only one of the four places **will
red** — this is the intended behaviour.

---

## 5. Cross-references

* **G6 #1** — `.github/workflows/dr-drill-daily.yml` + contract
  `backend/tests/test_dr_drill_daily_g6_1.py`. Daily CI evidence
  that the restore-execution path holds.
* **G6 #3** (row 1381, not yet landed) — manual failover runbook
  for DB primary + reverse-proxy. MUST reference this doc's RTO
  budget in its step-by-step.
* **G6 #4** (row 1382, not yet landed) — annual DR drill
  operator checklist. MUST reconcile observed RTO / RPO against
  these targets.
* **G6 #5** (row 1383, not yet landed) — `scripts/dr_drill.sh` +
  `docs/ops/dr_runbook.md` bundle closure. MUST cite this file.
* **G4** — `docs/ops/db_failover.md` (the Postgres HA runbook;
  §6.2 already references the 5-min RTO-class threshold).
* **G1 / G5 #4** — `/readyz` endpoint; the canonical "service is
  back" probe. See `backend/routers/health.py` + the K8s probe
  contract at `deploy/k8s/10-deployment-backend.yaml`.
* **G7** (row 1385–1388, not yet landed) — Prometheus + Grafana
  alert rules. The monitoring side of RTO declaration.

---

## 6. Scope — what this document does NOT cover

* **Manual failover steps.** Owned by G6 #3 (row 1381).
* **Annual operator checklist.** Owned by G6 #4 (row 1382).
* **`scripts/dr_drill.sh` operator-runnable wrapper.** Owned by G6 #5
  (row 1383).
* **Observability dashboard + alert thresholds.** Owned by G7.
* **Compliance / legal data retention.** Out of G6 entirely; if a
  tenant or regulation requires it, open a new bucket.

If any of the above lands here silently, the contract test's
sibling-scope guards RED-flag it.
