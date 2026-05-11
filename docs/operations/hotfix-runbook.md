# Hotfix Runbook — D14 (OP-886)

**Owner:** Deploy on-call · **Spec:** META OP-761 §Phase 4 · **Ticket:** OP-886

This runbook covers the emergency production path for a single merged
change that must ship before the next normal release. The path is
deliberately narrower than the release train: cut one `hotfix/vX.Y.Z+1`
branch from `main`, cherry-pick one Gerrit change, require one human
`+2` and a smoke pass, then deploy immediately. D1 milestone acceptance
is skipped for this path.

## 0. Preconditions

| Check | Command / location |
| --- | --- |
| Change is approved for emergency release | Gerrit change has one human `Code-Review +2` |
| Target production branch exists | `git rev-parse --verify main^{commit}` |
| Current production line has a SemVer tag | `git tag --list 'v*.*.*' --merged main` |
| Production smoke command is known | usually `scripts/prod_smoke_test.py https://<prod-host>` |
| Backport owner is assigned | same incident ticket, before the production deploy starts |

The hotfix branch name is derived from the latest SemVer tag reachable
from `main`. If `main` is at `v1.4.2`, the script cuts
`hotfix/v1.4.3`.

## 1. Cut the hotfix branch

Run from a clean worktree:

```bash
scripts/hotfix_cut.sh \
  --from-change <gerrit-change-number> \
  --target main \
  --cut-only
```

For local drills, `--from-change` also accepts a commit SHA. For Gerrit
changes, the script fetches the latest patch set ref from the configured
remote (`gerrit` by default), checks out `hotfix/vX.Y.Z+1` from `main`,
and runs:

```bash
git cherry-pick -x <change-commit>
```

The script is idempotent at the branch level: if the hotfix branch
already exists it checks it out, then skips the cherry-pick when the
change is already reachable or already recorded by a cherry-pick
trailer. Do not force-push or rewrite an already reviewed hotfix branch.

## 2. Conflict path: `HotfixCherryPickConflict`

If the cherry-pick conflicts, `scripts/hotfix_cut.sh` aborts the
cherry-pick, refuses the workflow, and emits a structured event:

```json
{"event":"hotfix.refused","code":"HotfixCherryPickConflict",...}
```

Required operator recovery:

1. File a manual-merge JIRA ticket linked to the incident and the
   Gerrit change.
2. Assign the merge to a human release owner.
3. Keep the original incident ticket in TODO until the merge ticket is
   complete.
4. Retry this runbook from §1 after the manual merge lands.

The automation intentionally does not create that JIRA issue itself
because runner-governed JIRA writes are restricted to the shared
`jira_dispatch` helpers.

## 3. Accelerated gate

After the branch is cut, run the full accelerated path:

```bash
scripts/hotfix_cut.sh \
  --from-change <gerrit-change-number> \
  --target main \
  --human-plus2 <reviewer-login> \
  --smoke-command 'scripts/prod_smoke_test.py https://<prod-host>' \
  --deploy-command 'scripts/deploy-prod.sh --branch hotfix/vX.Y.Z+1'
```

Gate semantics:

| Gate | Hotfix rule |
| --- | --- |
| D1 milestone acceptance | Skipped |
| Human approval | Exactly one attending human `+2` is required |
| Smoke | Required before production promotion |
| Production deploy | Starts immediately after approval + smoke |

The script prints an approval timestamp and a UTC deploy deadline
30 minutes later. Operators should page if deploy has not started by
that deadline.

## 4. Smoke failure: `HotfixSmokeFailed`

If the smoke command exits non-zero, the script refuses promotion and
emits:

```json
{"event":"hotfix.refused","code":"HotfixSmokeFailed",...}
```

Required operator recovery:

1. Do not deploy the hotfix branch.
2. Alert the incident channel with the smoke command and exit code.
3. Attach the smoke log to the incident ticket.
4. Fix forward on the hotfix branch or abandon it and restart from §1.

## 5. Production deploy

The default deploy command is:

```bash
scripts/deploy-prod.sh --branch hotfix/vX.Y.Z+1
```

If production policy requires deploying a signed tag instead, pass an
explicit `--deploy-command` that invokes `scripts/deploy-prod.sh --tag
vX.Y.Z+1` after the tag is created and signed by the release owner.

Rollback follows `docs/operations/prod-deploy-runbook.md`: restore the
previous image/tag, verify `/readyz`, and keep the incident open until
the failed hotfix branch is either fixed or deleted.

## 6. Mandatory develop backport

Every production hotfix must be cherry-picked back to `develop`.
CI should run:

```bash
scripts/hotfix_backport_check.py \
  --hotfix-ref hotfix/vX.Y.Z+1 \
  --develop-ref develop \
  --base-ref main
```

The gate compares Git patch identity rather than commit SHA, so a normal
`git cherry-pick -x` onto `develop` satisfies it. A missing patch exits
non-zero with `HotfixBackportMissing`.

## 7. Synthetic incident drill

Use a temporary clone or scratch repo:

1. Create `main`, `develop`, and a SemVer tag such as `v0.3.0`.
2. Commit a one-line fix on a side branch.
3. Run `scripts/hotfix_cut.sh --from-change <sha> --target main
   --human-plus2 <name> --smoke-command true --deploy-command true`.
4. Cherry-pick the hotfix commit to `develop`.
5. Run `scripts/hotfix_backport_check.py --hotfix-ref hotfix/v0.3.1`.

The expected end state is a hotfix branch containing exactly the
cherry-picked fix, a successful smoke gate, a deploy command invocation,
and a passing develop backport check.
