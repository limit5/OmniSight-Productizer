# Multi-Instance Runner — Bot Account Provisioning

> **Status**: Authoritative provisioning runbook (OP-783)
> **Owner**: project lead
> **Companion**: [`multi-instance-runner-runbook.md`](multi-instance-runner-runbook.md) — ops & on-call
> **Last verified**: 2026-05-08

This runbook walks the operator through provisioning ONE additional
bot account (e.g. `codex-bot-2`) so a second runner instance can be
brought up via `scripts/launch_runner_instance.sh codex-bot-2`.

The first bot (`codex-bot` / `claude-bot`) is the legacy default
instance — those credentials already exist; this doc is for adding a
**new** instance on top.

---

## When to add an instance

Add a new instance when:

1. **Throughput is bottlenecked on a single account's subscription cap.**
   Codex Plus / Pro / Business has a 5h message cap. A single instance
   running ~20 ticks/h for 5h saturates this. Two instances on the
   same bot account double consumption rate against the same cap;
   that's why per-instance accounts (each with its own subscription)
   are the correct fix.

2. **Pickup race window observed.** With ≥2 instances sharing a single
   bot, both can pass `pre_pickup_ok` on the same ticket in a 5–10s
   window before the JIRA atomic-update lands. Per-instance bots
   eliminate this structurally — JIRA's single-valued `assignee` field
   becomes the race-decider across distinct bot identities.

3. **Audit-trail attribution.** Reviewers need to identify which
   instance produced which work. Distinct bot identities show up in
   `git log --author`, Gerrit owner field, and JIRA assignee.

The 2026-05-08 30-min benchmark with 2 codex instances on a single
shared bot showed ~1.5–2× throughput vs single-instance baseline (6
merges in 31min vs ~3–4 single-instance), with zero observed race
incidents in that window. Adding a 3rd instance per the per-account
model targets ~3× steady-state.

---

## Pre-flight checklist

```text
[ ] Operator has admin access to soraapp.atlassian.net
[ ] Operator has admin access to sora.services Gerrit
[ ] Codex Plus/Pro/Business or Anthropic Pro/Max paid subscription budget
    approved (~$20–50/month for codex; $20–200/month for claude)
[ ] Email plus-addressing supported by the operator's primary inbox
    (rt3628+codex-bot-2@gmail.com routes to rt3628@gmail.com)
```

---

## Step 1 — Pick the instance ID

Bot username is derived: `<base>-<instance_id>`.

| agent_class           | base bot     | instance 2     | instance 3     |
| --------------------- | ------------ | -------------- | -------------- |
| `subscription-codex`  | `codex-bot`  | `codex-bot-2`  | `codex-bot-3`  |
| `subscription-claude` | `claude-bot` | `claude-bot-2` | `claude-bot-3` |

`instance_id="default"` is reserved for the existing pre-OP-783 bot
account — don't reuse it.

---

## Step 2 — JIRA Atlassian Cloud account

1. **Create the Atlassian account.**
   * Email: `rt3628+<bot-username>@gmail.com`
     (e.g. `rt3628+codex-bot-2@gmail.com`)
   * Display name: `<bot-username>` (e.g. `codex-bot-2`) — keep it
     literal so JIRA UI disambiguates from the legacy `codex-bot`.
   * Confirm the email via the activation link sent to your primary
     inbox (plus-addressed mail goes to `rt3628@gmail.com`).

2. **Generate a JIRA API token.**
   * Sign in as the new bot at https://id.atlassian.com/manage-profile/security/api-tokens
   * Click "Create API token", label "OmniSight runner".
   * Save the token — you'll need it for Step 5.

3. **Grant project membership.**
   * Site admin → Project `OP` → People → Add `<bot-username>` as a
     member with role `Members` (per the existing bots' permission
     scheme).

---

## Step 3 — Gerrit account

1. **Create the SSH ed25519 keypair.**

   ```bash
   ssh-keygen -t ed25519 -N "" \
       -f ~/.config/omnisight/gerrit-<bot-username>-ed25519 \
       -C "<bot-username>@omnisight"
   chmod 600 ~/.config/omnisight/gerrit-<bot-username>-ed25519
   ```

