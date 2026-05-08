# Multi-Instance Runner — Operations Runbook

> **Status**: Authoritative ops + on-call runbook (OP-783)
> **Owner**: project lead
> **Companion**: [`multi-instance-runner-provisioning.md`](multi-instance-runner-provisioning.md)
> **Last verified**: 2026-05-08

This doc covers daily ops for an N-instance runner fleet:
launch / teardown, monitoring, capacity planning, rollback to single-
instance, and incident response. The provisioning runbook covers
adding a new bot account end-to-end.

---

## Architecture at a glance

```
                 ┌────────────────────────────────────────────┐
                 │ Single host (WSL / Ubuntu)                 │
                 │                                            │
   tmux/systemd  │   runner-codex-bot           │  pickup     │
       ─►        │      ↓ OMNISIGHT_RUNNER_*    │  scope:     │
                 │   auto-runner-jira.py        │  • JIRA     │
                 │      reads creds keyed on    │     assignee│
                 │      (agent_class, INSTANCE) │     = bot   │
                 │      ─ jira-<bot>.env        │  • Gerrit   │
                 │      ─ gerrit-<bot>-ed25519  │     owner   │
                 │      ─ idem-keys-<bot>.db    │     = bot   │
                 │      ─ runner-backpressure-  │  • backpressure
                 │           <bot>.state        │     cap = 8 │
                 │                              │     per bot │
                 │   runner-codex-bot-2         │             │
                 │   runner-claude-bot-3        │             │
                 │   ...                        │             │
                 └────────────────────────────────────────────┘
                            │                │
                  Gerrit ssh│         JIRA REST│
                  to sora.services    soraapp.atlassian.net
```

Each instance is a fully isolated unit: separate JIRA identity (so
the assignee field decides pickup races), separate Gerrit owner (so
review attribution is clean), separate quota (so one bot's cap doesn't
pause its siblings), separate worktree (so commits don't collide
mid-flight), separate idempotency DB (so JIRA mutations don't dedup
across instances against shared rows).

---

## Daily ops

### Launch

```bash
# Tmux-based (interactive, attachable):
scripts/launch_runner_instance.sh codex-bot-2
scripts/launch_runner_instance.sh claude-bot-3

# systemd-based (persistent, supervised):
systemctl --user enable --now runner-codex@2
systemctl --user enable --now runner-claude@3
```

Both forms are idempotent. Re-running re-establishes the worktree git
identity + commit-msg hook without spawning duplicates.

### Teardown

```bash
scripts/teardown_runner_instance.sh codex-bot-2
# or
systemctl --user stop runner-codex@2
systemctl --user disable runner-codex@2
```

Teardown does NOT delete the worktree — it's preserved so the operator
can salvage in-flight commits via the runner's
`backend.agents.orphan_salvage` flow on next launch. Manual cleanup:

```bash
git worktree remove ../OmniSight-codex-bot-2-worktree
```

### Inspection

```bash
# What's running?
tmux list-sessions | grep runner-
systemctl --user list-units 'runner-*'

# Live log for one instance:
tail -f ~/work/sora/logs/runner/codex-bot-2-*.log

# Recent JIRA pickups by this instance (last 24h):
# (set CRED env to the BOT's creds, not the operator's)
JIRA_BOT_EMAIL=rt3628+codex-bot-2@gmail.com \
JIRA_TOKEN=$(cat ~/.config/omnisight/jira-codex-bot-2-token) \
curl -s -u "$JIRA_BOT_EMAIL:$JIRA_TOKEN" \
  "https://soraapp.atlassian.net/rest/api/3/search/jql" \
  -H 'Content-Type: application/json' \
  -d '{"jql":"assignee = currentUser() AND updated >= -24h","fields":["summary","status"]}'

# Open Gerrit PSes owned by this instance:
ssh -i ~/.config/omnisight/gerrit-codex-bot-2-ed25519 \
    -p 29418 codex-bot-2@sora.services \
    gerrit query --format=JSON 'is:open owner:codex-bot-2'
```

---

## Monitoring + alerting

### Per-instance backpressure latch

Each instance has its own state file:

```text
/tmp/runner-backpressure-codex-bot.state          # legacy default
/tmp/runner-backpressure-codex-bot-2.state        # instance 2
/tmp/runner-backpressure-claude-bot-3.state       # instance 3
```

Existence of the file = that instance is paused (>= cap=8 open PSes).
Absence = active. The operator-notifier (`backend.agents.operator_notifier`)
fires once per pause edge — re-pauses on the same instance don't spam.

### Health probe

A trivial up/down probe per instance:

```bash
for bot in codex-bot codex-bot-2 claude-bot claude-bot-2; do
  if tmux has-session -t "runner-$bot" 2>/dev/null \
     || systemctl --user is-active "runner-$(echo $bot | sed -E 's/(codex|claude)-bot(-([0-9]+))?/\1@\3/')" >/dev/null 2>&1; then
    echo "  $bot: up"
  else
    echo "  $bot: DOWN"
  fi
done
```

### Throughput tracking

Daily merge counts per bot:

