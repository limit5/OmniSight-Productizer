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

The trigger comes from the **Gerrit stream-events SSH daemon** (OP-689 +
OP-715 + OP-801), NOT the webhooks plugin. Gerrit's webhooks plugin
v3.13.5 has zero auth surface (no `secret`, no signature header — see
lessons L-OP-713 + L-OP-801), so the auth-gated `/webhooks/gerrit`
endpoint is unreachable from a real Gerrit instance. OP-715 already
moved the proactive merger trigger to the daemon; OP-801 mirrors that
for AI Reviewer.

```
Gerrit stream-events SSH (claude-bot)
        │
        ▼
backend/agents/gerrit_jira_bridge.py::stream_forever
        │  (subprocess Popen + line iteration)
        ▼
process_stream_event(event)
        │  type=patchset-created
        ▼
_handle_patchset_created(event)
        │  spawns 2 daemon threads (independent fire-and-forget)
        ├──► _spawn_proactive_merger_thread → _proactive_merger_check  (OP-715)
        └──► _spawn_ai_reviewer_thread     → _ai_reviewer_check        (OP-801)
                                           │
                                           ▼
backend/routers/webhooks.py::_ai_reviewer_check     (in-process coroutine)
        │  1. loop-prevent (uploader=merger-agent-bot)
        │  2. (change_id, revision) throttle (24 h TTL)
        │  3. L2 notify  (operator dashboard heads-up)
        ▼
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

Greppable bridge log lines (per event):

- `ai_reviewer_thread_spawned change=<n> ps=<n>` — structured JSON,
  emitted by the bridge daemon at thread spawn.
- `ai_reviewer_invoked change=<id> model=<tier> loc=<n>` — emitted by
  `_run_ai_review` after `route_model` picks the tier; this is the
  one operators grep to confirm risk-tier routing fired.
- `ai_reviewer_skip change=<id> reason=uploader_is_merger` — emitted
  when the uploader is `merger-agent-bot` (loop prevention).
- `ai_reviewer_skip change=<id> rev=<sha8> reason=throttle_24h` —
  emitted when the `(change_id, revision)` throttle short-circuits a
  duplicate event (e.g. bridge restart that replays cursor events).
- `ai_reviewer_thread_error change=<n> ps=<n> err=...` — emitted if
  the per-event thread raises; the bridge survives.

The HTTP endpoint `/webhooks/gerrit` (`backend/routers/webhooks.py::
gerrit_webhook`) stays in place as-is — it accepts synthetic curl
events for smoke tests and is the natural landing pad for a future
plugin-auth fix. No real Gerrit traffic flows through it today.

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

## Trigger source — bridge daemon stream-events

OP-801: the AI Reviewer fires from the Gerrit stream-events SSH daemon
running as `claude-bot`, NOT the webhooks plugin. The daemon
subscribes via `ssh -p 29418 claude-bot@<host> gerrit stream-events`
and routes `patchset-created` events through
`gerrit_jira_bridge.py::_handle_patchset_created`, which spawns the
AI Reviewer pipeline in a fire-and-forget daemon thread.

To verify the trigger is live:

1. Push a throwaway test patchset to `refs/for/develop`.
2. `journalctl -u omnisight-bridge -f | grep ai_reviewer` — within
   ~30 seconds, look for two log lines:
   - `ai_reviewer_thread_spawned change=N ps=M` (structured JSON)
   - `ai_reviewer_invoked change=I... model=haiku|sonnet|opus loc=N`
3. Within ~5 minutes, the patchset should carry a `Code-Review +1`
   from `claude-bot` (the bridge SSH identity, also resolved via
   `git_accounts` for the actual `gerrit review` call) with a
   `reviewed-by:` footer.

The legacy webhooks plugin path is **structurally broken** for AI
Reviewer (no auth → `/webhooks/gerrit` 401s every event). The
endpoint is preserved only for synthetic curl smoke tests + as a
landing pad if Gerrit ever ships an auth-capable plugin.

## Manual smoke test

End-to-end (via the bridge daemon's in-process pipeline):

```bash
# In a backend shell
python - <<'PY'
import asyncio
from backend.routers import webhooks

async def run():
    await webhooks._ai_reviewer_check({
        "type": "patchset-created",
        "change": {
            "id": "Ismoke01",
            "number": 0,
            "subject": "smoke-test patchset",
            "project": "omnisight",
        },
        "patchSet": {
            "revision": "0" * 40,
            "uploader": {"name": "alice"},
            "sizeInsertions": 2000,   # over cap on purpose
            "sizeDeletions": 0,
        },
    })
asyncio.run(run())
PY
```

Expected: a `[ERROR]` log from the Gerrit stub if no real change
exists, or — if you supply a real revision — a `Code-Review 0` with
the "too large" message and no LLM call. This exercises the same
coroutine the bridge daemon's `_spawn_ai_reviewer_thread` invokes.

For a leaf-only smoke that bypasses skip checks, call
`_run_ai_review(...)` directly with the same kwargs the shared
function passes through.

## Failure modes & rollback

| Symptom                                                    | Likely cause                       | Fix                                                                                                           |
| ---------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| No `+1` after ~5 min on a fresh patchset                   | Bridge daemon down or stream broken | `systemctl status omnisight-bridge`; `journalctl -u omnisight-bridge -f` for `gerrit_stream_disconnected` / `gerrit_auth_failed`. |
| `ai_reviewer_thread_spawned` logged but no `ai_reviewer_invoked` | `_ai_reviewer_check` skipping early | Look for the matching `ai_reviewer_skip ...` line — usually `uploader_is_merger` or `throttle_24h`.       |
| Comment posted but no `Code-Review` label                  | `gerrit_client.post_review` error  | Tail backend log for `ai_reviewer post_review failed` — usually missing SSH key or stale `git_accounts` row.  |
| Wrong model in footer                                      | Routing rule changed unexpectedly  | `pytest backend/tests/test_ai_reviewer.py -k route_model` and inspect the parametrised cases.                 |
| Cost spike on opus                                         | A widely-touched HIGH-RISK pattern | Narrow the pattern in `HIGH_RISK_*` and add a unit test pinning the new boundary.                             |
| Duplicate comments on same SHA                             | Multi-worker throttle miss         | If > 1 % of events, promote throttle to Redis (see "Idempotency throttle" above).                             |

To **disable** the AI Reviewer in an emergency without a redeploy:

1. `systemctl stop omnisight-bridge` (also halts the OP-689 ticket
   transitions and OP-715 proactive merger — full bridge halt).
2. For an AI-Reviewer-only kill: comment-out the
   `_spawn_ai_reviewer_thread(event)` call in
   `_handle_patchset_created` and redeploy. The merger and ticket-
   transition paths keep running.

To **re-enable**: restart the bridge / revert the comment-out.
