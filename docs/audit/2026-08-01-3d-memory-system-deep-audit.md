# Deep Audit — OmniSight 3D Memory System (U6 three-principals architecture)

**Audit date:** 2026-08-01 · **Scope:** structural/temporal/causal axes, leg-1 (Sora 3-tier), leg-2 (worker experience loop), leg-3 (Claude cross-session memory), and the anti-hollow instrumentation built by Phase R (OP-2535..OP-2551) to prevent recurrence of the 2026-07-07 "silent-hollow compound".

**Evidence convention used throughout:**
- `EVIDENCE (this pass)` — I ran the command or read the file during this synthesis; commands and outputs are cited.
- `EVIDENCE (auditor)` — an upstream auditor ran it and it survived adversarial verification.
- `INFERENCE` — reasoned, not executed.
- `UNVERIFIED` — critic finding that no verification pass has adjudicated.

---

## 1. VERDICT

**Partially hollow — and the hollowness has moved.** The part that was audited most aggressively in 2026-07-07 (the three-axis project-state layer) is genuinely repaired: the structural axis returns real content, the causal axis demonstrably returns real failure neighbours whenever its window contains data, and the temporal axis now declares itself unavailable honestly instead of faking a payload. Five of the seven "confirmed" axis findings collapsed under verification precisely because the axes work better than the auditors assumed. But the memory *system* wrapped around those axes is still substantially hollow, and — this is the finding that matters — **the anti-hollow instrumentation Phase R built to prevent a recurrence is itself the hollowest layer in the stack**: Prometheus has zero alertmanagers and is running a config file that predates the ticket that wired alerting, the only alert group loaded covers one of three legs, one of its four rules has been firing permanently on a metric no code produces, and the single canary built to catch a dead incident writer was never installed and has never executed once. Below that, leg-2's entire post-publication arm (injection → citation → utility → revocation) has produced **zero rows in every environment** because its enabling flag exists in no env file, no container, and no live process; leg-3's DB mirror has measurably diverged from the file store while its designed drift gauge is pinned at 0 by construction (no code writes it); and leg-1 has written **two rows in its entire lifetime** while its own predicted hollow-signature (frozen count, growing age — now 9.96 days) sits in Prometheus with nothing watching it. The 2026-07-07 anti-pattern — "a writer that never existed, degrading silently, with zero alarms" — recurs inside the repair itself, three separate times. The system is healthier at the axis layer and no healthier at the systemic layer.

---

## 2. THE HEADLINE FINDINGS

### H1 — There is no alert delivery path at all. Every anti-hollow alert in the memory stack fires into a void. `CRITICAL`

**What is wrong.** Prometheus has zero configured alertmanagers, no Alertmanager container has ever been created on this host, and the config file the running Prometheus loaded contains no `alerting:` block — because the bind-mounted file's inode was replaced *after* the container started.

**EVIDENCE (this pass):**
```
curl -s localhost:9090/api/v1/alertmanagers
  {"status":"success","data":{"activeAlertmanagers":[],"droppedAlertmanagers":[]}}
docker ps -a | grep -i alertman            -> NONE
docker exec omnisight-productizer-prometheus-1 \
  sh -c 'wc -c /etc/prometheus/prometheus.yml; grep -nE "alerting|alertmanager" ...'
  346 /etc/prometheus/prometheus.yml
  NO ALERTING BLOCK IN RUNNING CONFIG
wc -c /home/user/omnisight-prod/configs/prometheus.yml    -> 607
grep -nE 'alerting|alertmanager' (host file)             -> 12: alerting: / 15: targets: ["alertmanager:9093"]
docker inspect -f '{{.State.StartedAt}}' ...prometheus-1 -> 2026-07-09T19:21:05Z
```
The host file (607 bytes, with the OP-2563 alerting block) and the in-container file (346 bytes, without it) are different inodes behind a single-file bind mount; the container has been up since 2026-07-09, and OP-2563 ("Deploy Alertmanager + wire alerting so anti-hollow alerts page") landed 2026-07-10. `EVIDENCE (auditor)`: `configs/alertmanager.yml:26` self-describes its receivers as `Name-only receiver = valid blackhole (delivers nowhere)` — so even if the container existed, delivery would be a no-op by design.

**What it means in practice.** Every alert the memory stack can produce — including `ProjectStateStructuralHollow` and `ProjectStateHalfHollow`, the two rules that exist specifically to catch a hollow axis — reaches nothing but the Prometheus `/alerts` web page. Phase R's central promise was "next time, an alarm fires." No alarm can fire anywhere. This is independently corroborated by the operator's own 2026-07-25 failed-unit sweep note: *"NO alert delivery path exists on this host."*

**Silent?** Yes, and doubly so: the deployment reports success (container healthy, rules loaded, tests green on the file content), and the one condition that would reveal it — an alert needing to page — is exactly the condition being lost.

