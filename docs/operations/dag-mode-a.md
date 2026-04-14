# DAG Mode A — Operator Guide

> Phase 56-DAG-D. Submit a hand-written DAG JSON plan, validate it,
> optionally ask the Orchestrator to auto-fix, persist + link to a
> fresh workflow_run.

## When to use this

Mode A ("advanced") is for operators who want full control: you write
the DAG JSON yourself against `docs/design/self-healing-scheduling-mechanism.md`
and POST it. Mode B (auto-plan from natural language) is **not yet
shipped** — chat-router integration is deferred.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/dag` | operator | Submit + validate + (optionally) mutate + link |
| GET  | `/api/v1/dag/plans/{plan_id}` | operator | Fetch one plan row |
| GET  | `/api/v1/dag/runs/{run_id}/plan` | operator | Latest plan for a run |
| GET  | `/api/v1/dag/plans/by-dag/{dag_id}` | operator | Full mutation chain |

## Submit body

```json
{
  "dag": {
    "schema_version": 1,
    "dag_id": "REQ-1042",
    "tasks": [ … ]
  },
  "mutate": false,
  "metadata": { "ticket": "OMNI-42" }
}
```

- `mutate=false` (default): deterministic validator only. If the DAG
  fails, you get 422 + the full error list. You edit and resubmit.
- `mutate=true`: on validation failure, the Orchestrator is called
  (up to 3 rounds). On success you get a **successor run_id** +
  `supersedes_run_id` pointing at the original. On exhaustion you
  get 422 with `stage=mutation_exhausted`, and a Decision Engine
  `dag/exhausted` (severity=destructive, default=abort) proposal is
  filed for admin review.

## Response shapes

### 200 — validated + executing

```json
{
  "run_id": "wf-abc123",
  "plan_id": 7,
  "status": "executing",
  "validation_errors": []
}
```

### 200 — mutated to success

```json
{
  "run_id": "wf-def456",
  "plan_id": 8,
  "status": "executing",
  "mutation_rounds": 1,
  "supersedes_run_id": "wf-abc123",
  "validation_errors": []
}
```

### 422 — schema error (pre-validation)

```json
{
  "detail": "DAG schema validation failed: …",
  "stage": "schema"
}
```

### 422 — semantic failure (no mutation)

```json
{
  "run_id": "wf-abc123",
  "plan_id": 7,
  "status": "failed",
  "validation_errors": [
    { "rule": "tier_violation", "task_id": "T1",
      "message": "toolchain 'flash_board' is explicitly DENIED in tier 't1'" }
  ]
}
```

### 422 — mutation exhausted

```json
{
  "run_id": "wf-abc123",
  "plan_id": 7,
  "status": "failed",
  "mutation_rounds": 3,
  "mutation_status": "exhausted",
  "validation_errors": [ … ],
  "stage": "mutation_exhausted"
}
```

## What validation checks

See `backend/dag_validator.py`. Seven rules:

| Rule | Meaning |
|---|---|
| `duplicate_id` | Two tasks share `task_id` |
| `unknown_dep` | `depends_on` points at a non-existent task |
| `cycle` | Graph has a cycle (detected via Kahn's algorithm) |
| `tier_violation` | `toolchain` not allowed (or explicitly denied) in `required_tier` per `configs/tier_capabilities.yaml` |
| `io_entity` | `expected_output` not a file path, `git:<sha>`, or `issue:<id>` |
| `dep_closure` | `input` not produced upstream and not `external:` / `user:` |
| `mece` | Two tasks share `expected_output` without unanimous `output_overlap_ack=true` |

All errors are returned at once (not first-fail) so you can fix a
broken DAG in one pass.

## Mutation chain

Each round of the Orchestrator mutation loop writes a new
`dag_plans` row linked to the previous via `parent_plan_id`. The
old `workflow_runs.successor_run_id` gets set to the new run so you
can traverse the full history via `GET /api/v1/dag/plans/by-dag/{dag_id}`.

## Common pitfalls

- **Tier violation** — `flash_board` only works under `t3`; `cmake`
  only works under `t1`. See `configs/tier_capabilities.yaml` for
  the full allow/deny table.
- **Forgot `depends_on`** — an `input` that isn't produced upstream
  and doesn't start with `external:` / `user:` trips `dep_closure`.
- **Same output path twice** — set `output_overlap_ack=true` on
  **both** tasks, or rename.
- **Mutation exhausted** — the Orchestrator couldn't fix your DAG in
  3 rounds. Read the error list in `validation_errors` and fix it
  manually, then resubmit with `mutate=false`.

## Related

- `docs/design/self-healing-scheduling-mechanism.md` — design doc
- `backend/dag_schema.py` — Pydantic models
- `backend/dag_validator.py` — the 7 rules
- `backend/dag_planner.py` — mutation loop
- `configs/tier_capabilities.yaml` — toolchain allow/deny per tier
