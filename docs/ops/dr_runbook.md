# OmniSight Disaster Recovery Runbook (G6 #5 / HA-06 bundle closure)

> **G6 #5 (TODO row 1383).** Fifth and final deliverable of the G6
> HA-06 bucket — the single-entry-point **aggregator** that points
> the on-call operator at every other G6 artefact in the order it
> needs to be read during an incident. Lives alongside
> [`scripts/dr_drill.sh`](../../scripts/dr_drill.sh), the
> operator-runnable wrapper that exercises the same backup →
> restore → selftest → smoke-subset round-trip the G6 #1 daily CI
> drill runs.
>
> If you are the on-call operator paged at 3am, **start at §1**.
> Every other section is reference.

---

## 0. What this runbook is (and is not)

This runbook is the **bundle closure** for the G6 bucket. It does
**not** restate the step-by-step commands or the objective
definitions — those have canonical homes and drifting them across
two docs is the single most common source of stale runbooks. What
this doc does is:

1. Give the operator a **decision tree** to the right other-doc for
   the failure mode in front of them (§1).
2. Declare the **contract** each sibling artefact is pinned to (§2)
   so the operator knows which doc is authoritative for which
   piece of the recovery contract.
3. Document the **operator-runnable drill wrapper**
   (`scripts/dr_drill.sh`) that ships with this runbook (§3).
4. Record the **relationships** between the G6 artefacts (§4) so a
   new on-call can read this doc end-to-end and know where every
   part of the DR promise is implemented.

What this doc is **NOT**:

* **Not** a replacement for any sibling runbook. It links; it
  does not duplicate.