**Contradiction flagged:** the axis-liveness verifier used this same fact to *lower* the severity of H3 ("no delivery path, so alert fatigue is theoretical"). Both readings are factually correct; I resolve it by promoting the missing delivery path to the top finding and demoting the individual rule defects to symptoms of it.

---

### H2 — Leg-2's terminal arm has never executed anywhere. Its enabling flag exists in no environment. `HIGH`

**What is wrong.** `OMNISIGHT_RUNNER_LEARNED_ITEMS` — the single gate on injection — is set in no env file, no container, and no live runner process. The runner's `_fetch_learned_items_block()` therefore returns `""` before doing anything, and since the runner is the only caller that passes `ticket=`, the only writer of `learned_item_citations` is structurally unreachable.

**EVIDENCE (this pass):**
```
grep -oE '^[A-Z0-9_]+' /home/user/.config/omnisight/runner.env
  -> 10 vars; OMNISIGHT_RUNNER_LEARNED_ITEMS NOT among them
tr '\0' '\n' < /proc/3999407/environ | grep -oE '^OMNISIGHT_[A-Z0-9_]+'
  -> 17 vars; OMNISIGHT_RUNNER_LEARNED_ITEMS absent
grep -ac 'learned_items.result' /tmp/runner-claude-1.log   -> 0   (over 94,302 cycle-ends)
grep -ac 'learned_items.result' /tmp/runner-codex-1.log    -> 0   (over 101,486 cycle-ends)
```
Prod DB row counts (`docker exec omnisight-pg-primary psql -U omnisight -d omnisight`):
```
learned_item_citations   0
learned_item_versions    0
memory_publications      0
memory_transition_events 0
```
`EVIDENCE (auditor, UNVERIFIED by harness but consistent)`: staging shows the same `learned_item_citations = 0` **despite** `learned_item_versions=9, memory_publications=8, memory_approvals=3, memory_eval_runs=14, curator_merge_candidates=5` — i.e. the query harness returns non-zero on that DB, so the zero is real absence, not a broken query. Code path confirmed at `backend/routers/learned_items.py:48` (`if result == "non_empty" and served and ticket and _TICKET_RE.match(ticket)`).

**What it means in practice.** MEMORY.md records leg-2 as "CODE-COMPLETE + STAGING-GREEN" through injection and citations. It is proven only through **publication**. Everything downstream — injection into agent prompts, citation capture, `learned_item_utility`, and the revocation-proposal logic in `memory_promotion_scheduler.py:543-560` which computes utility exclusively from `learned_item_citations` — runs forever on an empty input. Human-approved playbooks reach no agent, and a bad card can never be demoted because the demotion signal cannot exist. The governed loop is a half-loop.

**Silent?** Yes — and worse, the flag-off path is deliberately mute in contradiction of its own docstring. `EVIDENCE (this pass)`, `auto-runner-jira.py:1078-1086`:
```python
"""... Empty string on ANY failure or when
OMNISIGHT_RUNNER_LEARNED_ITEMS is off; result label logged on EVERY
pickup (audit #6: hollow serving must be visible from runner logs)."""
if os.environ.get("OMNISIGHT_RUNNER_LEARNED_ITEMS", "").strip().lower() not in (
    "1", "true", "yes", "on",
):
    return ""            # <-- no print
```
The `print(f"[runner] learned_items.result=...")` sits *after* the network call. The most common state emits nothing, so an operator grepping the logs cannot distinguish "feature off" from "code never reached". That is the 2026-07-07 degrade-silent shape, reintroduced by the commit that cites the 2026-07-07 audit.

---

### H3 — Leg-3's drift detector cannot move, and the mirror it watches has provably diverged. `HIGH`

**What is wrong.** `omnisight_claude_memory_drift` is defined and never written. Meanwhile the DB mirror is 9 days stale, missing 4 memories, and at least one ingested memory's stored body no longer matches disk.

**EVIDENCE (this pass):**
```
grep -rn 'claude_memory_drift' --include=*.py --include=*.yml .
  backend/metrics.py:1245   claude_memory_drift_gauge = Gauge(
  backend/metrics.py:1246       "omnisight_claude_memory_drift",
  backend/metrics.py:1407   ... = claude_memory_drift_gauge = _NoOp()
  (three hits total — definition, name, no-op alias. Zero .set() calls, zero tests, zero rules.)

curl 'localhost:9090/api/v1/query?query=omnisight_claude_memory_drift'
  backend-a: 0 ; backend-b: 0

on-disk memory files: 181      claude_memory_state rows: 177      matched: 177
disk slugs with no DB row:  MEMORY, project_case5_epicB_twoway,
                            project_failed_units_remediation_2026_07_25,
                            project_s12g_A_v2_runtime_defense
max(updated_at) in claude_memory_state = 2026-07-23 15:08:42+00
files with mtime after that: 9, newest project_failed_units_remediation_2026_07_25.md 2026-07-29 10:24
```
`EVIDENCE (auditor, UNVERIFIED)`: content-hash divergence on an *ingested* slug — DB head for `project_u6_kernel_handoff_and_roadmap` has `body_sha256 aabcb571…` / 263,535 bytes (ingested 2026-07-22 17:06) while disk is 265,802 bytes / `ab5760c2…`. I did not re-hash this myself.

