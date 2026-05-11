# Spike Report — Merger Agent Proactive-Trigger (OP-876)

**Date**: 2026-05-11
**Author**: claude-bot (under operator oversight per OP-876)
**Scope**: Spike-only investigation of three unknowns blocking OP-713
Phase 2 (`patchset-created` consumer) and OP-733 (`change-merged`
subscription) follow-throughs. No production code changes; no
write-side touches to `webhooks.config`, `project.config`, or the
backend handlers — only static reads + an operator-runnable harness.

**Deliverables** (per DoD):

- This decision document with a go/no-go verdict per question.
- `scripts/spike_merger_proactive_trigger.py` — static-analysis +
  live-Gerrit harness (live mode operator-run only).
- `tests/integration/test_merger_spike.py` — 12 contract tests that
  pin the decision-rule mapping so future drift breaks the build.

**Out-of-area domains untouched**: db, embedded, frontend, security,
tooling. The spike reads `backend/routers/webhooks.py`,
`backend/agents/auto_rebase.py`,
`backend/agents/gerrit_jira_bridge.py`, and
`/tmp/gerrit-meta/project.config`; it modifies none of them.

---

## TL;DR — Verdict matrix

| Question | Verdict | Confidence | Next step |
|---|---|---|---|
| Q1 — `mergeable` race at `patchset-created` | **deferred** | static analysis cannot answer | operator run `--mode=live` against staging Gerrit before committing to OP-713 Phase 2 |
| Q2 — OP-733 end-to-end behaviour | **deferred (premise corrected)** | wiring correct in source; live exercise still missing | reframe OP-733 follow-up: the missing piece is a staging dry-run + log-tail, not adding a webhook subscription |
| Q3 — `ai-reviewer-webhook` auth | **no-go (confirmed broken)** | static evidence conclusive | patch `project.config` to `header = Authorization: Bearer …` (L-OP-708 pattern); file follow-up ticket |

Overall: the spike clears Q3 unambiguously and reshapes Q2 such that
the original premise (webhook subscription) is incorrect. Q1 is the
only blocker still requiring fresh measurement against a real Gerrit;
the harness is ready for the operator to execute.

---

## Q1 — `mergeable` race at `patchset-created` time

### Hypothesis (per ticket)

When Gerrit fires `patchset-created`, `change.mergeable` may still be
`null` or stale because mergeability is computed asynchronously after
indexing. If true, the proposed `patchset-created` trigger is unsafe
unless we either retry-with-backoff or fall back to a poll-after-N-
seconds approach driven by `ref-updated` on the target branch.

### Methodology (encoded in
`scripts/spike_merger_proactive_trigger.py`)

1. Operator runs the harness with `--mode=live` and credentials for an
   account that has push access to the project (per-bot HTTP password
   path; see `backend/agents/auto_rebase.py:OWNER_HTTP_PASSWORD_PATHS`).
2. For each of `--samples` iterations (default 10):
   a. Fetch `origin/develop~5` so the parent SHA reliably lags the
      tip.
   b. Edit a hot file (default `auto-runner-jira.py`) so the patch
      conflicts with subsequent commits.
   c. `git push origin HEAD:refs/for/develop%hashtag=spike-OP-876` —
      record wallclock as `t_event`.
   d. Poll `GET /a/changes/<id>?o=MERGEABLE` at 0.5 s intervals; stop
      when `mergeable` is non-null OR when the timeout
      (`Q1_MAX_WAIT_S = 30s`) elapses. Record `t_mergeable_ready` and
      `mergeable` final value.
   e. Abandon the change via SSH (`gerrit review --abandon`) to keep
      Gerrit clean.
3. Harness emits the histogram + `verdict` per
   `q1_decide()`'s decision rule.

### Decision rule (canonical, encoded in
`spike.q1_decide`; pinned by
`test_q1_decision_rule_matches_documented_bands`)

