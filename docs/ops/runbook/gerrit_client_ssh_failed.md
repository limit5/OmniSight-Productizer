# `gerrit_client_ssh_failed`

| field | value |
|-------|-------|
| Severity | `CRITICAL` (per-code override in `configs/error_pager.yaml`) |
| Source | T3 forwarder — `scripts/journal_error_forwarder.py` (record originates in bridge Gerrit client) |
| Tier owner | bridge-maintainer + gerrit-admin |
| Parent META | OP-721 |

## What triggers it

The bridge logs `event="gerrit_client_ssh_failed"` at `ERROR` when
the SSH transport to `sora.services:29418` fails — for any of:

* SSH connection refused (port closed / sshd down);
* SSH auth failure (key revoked / wrong account);
* `gerrit query` / `gerrit review` returning non-zero with a
  transport-level error string.

T3 promotes this to `CRITICAL` because **Track C** (Gerrit→JIRA
reverse path) cannot make any forward progress while SSH is broken,
and Track C is the load-bearing path for review trail integrity.

## Severity rationale

The SSH path is the **only** way the bridge writes Code-Review
votes, posts review comments, or queries patchset state. HTTPS REST
is intentionally not wired — Gerrit-3.13's SSH is the contract.

When SSH breaks:

* AI Reviewer Bot (OP-713) cannot post +1 votes;
* AI Reviewer auto-+1 dashboard (OP-735) goes blind;
* Merger Agent (OP-269) cannot post +2 on resolved conflicts;
* The submit-rule queue stalls invisibly.

Hence `CRITICAL`, not `DEGRADED`.

## Immediate action

1. **Triage the SSH endpoint** from the bridge host:

       ssh -p 29418 -i ~/.ssh/claude-bot-ed25519 \
           claude-bot@sora.services gerrit version

   Possible outcomes:

   * `gerrit version 3.13.5` → SSH itself is fine; the bridge has
     stale credentials cached. Bounce: `systemctl --user restart
     gerrit-jira-bridge`.
   * `Permission denied (publickey)` → key revoked or rotated.
     Cross-check `docs/ops/git_credentials.md` and
     `reference_gerrit_self_hosted.md` (memory). If rotation is
     pending, this is the same failure surface as
     [`credential_expiry`](credential_expiry.md) on a `ssh_key`
     entry — T6 should have warned. If not, the inventory is stale.
   * `ssh: connect to host sora.services port 29418: Connection
     refused` → Gerrit SSH daemon is down on the host. Escalate to
     gerrit-admin per `docs/ops/dr_runbook.md` §gerrit.
   * Hangs > 30s → network path is broken; check Tailscale /
     firewall.

2. **If credentials need rotating**, follow
   `docs/operations/credential_rotation_runbook.md` §ssh_key. Wire
   the new key; restart the bridge unit; confirm by re-running
   `gerrit version`.

3. **Backfill missed Track C work.** While SSH was down, the bridge
   queued un-postable votes / comments. After recovery, the
   `proactive_merger_thread` will replay them. Tail the bridge log
   for `event="track_c_replay_started"` to confirm.

## Root-cause investigation

| `last_error` excerpt | Likely cause | Reference |
|----------------------|--------------|-----------|
| `Permission denied (publickey)` | Key revoked / rotated | `docs/operations/credential_rotation_runbook.md` |
| `Connection refused` | Gerrit sshd down | `docs/ops/dr_runbook.md` §gerrit |
| `Connection timed out` | Network / Tailscale | `docs/ops/multi-wsl-deployment.md` |
| `RemoteRepositoryException: ssh:...` (intermittent) | Sporadic packet loss | `docs/ops/observability_runbook.md` §gerrit |
| `Read from socket failed: Connection reset by peer` | Gerrit OOM / restart | gerrit-admin |

## Escalation

* Self-resolves within 5 min (transient network blip): close alert;
  log under transient-Gerrit if you want to track flap rate.
* Persists ≥15 min: escalate to gerrit-admin. Treat as a Track C
  outage; document in the on-call log.
* Persists ≥1h: this is the same failure class as the OP-689 silent
  outage. Open a postmortem
  (`docs/retrospectives/YYYY-MM-DD-track-c-outage.md`).
