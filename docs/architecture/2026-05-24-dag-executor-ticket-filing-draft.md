# DAG Executor — ticket-filing DRAFT v2 (codex-revised; pre runner-blind-test)

**Status**: DRAFT v2 (2026-05-24, codex audit folded in). NOT filed yet. Pipeline:
~~codex audit~~ ✅ → **runner blind-test dry-run (goal-drift check)** → revise → file → execute.
**Source**: `docs/architecture/2026-05-24-dag-execution-engine-epic-design.md` (§7 plan).
**codex audit**: `docs/audit/codex-reviews/dag-ticket-filing-draft-codex-2026-05-24.txt` (verdict
NEEDS-REVISION → all fixes applied below).

## Conventions + filing mechanics (confirmed)
- `scripts/file_jira_ticket.py` always files **issuetype Story**; `--type bug/feature/docs/meta`
  is only a label — fine. Required: summary, --description-file, --tier, --class, --type, --areas, --scope.
- 6-tag prefix · 9-value area whitelist · tier:X = human/operator-only (runner JQL excludes it) ·
  Blocks: inwardIssue=blocker → outwardIssue=blocked · scope-anchor = Goal+Files+Spec-ref.
- **Any docs-touching ticket MUST carry `area:docs`.** **`TODO.md` is Tier-B-forbidden for runners**
  → never in an agent:auto ticket's scope.