**What it means in practice.** Leg-3's mirror is silently 9 days behind, missing four memories including `MEMORY.md` itself and the *failed-unit remediation* memory, with the designed detector reporting perfect health. MEMORY.md records "γ-2 regen + drift-exclusion + reconcile PROVEN" — the reconcile machinery may work when invoked, but nothing detects when it needs to be. Any future decision to cut Claude's memory serving over from the file store to the tables would be made on a mirror nobody can tell is wrong.

**Related, same shape:** `omnisight_memory_quarantine_depth` reads 0 on both replicas while `claude_memory_state` holds **137 quarantined / 40 published**. `EVIDENCE (this pass)`: the only `.set()` on that gauge in the entire repo is `backend/tests/test_u4_snapshot_publisher.py:679`. `INFERENCE`: the gauge was scoped to leg-2's quarantine (genuinely 0), but the net effect is an always-green gauge in the dashboard namespace while the backlog that actually exists — 77% of the leg-3 corpus — has no metric at all.

**Silent?** Yes. A metric that reads 0 because no producer exists is indistinguishable from a metric that reads 0 because everything is fine.

---

### H4 — Leg-1 has produced two rows, ever. Its own predicted hollow-signature has been visible for ten days with no rule watching it. `HIGH`

**EVIDENCE (this pass):**
```
omnisight_u6_l2_summaries_count            = 1   (both replicas)
omnisight_u6_l2_last_summary_age_seconds   = 860,108 s = 9.955 days, rising
omnisight_u6_l3_fact_count{promoted}       = 1 ; quarantined/superseded/rejected = 0
omnisight_memory_liveness_heartbeat        = 0
omnisight_memory_enabled                   = 0
prod DB: chat_session_summaries = 1 row ; l3_facts = 1 row
```
`EVIDENCE (auditor, UNVERIFIED)`: prod flags are ON (`OMNISIGHT_U6_L2_WRITE=1`, `OMNISIGHT_SORA_MEMORY_SCHEDULER=1`, `OMNISIGHT_SORA_L3_READ=1`) and `omnisight_u6_memory_scheduler_ticks_total{outcome="ok"}` = 2885/445 — the scheduler is alive and finding nothing every tick.

**Alert coverage — none.** `EVIDENCE (this pass)`:
```
grep -rln 'omnisight_u6_\|omnisight_claude_memory_\|omnisight_memory_' --include=*.yml --include=*.yaml .
  -> (no files)
control: grep -rln 'omnisight_project_state' --include=*.yml .
  -> deploy/observability/prometheus/project_state_health.yml
curl localhost:9090/api/v1/rules
  -> exactly one group, omnisight_project_state_health, 4 rules
```
The control proves the grep works. **Two of the three legs have no alert rules of any kind**, which the anti-hollow dimension did not count because it scoped itself to the project-state alert family.

**What it means in practice.** `backend/agents/u6_memory_scheduler.py:60-66` states the design intent explicitly: the DB-derived gauges are *"external to this producer, so a flag-ON-but-hollow scheduler is VISIBLE (frozen count + growing age), the 3D-memory lesson."* The prediction was correct, the signature is present exactly as described, and nothing consumes it. The instrumentation was built; the watcher was not.

**Silent?** Yes. Ten days of flag-ON-producing-nothing with zero escalation.

**Caveat, `INFERENCE`:** part of this is downstream of H7 (no traffic) — an L2 summariser with no sessions to summarise legitimately writes nothing. But `memory_enabled=0` and `memory_liveness_heartbeat=0` alongside ON flags suggests a wiring question that this audit did not resolve. Flagged as unverified in §6.

---

### H5 — `ProjectStateNoTraffic` has been firing permanently on a metric no code produces, and the test suite certifies it. `MEDIUM`

**EVIDENCE (this pass):** `curl localhost:9090/api/v1/rules` → `('ProjectStateNoTraffic', 'firing')`; the other three rules `inactive`.
**EVIDENCE (auditor, CONFIRMED):** `omnisight_project_state_calls_total` has no producer anywhere — `grep` for the bare stem `project_state_calls` across develop-tip, sora-bridge, and the Productizer tree returns only `deploy/observability/prometheus/project_state_health.yml:38`, `deploy/observability/grafana/project_state_health.json:245`, and two test assertions. `docker exec ...backend-{a,b}-1 grep -rn project_state_calls /app` → zero in both, and the live `/api/v1/metrics` exposes only `project_state_axis_total`, `project_state_structural_half_total`, `project_state_axis_latency_seconds`. Prometheus `activeAt = 2026-07-09T19:21:11Z` = process start, i.e. firing for its entire ~23-day uptime.

