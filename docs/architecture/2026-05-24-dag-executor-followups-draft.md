# DAG Executor — P1-9b first-run follow-ups (DRAFT for codex → blind-test → file)

**Status**: DRAFT v1 (2026-05-24). Pipeline: codex audit → revise → runner blind-test → file → runner fixes → **re-verify P1-9b**.
**Why**: the P1-9b first real run (engine works — 185 tests + a real cmake build — but the smoke
path can't complete) found 3 gaps. See `docs/architecture/2026-05-24-dag-execution-engine-epic-design.md` §7
+ the [[feedback_epic_decomposition_sop]] blind-spot lesson. scope:`dag-executor`. class:subscription-claude.

## UPDATE 2026-05-24 — F1 first-run found 2 MORE gaps (D + E); F4 filed (OP-1676)
The F1 (OP-1673) runner correctly §11-reverted: F1 (payload-only) can't make the smoke DAG run
because the executor lacks inter-task data flow. Two coupled gaps, both in `PlanWorkspaceBuilder.prepare()`:
- **D — no inter-task artifact passing**: a downstream input == an upstream task's `expected_output`
  (`run-test` needs `build/firmware.bin` from `compile`) is never copied — `prepare()` globs only
  `project_root`. Fix: stage each ok task's output per-plan; materialise downstream matching inputs from it.
- **E — `external:`/`user:` source inputs not materialisable**: raw source seeds (`CMakeLists.txt`)
  are dep-closure-INVALID per `dag_validator` (must be `external:`/`user:`), but `prepare()` treats the
  literal string as a glob → `external:CMakeLists.txt` matches nothing. Fix: strip the prefix, copy from project_root.
Both → **F4 (OP-1676)** (one `prepare()` change, honours the validator's existing input contract;
codex-reviewed SOUND-WITH-CHANGES). **Re-scoped chain: F4 → F1 → F2 → re-verify P1-9b.** F1's smoke
inputs corrected to validator-legal (`external:` seeds + the upstream-output ref). Resume-artifact
restore across re-claim is OUT OF SCOPE (documented follow-up). This is the SOP "integration-glue +
payload-correctness blind spot" again — both D and E are connective-tissue gaps only end-to-end run finds.

## The 3 gaps (empirically confirmed)
- **A — smoke DAG payload bug**: `compile` task has `inputs:[]` → `PlanWorkspaceBuilder` copies no
  source into the scratch → cmake rc=1 → never `completed`. (Proven: inputs=[] FAIL, inputs=[src] ok.)
- **B — run-loop seam unwired**: `DagExecutor.run()` is heartbeat-only ("SKELETON … no task
  execution", placeholder at the loop seam). The components (claim/scheduler/handlers/
  `record_and_finalize_plan`) exist but nothing assembles them into the live loop — no ticket owned this.
- **C — stale dev image**: dev stack runs `v0.5.0` (pre-executor). NOT needed for source-based P1-9b
  verification → deferred (rides the next release), filed as HOLD so it's tracked.

---

## F1 `[OP][dag-executor] smoke DAG: declare compile/run-test inputs + ship a buildable fixture`
- type:bug · tier:M · area:tests,devops,backend · agent:auto · capability:enable=gerrit_push
- **Goal**: make the smoke DAG actually buildable. (1) ship a tiny committed fixture — a minimal CMake
  project (`CMakeLists.txt` + `main.c` that builds `firmware.bin`) + a `run_test.py` that consumes
  `build/firmware.bin` and writes `logs/test.log`; (2) update the smoke DAG so `compile` declares
  `inputs:["CMakeLists.txt","main.c"]` (or the fixture's files) and `run-test` declares
  `inputs:["build/firmware.bin","run_test.py"]` + runs `run_test.py`; (3) **add the opt-in flag
  `metadata.dag_executor_opt_in: true` to the smoke DAG submission** (the per-run half of F2's safety
  gate). **Files**: `scripts/prod_smoke_test.py` (DAG def + submit metadata, lines ~72-99) + a NEW
  fixture dir **`backend/dag_smoke_fixture/`** (`CMakeLists.txt`, `main.c`, `run_test.py` — the runtime
  fixture `OMNISIGHT_DAG_PROJECT_ROOT` points at; NOT under `test_assets/`). **Spec**: this doc gap A.
- AC — **Code**: fixture builds locally (`cmake` produces `firmware.bin`; `run_test.py` reads it,
  writes `logs/test.log`); smoke DAG inputs declared. **Deploy**: ships in repo. **Integration**: a
  test runs the smoke DAG's two tasks through the real `LocalTaskHandler` with project_root=fixture →
  both `ok`, artifacts present. **Exercised**: `compile` no longer fails on an empty workspace.
  **Go-Live**: merged.
- **MUST NOT**: change the executor engine, `PlanWorkspaceBuilder`, or the handler internals; this is
  payload + fixture only. Do NOT touch `prod_smoke_test.py` orchestration/CLI/poll/auth. NOT `test_assets/`.

## F2 `[OP][dag-executor] wire DagExecutor.run() loop seam: claim → execute → finalize`
- type:feature · tier:M · area:backend,tests · agent:auto · capability:enable=gerrit_push
- blockedBy: F1. **Goal**: assemble the EXISTING components into the live loop — at the placeholder
  seam in `DagExecutor.run()`, when armed: claim ONE ready `executing` plan (the P1-2 lease `acquire`),
  bind `PlanWorkspaceBuilder(project_root=Path(os.environ["OMNISIGHT_DAG_PROJECT_ROOT"]))` — **skip
  execution if that env is unset** — call `record_and_finalize_plan(plan, handler=…)`, then
  release/renew the lease. Stay **disabled-by-default**. **Dual opt-in gate (codex): execute a plan
  ONLY when BOTH** `dag_id ∈ OMNISIGHT_DAG_EXECUTOR_ALLOW_DAG_IDS` (env — the deployment kill-boundary)
  **AND** the run's `metadata.dag_executor_opt_in is true` (F1 sets this) — so stale/bootstrap
  `executing` rows are never auto-run. **Files**: `backend/dag_executor.py` (`run()` seam only) +
  tests. **Spec**: this doc gap B + design §7 codex "smallest honest slice".
  **Discovery (pinned, blind-test):** there is no status-keyed plan query — discover via the existing
  `dag_storage.list_plans(dag_id)` iterated over the env-allowlist dag_ids (pick the first `executing`
  per id). Do NOT add a `list_executing_plans()`/status index (that's reimplementing storage). **Lease
  (pinned):** acquire→execute→release one plan per tick; a build outliving the ~45s lease TTL is an
  ACCEPTED limitation for the fast smoke MVP — do NOT build a background lease-renewer (deferred follow-up).
- AC — **Code**: `run()` drives one claimed+opt-in plan per tick through the existing
  `record_and_finalize_plan`; lease acquired before + released after; `OMNISIGHT_DAG_PROJECT_ROOT`
  bound (skip if unset); dual opt-in gate + enable-flag honored; no-op when disabled. **Deploy**:
  backend image, default OFF. **Integration**: in-harness double (mirror `test_dag_executor_terminal.py`)
  — an armed loop claims a seeded opt-in executing plan and drives it to `completed`; a plan missing
  EITHER the env-allowlist OR the metadata opt-in is left untouched; unset project_root → skip.
  **Exercised**: end-to-end — a submitted smoke DAG (F1 fixture, opt-in) reaches `completed` via the
  loop (local). **Go-Live**: merged, executor still default-OFF.
- **MUST NOT** *(codex-exact)*: *reimplement claim, scheduling, handlers, step recording, or terminal
  transitions; only wire the existing lease helpers, `LocalTaskHandler`/`PlanWorkspaceBuilder`, and
  `record_and_finalize_plan()` at the `DagExecutor.run()` seam. MUST NOT enable execution by default,
  MUST NOT execute plans unless both the env DAG-id allowlist and per-run metadata opt-in are present,
  and MUST NOT add promotion, timer, staging, registry, SSH, hardware, or nonlocal side-effect automation.*

## F3 `[HOLD][dag-executor] rebuild dev backend image from develop (containerized executor)`
- type:feature · tier:X · area:devops · NOT agent:auto. blockedBy: F2.
- **Goal**: once F1+F2 land, bake the executor into a dev backend image (build from develop, push to
  the dev registry, bump `deploy/dev/.env` `OMNISIGHT_IMAGE_TAG`) so the executor can run AS the dev
  compose service (P1-9a stanza). **DEFERRED** — P1-9b is verifiable from develop *source* against the
  dev DB without this; the containerized path rides the next release image. Tracked as HOLD.

---

## Dependency + re-verify
`F1 → F2 → (re-verify P1-9b from source against the dev DB) ; F2 → F3 (HOLD)`. After F1+F2 merge:
run the wired executor from a develop checkout against the dev postgres (`omnisight_dev`@58432,
`OMNISIGHT_ENV=dev`), submit the smoke DAG, confirm `completed` <300s + 2 steps + artifacts. That
closes P1-9b. F3 (containerize) is the later "persistent dev service" item.

## Open questions for codex
1. **`project_root` resolution (F2)** — for the smoke DAG it's the F1 fixture dir; for a general DAG
   it's a real project checkout. Where should the loop get it: an env (`OMNISIGHT_DAG_PROJECT_ROOT`),
   plan metadata, or a per-DAG registry? Pick the minimal-honest one for the smoke MVP without
   over-building the general case.
2. **Granularity** — is F1+F2 the right split, or should the fixture (F1) fold into F2's integration
   test? (Leaning split: F1 is payload/fixture, F2 is loop glue — distinct, F1 unblocks F2's e2e AC.)
3. **Opt-in allowlist (F2)** — how does the loop know a plan is "opt-in" to execute (so prod/bootstrap
   inert plans are never auto-run)? A plan-metadata flag? An env allowlist of dag_ids? This is the
   safety boundary that keeps F2 from turning every `executing` plan live.
4. **Is F2 runner-safe** or does the assembly + project_root design need to be operator/Claude-owned?
   (It's the integration-glue gap the SOP now warns about — the blind-test should stress it for drift.)
