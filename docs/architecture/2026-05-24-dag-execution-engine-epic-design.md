# DAG Execution Engine — EPIC design (for codex review)

**Status**: DRAFT for codex adversarial review (2026-05-24). No code yet.
**Origin**: 3-way audit (me + sub-agent + codex, all verdict TRUE) found the
firmware-DAG executor (path a) is unimplemented. Audit + memory:
`project_dag_execution_engine_unimplemented`, codex audit
`docs/audit/codex-reviews/dag-execution-engine-audit-codex-2026-05-24.txt`.
**Decision to scope this** (operator, 2026-05-24): treat the firmware-DAG as a
committed capability and produce the full EPIC design + 1-3wk scope.

## 1. Problem & framing
There are TWO independent "DAG" paths in the codebase:
- **(a) DAG-plan path** — `POST /api/v1/dag` → `workflow.start(dag=…)` validates +
  persists to `dag_plans` + marks `executing`, **then stops**. No executor runs the
  tasks (compile/flash/cross-compile/package); nothing sets `completed` or calls
  `workflow.finish()` from an executor. This EPIC builds path (a).
- **(b) Orchestration path** — JIRA webhook → `orchestrator_gateway.build_catcs_from_dag`
  → `queue_backend` → `worker.py` → `run_graph` → Gerrit. **Already works**
  (the live code-authoring pipeline). NOT touched by this EPIC, but its sandbox
  runtime + queue/claim patterns are the reuse substrate.

**Goal**: a `/api/v1/dag` submission of `smoke-compile-flash-host-native` (and, in
later phases, cross-compile/package/remote-board DAGs) **executes its tasks to
`completed`**, records `workflow_steps`, and greens `staging-gate-smoke` honestly.

**Non-goals**: replacing path (b); building a new UI; multi-tenant canary (Boreas-B
Gap B, deferred). The smoke gate is NOT a live promote blocker today (canary is the
operative gate; `staging-gate-smoke.timer` is `not-found`), so this is a
capability + fidelity build, not a hotfix.

## 2. Reusable substrate (confirmed by audit — do NOT rebuild)
| Piece | File | Use |
|---|---|---|
| `dag_schema.DAG`/`Task` | backend/dag_schema.py:26 | task = `{task_id, required_tier(t1/networked/t3), toolchain, inputs, expected_output, depends_on}` |
| `dag_validator` | backend/dag_validator.py | already gates submit (cycles/MECE/tier) |
| `dag_storage` state machine | backend/dag_storage.py:54 | `executing→completed` already LEGAL; `set_status` has FOR UPDATE row-lock; lookups by id/run_id |
| `workflow.step(run, idempotency_key)` | backend/workflow.py:538 | durable append-only checkpoint; `(run_id, idempotency_key)` UNIQUE → idempotent replay; `task_id` is part of the key |
| `workflow.finish(run_id, status)` | backend/workflow.py:340 | terminal run transition |
| `container.dispatch_t3 / start_container / exec_in_container` | backend/container.py:930/585/986 | sandbox exec primitives |
| `t3_resolver.resolve_t3_runner` | backend/t3_resolver.py:109 | LOCAL vs BUNDLE/SSH decision (host_native → LOCAL) |
| `ssh_runner.run_on_target` | backend/ssh_runner.py | real remote-board exec (t3 BUNDLE) |
| `sandbox_prewarm` | backend/sandbox_prewarm.py | topo-orders + pre-warms in-degree-0 tasks (reuse the topo walk) |
| `SandboxRuntime` protocol (`LocalSandboxRuntime`) | backend/worker.py:205 | prepare/run sandbox; path-b's executor substrate |

## 3. Target architecture
A **DAG executor** that turns an `executing` plan into a completed run:

```
[plan claimer] → [topo scheduler] → [task dispatch] → [handler] → [step record] → [terminal wiring]
  poll/claim       ready set          by toolchain     compile/    workflow.step    set_status(completed)
  executing        honor depends_on   + required_tier  flash/...   (idempotent)     + workflow.finish
```