| Observation | Verdict | Action |
|---|---|---|
| All deltas `< 1 s` | `go` | `patchset-created` is a real-time trigger; no retry needed. Proceed to OP-713 Phase 2 single-shot consumer. |
| Any delta in `[1 s, 10 s]`, none above | `go_with_backoff` | Backend must implement a 3-attempt backoff (0 s, 2 s, 5 s) before declaring `mergeable_unknown`. |
| Any delta `> 10 s` **or** any timeout | `no_go` | `patchset-created` is unsafe. Fall back to a `ref-updated` poll-after-N-seconds approach (separate follow-up; explicitly out of this spike's scope per ticket). |
| No samples captured | `deferred` | Operator must run the live harness; static analysis cannot answer this question. |

### Result

**Verdict: `deferred`** — until the operator executes `--mode=live`
against the staging Gerrit, the data set is empty and the rule
returns deferred (verified by
`test_q1_decision_rule_matches_documented_bands[empty]`).

There is no static signal that lets us short-circuit this question.
Gerrit's mergeability index timing is a runtime property of the
deployed server; reading source would be speculation. The harness is
designed to make the operator-run cheap (≈30 s of wallclock for 10
samples + abandon).

### Recommended sample size

The ticket prescribes 10 samples. Reasoning: the distribution is
bimodal in practice (Gerrit caches the result so steady-state pushes
hit fast; cache-cold pushes pay the index latency). 10 samples are
enough to detect a 30 % tail in the slow bucket with reasonable
confidence; if the tail looks bigger, bump to 30.

---

## Q2 — OP-733 backend consumer end-to-end behaviour

### Hypothesis (per ticket)

OP-733 code shipped but the webhook subscription was never added to
`webhooks.config` (the `[remote …]` blocks in `project.config` are
silent no-ops per L-OP-713). Therefore the consumer code has never
been triggered in production, so we don't know if it actually works.

### Finding — premise was incorrect

**OP-733 is not wired through the HTTP webhook at all.** Static read
of the codebase shows:

- `backend/agents/auto_rebase.py:1-20` — module docstring states
  OP-733 "extends the OP-714/OP-715 stream-events daemon. When a
  `change-merged` event arrives on `develop`, sweep all open bot-owned
  PSes and try to rebase each one onto the new tip."
- `backend/agents/gerrit_jira_bridge.py:895-931` —
  `_schedule_auto_rebase_sweep` is the entry point, invoked alongside
  the OP-689 ticket-transition handler on every `change-merged`
  stream event. Uses a 30 s debounce
  (`BridgeConfig.auto_rebase_debounce_seconds`).
- `backend/routers/webhooks.py:944-996` — the HTTP webhook's
  `_on_change_merged` handler does NOT import `auto_rebase` and only
  drives replication + intent bridge + L3 save.

The SSH stream-events daemon is auth'd at the SSH protocol layer (no
webhook header gymnastics), per the docstring at
`backend/routers/webhooks.py:218-234` (the OP-715 cleanup that
deliberately moved the merger trigger duty to the daemon precisely
because the webhooks plugin had no auth support — see L-OP-713).

**Consequence**: adding a `[remote …]` `change-merged` subscription
to `project.config` would NOT activate a dormant code path; it would
double-fire the existing daemon path. The original Q2 plan (add
subscription to staging webhooks.config, observe) is therefore the
wrong intervention.

### Reframed Q2 — does the SSH-bridge path actually fire?

Static wiring is correct (5 of 5 checkpoints, 3 static + 2 deferred,
in `q2_static_check`). The unanswered question is whether the daemon
is actually running in production AND whether a real `change-merged`
event triggers the sweep AND whether the sweep then succeeds at
pushing a rebased PS. That is a live-staging exercise:

1. Operator confirms the bridge daemon process is up (systemd unit or
   container PID).
2. Operator opens a synthetic PS on a feature branch that will
   conflict with a planned `develop` merge.
3. Operator merges a sibling PS into `develop`.
4. Tail logs for `auto_rebase_sweep_*` keys at the bridge log path.
5. Confirm whether the sweeper pushed a new PS (clean rebase) or
   logged a conflict outcome (intentional left-alone for OP-720
   stale-PS scanner).

### Decision rule (encoded in `spike.q2_static_check`)

| Static wiring | Live exercise | Verdict |
|---|---|---|
| 3/3 modules wired | Both live checkpoints green | `go` (declare path production-ready) |
| 3/3 modules wired | Live checkpoints not yet run | `deferred` |
| Any module mis-wired | n/a | `no_go` (fix wiring first) |

### Result

**Verdict: `deferred (premise corrected)`** — static wiring is sound;
the live exercise has not happened.

The original `OP-733 subscription` follow-up should be renamed to
"OP-733 staging live exercise" and assigned to the operator. A
**no-op** on `project.config` (no new subscription) is the correct
action.

---

## Q3 — `ai-reviewer-webhook` auth still on broken `secret = …`

