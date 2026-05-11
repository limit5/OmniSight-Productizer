# OmniSight-Productizer main-repo auto-sync (OP-837)

Sister of [`docs/operations/sora-bridge-sync.md`](sora-bridge-sync.md) (OP-798). Keeps the runner's main checkout (`/home/user/work/sora/OmniSight-Productizer/`) on `gerrit/develop` so merged fixes reach the running runner without operator intervention.

## What it does

Every 5 minutes:

1. Verify `MAIN_DIR` is a git repo (`/home/user/work/sora/OmniSight-Productizer` by default).
2. Check for dirty / untracked state. **Skip + log** if dirty (operator drafts are sacred). Opt-in `AUTO_STASH=1` to auto-`git stash --include-untracked` + ff-pull + `git stash pop`.
3. `git fetch gerrit develop`.
4. If local HEAD == remote HEAD → exit clean (most ticks land here).
5. Otherwise, `git diff` to detect which files changed. If any of the runner-code paths (auto-runner-jira.py, jira_dispatch.py, runner_workspace_safety.py, etc.) changed → flag in log so operator's tail picks it up.
6. `git pull --ff-only`. Refuse non-FF (history diverged → operator must fix).
7. After 3 consecutive failures, fire a P0 via `operator_notifier`.

## Why no service restart

The runner is a bash loop in tmux, not a systemd unit. Each tick re-reads source from disk via `python3 auto-runner-jira.py`, so a successful pull is automatically picked up by the next tick (~90 s). The script just emits structured logs; no `systemctl restart` needed.

This is the intentional difference from OP-798 (sora-bridge), where the daemon is a long-lived Python process that must be bounced after relevant-file changes.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/omnisight-main-sync.service ~/.config/systemd/user/
cp deploy/systemd/omnisight-main-sync.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omnisight-main-sync.timer
loginctl enable-linger $USER  # survive logout (one-time, idempotent)
```

## Verify

```bash
# Timer scheduled?
systemctl --user list-timers --all | grep omnisight-main-sync

# Last fire result?
journalctl --user -u omnisight-main-sync.service -n 20

# Structured log of recent decisions?
tail -n 20 ~/.local/state/omnisight-main-sync/sync.log

# Failure counter (should be 0 in steady state)?
cat ~/.local/state/omnisight-main-sync/state.json
```

## Tunables

All are env vars on the `omnisight-main-sync.service` unit:

| Var | Default | Purpose |
|---|---|---|
| `MAIN_DIR` | `/home/user/work/sora/OmniSight-Productizer` | Repo path |
| `MAIN_REMOTE` | `gerrit` | Remote name (NOT `origin` — origin is the GitHub fork) |
| `MAIN_BRANCH` | `develop` | Branch to track |
| `AUTO_STASH` | `0` | `1` = auto-stash dirty + untracked + pop after pull |
| `SYNC_LOG` | `~/.local/state/omnisight-main-sync/sync.log` | Structured log |
| `SYNC_STATE` | `~/.local/state/omnisight-main-sync/state.json` | Failure counter + last-synced SHA |
| `SYNC_DRY_RUN` | `0` | `1` = log decisions, skip pull |
| `SYNC_FAIL_ALERT_THRESHOLD` | `3` | Consecutive failures before P0 fires |

## When to override defaults

* **`AUTO_STASH=1`** — turn on if operators routinely leave draft files in main repo and want auto-preservation. **Trade-off**: stash-pop conflict still requires operator intervention (script leaves the stash + logs `stash_pop_conflict`); the auto-stash mode just shrinks the window where operator drafts block sync.
* **`SYNC_DRY_RUN=1`** — temporarily, when investigating drift. Logs what WOULD change without touching the working tree.
* **`SYNC_FAIL_ALERT_THRESHOLD`** — lower (1 or 2) on a brittle network where one fetch failure is already worth a heads-up; raise (5+) on dev hosts with flaky uplink where 3 in a row is normal.

## Failure modes + recovery

| Symptom | Diagnosis | Recovery |
|---|---|---|
| `sync_failure code=fetch_failed` repeating | Network / SSH / Gerrit unavailable | Check Gerrit reachability + `~/.config/omnisight/gerrit-claude-bot-ed25519` perms |
| `sync_failure code=pull_not_fast_forward` | Local main has commits not on develop (operator manual commit OR earlier wedge from OP-836) | `git log gerrit/develop..HEAD` to inspect; usually safe to `git reset --hard gerrit/develop` after backing up local-only commits |
| `sync_skipped_dirty` repeating | Operator left edits in main repo | Either commit/discard the edits OR opt into `AUTO_STASH=1` |
| `stash_pop_conflict` | AUTO_STASH mode + draft conflicts with pulled changes | `git stash list` shows the named stash; `git stash apply stash@{N}` + resolve manually |
| Timer not firing | `systemctl --user list-timers` shows it disabled / not scheduled | Re-run install steps; verify `loginctl show-user $USER ` has `Linger=yes` |

## Cross-references

* OP-798 — sora-bridge sync (template this script is adapted from)
* OP-836 — runner workspace safety (this sync is the *infrastructure* layer; OP-836 is the *runtime* layer)
* OP-827 — typed exceptions in ensure_change_ids (the kind of fix this sync makes reachable to the running runner)
* L-OP-827 — twin-defect post-mortem (lesson covering the wedge family)