### Components (each ≈ a child Story)
- **E1 — Plan claimer / executor loop.** Poll `dag_plans WHERE status='executing'`,
  claim with a lease so multi-replica backends/workers don't double-run. Reuse the
  OP-977 fencing-token pattern (`claim:{instance}:{epoch}-{uuid}`, lowest-token-wins)
  or `SELECT … FOR UPDATE SKIP LOCKED`. Lease renew + reclaim on crash.
- **E2 — Topological scheduler.** Build the ready-set from `depends_on` (reuse
  `sandbox_prewarm`'s indegree walk), run ready tasks (bounded concurrency within a
  plan), advance as deps complete. Fail-fast vs continue-on-error policy.
- **E3 — Task-type → handler dispatch.** Registry keyed on `toolchain` × `required_tier`.
  t1 = local sandbox container; networked = sandbox + egress; t3 = `t3_resolver` →
  LOCAL (host-native, same arch/OS) or BUNDLE/SSH (`ssh_runner`). **CLAUDE.md rule:
  cross-compile MUST use the platform toolchain from `get_platform_config` +
  `-DCMAKE_TOOLCHAIN_FILE` + `--sysroot` — never system gcc.**
- **E4 — Task handlers.** `compile` (cmake/make/host gcc; cross via platform
  toolchain), `flash` (host_native LOCAL = symbolic/no-op-to-loopback; real device =
  `ssh_runner`/hardware bridge), `package`, `cross-compile`. Each runs the task's
  `toolchain` command in a workspace, captures rc/stdout/artifact at `expected_output`.
- **E5 — Step recording + artifact check.** One `workflow.step` per task (idempotency
  key includes `task_id`); validate `expected_output` produced; link
  `workflow_steps.dag_task_id`.
- **E6 — Terminal wiring.** All tasks ok → `set_status(plan,'completed')` +
  `workflow.finish(run,'completed')`. Any fail → `failed` + finish `failed`/`halted`
  (+ optional Phase 56-DAG-C mutation hook).
- **E7 — Failure / retry / crash-resume.** Idempotent re-claim skips done steps (via
  `workflow.step` UNIQUE); per-task retry policy; poison-plan handling.
- **E8 — Side-effect isolation.** Dev/staging MUST NOT touch prod artifacts/registry/
  Gerrit/real hardware. Env-gated handler "test mode" (symbolic flash, scratch
  workspace, no push). Ties to A2 env-contract.
- **E9 — Deployment.** Decide: in-process lifespan task gated by
  `OMNISIGHT_DAG_EXECUTOR_ENABLED` vs a separate worker container/systemd unit
  (mirror `omnisight-worker@`). Wire onto staging; re-green `staging-gate-smoke`;
  re-enable the timer. 4-AC + Go-Live.
- **E10 — Observability + honesty pass.** Executor heartbeat/metrics; fix misleading
  surfaces (`TODO.md:131` "2 real DAGs end-to-end"; any `/api/v1/dag`-as-executable
  product copy).

## 4. Phasing & effort (3-way consensus)
- **Phase 1 — Smoke MVP (~2-5 person-days, MEDIUM risk).** E1(single-claim) + E2 +
  E3/E4 for host-native `compile`+`flash` only (LocalSandboxRuntime, symbolic flash)
  + E5 + E6 happy-path + E8 test-mode. Outcome: `smoke-compile-flash-host-native`
  reaches `completed`; smoke gate greens for real on staging.
- **Phase 2 — Tier expansion (~1 week).** cross-compile (platform toolchain rule) +
  package + t3 BUNDLE/SSH remote-board via `ssh_runner`; the second smoke DAG
  (`smoke-cross-compile-aarch64`).
- **Phase 3 — Production hardening (~1 week).** Multi-replica claim/fencing (E1 full),
  retry/crash-resume (E7), failure semantics, full side-effect isolation, metrics.
- **Total: ~1-3 weeks** depending on how far past MVP.

## 5. Open questions for codex
1. **In-process vs separate worker (E9).** A lifespan background task in the staging
   backend is simplest but couples executor lifecycle to the API process + multiplies
   across replicas (a/b) → needs claim even for MVP. A separate worker
   (mirror `omnisight-worker@`) is cleaner isolation but more infra. Which for MVP?