**The test actively defends the defect.** `backend/tests/test_project_state_alert_rules.py:148` asserts the expr *string*; `:213` asserts the phantom metric name appears in the dashboard; `EXPECTED_RULES` at `:47-52` pins `"for": None`, so the suite *requires* the immediate-fire behaviour; `test_alert_exprs_are_ratio_or_absence_only` special-cases this rule to `assert expr.startswith("absent(")` and skips every other check. 9 tests pass in 0.96 s.

**Additionally wrong in the other direction (`INFERENCE` from PromQL semantics):** even with a correct producer, `absent(rate(X[30m]))` cannot detect "no traffic" — a counter that exists but stops incrementing yields `rate() == 0`, not `absent`. The rule would go *silent* the moment a producer was added, including during a genuine traffic loss. It is un-wired now and would be wrong later.

**In practice.** A permanently-firing info-level rule sits in the same group as the two genuine hollow-detectors, which is classic alert-fatigue seeding — though currently theoretical, because per H1 nothing is delivered anywhere. The Grafana panel "Project-state call rate" renders permanently "No data". And the real no-traffic condition *is* currently true (H7), so the honest signal is indistinguishable from the phantom noise.

---

### H6 — The one canary built to catch a dead incident writer was never installed, and its predicate is wrong in the noisy direction. `MEDIUM`

**EVIDENCE (auditor, CONFIRMED, multiple independent routes):** `grep -rl incident_write_canary ~/.config/systemd/user/ /etc/systemd/system/` → no match; no matching unit in `systemctl --user list-unit-files` or either `list-timers`; no cron entry; a host-wide grep finds only repo copies, its unit files, its test, and the Phase-R ticket draft — no wrapper, orchestrator, or pipeline calls it. **Decisive:** the service declares its log at `/home/user/work/sora/logs/canary/incident-write-canary.log`, and that directory does not exist — systemd cannot append into a nonexistent directory, so the unit has never started once. Sibling units from the same repo directory (`staging-gate-canary.timer`, `anthropic-api-canary.timer`) *are* installed, so this was a specific omission; but 20 of 48 repo timers are likewise uninstalled, a systemic "repo unit ≠ installed unit" pattern.

**The predicate defect is the inverse of what was reported.** The auditor's claim that it is "structurally unable to fire" was refuted by executing the shipped prod script's own `collect_counts()` read-only against prod: at `2026-07-22T23:59Z / 1d` it returns `incidents=0 fleet=2 should_escalate=True`. Its real flaw is that incidents are written essentially only on *failure*, so "fleet active AND zero incidents" fires on a perfectly healthy fleet — 11 of 18 active days since 2026-07-01 would have escalated.

**In practice.** Zero live coverage from OP-2544: a detection net that was built and never hung, and which would have been noisy rather than useful had it been. Combined with H1, the durable-incident-writer failure mode has *no* detection at all.

---

### H7 — Upstream of everything: the runner fleet is alive but has picked up nothing for ten days. `INFO / structural`

This is not a memory-system defect, but it is the single fact that explains most "the axis is empty today" observations and it materially limits this audit.

**EVIDENCE (this pass):**
```
claude-1: cycle-ends 94,302   "no candidate passed pre-pickup checks" 93,889  (99.56%)
codex-1:  cycle-ends 101,486  no-candidate 87,101
last line /tmp/runner-claude-1.log: === 23:44:15 claude-1 cycle #58160 end rc=0 ===   (today)
prod DB: max(runner_incidents.created_at) = 2026-07-20 05:40:06
         max(runner_metrics.ts)           = 2026-07-22 16:49:36
         db now                           = 2026-08-01 15:43:41
```
The fleet is cycling every ~40 s and finishing rc=0; what stopped is *pickup*, not the fleet. `EVIDENCE (auditor)`: the last two pickups (OP-2724, OP-2726) both **succeeded** and pushed Gerrit changes, and the last failing pickup on 2026-07-20 produced its three incident rows 16 seconds after the claim — so the durable incident writer is alive and correctly correlated, not dead.

**In practice.** The empty causal axis is a fuel/pickup-gate problem, not a wiring problem, and it self-heals the moment the fleet resumes and a pickup fails. But it also means **prod served roughly 3-5 project-state requests in nine days**, so essentially every production-side conclusion in this audit rests on a handful of samples. Two dimensions each assumed the other owned the fleet-idle question and neither checked the live process; the log path one auditor cited (`/home/user/work/sora/logs/runner/claude-1.systemd.log`, 1,080 bytes, last written 2026-07-20) is not the path the wrapper writes (`/tmp/runner-$INSTANCE.log`, 128 MB-1.4 GB, live).

**Secondary exposure:** the only durable runner history lives in `/tmp` (gemini-1 1.38 GB, grok-1 1.37 GB), outside log-retention scope and lost on reboot.

---

## 3. BY DIMENSION

I received material from three of the nine dimensions by name. I list what I have and mark the gaps honestly rather than inventing coverage.

