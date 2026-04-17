# OmniSight Annual DR Drill Checklist (G6 #4 / HA-06)

> G6 #4 (TODO row 1382). Fourth deliverable of the G6 HA-06 bucket —
> the **operator-led, human-in-the-loop** annual rehearsal that
> complements the automated daily drill shipped by G6 #1
> (`.github/workflows/dr-drill-daily.yml`).
>
> The daily drill proves the **execution** path still works on a warm
> CI runner. This annual checklist proves the **full operator loop**
> still works — paging, handoff, decision-making, typing the real
> commands under time pressure — against the RTO ≤ 15 min / RPO ≤ 5 min
> budget pinned by G6 #2 (`docs/ops/dr_rto_rpo.md`), using the
> step-by-step from G6 #3 (`docs/ops/dr_manual_failover.md`).
>
> Read this doc once a year, pick a drill date, run the scenarios
> below on the live staging HA pair (NOT production), fill in the
> report template in §6, and reconcile observed RTO / RPO vs budget.

---

## 1. Why **annual** — and not monthly / quarterly / ad-hoc

| Cadence | Why NOT for OmniSight |
|---|---|
| **Monthly / quarterly** | OmniSight is a dev command-center with one operator pool; a monthly human drill consumes ≥ 2 % of operator hours. The daily automated drill (G6 #1) already surfaces **execution-path** drift within 24 h — monthly human drills would overlap that coverage. |
| **Ad-hoc only (when something breaks)** | Real incidents don't exercise the *decision* phase cleanly (operator is already in firefight mode, runbook feels natural because adrenaline carries it). An unscheduled drill catches the "did we update the runbook but never re-walk it?" drift. |
| **Every 2–3 years** | Too slow — runbook commands, container names, env-var names, and cross-reference paths drift faster than that. The G6 #3 runbook cites ~20 literal commands + ~10 file paths; a 3-year gap means half of them have probably moved. |

**Annual** is the smallest cadence at which the human drill adds
coverage the daily automated drill does not, without over-taxing the
operator pool. If the product changes (multi-tenant SaaS, regulated
audit, 24×7 oncall rotation ≥ 3 operators), revisit this cadence
first — don't silently tighten it without updating §1 here.

---

## 2. Pre-drill preparation (T-14 days → T-0)

Work backwards from the chosen drill date.

### 2.1 T-14 days — schedule + announce

- [ ] Pick a drill date. Avoid release-candidate cut weeks, public
  holidays in the operator's time zone, and the 48 h after any G4 /
  G6 / G7 artefact change (give contract tests a chance to green).
- [ ] Choose the **drill operator**. Must be someone who has **not**
  driven a real incident in the last 90 days — the drill's value is
  checking that a cold operator can follow the runbook, not that the
  incident veteran still remembers it.
- [ ] Choose a **drill observer** (ideally different from the drill
  operator). Observer starts the stopwatch, reads the report template
  back at the end, and is the second pair of eyes on each `/readyz`
  verification.
- [ ] Announce in the team channel + calendar invite. Include: drill
  date, target staging host, scenario list (§3–§5), expected duration
  (45–75 min total across three scenarios).
- [ ] File a pre-drill ticket in the incident tracker. This is the
  same tracker real incidents use — walking the tracker flow is part
  of what the drill exercises.

### 2.2 T-7 days — dry check

- [ ] Confirm the latest daily drill (G6 #1) is **green** on the
  target staging env. If it has been red for > 1 day, **reschedule
  the drill** — annual checklist runs on top of a known-good
  execution path, not instead of one.
- [ ] Confirm the staging HA pair is running the same container
  image digests as production (or document the diff). Drilling on a
  drift-ahead or drift-behind version is a drill of a runbook we
  don't ship.
- [ ] Re-read `docs/ops/dr_manual_failover.md` end to end.
  Sanity-check every path literal it cites still exists:
  `deploy/reverse-proxy/Caddyfile`, `docs/ops/db_failover.md`,
  `scripts/backup_selftest.py`, the
  `.github/workflows/dr-drill-daily.yml` workflow, etc.
- [ ] Re-read `docs/ops/dr_rto_rpo.md` §2.2 phase split so the
  observer can call out which phase is at risk during the drill.

### 2.3 T-0 — pre-flight

- [ ] Drill operator at keyboard, drill observer with stopwatch +
  report template (§6) open.
- [ ] Verify staging endpoints respond to `/readyz` 200 (edge + both
  backend replicas). If any is already red, stop — fix first.
- [ ] Snapshot current `pg_is_in_recovery()` on both PostgreSQL
  nodes; standby must be streaming (replay lag < RPO 5 min). If the
  pair is not in healthy replicating state, stop — fix first.
- [ ] Start the wall-clock timer — this is t₀ for scenario 1.

---

## 3. Scenario A — Database primary failover (manual switch)

Rehearses `docs/ops/dr_manual_failover.md` §2. Budget: ≤ 15 min
from induced-failure to `/readyz` 200 on the staging HA pair.

### 3.1 Induce the failure (simulated, on staging only)

- [ ] Kill the primary container on the staging host:
  ```bash
  docker compose -f deploy/postgres-ha/docker-compose.yml stop pg-primary
  ```
  This mimics "primary host died" without needing to power-cycle
  hardware.
- [ ] Observer starts the stopwatch at the moment `docker compose
  stop` returns.

### 3.2 Walk the G6 #3 runbook (do NOT skip steps)

- [ ] Step 1 — Detect + declare (target 0–2 min). Run the two
  `docker compose ps` + `psql` probes from
  `dr_manual_failover.md` §2.2 Step 1. Observer notes wall-clock.
- [ ] Step 2 — Diagnose + decide (target 2–5 min). Operator reads
  `dr_manual_failover.md` §2.2 Step 2 symptom table, declares
  path B (promote). Observer notes wall-clock.
- [ ] Step 3 — Execute: `pg_ctl promote` against staging standby
  (target 5–8 min). Observer captures `replay_lag` at promote
  moment (this is the **observed RPO** for the scenario).
- [ ] Step 4 — Re-point app `OMNISIGHT_DATABASE_URL`, restart one
  replica at a time (target 8–12 min).
- [ ] Step 5 — Verify + ack: `/readyz` 200 from edge + both
  replicas (target 12–15 min). Observer stops the stopwatch — this
  is the **observed RTO**.

### 3.3 Capture & clean up

- [ ] Record observed RTO + observed RPO in the §6 report template.
- [ ] Rebuild the staging old-primary as the new standby via
  `docs/ops/db_failover.md` §7 (`pg_basebackup`). This is out of
  the drill budget but in the drill scope — topology must be
  restored to the known-good HA pair before the next scenario.
- [ ] Note any step where the operator deviated from the runbook
  (runbook said X, operator did Y). These are the **highest-value
  findings** — runbook drift that the daily drill cannot catch.

---

## 4. Scenario B — Reverse proxy (Caddy) fallback

Rehearses `docs/ops/dr_manual_failover.md` §3. Budget: ≤ 15 min
from induced-failure to edge `/readyz` 200.

### 4.1 Induce the failure (pick ONE path per drill year — rotate annually)

The G6 #3 runbook §3.2 has four symptom paths (A/B/C/D). Rotate
which path the drill exercises each year so no single path goes
untested for > 4 years.

- [ ] **Year N (Path A — Caddy crash-loop)**: introduce a deliberate
  syntax error into a copy of the Caddyfile on the staging host,
  restart Caddy. Drill operator must identify the bad config via
  `caddy validate`, rollback, restart.
- [ ] **Year N+1 (Path B — bad Caddyfile lands)**: git-revert the
  Caddyfile to a known-good HEAD~1 on staging, `caddy reload`. Drill
  operator walks the revert + reload path.
- [ ] **Year N+2 (Path C — direct-to-backend bypass)**: stop the
  Caddy container on staging, bring up the foreground one-liner
  bypass listener on `:80`. Drill operator must return to `:443`
  TLS-on within the shift.
- [ ] **Year N+3 (Path D — edge-layer bypass)**: simulate the
  upstream edge (Cloudflare tunnel / host firewall) being down;
  drill operator writes the DNS / firewall bypass plan on paper
  (not executed on staging) + the restore-to-canonical plan. This
  path cannot be safely rehearsed end-to-end on a shared staging
  env — documenting the plan is the drill artefact.

### 4.2 Walk the G6 #3 runbook (do NOT skip steps)

- [ ] Step 1 — Detect (target 0–2 min): three-layer probe (Caddy
  container, edge `/readyz`, backend host-port `/readyz`).
- [ ] Step 2 — Diagnose (target 2–5 min): read `caddy logs`,
  select A / B / C / D path from the symptom table.
- [ ] Step 3 — Execute the selected path (target 5–12 min).
- [ ] Step 4 — Verify: edge `/readyz` 200 (target 12–15 min).
  Observer stops the stopwatch.

### 4.3 Capture & clean up

- [ ] Record observed RTO in §6.
- [ ] Restore Caddy to the canonical `:443` TLS-on edge.
- [ ] For Path C / Path D drills, confirm TLS is back on — the
  runbook explicitly warns bypass windows must not persist past the
  shift, and the annual drill is where that discipline is practiced.

---

## 5. Scenario C — Backup / restore chain + RPO reconciliation

Rehearses the **data-plane** side of the DR contract — the piece
the daily drill already exercises on a synthetic DB, but re-done
here against the real staging data surface.

- [ ] On staging, trigger `workflow_dispatch` of
  `.github/workflows/dr-drill-daily.yml`. Confirm it greens end to
  end (`primary-backup` → `secondary-restore` → `smoke-subset` →
  `report`).
- [ ] Pull the latest 7 daily drill reports from the workflow's
  `dr-drill-report.md` artefact. Count green days. If < 350 / 365
  for the preceding 12 months, the RPO claim is **suspended** for
  the reconciliation (per `docs/ops/dr_rto_rpo.md` §3.3) — note in
  the report.
- [ ] For each of the 7 most-recent drills, extract the observed
  backup-size + restore-time + replay_lag (if logged). Average and
  compare to the 5-min RPO budget.
- [ ] Run `scripts/backup_selftest.py` against the latest
  production backup artefact (on the staging host, never write to
  production). Confirm integrity_check + 6 required tables + Phase
  53 audit_log hash chain all pass.
- [ ] Record the **observed RPO** for the scenario (= the replay_lag
  captured during Scenario A + the SQLite backup cadence delta).

---

## 6. Post-drill report template

Fill in the table below and attach it to the drill ticket. Copy
the filled-in version into the team wiki / shared drive as the
year's archival record — the report template is the drill's
**only** durable output besides a "we rehearsed it" assertion.

| Field | Year N value |
|---|---|
| Drill date | YYYY-MM-DD |
| Drill operator | name |
| Drill observer | name |
| Scenario A observed RTO | N min (budget ≤ 15 min) |
| Scenario A observed RPO | N min (budget ≤ 5 min) |
| Scenario B path rehearsed | A / B / C / D |
| Scenario B observed RTO | N min (budget ≤ 15 min) |
| Scenario C drill-green-days (past 365) | N / 365 |
| Scenario C observed RPO (from drill data) | N min (budget ≤ 5 min) |
| Runbook drift found | free text — list of runbook-vs-reality gaps |
| Follow-up tickets opened | ticket numbers |
| Next drill target date | YYYY-MM-DD (≈ +365 d) |

### 6.1 Reconciliation — observed vs budget

If **any** of the observed values breach the budget:

- Breach by ≤ 10 % (e.g. 16 min RTO vs 15 min budget): file a
  runbook-tightening ticket; the drill was green-ish.
- Breach by > 10 %: the runbook is broken for the scenario that
  breached. File a **blocker ticket** + post-mortem; the next
  annual drill cannot proceed until the breach is closed. This
  mirrors G6 #2 §4 "tighten the offender, not the budget".

### 6.2 Drift findings — what to do with them

The annual drill's highest-value output is runbook drift: commands
that moved, paths that renamed, env vars that changed, containers
that got merged or split. For each drift line:

- Open a PR against the specific runbook line (commit title:
  `runbook: G6 annual drill N — <short description>`).
- Cross-reference the drift PR in the drill ticket.
- Do not batch drift PRs — one drift per PR keeps blame-surface
  small and re-reviewable.

---

## 7. Cross-references

* **G6 #1** — `.github/workflows/dr-drill-daily.yml` + contract
  `backend/tests/test_dr_drill_daily_g6_1.py`. The automated daily
  execution-path drill this checklist complements (not replaces).
* **G6 #2** — `docs/ops/dr_rto_rpo.md` + contract
  `backend/tests/test_dr_rto_rpo_g6_2.py`. The RTO ≤ 15 min /
  RPO ≤ 5 min budget this checklist reconciles observed values
  against.
* **G6 #3** — `docs/ops/dr_manual_failover.md` + contract
  `backend/tests/test_dr_manual_failover_g6_3.py`. The operator
  step-by-step for scenarios A + B. This checklist **exercises**
  that runbook — every drift finding flows back into a line change
  in that doc.
* **G6 #5** (row 1383, not yet landed) — `scripts/dr_drill.sh`
  operator-runnable wrapper + `docs/ops/dr_runbook.md` bundle
  aggregator. The aggregator will link to this checklist as the
  annual cadence artefact.
* **G4 #6** — `docs/ops/db_failover.md`. §7 (rebuild old primary as
  new standby) is referenced from Scenario A §3.3 clean-up.
* **G3** — `docs/ops/blue_green_runbook.md`. Reference for the
  Scenario A Step 4 re-point on hosts running blue-green.
* **G1 / G5 #4** — `/readyz` endpoint; the canonical "service is
  back" signal used as the stopwatch-stop criterion in every
  scenario.
* **G7** (rows 1385–1388, not yet landed) — Prometheus + Grafana
  alert rules. The page-fire source the drill's Scenario A §3.1
  Step 1 assumes is already working (this checklist does not drill
  the alerting side — that's G7's scope).

---

## 8. Scope — what this checklist does NOT cover

* **Automated daily DR drill execution.** Owned by G6 #1. The
  annual checklist *consumes* the daily drill's green/red signal
  (Scenario C §5) but does not replace it.
* **RTO / RPO budget definition.** Owned by G6 #2. The checklist
  cites the 15-min / 5-min numbers but does not define them; a
  change to either number must happen in G6 #2 first.
* **Manual failover step-by-step.** Owned by G6 #3. The checklist
  *invokes* the G6 #3 runbook inside the scenarios but does not
  duplicate the commands — every command literal the operator
  types comes from the G6 #3 runbook, not from this file.
* **`scripts/dr_drill.sh` operator-runnable wrapper + the
  `docs/ops/dr_runbook.md` bundle-closure aggregator.** Owned by
  G6 #5 (row 1383, not yet landed). This checklist is a separate
  artefact the aggregator will cross-reference.
* **Observability dashboards + alert rules.** Owned by G7 (rows
  1385–1388, not yet landed). The drill assumes the page-fire side
  works; verifying *that* is G7's scope.
* **PostgreSQL HA operation under planned maintenance (rolling
  restart, minor-version upgrade, extension enable).** Owned by
  G4 (`docs/ops/db_failover.md`). The annual drill is for
  **unplanned** failover scenarios.
* **PITR / WAL archival recovery.** Out of OmniSight scope per
  `docs/ops/db_failover.md` §1.2.
* **Cross-region DR, multi-standby topology, tenant isolation.**
  Out of OmniSight scope — the HA pair is one primary + one hot
  standby.
* **Compliance / legal / retention SLA drills.** Out of G6
  entirely; if a regulated tenant arrives, open a new bucket.

If any of the above lands here silently, the contract test's
sibling-scope guards in
`backend/tests/test_dr_annual_drill_checklist_g6_4.py` RED-flag it.
