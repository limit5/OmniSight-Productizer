# AI Reviewer (OP-713) — Runbook

## TL;DR

Every Gerrit `patchset-created` event triggers an AI code review.
Three model tiers — haiku / sonnet / opus — picked by the touched
file paths. Cheap baseline (haiku); escalate to opus only on
high-risk paths. Result: one `Code-Review +1` (or `0` on uncertainty
or oversized diffs), with the model id + cost in the comment footer.

**Hard gate is unchanged.** AI maxes out at +1 — human +2 from a
member of the `non-ai-reviewer` group is still the only thing that
permits submission (`CLAUDE.md` L1 + ADR-0003).

## Wiring

```
Gerrit patchset-created webhook
        │
        ▼
backend/routers/webhooks.py::gerrit_webhook
        │
        ▼
_on_patchset_created   ── synchronous: loop-prevent + throttle + L2 notify
        │
        ▼  (asyncio.create_task — fire and forget)
_run_ai_review
        │  files,subject ←── gerrit_client.query_change
        │  diff          ←── git show <revision>  (best-effort, local mirror)
        │
        ▼
backend/agents/ai_reviewer.py
        │  route_model()       — pick haiku / sonnet / opus
        │  is_too_large()      — > 1500 LOC fast-path
        │  review_patchset()   — invoke_chat → ReviewResult
        │
        ▼
gerrit_client.post_review(..., labels={"Code-Review": +1 | 0})
        │
        ▼
billing_usage.record_llm_call(...)   — model_id + tokens + cost
```

## Model tiers

| Tier   | Model               | When                                                                |
| ------ | ------------------- | ------------------------------------------------------------------- |
| haiku  | `claude-haiku-4-5`  | Default — docs, scripts, configs that don't sit under a higher tier |
| sonnet | `claude-sonnet-4-6` | `backend/`, `frontend/`, `lib/`, `components/`, `app/` (general)    |
| opus   | `claude-opus-4-7`   | HIGH-RISK paths — alembic migrations, SQL, security, deploy, …      |

Source-of-truth constants live in `backend/agents/ai_reviewer.py`:
`MODEL_HAIKU`, `MODEL_SONNET`, `MODEL_OPUS`,
`HIGH_RISK_PATH_PREFIXES`, `HIGH_RISK_FILES`, `HIGH_RISK_SUFFIXES`,
`SONNET_PATH_PREFIXES`.

### HIGH-RISK paths (always opus)

Prefixes:

- `alembic/versions/` and `backend/alembic/versions/`
- `security/` and `backend/security/`
- `deploy/`
- `.gerrit/`
- `config/` / `configs/`

Exact files:

- `backend/submit_rule.py`
- `backend/merger_agent.py`
- `backend/merge_arbiter.py`
- `CLAUDE.md`

Suffixes:

- `*.sql`

If *any* file in a patchset matches HIGH-RISK, the whole patchset is
reviewed by opus.

### How to add a new HIGH-RISK pattern

1. Add the path / prefix / suffix to the appropriate constant in
   `backend/agents/ai_reviewer.py`.
2. Extend `backend/tests/test_ai_reviewer.py::test_route_model_high_risk_paths_force_opus`
   so the new pattern is locked in.
3. Update this runbook (the table above).
4. PR review (Gerrit) — change touches `backend/agents/ai_reviewer.py`
   so the AI reviewer itself uses opus when self-reviewing.

## Size cap

Patchsets above **1500 LOC** (insertions + deletions) skip the LLM
entirely and post:

```
too large for AI review, please ensure human deep-review
(diff size N > 1500 LOC limit).
```

Score = `0` (NOT `+1`). Human reviewers must deep-read these.

The cap lives at `DEFAULT_REVIEW_DIFF_LIMIT_LOC`. Changing it requires
a corresponding update to `test_review_patchset_too_large_returns_score_zero`
and a note in the next deploy runbook so operators expect the cost
shift.

## Idempotency throttle

`(change_id, revision_sha)` → last-reviewed timestamp, in-memory per
worker. TTL = 24 hours.

- Same SHA delivered twice within 24 h → only the first review fires.
- New patchset (different SHA) on the same change → reviewed.
- Multi-worker dedup is best-effort: Gerrit retries usually re-deliver
  to the same worker, and if not, Gerrit merges duplicate label scores
  on the same revision so the worst case is a benign double-comment.
  Promote to Redis (`ai_reviewer:reviewed:{change}:{rev}` keys with
  TTL 24 h) if dashboards show duplicate comment posts > 1 % of
  events.