* **Not** the step-by-step for DB primary failover or proxy
  fallback — those are [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md)
  (G6 #3).
* **Not** the RTO / RPO budget definition — that is
  [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) (G6 #2).
* **Not** the annual human-led drill checklist — that is
  [`docs/ops/dr_annual_drill_checklist.md`](dr_annual_drill_checklist.md)
  (G6 #4).
* **Not** the automated CI drill — that is
  [`.github/workflows/dr-drill-daily.yml`](../../.github/workflows/dr-drill-daily.yml)
  (G6 #1).

---

## 1. Incident decision tree (read this first)

Paged. What is broken? Follow the branch, open the doc it points
at, come back here when recovery is acknowledged.

```
Page fired — what is broken?
│
├── DB primary host dead / unreachable / pg-primary container OOM-looping
│   → docs/ops/dr_manual_failover.md §2  (DB primary failover)
│     Budget: ≤ 15 min per docs/ops/dr_rto_rpo.md §2.2.
│
├── Edge https://<host>/readyz 5xx / connection refused / TLS error
│   AND both backend replicas answer /readyz 200 on their host ports
│   → docs/ops/dr_manual_failover.md §3  (Reverse proxy fallback)
│     Budget: ≤ 15 min per docs/ops/dr_rto_rpo.md §2.2.
│
├── Daily DR drill (G6 #1) red on CI — operator investigation
│   → §3 below  (scripts/dr_drill.sh local reproducer)
│     Reproduce locally with the identical four-stage flow, bisect,
│     file a G6 post-mortem (do NOT silently extend the RTO budget).
│
├── Backup / restore chain suspect (integrity_check flaps,
│   selftest reports schema miss, audit_log hash chain broken)
│   → scripts/backup_selftest.py against the latest backup artefact
│   → §3 below  (scripts/dr_drill.sh --no-smoke for the data-plane only)
│   → if root cause > 1 day old, treat RPO claim as suspended per
│     docs/ops/dr_rto_rpo.md §3.3 until the drill is green again.
│
├── Annual drill is due
│   → docs/ops/dr_annual_drill_checklist.md (§2 pre-drill → §6 report)
│     This runbook's §3 scripts/dr_drill.sh is the local-host
│     reproducer invoked in that checklist's Scenario C §5.
│
└── Something else (single replica down, transient 5xx, config drift)
    → out of G6 scope; see:
      • G2 rolling-restart         : scripts/deploy.sh --strategy rolling
      • G3 blue-green cutover      : docs/ops/blue_green_runbook.md
      • G4 full Postgres HA        : docs/ops/db_failover.md
```

The decision tree above is the **only** mandatory read before
acting. Every branch terminates at a sibling doc that owns the
step-by-step.

---

## 2. Contract pins — which doc owns which piece of the promise

The G6 bucket ships a single recovery promise:

> **OmniSight recovers from a single host failure within 15 min,
> with at most 5 min of data loss, along a path that is rehearsed
> daily on CI and annually by a human operator.**

That sentence is implemented by five artefacts. Each artefact owns
exactly one contract line — if you are unsure where to look, this
table is the index.

| Contract | Owning artefact | Row | CI contract test |
|---|---|---|---|
| Daily CI drill (backup → restore → selftest → smoke) rounds-trips cross-host | [`.github/workflows/dr-drill-daily.yml`](../../.github/workflows/dr-drill-daily.yml) | G6 #1 / row 1379 | [`backend/tests/test_dr_drill_daily_g6_1.py`](../../backend/tests/test_dr_drill_daily_g6_1.py) |
| RTO ≤ 15 min / RPO ≤ 5 min are **literally pinned** in one doc | [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) | G6 #2 / row 1380 | [`backend/tests/test_dr_rto_rpo_g6_2.py`](../../backend/tests/test_dr_rto_rpo_g6_2.py) |
| Manual DB-primary failover + reverse-proxy fallback step-by-step fit the 15-min budget | [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md) | G6 #3 / row 1381 | [`backend/tests/test_dr_manual_failover_g6_3.py`](../../backend/tests/test_dr_manual_failover_g6_3.py) |
| Annual human-led drill reconciles observed RTO / RPO vs budget | [`docs/ops/dr_annual_drill_checklist.md`](dr_annual_drill_checklist.md) | G6 #4 / row 1382 | [`backend/tests/test_dr_annual_drill_checklist_g6_4.py`](../../backend/tests/test_dr_annual_drill_checklist_g6_4.py) |
| Operator-runnable local-host drill + single-entry aggregator | [`scripts/dr_drill.sh`](../../scripts/dr_drill.sh) + **this doc** | G6 #5 / row 1383 | [`backend/tests/test_dr_bundle_closure_g6_5.py`](../../backend/tests/test_dr_bundle_closure_g6_5.py) |

If a reader is ever uncertain "is THIS doc the authoritative source
for X?" — the answer is in the table above, not in section
headers.

The pins themselves:

* **RTO ≤ 15 min** and **RPO ≤ 5 min** are pinned in
  [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) §2 / §3 and must match
  the TODO row 1380 headline + the G6 #2 contract test's literal
  regex. This runbook **cites** those numbers; it does not
  redefine them.
* **`pg_ctl promote`** is the load-bearing command for DB primary
  failover — owned by [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md)
  §2.2 Step 3, not here.
* **`caddy reload` / `caddy validate`** are the load-bearing
  commands for proxy fallback — owned by
  [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md) §3,
  not here.
* **`scripts/backup_selftest.py` exit codes** are owned by that
  script's own module docstring; this runbook only references
  them in §3's interpretation table.

---

## 3. The operator-runnable drill — `scripts/dr_drill.sh`

The G6 #1 workflow runs on GitHub-hosted runners every day at
17:00 UTC. The annual drill (G6 #4 Scenario C §5) exercises the
staging HA pair once a year. **`scripts/dr_drill.sh`** fills the
gap in between: it lets an operator reproduce the same four-stage
round-trip from their own shell, on their own host, in ~15 s on a
warm laptop.

### 3.1 When to run it

| Signal | Action |
|---|---|
| G6 #1 daily drill red on CI | Run `scripts/dr_drill.sh --seed --no-smoke` locally; if it greens on the same commit, the issue is a CI-runner-image change rather than a code change — open a G6 post-mortem pointing at the runner image delta. |
| Schema PR touches any of the 6 required tables (`tasks / agents / workflow_runs / workflow_steps / audit_log / episodic_memory`) | Pre-merge, run the script against a seeded DB — the selftest's `REQUIRED_TABLES` tuple must match the new schema, or the next daily drill reds. |
| PR touches `scripts/backup_selftest.py` | Run the script against a production-shape SQLite snapshot (`--db data/omnisight.db`) to confirm the selftest's invocation contract did not regress. |
| Annual drill Scenario C §5 | The checklist explicitly invokes this script against the latest prod backup artefact on the staging host. |
| Incident forensics | After an unplanned failover, run `scripts/dr_drill.sh --no-smoke` against the new primary's freshest backup to confirm the **post-incident** topology still honours the backup chain. |

### 3.2 Usage

```bash
# Default: drill against data/omnisight.db, write artefacts to dr-artifacts/
scripts/dr_drill.sh

# Drill a specific DB
scripts/dr_drill.sh --db data/prod.db

# Write artefacts elsewhere (defaults under the project root)
scripts/dr_drill.sh --out /tmp/drill-$(date +%s)

# Skip smoke subset — data-plane-only drill (matches the CI
# secondary-restore job's scope, without the smoke-subset VM)
scripts/dr_drill.sh --no-smoke

# Seed a synthetic DB first (same 6-table + 5-row-audit-chain seed
# the CI workflow uses) — lets the drill run on an empty checkout
scripts/dr_drill.sh --seed

# See all flags + exit codes
scripts/dr_drill.sh --help
```

### 3.3 Stages + failure modes

The script runs four stages in order. A report is always written
to `<out>/dr-drill-report.md` (even on failure) so the operator
has a durable artefact to attach to an incident ticket.

| Stage | What runs | Failure exit | Failure interpretation |
|---|---|---|---|
| **1. primary-backup** | `sqlite3.Connection.backup()` of source DB → `<out>/backup.db` (WAL-safe, same API the selftest uses) | `2` | Source DB unreadable, or the CPython `sqlite3` library drifted. Run `sqlite3 <db> "PRAGMA integrity_check;"` against the source. |
| **2. secondary-restore** | `cp <out>/backup.db <out>/restored.db` (simulated cross-host hop) | `3` | Filesystem / permissions / disk-full. Verify `<out>` is writable + has space. |
| **3. selftest** | `python3 scripts/backup_selftest.py <out>/restored.db` | `3` | See selftest's own exit codes: `2` = backup step, `3` = integrity_check, `4` = schema or audit_log hash chain. |
| **4. smoke-subset** | `pytest backend/tests/test_prod_smoke_test_subset_cli.py` | `4` | The DAG-1 CLI contract regressed. Bisect commits touching `scripts/prod_smoke_test.py`. Skippable with `--no-smoke`. |
| **report (always)** | Writes markdown report with the status table | `5` | `<out>` not writable. Re-run with `--out <writable-dir>`. |

Exit `1` is reserved for usage errors (missing source DB, unknown
flag). Exit `0` means every stage that ran landed `success`.

### 3.4 Why a shell wrapper (and not another CI job)

The G6 #1 workflow already covers the CI case. What it does **not**
cover:

* **Local-host reproduction.** A real DR event is resolved on the
  operator's host, not on a GitHub runner. The operator needs a
  single command that stands up the same flow without network
  access to github.com.
* **Pre-merge developer loop.** Anyone touching the selftest, the
  schema, or the smoke subset can run this script in the PR
  before push to avoid next-day red CI.
* **Staging drill invocation.** The G6 #4 annual checklist
  Scenario C §5 runs this script on the staging host against the
  real prod backup artefact — writing that as YAML in a workflow
  would re-encode the invocation twice.

The shell wrapper is the smallest surface that satisfies all
three. It reuses the Python API the selftest uses (one source of
truth for "what does a backup look like?") and the exact same
smoke test file the CI workflow runs (one source of truth for
"what smoke subset rounds out the drill?").

---

## 4. G6 artefact map

The five G6 artefacts form one promise with one-way dependencies:

```
                  ┌───────────────────────────────────────┐
                  │  G6 #2  dr_rto_rpo.md                 │
                  │  RTO ≤ 15 min / RPO ≤ 5 min           │ ◀── single source of truth
                  │  for the two numbers the bucket honours
                  └───────────────────────────────────────┘
                    ▲         ▲                 ▲
                    │         │                 │ cites
                    │ cites   │ cites           │ + reconciles observed
                    │         │                 │
                  ┌─┴──────┐ ┌┴──────────────┐ ┌┴──────────────────────┐
                  │ G6 #1  │ │ G6 #3         │ │ G6 #4                 │
                  │ daily  │ │ manual_       │ │ annual_drill_         │
                  │ CI     │ │ failover.md   │ │ checklist.md          │
                  │ drill  │ │ step-by-step  │ │ human-led rehearsal   │
                  │ .yml   │ │ for failover  │ │ reconciles + drifts   │
                  └───┬────┘ └───────┬───────┘ └────────┬──────────────┘
                      │              │                  │
                      │ local-host   │ exercises        │ invokes
                      │ reproducer   │                  │
                      ▼              ▼                  ▼
              ┌────────────────────────────────────────────────────┐
              │  G6 #5  (this bucket closure)                      │
              │   • scripts/dr_drill.sh                            │
              │   • docs/ops/dr_runbook.md  ← aggregates all above │
              └────────────────────────────────────────────────────┘
```

Reading order for a new on-call (one-off, ~30 min):

1. [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) — the 2 numbers.
2. [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md) — the
   step-by-step for the 2 failure modes.
3. **This doc §1** — the decision tree that maps page → failure
   mode → sibling doc.
4. [`docs/ops/dr_annual_drill_checklist.md`](dr_annual_drill_checklist.md)
   — what the annual rehearsal looks like.
5. `scripts/dr_drill.sh --help` — how to reproduce the drill
   locally.

---

## 5. Cross-references (beyond G6)

* **G4 #6** — [`docs/ops/db_failover.md`](db_failover.md). The full
  PostgreSQL HA + cutover playbook. §7 is the rebuild-standby
  procedure invoked from G6 #3 §2.3 and G6 #4 Scenario A §3.3.
* **G3** — [`docs/ops/blue_green_runbook.md`](blue_green_runbook.md).
  The app-layer cutover the G6 #3 §2 Step 4 re-point can ride on
  top of if a blue-green ceremony is already in progress.
* **G2** — `deploy/reverse-proxy/Caddyfile` is the source of truth
  for the `:443` listener; G6 #3 §3 references it.
* **G1 / G5 #4** — the `/readyz` endpoint. The canonical
  "service is back" signal used as the stopwatch-stop criterion in
  every G6 scenario. See `backend/routers/health.py` + the K8s
  probe contract at `deploy/k8s/10-deployment-backend.yaml`.
* **G7** (rows 1385–1388, not yet landed) — Prometheus + Grafana
  alert rules. The monitoring side that **fires the page** which
  triggers this runbook's §1 decision tree. G6 owns the recovery
  side; G7 owns the detection side. Silent scope creep of G7
  content into G6 (or vice versa) RED-flags the bucket's contract
  tests.

---

## 6. Scope — what this runbook does NOT cover

The scope fence below prevents silent creep into sibling buckets.
Every line here has an explicit owner.

* **Daily automated drill.** Owned by G6 #1
  (`.github/workflows/dr-drill-daily.yml`). This runbook's §3 is
  the **local-host twin** of that workflow, not the workflow
  itself.
* **RTO / RPO budget definition.** Owned by G6 #2
  (`docs/ops/dr_rto_rpo.md`). This runbook **cites** the 15 min /
  5 min numbers but MUST NOT define them.
* **Step-by-step DB failover + proxy fallback commands.** Owned by
  G6 #3 (`docs/ops/dr_manual_failover.md`). This runbook **links**
  to the step-by-step via §1's decision tree but MUST NOT inline
  `pg_ctl promote`, `caddy reload`, or any other failover command.
* **Annual operator-led drill checklist.** Owned by G6 #4
  (`docs/ops/dr_annual_drill_checklist.md`). This runbook
  **references** it in §1 and §3.1 but is not itself the
  checklist.
* **Observability dashboards + alert rules.** Owned by G7 (rows
  1385–1388, not yet landed). This runbook references G7 in §5
  but does not ship dashboards, panels, or alert thresholds.
* **PITR / WAL archival recovery.** Out of scope for OmniSight per
  [`docs/ops/db_failover.md`](db_failover.md) §1.2.
* **Cross-region / multi-standby topology.** Out of scope —
  OmniSight's HA pair is one primary + one hot standby.
* **Compliance / legal data retention.** Out of G6 entirely.

If any of the above lands here silently, the contract test at
[`backend/tests/test_dr_bundle_closure_g6_5.py`](../../backend/tests/test_dr_bundle_closure_g6_5.py)
RED-flags it.

---

## 7. How this bundle closure stays honest

The G6 bucket is **closed** with this row. That means:

1. All five G6 TODO rows (1379–1383) are flipped `[x]`.
2. All five contract tests are green on the `main` branch at the
   commit that lands this doc.
3. The explicit-migration pattern (remove a sibling guard in the
   commit that lands the row it guards against) is carried
   forward one last time: G6 #4's `test_no_dr_runbook_doc` and
   `test_no_dr_drill_shell_script` are removed in the commit that
   lands this doc + `scripts/dr_drill.sh`. The G6 #5 contract
   asserts the migration is documented in the G6 #4 test.
4. The next bucket to open is **G7** (observability for HA
   signals, rows 1385–1388). G7 is independent of G6 and can open
   in parallel with any later bucket.

A contributor who wants to change the recovery promise (different
RTO / RPO, different failure modes, different drill cadence) must
update the appropriate sibling artefact first — this runbook does
not own any of those numbers, and its `dr_runbook.md` file name is
reserved for the aggregator role only.
