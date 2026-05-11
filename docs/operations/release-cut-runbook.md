# Release Branch Cut Runbook

> Ticket: OP-880 (D8 - tag + release branch automation)
>
> Audience: release operator cutting a SemVer release branch and tag.

This runbook covers the mechanical release-cut step before the production
approval workflow takes over. It is intentionally separate from
`docs/operations/release-runbook.md`, which covers deploy approval,
rollback, and audit handling after a tag exists.

## 1. Pre-flight

Verify the release is ready:

```bash
git checkout main
git pull --ff-only
scripts/milestone_check.py --version vX.Y.Z
```

Confirm both remotes exist:

```bash
git remote get-url gerrit
git remote get-url origin
```

The script pushes the release branch and tag directly to both remotes,
so the operator account must have branch/tag creation permission in
Gerrit and GitHub.

## 2. Cut the Branch and Tag

Run a dry-run first:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --dry-run
```

Run the real cut:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --changelog-jira
```

The script creates `release/vX.Y.Z` from `main`, updates `CHANGELOG.md`,
asks for a manual tag approval, creates annotated tag `vX.Y.Z`, and
pushes branch plus tag to Gerrit and GitHub.

For automation where a separate approval step already captured the
operator decision, pass:

```bash
scripts/cut_release_branch.sh --version vX.Y.Z --changelog-jira --approve-tag
```

## 3. Changelog Fallback

`scripts/auto_changelog.py` tries to render tickets from the JIRA
fixVersion when `--changelog-jira` is supplied. D16 owns the richer
milestone export; until it ships, lookup failures degrade to a manual
template and the script prints `ChangelogGenFailed` in its JSON output.

When the template path is used, edit the generated section before the
release is promoted.

## 4. Duplicate Recovery

The script refuses existing release branches and tags by default:

| Error | Meaning | Recovery |
|---|---|---|
| `ReleaseBranchExists` | `release/vX.Y.Z` exists locally, in Gerrit, or in GitHub | Inspect the existing ref. If replacing it is intentional, rerun with `--override-existing`. |
| `TagAlreadyExists` | `vX.Y.Z` exists locally, in Gerrit, or in GitHub | Inspect the tag target. If replacing it is intentional, rerun with `--override-existing`. |
| `ChangelogGenFailed` | JIRA milestone export failed | The script inserts the manual template; fill it before promotion. |

Do not force-push over a release branch or tag without a human decision
recorded in the release ticket.

## 5. Rollback

If the script fails before any push, delete local scratch refs if needed:

```bash
git checkout main
git branch -D release/vX.Y.Z
git tag -d vX.Y.Z
```

If either remote push succeeded, do not delete remote refs silently.
Comment on the release ticket with the pushed refs and ask the operator
whether to keep them, replace them with `--override-existing`, or cut a
new patch/release-candidate version.

## 6. Verification

After a successful cut:

```bash
git ls-remote gerrit refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git ls-remote origin refs/heads/release/vX.Y.Z refs/tags/vX.Y.Z
git show --stat vX.Y.Z
```

The expected result is one branch ref and one tag ref on each remote,
with the tag pointing at the release branch tip.