## Cost monitoring

Every successful review writes a `billing_usage_events` row via
`backend.billing_usage.record_llm_call` with:

- `model` = chosen tier model id
- `provider` = `anthropic`
- `product_line` = `ai_reviewer`
- `input_tokens` / `output_tokens` (char/4 estimate — adapter doesn't
  surface real usage in this code path)
- `cost_usd` = computed from the central `backend/pricing.py` table
- `metadata.change_id` / `metadata.revision` / `metadata.score` /
  `metadata.ticket = "OP-713"`

Daily / weekly cost rollups:

```sql
SELECT
  to_char(date_trunc('day', to_timestamp(occurred_at)), 'YYYY-MM-DD') AS day,
  model,
  COUNT(*) AS reviews,
  SUM(input_tokens) AS in_tok,
  SUM(output_tokens) AS out_tok,
  ROUND(SUM(cost_usd)::numeric, 4) AS cost_usd
FROM billing_usage_events
WHERE kind = 'llm_call'
  AND metadata_json::jsonb @> '{"ticket": "OP-713"}'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

If a single tier (especially opus) starts dominating, check whether a
new HIGH-RISK pattern should be narrowed (e.g. tightening
`config/`).

## Bot identity

- General reviews → posted by `codex-bot`
- Security-touching reviews → posted by `claude-bot`

Both accounts are members of `ai-reviewer-bots` and DENY-listed for
submit / push-force / addPatchSet by `.gerrit/project.config.example`
(O10 least-privilege).

## Gerrit subscription (Phase 1 — operator action)

The webhooks plugin reads from `webhooks.config` on `refs/meta/config`
(NOT `project.config` — see lesson L22). The deployed config must
include `events = patchset-created` on the omnisight-bot remote.

Reference: `.gerrit/webhooks.config.example`.

To verify the subscription is live:

1. Push a throwaway test patchset to `refs/for/develop`.
2. `journalctl -u omnisight-backend -f | grep 'Gerrit webhook'` —
   look for `type=patchset-created` within 30 seconds.
3. Within ~5 minutes, the patchset should carry a `Code-Review +1`
   from `codex-bot` (or `claude-bot` for security paths) with a
   `reviewed-by:` footer.

## Manual smoke test

```bash
# In a backend shell
python - <<'PY'
import asyncio
from backend.routers import webhooks

async def run():
    await webhooks._run_ai_review(
        change_id="Ismoke01",
        change_number="0",
        revision="0" * 40,
        project="omnisight",
        subject="smoke-test patchset",
        insertions=2000,   # over cap on purpose
        deletions=0,
    )
asyncio.run(run())
PY
```

Expected: a `[ERROR]` log from the Gerrit stub if no real change
exists, or — if you supply a real revision — a `Code-Review 0` with
the "too large" message and no LLM call.

## Failure modes & rollback

| Symptom                                                    | Likely cause                       | Fix                                                                                                           |
| ---------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| No `+1` after ~5 min on a fresh patchset                   | Webhook not delivered              | `journalctl -u omnisight-backend ` for `type=patchset-created`. If absent, re-check `webhooks.config` on `refs/meta/config`. |
| Comment posted but no `Code-Review` label                  | `gerrit_client.post_review` error  | Tail backend log for `ai_reviewer post_review failed` — usually missing SSH key or stale `git_accounts` row.  |
| Wrong model in footer                                      | Routing rule changed unexpectedly  | `pytest backend/tests/test_ai_reviewer.py -k route_model` and inspect the parametrised cases.                 |
| Cost spike on opus                                         | A widely-touched HIGH-RISK pattern | Narrow the pattern in `HIGH_RISK_*` and add a unit test pinning the new boundary.                             |
| Duplicate comments on same SHA                             | Multi-worker throttle miss         | If > 1 % of events, promote throttle to Redis (see "Idempotency throttle" above).                             |

To **disable** the AI Reviewer in an emergency without a redeploy:

1. Remove `events = patchset-created` from `webhooks.config` on
   `refs/meta/config`.
2. `git push origin HEAD:refs/meta/config`.
3. Existing in-flight events drain naturally (no new ones queue).

To **re-enable**: revert the webhooks.config edit and push again.