**1. axis-liveness — 7 findings delivered; 1 confirmed, 6 partially confirmed, 5 downgraded.** This was the most aggressively verified dimension and it largely *exonerated* the axes. Net position:
- *Structural axis:* healthy. 5/5 served axes `content=non_empty`, zero `degraded`, all latencies ≤0.6 s, zero `axis_timeout` warnings in 9 days. The "a slow JIRA call destroys the axis" claim was refuted by stub experiment (JIRA at 1.5 s and 5.0 s both returned at ~610 ms with `jira_source=degraded` **and** `kg_source=live, kg_neighbours=5`); the 0.6 s inner fence cancels correctly, including killing the curl subprocess.
- *Causal axis:* wiring genuinely repaired. Replayed inside the prod container at historical timestamps it returns 1-2 real neighbours per ticket; Prometheus shows `axis="causal",content="non_empty"` grew 9→20 between 2026-07-17 and 2026-07-22. It returns empty today only because of H7.
- *Temporal axis:* deliberately and honestly unavailable (Phase R design doc `docs/design/2026-07-07-phase-r-3d-memory-repair-design.md:62,224`, Graphiti deferred to U5 at `:283`). `dispatch_graphiti_temporal_query` has zero production call sites.
- Residual axis defects, all LOW, listed in §5.

**2. anti-hollow / alerting — reached me only by cross-citation.** Other auditors record that it examined the project-state alert family and the Phase S test harness ("tests prove files contain strings"). Its own findings were not delivered to synthesis. Its central conclusion, that alerting is the weak point, is confirmed and then some — see H1/H4/H5/H6.

**3. kernel-invariant (memory must never authorize a side effect) — no findings delivered.** It is cited by the critic as having "listed [leg-2 injection] explicitly as an unchecked gap" and as having "audited write/side-effect authorization". **I cannot confirm the kernel invariant was actually verified.** Absence of delivered findings is not evidence the invariant holds; see §6. The one authorization-adjacent defect I can evidence is a *read*-scoping gap (§3.5 below), which no dimension owned.

**4. hollow-hunter — one CRITICAL finding delivered (truncated in transit): the Prometheus/Alertmanager gap.** I independently re-verified it in full; it is H1.

**5. completeness-critic — 10 findings, marked UNVERIFIED by the harness. I re-verified 9 of them myself this pass; all 9 reproduced.** Specifically:
- Leg-2 injection flag absent everywhere → citations unreachable — **verified** (H2).
- Flag-off path silent, contradicting its docstring — **verified** (H2).
- `claude_memory_drift` no setter, mirror diverged — **verified** (H3).
- No alert rules for leg-1/leg-3 — **verified** (H4).
- `memory_transition_events` zero production writers — **verified**: `grep -rn memory_transition_events --include=*.py . | grep -v alembic/versions | grep -v tests | grep -v claude_memory` returns **nothing**; prod count 0. The table is created, migrated to prod, exercised only by tests. This is literally the "writer never existed" defect the 2026-07-07 audit named, recurring inside the repair.
- `memory_quarantine_depth` no production setter, 137 quarantined — **verified** (H3).
- `omnisight-main-sync` green while stale — **verified**: `git -C /home/user/work/sora/OmniSight-Productizer rev-list --count HEAD..gerrit-sora/develop` = **1071**; unit `status=0/SUCCESS`; `journalctl --user -u omnisight-main-sync.service --since 2026-07-01 | grep -o '"event":"[a-z_]*"' | sort | uniq -c` → **240 `sync_skipped_dirty` and nothing else** — zero successful syncs in the retained journal. `scripts/sync_omnisight_main.sh:202-208` runs the dirty check *before* the fetch, calls `_reset_failure`, then `exit 0`, so the skip path actively clears the counter that drives escalation. OP-2739 fixed this exact defect on the sora-bridge sibling and not here.
- `/api/v1/learned-items/block` tenant unvalidated — **verified**, see 3.5.
- Fleet-not-idle correction — **verified** (H7).
- Not re-verified by me: the `published_at` NULL finding (§5) and the staging row counts.

**3.5 — Read-scoping gap nobody owned (`MEDIUM`, security).** `EVIDENCE (this pass)`, `backend/routers/learned_items.py`:
```python
async def learned_items_block(
    tenant: str | None = None, context: str = "", ticket: str | None = None,
    user: _au.User = Depends(_au.current_user),
) -> dict[str, Any]:
    block, result = get_learned_items_block(tenant_id=(tenant or None), ...)
```
`backend/learned_item_loader.py:177-186` then builds `scope_keys = ["global:-", f"tenant:{tenant_id}"]` straight from that caller-supplied value. There is no membership check against `user`. The runner calls it as `?tenant=omnisight-self` (`auto-runner-jira.py:1094`). **Not exploitable today** — the leg-2 store is empty and the read flag is off — but it becomes live the moment leg-2 is activated, i.e. exactly when H2 is remediated. The kernel dimension audited write/side-effect authorization; read scoping on the memory-serving endpoints was audited by nobody.

