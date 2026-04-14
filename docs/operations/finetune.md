# Fine-tune Pipeline — Operator Guide

> Phase 65. Audience: operators enabling, monitoring, or debugging the
> nightly fine-tune flow.

## Pipeline overview

```
[1 Export] workflow_runs ⨝ audit_log ⨝ git diff
              │  double-gate: completed × hvt_passed
              │              × clean resolver × scrub-safe
              │  + shortest-path filter (drop failed retries)
              ▼
        train-<ts>.jsonl
              │
[2 Submit ] FinetuneBackend.submit(jsonl, base_model, suffix)
              │  noop | openai | unsloth (env-selected)
              ▼
          JobHandle
              │
[3 Poll  ] poll_until_terminal (bounded 60×60s = 1h)
              │
[4 Eval  ] compare_models(baseline, candidate)  on
              │  configs/iq_benchmark/holdout-finetune.yaml
              │  delta_pp ≥ -OMNISIGHT_FINETUNE_REGRESSION_PP → promote
              ▼
[5 Gate  ] Decision Engine proposal
              promote → kind=finetune/promote     severity=routine     default=accept
              reject  → kind=finetune/regression  severity=destructive default=reject
```

## Enable

```bash
# Master switch (also enables Phase 62/63 if those bits are set)
export OMNISIGHT_SELF_IMPROVE_LEVEL=l4        # or l1+l3+l4 / all

# Pick a backend
export OMNISIGHT_FINETUNE_BACKEND=noop        # default — synthetic
export OMNISIGHT_FINETUNE_BACKEND=openai      # needs OPENAI_API_KEY
export OMNISIGHT_FINETUNE_BACKEND=unsloth     # local CLI; wrap in T2 sandbox

# Tune the regression gate (default 5pp)
export OMNISIGHT_FINETUNE_REGRESSION_PP=5
```

The lifespan loop ticks every 24h. Setting the master switch off
parks the loop without restart — the next tick logs and skips.

## Run on demand

```bash
# Just the export (useful for inspecting the funnel)
python -m backend.finetune_export --out /tmp/train.jsonl --limit 1000
```

A full nightly run from a Python REPL or a one-shot script:

```python
from backend import finetune_nightly as fn
out = await fn.nightly(
    baseline_model="anthropic/claude-sonnet-4-20250514",
    base_model_for_finetune="anthropic/claude-sonnet-4-20250514",
)
print(out.status, out.reason)
```

## Outcome statuses

| Status | Meaning | Operator action |
|---|---|---|
| `disabled` | `OMNISIGHT_SELF_IMPROVE_LEVEL` doesn't include `l4` | None — opt-in is off by design |
| `no_eligible_runs` | Exporter wrote 0 rows | Look at `audit_log` `finetune_exported.skipped` to see why every run was rejected |
| `below_min_rows` | Eligible rows below `MIN_ROWS_TO_SUBMIT` (50) | Wait — accumulating clean traces takes time |
| `submit_unavailable` | Backend missing prerequisite (SDK, env key, binary) | Install / set env; falls through to noop in the meantime |
| `submit_error` | Backend hit a runtime error during submit | Check the audit row's `error` field; transient errors retry next tick |
| `poll_timeout` | Job didn't reach a terminal state in `poll_max_attempts × poll_interval_s` | Verify the job side; raise `OMNISIGHT_FINETUNE_POLL_*` if your provider is slow |
| `job_failed` | Backend reported `failed` | Read the audit `finetune_failed.error` field |
| `eval_skipped` | Job succeeded but backend returned no model id | Backend bug; switch backends or file an upstream report |
| `ok_promoted` | Eval said promote; `finetune/promote` proposal opened | Approve the DE proposal to actually deploy |
| `ok_rejected` | Eval said reject; `finetune/regression` proposal opened (destructive) | Read the regression reason, decide if it's worth `accept_anyway` |

## Audit trail

Every step writes to `audit_log` (Phase 53 hash chain). A full
successful run produces:

```
finetune_exported       actor=system:finetune-nightly
finetune_submitted      actor=system:finetune-nightly
finetune_evaluated      actor=system:finetune-nightly
finetune_promoted       actor=system:finetune-nightly  # OR finetune_rejected
```

Reject paths additionally include `finetune_failed`,
`finetune_eval_skipped`, `finetune_submit_unavailable`, or
`finetune_submit_error` rows depending on where it bailed.

## Metrics

| Metric | Type | Use |
|---|---|---|
| `omnisight_training_set_rows_total{result}` | Counter | `result=written` is the headline; `skip:<reason>` lines tell you the funnel |
| `omnisight_finetune_eval_score{model}` | Gauge | Side-by-side baseline vs candidate weighted score |

The Phase 47 Decision Engine queue carries a per-kind counter;
`omnisight_decision_total{kind="finetune/regression"}` lets you
graph the rejection rate over time.

## Backends

### `noop`

Default. Submit returns a synthetic handle, poll returns `succeeded`
immediately with `ft:<base>:<suffix>-<id>`. Useful in dev or as a
prod opt-out (the gate logic still runs and writes audit rows).

### `openai`

Lazy-imports the `openai` SDK. Requires `OPENAI_API_KEY`. Uses
`client.files.create` + `client.fine_tuning.jobs.create`; poll
hits `client.fine_tuning.jobs.retrieve`. SDK statuses are mapped
to our 5-state vocabulary
(`queued`, `running`, `succeeded`, `failed`, `cancelled`).

### `unsloth`

Subprocess via an injectable `runner`. The default runner is local
`asyncio.create_subprocess_exec` — **dev only**. Production callers
inject a wrapper that calls `container.exec_in_container` for a
**Phase 64-B Tier-2 sandbox** so the Hugging Face hub model pull
goes through the egress-controlled bridge.

The CLI contract is:

```
unsloth-cli submit --data <jsonl> --base <model> --suffix <s> --job-id <id>
unsloth-cli status --job-id <id>
   STATUS: succeeded
   MODEL: meta/llama3-omn-abc123
```

A 2-line stdout format keeps the parser dependency-free.

## Hold-out set curation

`configs/iq_benchmark/holdout-finetune.yaml` ships 10 hand-curated
embedded-firmware questions. To grow the set toward the design
target of 100:

1. Edit the YAML, append new questions (don't renumber).
2. Restart the backend — `prompt_registry` bootstrap will not touch
   this file (it lives in `configs/`, not `backend/agents/prompts/`).
3. Verify with `pytest backend/tests/test_finetune_eval.py`.

**Do NOT auto-generate** hold-out questions from `episodic_memory`.
A degrading model would also pick easier questions for itself —
the metric loses meaning.

## Common pitfalls

- **All exports rejected** — check the audit row's `skipped` field.
  Most-common: `pii_scrub_unsafe` (recent runs leaked secrets) or
  `resolver_auto_only` (no operator-touched decisions in the window).
- **Repeated `eval_skipped`** — the backend says succeeded but
  doesn't fill `fine_tuned_model`. Re-check the backend's status
  parser.
- **`finetune/regression` proposal sitting open** — DE timeout is
  24h. After it expires the default (`reject`) is auto-applied;
  the candidate is dropped. Set explicitly to `accept_anyway` if
  you've manually verified the candidate.

## Related

- `docs/design/agentic-self-improvement.md` — L4 design rationale
- `backend/finetune_export.py` — JSONL exporter
- `backend/finetune_eval.py` — hold-out comparator
- `backend/finetune_backend.py` — backend abstraction
- `backend/finetune_nightly.py` — orchestrator
- `configs/iq_benchmark/holdout-finetune.yaml` — hold-out questions
