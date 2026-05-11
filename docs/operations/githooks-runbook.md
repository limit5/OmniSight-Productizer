# Git Hooks Runbook

OP-872 adds a project-tracked `post-commit` hook for interactive commits made
from the main repository checkout. Runner-created linked worktrees already use
the auto-push pipeline; the hook closes the gap for operator hotfixes, amends,
and manual commits in the main checkout.

The hook is best-effort. A notification or Gerrit push failure never blocks
`git commit`.

## Install

From the repository root:

```bash
scripts/install_githooks.sh
```

The installer marks `.githooks/post-commit` executable, sets
`core.hooksPath=.githooks`, and verifies the configured hook path. Run
`git config --get core.hooksPath` to confirm installation.

## Notification Behavior

On every successful `git commit` in the main repository working tree, the hook
sends an operator notification through the OP-721 bridge. It uses existing
`backend.agents.operator_notifier` Slack and email channel implementations.

Linked worktrees are ignored. If `git rev-parse --git-dir` and
`git rev-parse --git-common-dir` differ, the hook exits without notifying or
pushing.

If OP-721 is unavailable or no Slack/email channel is configured, the hook logs:

```text
[omnisight-post-commit] PostCommitHookMissingNotificationBridge: OP-721 bridge unavailable
```

## Gerrit Push

Automatic Gerrit push is opt-in per commit. Add this trailer when creating or
amending the commit:

```text
Push-To-Gerrit: yes
```

When present, the hook runs:

```bash
git push "${OMNISIGHT_POST_COMMIT_GERRIT_REMOTE:-gerrit}" "HEAD:refs/for/<branch>"
```

When the trailer is absent, no push is attempted.

If Gerrit rejects the push or SSH auth fails, the hook logs:

```text
[omnisight-post-commit] PostCommitGerritPushFailed: push failed; details will be included with the next main-repo commit notification
```

The last 20 lines of push output are stored as
`omnisight-post-commit-push-failed` in the git dir and included in the next
main-repo commit notification.

## Dry Run

Use dry-run logs to verify behavior without sending real notifications or
pushing to Gerrit:

```bash
tmpdir="$(mktemp -d)"
notify_log="$tmpdir/notify.log"
push_log="$tmpdir/push.log"

OMNISIGHT_POST_COMMIT_NOTIFY_LOG="$notify_log" \
OMNISIGHT_POST_COMMIT_PUSH_LOG="$push_log" \
git commit --allow-empty -m "dry run"
```

Expected result: `notify.log` receives `MainRepoInteractiveCommit`; `push.log`
is not created unless the commit message includes `Push-To-Gerrit: yes`.

To verify the opt-in push path:

```bash
OMNISIGHT_POST_COMMIT_NOTIFY_LOG="$notify_log" \
OMNISIGHT_POST_COMMIT_PUSH_LOG="$push_log" \
git commit --allow-empty -m "$(cat <<'MSG'
dry run with push

Push-To-Gerrit: yes
MSG
)"
```

Expected result: `push.log` contains `HEAD:refs/for/<branch>`.

## Worktree Check

To confirm linked worktrees do not emit notifications:

```bash
main_repo="$(pwd)"
tmpdir="$(mktemp -d)"
git worktree add "$tmpdir/wt" HEAD
(
  cd "$tmpdir/wt"
  git config core.hooksPath "$main_repo/.githooks"
  OMNISIGHT_POST_COMMIT_NOTIFY_LOG="$tmpdir/worktree-notify.log" \
    git commit --allow-empty -m "worktree dry run"
)
test ! -e "$tmpdir/worktree-notify.log"
git -C "$main_repo" worktree remove --force "$tmpdir/wt"
```

## Troubleshooting

`PostCommitHookMissingNotificationBridge`

: OP-721 is not importable, or Slack/email channels are not configured.
  Configure `OMNISIGHT_NOTIFIER_SLACK_WEBHOOK` or the SMTP environment variables
  before expecting live delivery.

`PostCommitGerritPushFailed`

: Gerrit SSH auth, remote name, branch permissions, or conflict rejected the
  opt-in push. Inspect the next notification for stored output, or run the same
  `git push` command manually for full stderr.