**6-9. Four dimensions produced no findings that reached synthesis.** I do not know whether they found nothing, found nothing that survived verification, or were truncated in transit. Treated as uncovered in §6.

---

## 4. WHAT IS ACTUALLY HEALTHY

This matters: the 2026-07-07 audit found 3 of 4 data sources delivering nothing. That is no longer true, and the improvement is real and provable.

1. **The structural axis works, including under stress.** `EVIDENCE (auditor)`: 5/5 served axes non-empty, zero degraded, all latencies ≤0.6 s, zero timeout warnings across 9 days of logs (and WARNINGs demonstrably propagate — other warnings are present in the same logs). Stub-driven experiments show it survives a 5-second JIRA backend intact.
2. **The BM25 lessons half is genuinely rewired and genuinely fast (OP-2556).** `EVIDENCE (auditor)`: `_b10_fallback` returns the three correct lessons for a real query in ~3 ms warm; the fallback logic is not decorative.
3. **The causal axis repair (OP-1449/1450/1452/1454) is real.** `EVIDENCE (auditor)`: `default_incident_source()` resolves to `PostgresIncidentSource`; the SQL is index-served and returns 33 rows over a wider window; historical replay yields 1-2 genuine `same_failure_class` neighbours per ticket; Prometheus recorded 11 non-empty causal responses out of ~28 in the five days before the current backend process started.
4. **The durable incident writer (OP-2537) is alive and correctly correlated with failures.** `EVIDENCE (auditor)`: 1,973 rows; 2026-07-20 shows 3 failure-outcome `runner_metrics` → 3 `live-v1-` incidents matching to the second; 07-12 → 8 failures/11 incidents; 07-10 → 10/14. The writer worked correctly at its last opportunity.
5. **The temporal axis is honest, not fake.** It returns `{"status":"unavailable"}`, is classified `content="unavailable"` in metrics (not "empty"), and the runner strips it from the prompt (`auto-runner-jira.py:1176-1178`). Deliberately scoped in the Phase R design doc. This is exactly the right behaviour for an unbuilt component, and it is the single clearest improvement over the 2026-07-07 posture.
6. **Failure isolation between axes works.** Positive controls: with all three fetchers raising, and again with all three hanging past budget, `ProjectStateAllAxesFailed` raises correctly. The budget/fence machinery is functional.
7. **The runner fleet itself is healthy.** Six ephemeral loops alive since Jul 03, self-deploying per cycle, exiting rc=0, ~40 s cadence, ~94k-101k cycles each. The last two real pickups succeeded and pushed Gerrit changes.
8. **Cognee lesson recall degrades correctly in every environment that actually calls it.** The reported exception-tuple gap is real but gated upstream three ways on the runner (flag unset, `is_productizer_self`, cognee not installed on `/usr/bin/python3`) and unreachable in the container where it would fire. Forced ON on the runner's real interpreter, it returns the correct three lessons via `CogneeNotInstalled` → `_b10_fallback`.
9. **Leg-3 ingest largely succeeded.** 177 of 181 memories are mirrored with matching slugs — the mirror is stale and incomplete, not absent.

---

## 5. EXPECTATION GAPS