2. **Should path (a) and path (b) converge?** Both use `dag_schema.DAG` + a sandbox
   runtime. Is building a *second* executor the right call, or should path-a DAGs be
   lowered into path-b TaskCards (reuse `worker.py` + queue) so there's ONE execution
   substrate? Trade-off: reuse vs the two having different semantics (dag_plans
   lifecycle + workflow_steps vs CATC queue + Gerrit push).
3. **What does host-native `flash` even mean (E4)?** On a server there's no device.
   Is symbolic/no-op flash an honest "completed", or does that make the smoke gate
   test a hollow path? Should the smoke DAG be redefined to something with real
   host-native semantics (compile + run-tests) instead of compile+flash?
4. **Claim mechanism for MVP (E1).** `FOR UPDATE SKIP LOCKED` (DB-native, simple) vs
   the OP-977 fencing-token pattern (already in-codebase for runners). Given staging
   has backend-a + backend-b, even MVP needs *some* claim — which?
5. **Side-effect isolation (E8).** Is an env flag (`OMNISIGHT_DAG_EXECUTOR_TEST_MODE`)
   enough, or must handlers hard-refuse prod artifact/registry/Gerrit endpoints when
   `OMNISIGHT_ENV != prod` (defense-in-depth like the A2 contract)?
6. **Is this worth building at all** given the runner-as-product pivot — i.e. is the
   firmware compile/flash capability still strategically wanted, or should Phase 1
   stop at "smoke greens" and the rest stay parked? (Operator already chose to scope
   it; codex: sanity-check whether the EPIC over-builds vs the actual product need.)
7. **MECE / artifact collisions** on a shared staging workspace across concurrent
   plans (E2/E8) — does `expected_output` + `output_overlap_ack` suffice, or is
   per-plan workspace isolation required from day 1?

---

## 6. codex review round 1 (2026-05-24) — folded in
**Verdict: SOUND-WITH-CHANGES.** Audit: `docs/audit/codex-reviews/dag-engine-epic-design-codex-2026-05-24.txt`. Corrections, all to apply before/within implementation:

### Three data-model blockers (must fix first — were not in the original draft)
- **B1 — `executing → failed` is currently ILLEGAL.** The `dag_storage` state machine
  only allows `executing → {completed, mutated, exhausted}` (dag_storage.py:54-58). E6
  must first ADD an `executing → failed` transition (matches `workflow.finish(...,"failed")`).
- **B2 — step attribution gap.** `workflow_steps.dag_task_id` column exists (db.py:271)
  but `_record_step()` doesn't write it (workflow.py:512). E5 must extend `workflow.step()`
  to persist `dag_task_id` — "link dag_task_id" is NOT achievable as-is.
