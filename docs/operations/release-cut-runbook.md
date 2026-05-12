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

## 7. R5 — Staging tip-match audit (continuous-staging model)

> **Redefined by OP-975 / AUDIT-22 (2026-05-12).** The pre-AUDIT-19c R5
> contract was "deploy vX.Y.Z to staging (blue-green) before promoting
> main" — a per-release *deploy* action. Continuous staging (AUDIT-19c,
> OP-973) makes R5 an **audit step, not a deploy step**: staging always
> tracks the `develop` tip, so R5 only confirms that the tip the release
> is being cut from is the tip staging has already converged on *and*
> gated green. This section is the R5 implementation reference the
> release-conductor runbook points the R5 child at; R1 already lives in
> this file (§1–§6), so R5 lands here too.

**Applies to**: v0.5.1+ RELEASE chains. v0.5.0-rc1 R5 was operator-handled
via a synthetic-JSONL bypass (the AUDIT-17 staging gate gap) — see the
`RELEASE-v0.5.0-rc1` META retrospective note.

**Pre-condition**: AUDIT-19c (OP-973) complete — the develop→staging sync
orchestrator and its systemd units are installed and running on the
staging host. The `release/vX.Y.Z` branch has been cut (§2) from a known
`develop` tip commit; call it `$DEVELOP_TIP_SHA`.

**Action 1 — `milestone_ready` emitted for the tip:**

```bash
journalctl --user -u release-milestone-checker.service --no-pager \
  | grep -F "$DEVELOP_TIP_SHA" | grep -F milestone_ready | tail -1
# or, if the unit logs to a file: release_milestone_checker.systemd.log
```

Expect ≥1 `milestone_ready` event whose develop tip equals
`$DEVELOP_TIP_SHA`. A `milestone_blocked` line for that tip with no later
`milestone_ready` is a **FAIL** → "On fail".

**Action 2 — `release_audit` has a recent `staging_synced` row for the tip:**

```bash
set -a; . /home/user/.config/omnisight/release-audit.env; set +a
PSQL_URL="${OMNISIGHT_DATABASE_URL/+asyncpg/}"
psql "$PSQL_URL" -tAc \
  "SELECT count(*) FROM release_audit
     WHERE outcome = 'staging_synced'
       AND detail LIKE '%'||'$DEVELOP_TIP_SHA'||'%'
       AND ts > now() - interval '4 hours'"
```

Expect `>= 1`. Zero rows ⇒ staging has not converged on this tip in the
last 4h — **FAIL**.

**Action 3 — gate JSONLs are green for the tip:**

```bash
for f in canary-status.jsonl smoke-status.jsonl; do
  echo "== $f =="; grep -F "$DEVELOP_TIP_SHA" "$f" | tail -1
done
```

Each file must have a record for `$DEVELOP_TIP_SHA` with a green/passing
status. A red record — or no record for the tip — is a **FAIL**.

**On pass**: post the AC verification comment on the R5 child ticket. R5
produces **zero commits**; the `runner:no-commits-expected` label
(`docs/sop/jira-ticket-conventions.md` §14) lets the runner forward-walk
the child after a clean exit. Do **not** fabricate a placeholder commit.

**On fail**:

1. Do **not** advance the RELEASE chain — leave the R5 child blocked.
2. Revert the R5 child to To Do.
3. File an operator-alert ticket linked to the active `RELEASE-vX.Y.Z`
   META naming the failed Action and the offending tip SHA (the Q8 "8b"
   model: red staging ⇒ JIRA blocker on the active META).