| Promised / documented | Actual | Evidence |
|---|---|---|
| Leg-2 "code-complete + staging-green" through injection and citations (MEMORY.md) | Proven only through publication; injection→citation→utility→revocation has zero rows in prod **and** staging | H2 |
| OP-2563 "Deploy Alertmanager + wire alerting so anti-hollow alerts page" | No Alertmanager container ever created; running Prometheus config predates the commit; receivers are self-described blackholes | H1 |
| OP-2544 daily incident-write canary escalates a JIRA ticket | Never installed, never executed once; its log directory does not exist | H6 |
| `ProjectStateNoTraffic` fires when the aggregator stops receiving traffic | Fires unconditionally on a metric with no producer; cannot detect the real condition even if fixed | H5 |
| Leg-3 drift gauge goes non-zero when file store and mirror diverge ("γ-2 drift-exclusion + reconcile PROVEN") | No code writes the gauge; 9 days of measurable divergence reported as 0 | H3 |
| `u6_memory_scheduler.py:60-66` — "a flag-ON-but-hollow scheduler is VISIBLE" | Visible and unwatched: no rule, no gate, no dashboard consumes it | H4 |
| Append-only lifecycle ledger for learned-item state changes | `memory_transition_events` has zero production writers; only tests touch it | §3.5 |
| Publication ledger records when a card became live | `insert_publication_event(..., published_at=None)` at `backend/learned_item_publisher.py:100-152`; all three call sites (`:400,:414,:484`) omit it; staging `max(published_at)` over 8 rows = NULL; `trg_memory_publications_no_update`/`no_delete` make it uncorrectable in place. **UNVERIFIED by me** — I did not re-read staging | critic, unverified |
| "Result label logged on EVERY pickup (audit #6: hollow serving must be visible from runner logs)" | The flag-off state — the only state that occurs — logs nothing | H2 |
| "merged-to-develop == live for runner code" | True for the ephemeral Python clone; **false for the wrapper layer**, whose tree is 1,071 commits behind with a green sync unit. Bytes happen to match today, so nothing is actively broken — but any wrapper-level change (e.g. exporting the leg-2 injection flag, the fix for H2) would be merged-but-dead | §3.5 |
| Causal payload contract: up to 3 cross-ticket neighbours | Truncates to 3 edges *before* filtering same-ticket edges (`project_state_aggregator.py:712-716`). Real cost measured at ~5-10% (one neighbour lost in ~12% of tickets on dense data), **not** the 33-67% originally claimed — the original counterfactual counted list slots, not distinct neighbours, and was strictly *worse* on 2 of 4 sample tickets | LOW |
| `ProjectStateAllAxesFailed` makes total failure legible | Structurally unreachable: `_temporal_graphiti` is a synchronous constant-return, so `results['temporal'].payload` is never None. Verified with positive controls including a 1.5 s event-loop block. The router's `except` handler at `backend/api/project_state.py:180` is dead code | LOW |
| Axis metrics distinguish healthy from hollow | `content="empty"` conflates "honestly empty window" with "incident source unavailable" (`project_state_aggregator.py:663-669,727-735`). The logs distinguish them; the metric does not — the only silent-hollow residue inside the axis layer itself | LOW |
| Per-axis budget contract (0.6 s inner / 0.8 s axis / 2 s total) | A loop-blocking half overruns to 1,320 ms while reporting `err=None`, with no telemetry. Currently unreachable in prod (`backend/main.py:1723-1724` eagerly imports the JIRA adapter at startup) but a silent contract violation by construction | LOW |

**Two additional axis-layer defects surfaced during verification that no original finding named:**
- **Fabricated causality labels on depth-2 edges.** `project_state_aggregator.py:713`: `other_id = e.dst_id if e.src_id == inc.incident_id else e.src_id`. For a depth-2 BFS edge, *neither* endpoint is `inc.incident_id`, so it silently returns an arbitrary endpoint of an edge the queried ticket is not part of — and attaches a `causality` label describing a relationship between two *other* incidents. Observed: OP-1886's counterfactual neighbour `c0a7eff3` carried `causality='same_tic
ket'` which was actually the same-ticket edge between two OP-2663 incidents. `INFERENCE`: this is content that is *wrong*, not merely missing — worse than an empty axis, for a system whose purpose is causal explanation. The `[:3]` slice currently masks it. **Contradiction flagged:** the truncate-before-filter finding proposes removing that slice, which would *surface* the garbage. Do not fix them in that order.
- **Duplicate neighbours.** Prod emits duplicate incidents in ~13% of causal results (1/13 July, 8/60 May). Deduping is a higher-value change than the slice reordering.

---

## 6. WHAT COULD NOT BE VERIFIED

- **The kernel invariant itself.** "Persistent memory can NEVER authorize a side effect; enforced in code, not prompt" — no verified findings on this reached me, and I did not test it in this pass. **I cannot state that the invariant holds.** Absence of delivered findings is not evidence.
- **Four of nine dimensions produced nothing that reached synthesis.** Unknown whether they found nothing or were truncated.
- **Staging.** All my DB verification was against prod. The leg-2 staging counts (`learned_item_versions=9`, `memory_publications=8`, `published_at` all NULL) are auditor-reported and unverified by me.
- **The leg-3 content-hash mismatch** on `project_u6_kernel_handoff_and_roadmap` is auditor-reported; I verified slug-set and mtime divergence myself but did not re-hash bodies.
- **Whether the Grafana dashboards are actually loaded.** No provisioning bind mount, `/api/search` returns 401, no authentication attempted.
- **Prometheus retention is 15 days** (and its process only started 2026-07-09), so no metric-side claim extends before 2026-07-17.
- **Root's crontab** — no passwordless sudo, so a root-cron invocation of the incident canary cannot be 100% excluded (implausible: it is a `--user` oneshot writing to a nonexistent user-owned directory).
- **Write paths were never exercised** — read-only constraint. The incident writer is confirmed alive by its output rows, not by re-running it.
- **Production sample size is near zero.** ~3-5 project-state requests in 9 days, zero pickups since 2026-07-22. Most prod-side conclusions rest on mechanism reproduction rather than production volume. **If the fleet resumes, several LOW findings should be re-measured under load before being closed.**
- **`memory_enabled=0` / `memory_liveness_heartbeat=0` on both replicas while U6 flags are reportedly ON** — I observed the gauges but did not trace which producer owns them or why they read 0. Possible additional wiring gap, unresolved.
- **Whether any out-of-repo script invokes lesson recall inside the backend container** — only in-repo callers were enumerated.

