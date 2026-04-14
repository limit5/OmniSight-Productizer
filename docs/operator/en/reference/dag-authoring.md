# DAG Authoring — how to write a workflow the planner will run

> **source_en:** 2026-04-15 · Phase 56-DAG-E/F/G + Product #1-3 · authoritative

The DAG panel (`?panel=dag`) is where you hand OmniSight a **directed
acyclic graph** of tasks: "build this, then flash it onto that, then
run these three benchmarks in parallel". The backend plans, schedules,
sandboxes, and gates each node. This doc covers what you type in, what
the validator checks, and how to pick from the three editing modes.

## TL;DR

1. Load a template chip (right side of the header) — never start from
   blank.
2. Use **Form** tab if you don't want to hand-write JSON; use **JSON**
   tab for diff-review; use **Canvas** to see dependency flow at a
   glance.
3. Green badge on the header = `POST /dag/validate` passed. Submit is
   disabled until it is.
4. Click a node in Canvas to jump to its Form row.
5. Submit → link in the success banner jumps you to Pipeline Timeline
   to watch execution.

## The schema (what the validator cares about)

Each task carries:

| Field | Type | Rule |
|---|---|---|
| `task_id` | string ≤ 64 | Alphanumeric / dash / underscore only. Used as the Decision Engine key + workflow step idempotency key, so treat it as permanent once the plan is submitted. |
| `description` | string ≤ 4000 | Human-readable. Shown in Canvas tooltips and audit logs. |
| `required_tier` | `t1` / `networked` / `t3` | Tier 1 = airgapped compile sandbox. Tier 2 (`networked`) = egress-controlled. Tier 3 = physical hardware. The validator refuses a task that asks for a toolchain its tier can't run (`flash_board` on T1 is rejected). |
| `toolchain` | string ≤ 128 | Free-form, but must match one the agents recognise (`cmake`, `flash_board`, `simulate`, `git`, `checkpatch`, `finetune_export`, `http_download`, …). Make it up and the sandbox will fall through to "unknown toolchain". |
| `inputs` | list[string] | File paths the task reads. Usually another task's `expected_output`. Empty is fine for roots. |
| `expected_output` | string ≤ 512 | Where the result lands. Three shapes allowed: a file path (`build/firmware.bin`), a git ref (`git:abc1234`), or an issue ref (`issue:OMNI-42`). |
| `depends_on` | list[task_id] | Which tasks must complete first. No self-edges, no duplicates, no cycles. |
| `output_overlap_ack` | bool | *MECE escape hatch.* Two tasks are forbidden from sharing the same `expected_output` — unless BOTH explicitly set this to true. Use for parallel benchmarks that merge into one report. Leave false otherwise. |

### The seven rules

When you type, the editor debounces for 500 ms then calls `POST
/dag/validate`. Errors that light up:

| Rule | What triggers it |
|---|---|
| `schema` | Pydantic rejected the shape — missing required field, wrong type. |
| `duplicate_id` | Two tasks with the same `task_id`. |
| `unknown_dep` | `depends_on` points at a `task_id` that doesn't exist. |
| `cycle` | A cycle exists in the dependency graph. |
| `tier_violation` | Toolchain isn't allowed on the requested tier. |
| `io_entity` | `expected_output` doesn't match one of the three legal shapes. |
| `dep_closure` | A task's `inputs` reference a path no upstream task produces. |
| `mece` | Two tasks produce the same output and neither has `output_overlap_ack=true`. |

## The three tabs

**JSON.** Raw text edit. Good for diff review and copy-paste between
instances. Loss path if the JSON is broken (unparseable braces etc.):
the Form tab refuses to render and tells you to fix here first — it
won't silently discard your draft.

**Form.** One card per task, chip toggles for `depends_on`, typeahead
chips for `inputs`, checkbox for `output_overlap_ack`. Covers 100 %
of the schema — you never have to flip to JSON except for the use
cases above.

**Canvas.** Read-only topological view. Tier colouring
(purple = T1, blue = networked, orange = T3). Red border = this node
is part of a validation error. **Click a node to jump to its row in
Form.** For 1 – 20 task DAGs the depth-based auto-layout is enough; a
react-flow upgrade with pan/zoom/minimap is tracked as a follow-up
phase.

## Templates

Chips along the header load one of:

| Template | Shape | When |
|---|---|---|
| `Minimal` | 1 T1 compile | Smoke-test the editor / smallest possible submit |
| `Compile → Flash` | T1 → T3 | Typical happy path |
| `Fan-out (1→3)` | 1 build → 3 parallel sims | Exercise parallelism / fan-out |
| `Tier Mix` | T1 + NET + T3 | Shows the three-tier hand-off |
| `Cross-compile` | configure / compile / checkpatch | Embedded SoC pattern with sysroot |
| `Fine-tune (Phase 65)` | export / submit / eval | Kick off a self-improve round on demand |
| `Diff-Patch (Phase 67-B)` | propose / dry-run / apply | Workspace mutation via DE approval |

Pick the closest and edit — do not start from an empty textarea.

## Submitting

The **Submit** button stays disabled until validate returns `ok:
true`. When you click it, OmniSight:

1. `POST /dag` — runs the full validator again, persists the DAG, opens
   a `workflow_run` linked to it.
2. If the plan validates, execution starts immediately.
3. If validation fails *and* you ticked `mutate=true`, OmniSight asks
   the LLM to propose a fix (up to 3 rounds). Otherwise a 422 comes
   back with the rule breakdown.

The green banner after a successful submit has a **View in Timeline**
button — click it to jump to `?panel=timeline` and watch the run
execute.

## `mutate=true` — when to tick

Only tick it if you want OmniSight to try to auto-fix a plan that
fails validation. The LLM sees the errors, proposes edits, we revalidate.
Three rounds max, then a Decision Engine proposal opens so you decide
whether to accept the mutated plan.

Leave off if you want strict behaviour ("fail now, I'll fix it") —
that's the right default for hand-crafted plans.

## Common mistakes

- **Unknown toolchain.** The validator doesn't catch typos — the
  sandbox does, at run time. When in doubt, copy from a template.
- **`expected_output` not actually produced.** If you write
  `expected_output: "build/foo.bin"` but the toolchain drops the file
  in `out/foo.bin`, the downstream task's `inputs` check will fail at
  run time with `dep_closure`. Read your toolchain's contract.
- **Cycle via rename.** Renaming `task_id` in Form doesn't scrub it
  from other tasks' `depends_on` automatically (only *delete* does).
  After a rename, check the chip toggles on downstream tasks.
- **Over-ambitious first DAG.** Start with 2 – 3 tasks, submit, watch
  it run, then grow. The Canvas gets useful past 5 tasks.

## Related

- [`panels-overview.md`](panels-overview.md) — every panel at a glance.
- `docs/design/dag-pre-fetching.md` — how RAG injects past solutions
  into the next retry if a task fails.
- `backend/dag_validator.py` — the 7 rules, in code.
