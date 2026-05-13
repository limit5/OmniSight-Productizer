# Orphan reaper runbook (OP-1059)

The C1 orphan reaper recovers JIRA tickets that a runner host claimed
but never finished — typically because the host died, was rebooted, or
its runner was `kill -9`'d mid-pickup. Without the reaper, those tickets
stay In Progress with a `claim:{instance}:{token}` label forever, and
the underlying work blocks any same-instance peer that would otherwise
pick it up next.

The reaper is a single-shot systemd job. The timer fires every ten
minutes; each fire imports `backend.agents.runner_orphan_reaper` and
runs one classify-then-cleanup cycle.

## What it does on each cycle

1. Pulls every In-Progress JIRA ticket in the project (English and
   Japanese-locale status names both included).
2. Filters locally to issues carrying a `claim:*` label. JIRA does not
   support label wildcards, so this filter runs in Python on the
   payload returned by `POST /search/jql`.
3. For each ticket, parses the fenced `claim:{instance}:{epoch_us}-{uuid8}`
   labels and computes the oldest token's age. Skips the ticket when
   every token is younger than `OMNISIGHT_RUNNER_CLAIM_TTL_SEC`
   (default 3600 seconds — twice the standard runner CLI hard timeout).
4. Cross-checks `/tmp/runner-pickup/{INSTANCE}/{EPOCH_US}-{TICKET}/heartbeat`.
   The runner writes JSON `{ "pid": <int>, ... }` to this path at
   pickup. The reaper treats *any* of `missing file`, `malformed JSON`,
   `missing pid`, `dead pid` as orphaned. A live PID (probed via
   `os.kill(pid, 0)`) means the runner is just slow — leave it alone.
5. For declared orphans:
   - Posts a `[reaper-detected-orphan]` JIRA comment with the
     instance id, token, age, and the heartbeat probe's `reason`.
   - Calls `backend.agents.jira_dispatch.release_ticket_claim()` to
     strip every `claim:{instance}:*` label for that instance.
   - Clears the `assignee` field.
   - Transitions the ticket In Progress → To Do.
   - Emits an `operator_notifier.notify(Severity.WARN,
     "runner_orphan_reaped", ...)` so the alert lands in the on-call
     channels per the standard severity matrix.

Every step is best-effort: a JIRA transport hiccup on (say) the comment
post does not block the label strip. A partial reap still leaves the
ticket in a strictly better state, and the next ten-minute cycle
picks up where this one left off.

## Install (per host)

User-level units — share JIRA credentials with the runner.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/runner-orphan-reaper.service ~/.config/systemd/user/
cp deploy/systemd/runner-orphan-reaper.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now runner-orphan-reaper.timer
```

Log destination: `~/work/sora/logs/runner-orphan-reaper/run.log`.
The unit appends to that file; rotate via your existing journald /
logrotate setup.

## Configuration

All knobs are environment variables. Set them in
`~/.config/omnisight/runner-orphan-reaper.env` (the systemd unit pulls
it via `EnvironmentFile=-`, so the file is optional).

| Variable | Default | Effect |
| --- | --- | --- |
| `OMNISIGHT_RUNNER_REAPER_ENABLED` | `1` | Set to `0` / `false` / `no` / `off` to short-circuit the cycle. The timer keeps firing; each fire exits 0 without touching JIRA. |
| `OMNISIGHT_RUNNER_CLAIM_TTL_SEC` | `3600` | Minimum claim age in seconds before a ticket is eligible for reaping. Values that fail `int()` or are `<= 0` fall back to the default. |
| `OMNISIGHT_REAPER_AGENT_CLASS` | `subscription-claude` | Selects which `~/.config/omnisight/jira-*.env` credential file the reaper authenticates with. |

The reaper also accepts `--ttl-seconds`, `--agent-class`, and
`--dry-run` on the CLI for ad-hoc operator runs.

## Operator playbook

### A reap fired — what now?

`runner_orphan_reaped` alerts include the ticket key, the claim age,
the instance id, and the heartbeat probe's `reason` string. Workflow:

1. Inspect the ticket — confirm the `[reaper-detected-orphan]`
   comment is present and the assignee + claim labels are gone.
2. Check the `reason` field:
   - `heartbeat-file-absent` — host probably rebooted; nothing to do
     beyond letting the runner repickup the ticket from To Do.
   - `heartbeat-pid-dead` — runner process crashed. Check
     `~/work/sora/logs/auto-runner-*/run.log` around the timestamp
     in the alert for the failure mode that killed it.
   - `heartbeat-malformed-json` / `heartbeat-missing-pid` — bug in
     the runner's heartbeat writer; file a ticket against the runner
     pickup code.
3. If the ticket should *not* repickup automatically (e.g. the work
   was actually completed but the runner crashed before transitioning),
   move it to the appropriate status manually.

### Dry-run an ad-hoc check

```sh
cd ~/work/sora/OmniSight-claude-worktree
python3 -m backend.agents.runner_orphan_reaper --dry-run
```

Logs the classification result for every In-Progress ticket carrying a
claim label, but writes nothing to JIRA.

### Disable the reaper temporarily

```sh
echo 'OMNISIGHT_RUNNER_REAPER_ENABLED=0' >> ~/.config/omnisight/runner-orphan-reaper.env
systemctl --user restart runner-orphan-reaper.service
```

Re-enable by removing or flipping that line and restarting the service.

### Stop the timer entirely

```sh
systemctl --user disable --now runner-orphan-reaper.timer
```

Use this only when a deploy or rehearsal requires the In-Progress set
to remain frozen.

## Failure modes the reaper does NOT cover

- **Cross-host orphan claims** — per the OP-783 one-instance-per-host
  invariant, the heartbeat PID probe is local-only. A claim left
  behind by a runner on a different host will age out on the next
  same-instance claimer's stale-sweep, not by this reaper.
- **Pre-AUDIT-24 bare `claim:{instance}` labels** — those carry no
  epoch prefix, so the reaper has no age signal. They are GC'd on the
  next claim's pre-GET sweep per `release_ticket_claim` semantics.
- **Tickets stuck without a claim label** — different failure mode
  (probably a botched transition); the reaper deliberately ignores
  these so it cannot accidentally revert a legitimate operator-driven
  ticket.
- **Snapshot / durability recovery** — SP-B-X-002a is the separate
  ticket family for that.

## Related design / code

- Design doc: SP-B-X-002 §C1 (lines 543–559).
- Mutex it relies on: `backend/agents/jira_dispatch.py`
  `release_ticket_claim()` (the reaper imports it — does **not**
  duplicate the label-strip logic).
- Alert plumbing: `backend/agents/operator_notifier.py`
  `notify(Severity.WARN, ...)`.
- Runner pickup mutex (sibling subsystem):
  `docs/sop/runner-pickup-mutex.md`.