2. **Register the public key with Gerrit.**

   Sign in at `https://sora.services:29419/` as a site admin (the
   operator's `git-admin` account, not the bot itself), then:

   ```text
   * Go to: People → Browse → "+ Add" → fill:
       Username: <bot-username>            (e.g. codex-bot-2)
       Email:    rt3628+<bot-username>@gmail.com
       Full name: <bot-username>
   * Open the new account → "SSH Public Keys" → paste the contents of
     ~/.config/omnisight/gerrit-<bot-username>-ed25519.pub
   * Add to group: Registered Users (default)
   * Add to group: non-ai-reviewer  ⚠ ONLY if this bot will cast
     human-equivalent reviews. Per CLAUDE.md L1 the bot otherwise
     stays bound by the AI-reviewer +1 ceiling.
   ```

3. **Verify SSH access.**

   ```bash
   ssh -i ~/.config/omnisight/gerrit-<bot-username>-ed25519 \
       -p 29418 <bot-username>@sora.services gerrit version
   ```

   Should print the Gerrit version string.

---

## Step 4 — Codex / Claude subscription

Each bot must have its **own paid subscription** to spread the 5h cap
across instances.

### Codex (subscription-codex)

1. Sign in to https://chat.openai.com as the new bot Atlassian
   identity (use plus-addressed email + the Atlassian password you
   set; Codex auth works via OpenAI's OAuth so the operator may have
   to re-do the OpenAI signup with that same email).
2. Subscribe to Plus / Pro / Business via Settings → Billing.
3. Run `codex login` from the operator's shell while logged into the
   bot's OpenAI session in the browser; this writes the OAuth tokens
   the `codex` CLI uses.
4. Verify with `codex exec --yolo "echo hi"` that the new bot's quota
   is being consumed (check the bot's OpenAI dashboard for usage tick
   afterward).

### Claude (subscription-claude)

1. Sign in to https://claude.ai with `rt3628+<bot-username>@gmail.com`.
2. Subscribe to Pro / Max / Max 5x / Max 20x.
3. Run `claude login` from the operator's shell while logged into the
   bot's claude.ai session.
4. Verify with `claude --dangerously-skip-permissions -p "echo hi"`.

---

## Step 5 — Drop credential files

Place these files (all `chmod 600`) under `~/.config/omnisight/`:

```text
jira-<bot-username>.env         # Atlassian endpoint + email
jira-<bot-username>-token       # Atlassian API token (Step 2.2)
gerrit-<bot-username>-ed25519   # private key (Step 3.1)
gerrit-<bot-username>-ed25519.pub
```

Contents of `jira-<bot-username>.env`:

```ini
OMNISIGHT_JIRA_SITE_URL=https://soraapp.atlassian.net
OMNISIGHT_JIRA_PROJECT_KEY=OP
# Recommended for new instances: instance-agnostic key.
OMNISIGHT_JIRA_BOT_EMAIL=rt3628+<bot-username>@gmail.com
# Legacy keys (still accepted as fallback):
#   OMNISIGHT_JIRA_CLAUDE_EMAIL=...
#   OMNISIGHT_JIRA_CODEX_EMAIL=...
```

The runner's `make_client` looks for `OMNISIGHT_JIRA_BOT_EMAIL` first
and falls back to the legacy class-specific keys.

Contents of `jira-<bot-username>-token`:

```text
<paste API token from Step 2.2 — single line, no trailing whitespace>
```

---

## Step 6 — Bring up the instance

```bash
# Idempotent. Re-running is safe.
scripts/launch_runner_instance.sh codex-bot-2

# Inspect:
tmux attach -t runner-codex-bot-2
tail -f ~/work/sora/logs/runner/codex-bot-2-*.log

# Stop:
scripts/teardown_runner_instance.sh codex-bot-2
```

To run as a systemd service instead of tmux:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/runner-codex@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now runner-codex@2
loginctl enable-linger $USER
```

The `@2` part of the unit name is the instance ID — systemd injects
it as `%i` and the service file forwards it as
`OMNISIGHT_RUNNER_INSTANCE_ID=2`.

---

## Step 7 — Verify it's working

Acceptance signals:

* `tmux list-sessions` shows `runner-codex-bot-2: ...`.
* `~/work/sora/logs/runner/codex-bot-2-*.log` shows
  `[runner] agent_class=subscription-codex, instance_id=2, bot=codex-bot-2, ...`.
* JIRA OP project: tickets the new bot picks up show
  `assignee = codex-bot-2`.
* Gerrit: PSes pushed by the new bot show `owner: codex-bot-2`.
* `/tmp/runner-backpressure-codex-bot-2.state` does NOT exist while
  steady-state (only appears when the per-bot cap hits).

---

## Cost analysis

| Item                                             | Cost                                |
| ------------------------------------------------ | ----------------------------------- |
| Codex subscription per instance                  | $20–50 / month (Plus/Pro/Business)  |
| Claude subscription per instance                 | $20–200 / month (Pro/Max/Max5x/20x) |
| One-time provisioning per bot                    | ~30 min (mostly OAuth flows)        |
| Throughput gain per added instance (gross)       | ~0.8–1× single-instance baseline    |
| Steady-state ops overhead per added instance     | 0 (instance is supervised)          |

The throughput is sub-linear because a single backend / Gerrit /
JIRA / merger pipeline still serves all instances. Past 3–4 instances
the bottleneck shifts to review queue depth (operator +2 capacity) and
then to the Gerrit submit-queue — see runbook §Capacity Planning.

---

## See also

* [`multi-instance-runner-runbook.md`](multi-instance-runner-runbook.md) — daily ops, monitoring, rollback
* [`runner-strategy.md`](runner-strategy.md) — phase roadmap
* [`L-OP-783-horizontal-scaling-runner-instances.md`](../sop/lessons/L-OP-783-horizontal-scaling-runner-instances.md)