- **B3 — claim columns don't exist.** MVP needs `claim_owner/claim_token/claim_expires_at/
  heartbeat_at` on `dag_plans` (or a `dag_plan_claims` table) with compare-and-set. The
  existing runner-claim code (runner_coordination.py) is sync-SQLite + shadow-era — copy
  the *pattern*, not the module.

### Answers to the 7 open questions (resolved)
1. **Separate `omnisight-dag-executor` process/systemd unit, NOT FastAPI lifespan** — a
   lifespan task multiplies across backend-a/b on staging; a separate process gives clean
   lifecycle/restart/logs/kill-switch. (E9 decided.)
2. **Do NOT converge path-a into path-b.** `build_catcs_from_dag` + `worker.py` always run
   agent work + commit + push to Gerrit (orchestrator_gateway.py:370, worker.py:1006-1052);
   it does not aggregate deps, record `workflow_steps`, or call `finish()`. Lifecycles are
   incompatible. Build a path-a executor; reuse only LOW-level primitives (LocalSandboxRuntime
   workspace isolation, queue/lease patterns) — never `Worker.handle()` as-is.
3. **Symbolic host-native `flash` is a HOLLOW green** — docs/operations/sandbox.md:320,325
   explicitly: "You can't pretend to flash a board over localhost." **Redefine smoke DAG1 to
   `compile → run-artifact-check` / `compile → run-self-test`** (step 2 actually consumes
   `build/firmware.bin`, writes `logs/test.log`). This is the honest-slice fix. (E4/E10.)
4. **MVP REQUIRES a claim** — `FOR UPDATE SKIP LOCKED` alone is insufficient unless the row
   is leased-before-commit; use compare-and-set lease w/ expiry (B3). backend-a+b → real
   double-execution risk without it.
5. **Env flag alone insufficient** — hard fail-closed points (modelled on the A2
   `backend/env_contract.py` guard, :136/190/200) for Gerrit push, registry publish, SSH/
   remote exec, hardware flash, prod-DB writes, host mutation: refuse real side effects
   unless `OMNISIGHT_ENV=prod` AND explicit per-capability allow flags. (E8 hardened.)
6. **Phase 1 worth building** (current smoke claims are misleading). **Phase 2/3 over-build
   for the runner-as-product pivot — PARK real board flashing, SSH remote-board execution,
   package registry, and cross-compile breadth** until a real product user needs them.
7. **`expected_output`/`output_overlap_ack` NOT enough** (schema only validates *declared*
   outputs; real tools write elsewhere) — **per-plan/per-task workspace isolation from day 1**
   (LocalSandboxRuntime already creates fresh workspaces, worker.py:280-304). (E2/E8.)

### Corrected Phase-1 "smallest honest slice"
Separate `dag_executor` process · DB lease claim w/ expiry · **serial** topo execution first
(concurrency deferred) · handlers for `cmake`/`make`/`python3` only, in isolated scratch
workspaces · smoke DAG = `compile → verify/run-test` (NOT symbolic flash) · one `workflow.step`
per task with `dag_task_id` · terminal wiring `dag_plans.completed/failed` + `workflow.finish()`
· side effects fail-closed outside prod. This proves submit→claim→dep-order→sandbox-exec→
artifact→step-record→terminal — a *real* green.

### Corrected effort (codex; my 2-5d MVP was optimistic)
- **Honest MVP: 4-7 person-days**, medium risk (the +days = claim schema B3 + step-attr B2 +
  state-machine B1 + honest-smoke redesign).
- **Full cross-compile/package/SSH path: 2-4 weeks**, medium-high risk.
- **Real hardware flash: PARKED** unless a current product requirement exists.

---

## 7. Expanded 3-way impact audit + deep plan (2026-05-24)
Operator-requested 2nd 3-way audit (me + sub-agent + codex) across 7 system dimensions.
Audits: `docs/audit/codex-reviews/dag-epic-impact-audit-codex-2026-05-24.txt` (+ two sub-agent
runs). All three converge; codex rates a notch more conservative but the substance is identical.

### Per-dimension consensus (impact + the specific risk)
| # | Dimension | Impact | Key finding / constraint |
|---|---|---|---|
| 1 | Runner capability/claim | **LOW** | Runner mutex is JIRA-label fencing (`claim_ticket_atomic`) + a shadow-only sync-SQLite `runner_claims` table — **different DB/table/key** from a Postgres `dag_plans` lease. No collision if executor uses its own table + distinct `OMNISIGHT_INSTANCE_ID` namespace (e.g. `dag-exec-*`). Executor is NOT a runner: no capability, no `agent:auto`, no JQL change. |
| 2 | Deployment line | **MED–HIGH** | Ship executor **inside the backend image as an alt entrypoint** (`python -m backend.dag_executor`), like `bridge-daemon` reuses the image with a different `command:` → same `OMNISIGHT_IMAGE_TAG`/digest, no new image/registry/cosign. Clone `omnisight-worker@.service`. backend-a/b double-run = the concrete reason it must be a **separate single/claim-gated process, NOT a FastAPI lifespan task**. Adds a managed daemon to cold-start inventory → needs a `deployed:` AC (AUDIT-23 prevention). |
| 3 | Workflow data-model | **MED** | B1 (`executing→failed`) additive — `mutate_workflow` (`executing→mutated`) untouched. B2 (`dag_task_id`) column already exists (db.py:943/0016), `_record_step` just doesn't write it — additive, no migration. B3 (lease columns) = **new alembic after 0247** + dup-revision pre-flight. **Side-effect**: completing DAG runs newly fire `workflow.finish` billing counters + skills/finetune hooks (workflow.py:406); finetune gates on `hvt_passed` so likely safe, but **mark executor runs + suppress from the export/skills lane** to be sure. |
| 4 | Current work path | **LOW** | `/api/v1/dag → dag_plans` is exercised ONLY by tests + `prod_smoke_test.py` (operator-gated `require_operator`); live work = path-b (JIRA→runner→Gerrit), disjoint. Isolated new lane. **Sharpening (codex): executor must claim ONLY explicit opt-in/allowlisted plans** (metadata flag), so existing inert smoke/bootstrap/API submissions don't suddenly start executing. |
| 5 | Release flow | **MED–HIGH** | `smoke_suite` is a **hard block reason** in `release_milestone_checker:515` and the unattended **`release:force-promote` label is RETIRED** (`LabelInvalid`, :404) → human break-glass only. A flaky executor → RED smoke → `milestone_blocked` with NO label escape. **Hard constraint: keep `staging-gate-smoke.timer` DISABLED until the executor has a soak record; enabling it is a SEPARATE operator-gated step, NEVER bundled into the executor-ship PR.** Canary stays HTTP-only + operative. |
| 6 | External links | **LOW (honest MVP) / HIGH (full)** | Honest MVP (compile→run-test, local cmake/make/python3, scratch workspace) touches **no** external system. Staging already holds Gerrit SSH creds (`staging_gerrit_ssh_key`) — exactly why handlers must **fail-closed** (model on `env_contract.py:136/190`) on Gerrit push / registry publish / SSH / hardware flash / prod-DB write unless `OMNISIGHT_ENV=prod` + explicit per-capability allow. Those touchpoints are all Phase 2/3 → parked. |
| 7 | Agent memory | **LOW** | No runtime coupling (executor doesn't use `/var/omnisight/memory`/cognee). Docs-staleness only: after landing, update `project_dag_execution_engine_unimplemented` (→ MVP shipped), `reference_cold_start_safety_inventory` (+1 daemon), `project_staging_gate_gap` (smoke now real), `TODO.md:131`. |

### Deep plan — de-risked build order (3-way consensus)
**Phase 0 — data model only, disabled, ZERO behavior change (SAFE to land first):**
B1 `executing→failed` + B2 `_record_step` writes `dag_task_id` + B3 new alembic (lease columns
on `dag_plans`) + dup-revision pre-flight. Invisible to runner/release/work-path/external — makes
the data model executor-ready with no live effect. **This is the smallest safe landing.**

**Phase 1 — honest MVP (executor disabled-by-default, opt-in plans only):**
separate `omnisight-dag-executor` (clone `omnisight-worker@`, backend-image alt entrypoint,
disabled by default) · DB compare-and-set lease w/ expiry · **serial** topo · handlers
`cmake/make/python3` only in per-plan `LocalSandboxRuntime` scratch workspaces · smoke DAG redefined
`compile → run-test` (NOT symbolic flash) · `workflow.step` per task w/ `dag_task_id` · terminal
`completed/failed` + `workflow.finish` · **claim only allowlisted/opt-in plans** · **fail-closed**
side effects · suppress executor runs from finetune/skills export. Enable in **dev first**, then
staging manually; collect red/green records with NO promotion automation.

**Operator-gated (NEVER bundled into the ship PR):**
- Flipping `staging-gate-smoke.timer` to a real R3 producer — only after a soak record (Dim 5).
- Any handler touching Gerrit/registry/SSH/hardware (Phase 2/3).

**DON'T BUILD YET (parked):** real board flash, SSH remote-board exec, package-registry publish,
cross-compile breadth — the MED-HIGH external-blast items the runner-as-product pivot doesn't need.

**6 planning constraints (must-hold):** (1) separate process, not lifespan; (2) reuse backend
image + alt entrypoint, no new image/cosign; (3) executor instance-id namespace distinct from
runners; (4) smoke-timer flip is a separate operator-gated post-soak step; (5) suppress executor
`completed` runs from finetune/skills/billing-sensitive lanes; (6) `deployed:` AC on the executor
unit.