### Hypothesis (per ticket)

Per L-OP-713, webhooks plugin v3.13.5 silently ignores `secret = …`;
no `X-Gerrit-Signature` header is sent, so backend 401s every event.
Looking at `/tmp/gerrit-meta/project.config:143-151`, the
`ai-reviewer-webhook` block uses `secret = …`.

### Static evidence

Reading `/tmp/gerrit-meta/project.config:143-151`:

```ini
[remote "ai-reviewer-webhook"]
    url = https://ai.sora-dev.app/api/v1/webhooks/gerrit
    event = patchset-created
    secret = XxA42drA09qLMEkZpOvUUsJB5Bq4o-vA83GEjk8_eOk
    connectionTimeout = 5000
    socketTimeout = 5000
    maxTries = 3
    retryInterval = 30000
    sslVerify = true
```

The `secret = …` line is present; no `header = Authorization: Bearer
…` line is present. Per L-OP-713, the webhooks plugin v3.13.5's
documented `[remote …]` schema does NOT include `secret`; the plugin
silently drops it. The harness confirms this with the regex match
applied by `q3_inspect_config()`.

The handler-side docstring at `backend/routers/webhooks.py:218-234`
already acknowledges this gap and notes that "OP-715 moves the
proactive merger trigger duty to the existing stream-events SSH
daemon at `backend.agents.gerrit_jira_bridge`" — i.e. the webhook is
known-broken and the production path was migrated to SSH. For the
AI-reviewer use case, the webhook IS the path; SSH is not an option
because the reviewer needs the HTTP entry point.

For reference, the sibling `merge-conflict-webhook` block in the same
file already uses the working pattern (L-OP-708):

```ini
[remote "merge-conflict-webhook"]
    url = https://ai.sora-dev.app/api/v1/orchestrator/merge-conflict
    event = change-merge-failed
    header = Authorization: Bearer omni_F9dZ3_…
    header = X-Jira-Webhook-Secret: ji_…
```

### Decision rule

| `secret = …` line | `header = Authorization: Bearer …` line | Verdict |
|---|---|---|
| present | absent | **`no_go`** — confirmed broken per L-OP-713 |
| absent | present | `go` — already on working pattern |
| both | both | `go` — header path wins, legacy `secret` is harmless |
| neither | neither | `no_go` — unauthenticated webhook |
| block absent / config unreadable | n/a | `deferred` |

### Result

**Verdict: `no_go` (confirmed)** — the block uses `secret = …` and no
`header = Authorization: Bearer …`. The L-OP-713 hazard is live in
production config.

### Prescribed fix (out of this spike's scope; file as follow-up)

Patch `project.config:143-151` to remove `secret = …` and add
`header = Authorization: Bearer <omni_api_key>` per the
`merge-conflict-webhook` block pattern. The backend handler at
`backend/routers/webhooks.py:211-283` already accepts unauthenticated
events at this endpoint by design (the auth boundary is Caddy /
Cloudflare upstream per the OP-715 cleanup docstring), so the change
is Gerrit-side only. After patching, run the harness with a captured
live header dump fed to `q3_classify_live_headers(observed=[...],
backend_status=200)` to confirm the verdict flips to `go`.

The smoke test
(`tests/integration/test_merger_spike.py::test_q3_accepts_header_auth_fix`)
encodes this: as soon as `project.config` is patched, the harness
returns `go`. That test is the tripwire that signals the hazard is
cleared.

---

## Recommended follow-up scope

Three follow-up tickets are proposed; none of this spike's findings
authorise production changes.

1. **OP-876-followup-q1** (size S, owner: operator): run
   `python scripts/spike_merger_proactive_trigger.py --mode=live …`
   against staging Gerrit, capture 10 samples, paste the histogram +
   verdict into a new spike-supplement comment on OP-876. Block
   OP-713 Phase 2 design on the result.
2. **OP-876-followup-q2** (size S, owner: operator): live-exercise
   the OP-733 SSH-bridge path in staging. **Do NOT** add a webhook
   subscription. Confirm the daemon is running, fire a synthetic
   sibling merge, observe log + Gerrit state.
3. **OP-876-followup-q3** (size XS, owner: ops, area: devops only):
   patch `project.config` `[remote "ai-reviewer-webhook"]` to header-
   auth pattern. Re-run smoke test to verify the verdict flip.