**Operational security note, not a finding but requiring action:** during verification of the structural-axis finding, an auditor's redaction failed while grepping `/home/user/.config/omnisight/runner.env` and the value of `OMNISIGHT_RUNNER_API_TOKEN` was printed into an audit transcript. **That token should be rotated.** It is the shared bearer used by all six runners and, per §3.5, is sufficient to read any tenant's learned-item cards once leg-2 is activated. No credential value appears anywhere in this report.

---

## 7. RANKED REMEDIATION ORDER

Sequence only — no fixes prescribed. The ordering principle: **restore the ability to detect before repairing the thing being detected**, because this project's entire failure history is repairs that could not be observed.

1. **Rotate `OMNISIGHT_RUNNER_API_TOKEN`.** Independent of everything else, time-sensitive, cheap. Do it first.
2. **Restore an alert delivery path (H1).** Nothing below this is verifiable while it is broken. Two distinct problems: the container is running a stale config inode (a restart/redeploy question — note the single-file bind mount is the mechanism, and it will recur), and there is no Alertmanager and no real receiver. Until an alert can reach a human, every other fix is unfalsifiable. Add a delivery-path self-test as part of this, not as a follow-up.
3. **Fix the sync-reporter honesty defect (§3.5, `omnisight-main-sync`).** Second, because it is the mechanism by which fix #4 and beyond could be merged-but-dead. A green unit that has never once succeeded, and which resets its own escalation counter on the skip path, will silently swallow the wrapper-level change that H2's remediation requires. OP-2739's fix already exists on the sibling; only the transplant is missing.
4. **Delete or correct `ProjectStateNoTraffic` and its tests (H5).** Small, and it must precede any trust in the alert group. Note the test suite *requires* the broken behaviour, so the tests change with the rule. Also apply the same skepticism to the phantom Grafana panel.
5. **Give leg-1 and leg-3 alert rules at all (H4/H3).** They have none. The gauges already exist and already carry the right signal — `l2_last_summary_age_seconds` at 9.96 days rising, `l3_fact_count{promoted}=1` — so this is rule-authoring, not instrumentation. Highest detection value per unit of effort in the entire list. Include the axis `content="empty"`/`unavailable` conflation while here.
6. **Make the leg-3 drift gauge and quarantine depth real (H3).** Only after #5, so a non-zero reading has somewhere to go. Then reconcile the 4 missing memories and the 9-day staleness — and treat *that* reconciliation as the first proof the new detector works. Do not cut Claude's serving over to the tables until the detector has demonstrated a true positive and a true negative.
7. **Decide leg-2's status explicitly, then act (H2).** This is a decision, not a bug: either activate injection (set the flag, in the right file, on a tree that actually syncs) or mark the loop as "publication-complete, injection deferred" in MEMORY.md and the tickets, and stop counting it as green. Do not activate without first closing the read-scoping gap in §3.5 — activation is precisely what makes that gap exploitable. Whichever path, fix the mute flag-off path first; it costs nothing and it is the reason the state was invisible for weeks.
8. **Restore runner pickup fuel (H7).** 99.56% "no candidate" starves the entire system of the traffic that makes every other signal meaningful. Not a memory defect, but nothing in this report can be re-measured under realistic load until it is resolved. It also self-heals the causal axis's current emptiness.
9. **Install (and de-noise) the incident-write canary (H6).** After #8, because its predicate fires on active-fleet-with-no-failures — running it against an idle fleet teaches nothing, and running it against a healthy fleet produces false positives. Consider the broader pattern: 20 of 48 repo timers are uninstalled.
10. **Wire a writer for `memory_transition_events`, or drop the table (§3.5).** A migrated, prod-deployed, test-only table is the exact artifact the 2026-07-07 audit named. Either state is defensible; the current state is not.
11. **Add a compensating event type for `published_at` (§5, unverified).** Verify the staging observation first. The ledger's immutability triggers mean this cannot be backfilled, so it needs design, not a patch.
12. **Causal-axis correctness cleanup, in this order:** (a) fix the depth-2 `other_id` mis-attribution at `project_state_aggregator.py:713` — it fabricates causal claims; (b) dedupe neighbours (~13% of results); (c) *only then* reorder filter-before-truncate. Doing (c) first would surface the garbage that (a) currently produces and (b) currently masks.
13. **Low-priority axis hardening:** warm the JIRA adapter next to `warm_lessons_index()` (or run the lessons half first) so the always-available half can never be lost to the flaky one; add telemetry for budget overruns that currently report `err=None`; widen the Cognee exception tuple as latent hardening before anyone installs cognee on the runner host; either remove `OMNISIGHT_COGNEE_RECALL=true` from the backend container (nothing there consumes it) or wire a consumer; remove the dead `_LAST_AGENT_FEATURE_FLAGS` write at `auto-runner-jira.py:1663`; move runner logs out of `/tmp` into retention scope.
14. **Re-audit the kernel invariant explicitly (§6).** It is the architectural north star — "memory NEVER authorizes a side effect, enforced in code" — and this audit produced no evidence either way. It should not stay unverified.