```sql
-- against the gerrit_jira_bridge state DB (production):
SELECT
  jsonb_extract_path_text(payload::jsonb, 'owner_username') AS bot,
  date_trunc('day', merged_at) AS day,
  count(*) AS merges
FROM bridge_change_events
WHERE event_type = 'change-merged'
  AND merged_at >= now() - interval '7 days'
GROUP BY bot, day
ORDER BY day DESC, bot;
```

Expected ~3× single-instance throughput at 3 instances; any single bot
producing < 30% of the fleet median for >2 days is a triage signal
(check that bot's quota / log).

---

## Capacity planning

| Instance count | Expected merges/day | Bottleneck shift                |
| -------------- | ------------------- | ------------------------------- |
| 1 (default)    | ~25                 | bot subscription cap            |
| 2              | ~40–50              | pickup race window (mostly past) |
| 3              | ~60–75              | reviewer +2 capacity            |
| 4              | ~70–80              | Gerrit submit queue             |
| 5+             | diminishing return  | review queue depth + ops effort |

Past 3 instances, throughput improvements are sub-linear because the
human reviewer can only +2 so many PSes per day. Adding an AI-reviewer
auto-+1 path (separate from this ticket) is the next lever.

---

## Rollback to single-instance

If a multi-instance setup goes wrong (e.g., the per-instance bot
account gets quota-suspended or accumulates a runaway Change-Id
collision), back out cleanly:

```bash
# 1. Stop the misbehaving instance.
scripts/teardown_runner_instance.sh codex-bot-2
# or: systemctl --user stop runner-codex@2 && systemctl --user disable runner-codex@2

# 2. Confirm the legacy default-instance runner is still up:
tmux list-sessions | grep -E 'runner-codex-bot$|runner-claude-bot$'

# 3. (Optional) garbage-collect the per-instance worktree:
git worktree remove ../OmniSight-codex-bot-2-worktree --force

# 4. Re-park any in-flight tickets the dead instance was holding.
#    They'll auto-recover next tick because pre_pickup_ok skips
#    tickets whose assignee != the picking bot.
```

The legacy default instance keeps working because its credentials,
state files, and worktree are byte-identical to pre-OP-783 — no
migration required.

---

## Incident response

### Symptom: two instances picked the same ticket

**Don't see this at all** if instances have distinct bot accounts —
JIRA atomic-update on `assignee` makes pickup mutex-exclusive. If
seen, the cause is one of:

1. Both instances accidentally configured for the same `INSTANCE_ID`
   (creds files collide; check `~/.config/omnisight/`).
2. JIRA workflow changed and the assignee field is no longer
   single-valued (verify in Atlassian admin).
3. The pre-OP-783 shared-quota setup is still active (one bot, two
   processes); migrate to per-instance bots per the provisioning
   runbook.

### Symptom: instance keeps hitting backpressure pause

Per-bot backpressure cap is 8 (configurable via
`OMNISIGHT_RUNNER_PS_CAP`). Hitting it means review queue is full for
this bot's PSes — operator needs to +2 / merge / abandon enough PSes
to drop below floor=4.

```bash
# What's stuck for codex-bot-2?
ssh -i ~/.config/omnisight/gerrit-codex-bot-2-ed25519 \
    -p 29418 codex-bot-2@sora.services \
    gerrit query --format=JSON \
    'is:open owner:codex-bot-2' | head -20
```

### Symptom: instance's credentials expired

JIRA API tokens last 1 year per Atlassian default; rotate per
[`credential_rotation_runbook.md`](credential_rotation_runbook.md).
Codex/Claude OAuth tokens expire on browser session changes; re-run
`codex login` / `claude login` while signed into the bot's session.

---

## Migration path: from 1 → N instances

```text
[ ] Day 0: existing single-instance setup verified working
            (codex-bot OR claude-bot tmux/systemd healthy).
[ ] Day 0: provision codex-bot-2 per provisioning runbook.
[ ] Day 1: launch codex-bot-2 in tmux, observe for 30min:
            - bot picks up tickets
            - PSes push under codex-bot-2 owner
            - no JIRA dedup of comments across the two instances
[ ] Day 1: if healthy, leave running 24h. If misbehaving, teardown
            per "Rollback to single-instance" above.
[ ] Day 2: provision claude-bot-2 (or codex-bot-3) per runbook.
[ ] Day 7: convert tmux to systemd for persistence.
[ ] Day 14: add the next instance only after measuring whether
             throughput grew sub-linearly — past 3 instances the
             review queue caps gains.
```

---

## See also

* [`multi-instance-runner-provisioning.md`](multi-instance-runner-provisioning.md) — bot creation
* [`runner-strategy.md`](runner-strategy.md) — phase roadmap & ROI math
* [`credential_rotation_runbook.md`](credential_rotation_runbook.md) — token rotation
* `backend/agents/jira_dispatch.py` — `resolve_bot_username`, `backpressure_decide` source of truth
* `auto-runner-jira.py` — runner entrypoint, reads `OMNISIGHT_RUNNER_INSTANCE_ID`