The "polling design" alternative explicitly mentioned in the ticket
("a separate follow-up if all three unknowns close cleanly and we
still need belt-and-braces") becomes live only if Q1 returns `no_go`.
Q3 closing alone doesn't justify a new polling path — the
patchset-created path is the cheaper fix.

---

## Error catalog handling

The ticket enumerates three error states. The harness handles each as
follows:

- **`SpikeMergeableNeverReady`** — `q1_decide` returns `no_go` when
  any sample exceeds `Q1_MAX_WAIT_S = 30 s`. Operator inspection of
  Gerrit logs (`gerrit.log` + `error_log`) is the next step; this is
  a Gerrit-side regression and out of scope for OmniSight to fix.
- **`SpikeWebhookNeverArrives`** — surfaces as `q3_classify_live_
  headers(observed=[], backend_status=…)` returning `no_go`. Operator
  checks Caddy access log + Gerrit `webhooks.log` for queue
  backpressure or SSL errors.
- **`SpikeBackendConsumerSilentFailure`** — for Q2 this would show as
  the live checkpoints `live_trigger_fires_in_staging` /
  `live_sweep_pushes_ps2` failing in the live exercise; the harness
  marks them deferred until evidence arrives.

---

## Generalisable lesson candidate

The most interesting lesson is **Q2's premise correction**: the
ticket assumed OP-733 lived behind the HTTP webhook, but reading
source revealed it was migrated to SSH stream-events as part of the
OP-715 cleanup. Acting on the ticket's literal text (adding a
subscription) would have introduced a double-fire bug. The
generalisable rule: **before adding a webhook subscription for a
known feature, grep for the feature's tracking ticket in
`backend/agents/` — if a stream-events daemon already owns the path,
the webhook subscription is redundant at best and a double-fire at
worst.** A draft of this lesson should live at
`docs/sop/lessons/L-OP-876-grep-tracking-ticket-before-adding-webhook-
subscription.md` and be wired into the lessons index by
`scripts/build_lessons_index.py` per CLAUDE.md L1.

---

## Reproducibility

```bash
# Static-only (default; no network, no Gerrit creds needed)
python scripts/spike_merger_proactive_trigger.py

# Static + JSON dump for archival
python scripts/spike_merger_proactive_trigger.py \
    --json /tmp/spike-op876.json \
    --output /tmp/spike-op876.md

# Smoke test (no live Gerrit; pins the decision-rule contract)
python -m pytest tests/integration/test_merger_spike.py -v

# Live mode (operator-only, requires push creds for project)
OMNISIGHT_GERRIT_CLAUDE_HTTP_PASSWORD=… \
python scripts/spike_merger_proactive_trigger.py \
    --mode=live \
    --gerrit-url https://staging.sora.services:29420 \
    --samples 10
```

Live-mode invocation is intentionally guarded by `NotImplementedError`
in this spike — the operator wires the per-bot HTTP password before
unblocking. The script lays out the exact sequence
(`q1_run_live`'s docstring) so the wiring is a 20-line edit, not a
redesign.

---

## References

- `docs/retrospectives/2026-05-11-merge-conflict-prevention-cost-benefit.md`
  — context that motivated this spike (retrospective written same day
  as this report; if absent at lookup time treat as in-flight).
- OP-875 (META retrospective).
- `docs/sop/lessons/L-OP-713-…silent-…secret…` — the
  webhooks-plugin-v3.13.5-silently-drops-`secret` lesson (Q3
  motivation).
- `docs/sop/lessons/L-OP-708-…require-operator-signature-are-dual-g.md`
  — the `header = Authorization: Bearer …` pattern that works (Q3
  prescribed fix shape).
- OP-694 — Merger-Plus-2 conditional submit-requirement (current
  activation gate; explains why the dual-sign world starves the
  existing `change-merge-failed` trigger).
- `backend/merger_agent.py` — code under test (entry point
  `resolve_conflict`).
- `backend/merge_arbiter.py` — webhook-side dispatcher.
- `backend/agents/auto_rebase.py` — OP-733 sweeper module.
- `backend/agents/gerrit_jira_bridge.py` — SSH stream-events daemon
  that wires `_schedule_auto_rebase_sweep` to `change-merged`.
- `backend/routers/webhooks.py` — HTTP webhook handler (Q3 receiver).
- `/tmp/gerrit-meta/project.config` — Gerrit-side `[remote …]`
  declarations under audit.
