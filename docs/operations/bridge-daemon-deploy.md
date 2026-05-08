# sora-bridge daemon deployment

`/home/user/sora-bridge/` is a long-lived clone of OmniSight-Productizer
that hosts the `gerrit-jira-bridge.service` daemon. The clone is
deliberately separate from the runner / dev worktrees, which constantly
switch branches and reset state — sharing them would race the daemon
against every runner tick.

The trade-off is staleness: a separate clone needs its own automated
sync, otherwise the daemon silently keeps running yesterday's code.
This is exactly what happened on 2026-05-08 (the bridge was 233 commits
behind, missed OP-743's force-walk fix, stranded 8 tickets at
non-`Approved` for ~7 hours). OP-798 closes that gap.

## Components

The bridge runs under three user-scope systemd units:

1. **`gerrit-jira-bridge.service`** (long-lived) — the daemon itself.
   Owned by `deploy/systemd/gerrit-jira-bridge.service`. See
   `docs/sop/gerrit-jira-bridge.md` for daemon-specific config.
2. **`gerrit-jira-bridge-watchdog.{service,timer}`** (OP-723) — fires
   every 5 min, asks "is the daemon **alive**?". DEGRADED if its
   heartbeat log is silent for >10 min, P0 if the unit is missing.
3. **`sora-bridge-sync.{service,timer}`** (OP-798, this doc) — fires
   every 5 min, asks "is the daemon running **today's** code?".
   Fast-forwards `/home/user/sora-bridge/` to `origin/develop` and
   restarts the daemon only if a file it actually imports changed.

(2) and (3) are independent on purpose. Liveness and freshness can fail
in opposite directions: a daemon that crashes-and-restarts every 30s is
"fresh" but not "alive"; a daemon that runs steadily for 7 days is
"alive" but may be running a months-old commit. Both watchdogs need to
agree before the bridge can be trusted.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/sora-bridge-sync.service ~/.config/systemd/user/
cp deploy/systemd/sora-bridge-sync.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sora-bridge-sync.timer
loginctl enable-linger "$USER"   # if not already set for the bridge
```

The unit hard-codes `BRIDGE_DIR=/home/user/sora-bridge`. Override via
the standard `systemctl --user edit sora-bridge-sync.service` drop-in
if your install path differs (e.g. a parallel staging clone).

## Verify

Timer is active and counting down:

```bash
systemctl --user list-timers --all | grep sora-bridge-sync
```

Last firing's outcome (one JSON line per event):

```bash
tail -n 5 /home/user/work/sora/logs/bridge/sync.log
```

Expected steady-state events (one per timer firing):

* `sync_no_change` — develop already matches local HEAD. Cheapest path.
* `sync_no_restart_needed` — fast-forwarded but only docs / tests / unrelated
  backend code changed; daemon was left running.
* `sync_restarted` — fast-forwarded AND a relevant file changed; the
  daemon was bounced. `restart_count` increments here.

Failure events (each one bumps `consecutive_failures`; ≥3 fans out to
`operator_notifier` as a P0):

* `sync_failure` `code=fetch_failed` — network / Gerrit auth blip.
* `sync_failure` `code=pull_not_fast_forward` — local clone diverged
  (manual commit on the wrong branch, force-push to develop). Resolve
  manually, do **not** force the script.
* `sync_failure` `code=systemctl_restart_failed` — daemon unit broken;
  follow the gerrit-jira-bridge runbook.

Skip events (not failures):

* `sync_skipped_dirty` — operator has uncommitted edits in the bridge
  clone. The script intentionally leaves them alone. AC #4. Clean up
  the working tree (`git stash` / `git restore`) when the debug
  session is over.

## Relevant-files filter

A daemon restart costs ~5 s of stream-events downtime, so
`scripts/sync_sora_bridge.sh` only restarts when one of these exact
paths changed in the pulled commits:

* `backend/agents/gerrit_jira_bridge.py` — daemon entrypoint logic
* `backend/agents/jira_dispatch.py` — JIRA transition + comment helpers
* `backend/db.py` — DB session / engine config
* `backend/git_accounts.py` — Gerrit/JIRA cred lookup at bridge start
* `scripts/run_gerrit_jira_bridge.py` — the `ExecStart` target

Docs-only commits, frontend changes, embedded code, and unrelated
backend modules fast-forward the clone but do **not** restart the
daemon. To extend the trigger list, add the path to `RELEVANT_FILES` in
`scripts/sync_sora_bridge.sh` (PR review captures the trade-off — every
new entry buys correctness at the cost of an extra restart per touch).

## Manual recovery

If the sync script is reporting consecutive failures, run it directly
with verbose tracing:

```bash
SYNC_LOG=/dev/stderr bash -x scripts/sync_sora_bridge.sh
```

Common manual fixes:

* **Non-fast-forward pull** — someone committed locally on the bridge
  clone. `cd /home/user/sora-bridge && git status` to inspect, then
  decide whether to `git stash` (preserve) or `git reset --hard
  origin/develop` (discard). The script will never do the destructive
  reset on its own.
* **Dirty tree warnings** — operator left debug edits. Either commit
  to a throw-away branch or stash them. Once the tree is clean the
  next timer firing resumes normal sync.

## Future (Phase B, OP-798 follow-up)

Phase A (this doc) is the surgical stabiliser. Phase B will replace the
separate clone with a `Dockerfile.bridge` image built on every
`develop` merge, bringing bridge deploys into line with `Dockerfile.backend`.
At that point the timer + script in this doc become redundant and can
be retired in favour of `docker compose pull && docker compose up -d
sora-bridge`. Until then, this is the canonical sync mechanism.