- **Disabled-component AC rule** (codex Q3): for Phase-1 tickets that ship the executor INERT,
  Deploy/Go-Live = "artifact/config shipped, default OFF, no daemon enabled"; **Exercised = local
  harness/unit/integration evidence ONLY** (not live dev/staging — that's the tier:X enable tickets).
- **agent:auto AC must never require a host migration / systemctl / docker socket / live creds** —
  runners create + test files; they do not act on the host.
- Every ticket carries a **`MUST NOT` boundary** (codex-supplied) to pin scope against goal-drift.

**scope**: `scope:dag-executor` · **class**: `subscription-claude` (code-edit) for agent:auto;
operator for tier:X. **Spec-ref**: design doc §section.

---

## EPIC (META roll-up)
**`[META][dag-executor] DAG execution engine — path-a executor (firmware DAG → completed)`**
- type:meta · tier:X · area:backend · scope:dag-executor · NOT agent:auto.
- Body: path-a unimplemented (3-way audit TRUE) + path-a/b split + design-doc link + phased plan +
  6 planning constraints. All children Block it.

---

## Phase 0 — data-model readiness (SAFE-FIRST, executor absent, zero behavior change)

### P0-A `[OP][dag-executor] dag data-model executor readiness: failed transition + dag_task_id`
*(merged ex-P0-1+P0-2 per codex Q1 — tiny additive edits, shared test surface)*
- type:bug · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- **Goal**: (1) add `failed` to `_ALLOWED_TRANSITIONS["executing"]` (today only completed/mutated/
  exhausted); (2) `_record_step`/`step()` persists the existing-but-unwritten `workflow_steps.dag_task_id`.
  **Files**: `backend/dag_storage.py:54-62`, `backend/workflow.py:488-535`; column already at
  `backend/db.py:274` (schema-sync) + `0016_pg_schema_sync.py` (NOT in the CREATE-TABLE DDL at
  db.py:943 — relies on schema-sync, no DDL edit); tests `backend/tests/test_dag_storage.py`
  (transition) + `backend/tests/test_workflow.py` (dag_task_id round-trip). **Spec**: §7 B1+B2.
- AC — **Code**: both additive, back-compat. **Deploy**: ships in backend image, no migration.
  **Integration**: tests assert new transition + a rejected illegal one + a step round-trips `dag_task_id`.
  **Exercised**: `mutate_workflow` (executing→mutated) + existing no-dag_task_id `step()` callers green.
  **Go-Live**: merged to develop (additive; no activation).
- **MUST NOT**: *refactor dag_storage, workflow step semantics, or existing callers beyond the minimal
  additive failed-transition and nullable dag_task_id plumbing.*

### P0-3 `[OP][dag-executor] alembic: dag_plans executor-lease columns (file only)`
- type:feature · tier:M · area:db,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- **Goal**: new alembic revision (down_revision=`0247`) adding nullable `claim_owner / claim_token /
  claim_expires_at / heartbeat_at` to `dag_plans` (no consumer yet). **Files**: `backend/alembic/versions/`
  (new), `backend/db.py:1299`. **Spec**: §7 B3. ⚠ dup-revision pre-flight ([[feedback_alembic_dup_revision_preflight]]).
- AC — **Code**: additive nullable columns, correct down_revision. **Deploy**: revision FILE shipped;
  release migration runs in the normal deploy (NOT by the runner). **Integration**: local
  `alembic upgrade/downgrade` test + `python3 -W error::UserWarning` dup-check green. **Exercised**:
  local upgrade leaves existing dag_plans rows intact. **Go-Live**: merged; **host migration pending
  normal deploy**.
- **MUST NOT**: *run or require a dev/staging/prod host migration; verification is limited to local
  alembic upgrade/downgrade tests and duplicate-revision checks.*

---

## Phase 1 — honest MVP (executor SHIPPED INERT, default OFF; claims ONLY opt-in plans)

### P1-1 `[OP][dag-executor] executor entrypoint + disabled systemd unit template (inert)`
- type:feature · tier:M · area:backend,devops · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P0-3. **Goal**: `backend/dag_executor.py` (`python -m backend.dag_executor`) async poll-loop
  skeleton, **no-op unless `OMNISIGHT_DAG_EXECUTOR_ENABLED=1`** + `deploy/systemd/omnisight-dag-executor@.service`
  cloned from `omnisight-worker@.service` (backend-image alt entrypoint). Skeleton + heartbeat only —
  NO task execution, NO terminal transitions. Distinct instance-id namespace `dag-exec-*`. Heartbeat:
  reuse `worker._get_default_store()` under a distinct `omnisight:dag-exec:` key namespace (don't
  pollute the worker registry). Unit: relax `Requires=docker.service`→`Wants` (inert skeleton spawns
  no containers). **Files**: `backend/dag_executor.py` (new), `deploy/systemd/omnisight-dag-executor@.service`
  (new), `backend/tests/test_dag_executor_skeleton.py` (new — the Integration-AC unit test). **Spec**: §7 Dim 2.
- AC — **Code**: enable-flag gate; no-op when unset. **Deploy**: unit file shipped, default OFF, NOT enabled.
  **Integration**: unit test — disabled = claims nothing; enabled-in-harness = loop ticks + heartbeats; SIGTERM clean.
  **Exercised**: local harness only (not host). **Go-Live**: artifact shipped inert.
- **MUST NOT**: *execute DAG tasks, mark plans completed/failed, enable any systemd unit, call systemctl,
  or modify live host state; ship only an inert entrypoint and a disabled unit template.*

### P1-2 `[OP][dag-executor] DB compare-and-set lease claim on dag_plans`
- type:feature · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P0-3, P1-1. **Goal**: compare-and-set claim using P0-3 columns (claim/renew/expire-stale/
  release) — copy the OP-977 *pattern*, own `dag_plans`-local namespace. **Files**: `backend/dag_executor.py`,
  `backend/dag_storage.py`. **Spec**: §7 Dim 1 + codex Q4.
- AC — **Code**: atomic claim, lease expiry + heartbeat-renew. **Deploy**: backend image (executor still inert).
  **Integration**: two-claimer test → exactly one wins; stale lease reclaimed. **Exercised**: backend-a/b
  co-tenant sim → no double-grant (unit/integration, local). **Go-Live**: merged inert.
- **MUST NOT**: *reuse or import the runner claim module/table (`runner_coordination`/`runner_claims`);
  implement a dag_plans-local lease namespace only.*

### P1-3 `[OP][dag-executor] serial topological scheduler over dag.tasks`
- type:feature · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-2. **Goal**: claimed plan → topo-walk `dag.tasks` by `depends_on` (reuse
  `sandbox_prewarm` indegree walk), **serial** execution. **Files**: `backend/dag_executor.py`; reuse
  `backend/sandbox_prewarm.py`. **Spec**: §7 codex E2. **Workspace-bridge (pinned, see P1-4): use
  worker.py's module-level `_copytree/_copyfile/_rmtree/_resolve_glob` helpers, NOT `LocalSandboxRuntime.start()`.**
- AC — **Code**: ready-set order correct, cycle-safe. **Deploy**: backend image. **Integration**: 3-task
  diamond runs in dep order (local). **Exercised**: 2-task smoke sequences compile→test (harness).
  **Go-Live**: merged inert.
- **MUST NOT**: *add parallel execution, retry policy, crash resume, or task handlers beyond the
  scheduler-ordering state later tickets need.*

### P1-4 `[OP][dag-executor] local task handlers (cmake/make/python3) in scratch workspace`
- type:feature · tier:L · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-3. **Goal**: toolchain→handler dispatch for `cmake`/`make`/`python3` ONLY, each run in a
  per-plan scratch workspace, capturing rc/stdout + the artifact at `expected_output`. **Workspace-bridge
  (pinned — resolves the blind-test's one real ambiguity)**: build the workspace via worker.py's
  module-level `_copytree/_copyfile/_rmtree/_resolve_glob` + a `workdir_root/{plan_id}-{task_id}`
  convention, materializing from `Task.inputs` — **do NOT call `LocalSandboxRuntime.start()`** (it requires
  a `TaskCard` with a `PROJECT-NUMBER` jira_ticket + git-init, both path-b-specific). `expected_output`
  is **workspace-relative; a path escaping the workspace → task FAIL**. `required_tier`: this slice is
  **t1/local only — `networked`/`t3` → task FAIL** (out of slice). **Workspace lifecycle**: handler
  creates + returns the workspace; the **caller (P1-5) owns cleanup** (so step-recording can read artifacts
  first). The fail-closed side-effect guard is **P1-6's job** — P1-4 only FAILs unknown/nonlocal toolchains.
  **Files**: `backend/dag_executor.py`; reuse worker.py low-level helpers. **Spec**: §7 codex Q7.
  **(HIGH drift risk — boundary below.)**
- AC — **Code**: 3 local handlers; per-plan workspace isolation; unknown/nonlocal toolchain → task FAIL (not crash).
  **Deploy**: backend image. **Integration**: real `cmake` compile + `python3` test step run in scratch +
  produce declared artifact (local; fixtures in a test-local tmp dir, **never `test_assets/`**).
  **Exercised**: smoke `compile→run-test` end-to-end local. **Go-Live**: merged.
- **MUST NOT**: *implement Gerrit push, registry publish, SSH, remote-board execution, hardware flash,
  package upload, or cross-compilation; unknown/nonlocal toolchains must fail the task.*

### P1-5 `[OP][dag-executor] step recording + terminal wiring (completed/failed + finish)`
- type:feature · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P0-A, P1-4. **Goal**: one idempotent `workflow.step` per task (w/ `dag_task_id`); all-ok →
  `set_status(plan,'completed')`+`workflow.finish(run,'completed')`; any fail → `failed`+finish `failed`.
  **Files**: `backend/dag_executor.py`, `backend/workflow.py`. **Spec**: §7 codex E5/E6.
- AC — **Code**: terminal transitions + idempotent re-claim skips done steps. **Deploy**: backend image.
  **Integration**: smoke DAG → `completed` w/ 2 steps; forced fail → `failed` (local). **Exercised**:
  in-harness `prod_smoke_test`-style poll sees `completed`+steps. **Go-Live**: merged inert.
- **MUST NOT**: *add new export, billing, skills, retry, or side-effect behavior; only record per-task
  steps and terminal statuses.*

### P1-6 `[OP][dag-executor] fail-closed side-effect guard for handlers`
- type:feature · tier:M · area:backend,security,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-4. **Goal**: handlers hard-refuse Gerrit/registry/SSH/hardware/prod-DB unless
  `OMNISIGHT_ENV=prod` AND explicit per-capability allow — model on `backend/env_contract.py:136`.
  **Files**: `backend/dag_executor.py`, reuse `backend/env_contract.py`. **Spec**: §7 Dim 6 / codex E8. Blocks P1-9a.
- AC — **Code**: guard fail-closes outside prod. **Deploy**: backend image. **Integration**: handler attempting
  Gerrit/SSH on dev/staging env raises + aborts BEFORE any network call (fake endpoints). **Exercised**:
  env=staging + fake creds → guard blocks (local). **Go-Live**: merged.
- **MUST NOT**: *contact real Gerrit, registry, SSH hosts, hardware, or prod DBs during tests; use
  fake endpoints/env and assert the guard blocks before any network/action call.*

### P1-7 `[OP][dag-executor] suppress executor runs from finetune export`
*(narrowed to finetune_export only per codex — skills/billing was drift-bait)*
- type:bug · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-5. **Goal**: tag executor-completed runs (kind/metadata) so
  `finetune_export.list_runs(status='completed')` skips them. **Files**: `backend/finetune_export.py:296`,
  `backend/dag_executor.py`. **Spec**: §7 Dim 3.
- AC — **Code**: executor runs carry a marker; finetune export filters it. **Deploy**: backend image.
  **Integration**: a completed executor run is excluded from `finetune_export` (local). **Exercised**:
  smoke runs don't enter the finetune corpus. **Go-Live**: merged.
- **MUST NOT**: *change finetune selection semantics for non-executor completed runs.*

### P1-8 `[OP][dag-executor] redefine smoke DAG to compile→run-test (surgical)`
- type:bug · tier:M · area:tests,devops · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-4. **Goal**: replace `smoke-compile-flash-host-native` symbolic flash (hollow,
  sandbox.md:320) with `compile → run-self-test` (step 2 consumes `build/firmware.bin`, writes
  `logs/test.log`). **Files**: the DAG definition in `scripts/prod_smoke_test.py:72-99` + focused tests.
  **Spec**: §7 codex Q3.
- AC — **Code**: smoke DAG = compile→run-test. **Deploy**: ships in scripts. **Integration**: new DAG
  validates + executes locally to completed. **Exercised**: local `staging_gate --suite smoke` dry-run
  green (timer still OFF). **Go-Live**: merged (timer disabled — see G-2).
- **MUST NOT**: *rewrite `prod_smoke_test.py` orchestration, CLI parsing, polling, auth, or subset
  behavior; change only the DAG definition + focused tests for compile→run-test.*

### P1-9a `[OP][dag-executor] dev compose wiring for executor (disabled/opt-in config)`
*(split from ex-P1-9 — repo wiring only, agent:auto)*
- type:feature · tier:M · area:devops,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-5, P1-6, P1-8. **Goal**: add a `dag-executor` service stanza to
  `deploy/dev/docker-compose.yml` (default `OMNISIGHT_DAG_EXECUTOR_ENABLED=0`, opt-in plan allowlist
  config) — repo/config only. **Files**: `deploy/dev/docker-compose.yml`. **Spec**: §7 Phase 1.
- AC — **Code**: compose stanza, default OFF. **Deploy**: config shipped, NOT started. **Integration**:
  `docker compose config` validates the stanza (local, no `up`). **Exercised**: stanza lints; no live
  bring-up. **Go-Live**: config shipped inert.
- **MUST NOT**: *start docker compose, enable systemd, require a docker socket, use staging/prod
  credentials, or verify against a live dev host; repo wiring + local config/tests only.*

### P1-9b `[GATE][dag-executor] operator: start dev executor + verify live completion`
- type:feature · tier:X · area:devops · requires:operator-approval · NOT agent:auto.
- blockedBy: P1-9a. **Goal**: operator enables the executor on the dev stack, submits the smoke DAG,
  verifies `completed` <300s on the isolated dev DB; confirms no prod/runner impact. **Spec**: §7 Phase 1.

### P1-10a `[OP][dag-executor] executor heartbeat/metrics surface`
*(split from ex-P1-10 — backend metrics only)*
- type:feature · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push · class:subscription-claude
- blockedBy: P1-5. **Goal**: executor heartbeat + Prometheus metric surface. **Files**:
  `backend/dag_executor.py`, `backend/metrics.py`. **Spec**: §7 codex E10.
- AC — **Code**: heartbeat metric. **Deploy**: backend image. **Integration**: metric scrapeable in a
  test. **Exercised**: local scrape shows the gauge. **Go-Live**: merged.
- **MUST NOT**: *edit `TODO.md` or product copy; only add executor heartbeat/metric surfaces + tests.*

### P1-10b `[OP][dag-executor] honesty pass: correct misleading DAG-execution docs`
*(Claude/human-owned — touches TODO.md which is Tier-B-forbidden for runners)*
- type:docs · tier:X · area:docs · NOT agent:auto.
- blockedBy: P1-5. **Goal**: correct `TODO.md:131` ("2 real DAGs end-to-end") + any product copy
  treating `/api/v1/dag` as executable, reflecting the MVP reality. **Spec**: §7 Dim 7.
- **MUST NOT**: *change executor code; documentation only.*

---

## Operator-gated (tier:X — human-only, NOT agent:auto, NOT bundled into ship PRs)

### G-1 `[GATE][dag-executor] enable staging executor + collect soak records`
- type:feature · tier:X · area:devops · requires:operator-approval · NOT agent:auto. blockedBy: P1-9b.
- **Goal**: manually enable the executor on staging; collect red/green soak records, NO promotion automation. **Spec**: §7 Dim 5.

### G-2 `[GATE][dag-executor] wire staging-gate-smoke.timer as real R3 producer`
- type:feature · tier:X · area:devops · requires:operator-approval · NOT agent:auto. blockedBy: G-1.
- **Goal**: only after a soak record, flip `staging-gate-smoke.timer` to a real R3 producer. ⚠ makes
  smoke a HARD promote block (force-promote label retired → no escape hatch). **Spec**: §7 Dim 5.

---

## Parked backlog (`[HOLD]` — tracked, tier:X, NOT pickable)
- **H-1** `[HOLD][dag-executor] Phase 2: cross-compile (platform toolchain + sysroot) + package` — area:backend,embedded.
- **H-2** `[HOLD][dag-executor] Phase 2: t3 BUNDLE / SSH remote-board execution` — area:backend,embedded.
- **H-3** `[HOLD][dag-executor] Phase 3: multi-replica claim/fencing + retry/crash-resume` — area:backend.
- **H-4** `[HOLD][dag-executor] Phase 3: real hardware flash` — area:embedded.

---

## Dependency graph (real JIRA Blocks links; inwardIssue=blocker → outwardIssue=blocked)
```
P0-A ─→ P1-5
P0-3 ─→ P1-1 ─→ P1-2 ─→ P1-3 ─→ P1-4 ─→ P1-5 ─→ P1-7
                                  P1-4 ─→ P1-6
                                  P1-4 ─→ P1-8
P1-5, P1-6, P1-8 ─→ P1-9a ─→ P1-9b ─→ G-1 ─→ G-2
P1-5 ─→ P1-10a
P1-5 ─→ P1-10b
(all children ─→ EPIC)
```
**Ticket count**: 1 EPIC + 2 Phase-0 + 8 (P1-1..P1-8) + P1-9a/9b + P1-10a/10b + 2 GATE + 4 HOLD = **21**.
**agent:auto (runner-pickable)**: P0-A, P0-3, P1-1..P1-8, P1-9a, P1-10a = **12**.
**tier:X (human/operator)**: EPIC, P1-9b, P1-10b, G-1, G-2, H-1..H-4 = **9**.

## Runner blind-test dry-run — DONE (2026-05-24), folded in
3 fresh agents (zero conversation context, isolated worktrees, ticket-text only): P0-A (control),
P1-1 (skeleton), P1-4 (handlers — highest drift risk). **Result: all 3 stayed correctly on-scope; no
goal-drift.** Evidence the MUST-NOT works: P1-1's agent stated it *"would have been tempted to just add
the claim query since the substrate exists"* but the MUST-NOT stopped it; P1-4's agent declined every
external-reach temptation (SSH/cross-compile/Gerrit/registry) + refused to grab other tickets' scope
(claim/scheduler/terminal-wiring/systemd). Fixes folded in above:
- P0-A: corrected `db.py:274` column cite (943 = the table; relies on schema-sync, no DDL edit); added
  `test_workflow.py` for the `dag_task_id` round-trip.
- P1-1: added `test_dag_executor_skeleton.py` to Files; pinned heartbeat reuse + `omnisight:dag-exec:`
  namespace; `Requires`→`Wants` docker.
- P1-3/P1-4: **pinned the one systemic ambiguity all 3 surfaced** — the `LocalSandboxRuntime.start()` ↔
  DAG-`Task` impedance mismatch → reuse worker.py low-level helpers, not `start()`; + P1-4 pinned
  workspace-relative `expected_output` (escape→FAIL), t1/local-only tier (networked/t3→FAIL), caller-owned
  cleanup, and that the fail-closed guard is P1-6's.

**→ READY TO FILE.** Remaining filing mechanics: assign OP-keys at creation; file 12 agent:auto +
9 tier:X as Stories via `scripts/file_jira_ticket.py`; then wire the real Blocks links per the graph
(inwardIssue=blocker → outwardIssue=blocked — verify direction after each POST per [[feedback_jira_issuelink_direction]]).
