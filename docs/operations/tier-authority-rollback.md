# Tier-Authority Rollback Runbook

> **Created**: 2026-05-09
> **Owner**: G4 (META OP-802) — Phase 3 governance migration. Operational companion to ADR-0005 (`docs/adr/ADR-0005-tier-authority-levels.md`) and the install runbook (`deploy/gerrit/install-tier-submit-requirements.md`).
> **Status**: OP-806 — runbook landed alongside the install procedure.
> **Audience**: Gerrit admin / on-call ops with `refs/meta/config` push rights on `omnisight/OmniSight-Productizer` (sora.services:29418). Page on call-out **immediately** if the symptoms in §0 manifest — a misconfigured Tier rule can lock every future submit on the project.
> **Scope**: Procedures to revert OP-806's `refs/meta/config` change, neutralize the Tier label without a full revert, and recover from group-membership mistakes that break the per-tier submit-requirements.
>
> **Out of scope**: Restoring lost Gerrit DB state (see `docs/operations/disaster-recovery.md`); rotating Gerrit admin credentials (see `docs/operations/credential_rotation_runbook.md`); rolling back ACL changes outside the Tier blocks (the OP-806 patch does not touch ACLs).

---

## 0. When to use this runbook

Trigger any one of the following:

| Symptom | Severity | First lever |
|---|---|---|
| Gerrit UI shows "Cannot evaluate submit-requirement Tier-* — query syntax error" on every change | **P0** | §3 emergency revert |
| Every Tier S patchset blocks on Human-Plus-2 even after AI +2 (i.e. relaxation didn't take effect) | P1 | §4.1 verify Human-Plus-2 applicableIf actually deployed |
| Tier S submits with no votes at all (over-relaxed) | **P0** | §3 emergency revert |
| `architecture-reviewer` group missing → every Tier X submit `UNSATISFIED` even with operator +2 | P2 | §4.2 group-membership recovery |
| Path-classifier hook (G3) ships before this runbook is read and a flood of changes get the wrong tier | P2 | §4.3 mass mis-tier |

If unsure, **escalate before reverting**. The submit-rule change is push-once and a chained "wrong → right → right-again" sequence on `refs/meta/config` leaves a confusing history. Reverting cleanly is cheap; over-correcting is not.

---

## 1. Pre-flight before any rollback action

Confirm in this order:

1. **Identify the bad commit on `refs/meta/config`.** Run:
    ```sh
    git fetch origin refs/meta/config:refs/meta/config
    git log refs/meta/config --oneline -10
    ```
    The OP-806 commit subject is `[OP-806] Tier label + Tier-S/M/L/X submit-requirements (prod)`. If you see chained subsequent commits on `refs/meta/config` (e.g. someone pushed a corrective patch on top), revert all of them in reverse order — never `git reset --hard` on prod `refs/meta/config`.
2. **Quiesce in-flight submits.** Operator should pause the submit-queue worker (per `docs/operations/release-runbook.md`) so no half-evaluated change submits during the rollback window. The queue is paused with:
    ```sh
    ssh -p 29418 sora.services gerrit set-project-state --state read-only omnisight/OmniSight-Productizer
    ```
    Reverse with `--state active` after the rollback verifies green.
3. **Snapshot the current bad state** for forensic analysis:
    ```sh
    curl -s -u "$GERRIT_AUTH" \
        "$GERRIT_HOST/a/projects/omnisight%2FOmniSight-Productizer/config" \
        | sed '1d' \
        | tee ~/tier-rollback-evidence/bad-state-$(date -u +%Y%m%dT%H%M%SZ).json
    ```
    This is the input to the post-incident retrospective; do not skip it under time pressure.
4. **Notify** in the ops channel: "OP-806 rollback in progress — pause merges on `omnisight/OmniSight-Productizer`."

---

## 2. Lever menu

| Lever | What it changes | Reversible? | Time | When to use |
|---|---|---|---|---|
| §3 — `git revert` the OP-806 commit | Drops the Tier label + 4 Tier blocks, restores Human-Plus-2 to no-applicableIf | yes — reapply the patch | 1–2 min | **catastrophic** mis-config; submit broken globally |
| §4.1 — surgical re-push (correct one block, leave others) | Edits a single submit-requirement that has a query syntax error | yes — revert again | 5 min | known-good blocks + one bad one (e.g. typo in `applicableIf`) |
| §4.2 — group recovery (no `refs/meta/config` change) | Adds missing member or creates missing group | yes — `gerrit set-members --remove` | < 2 min | Tier X submits stuck because architecture-reviewer is empty |
| §4.3 — temporarily make the bad block inert | Sets `applicableIf = is:false` on the offending block | yes — flip back | 2 min | one block misbehaves; want to keep the others live while fixing |
| §5 — full project lockdown | `gerrit set-project-state --state read-only` | yes — flip back to active | < 30 s | suspected authorization gap; nothing should merge until investigated |

**Default lever** when symptoms in §0's P0 row trigger: §3 full revert. The cleanup (§4.1, §4.3) is for cases where the operator already understands the failure mode and wants to surgically fix one block.

---

## 3. Emergency full revert

This is the safe rollback. It restores the prod project.config to the OP-697 baseline (i.e. before OP-806 was applied).

### 3.1 Revert commit

```sh
git fetch origin refs/meta/config:refs/meta/config
git checkout refs/meta/config

# Identify the commit subject — typically HEAD on refs/meta/config:
git log -1 --pretty=format:'%H %s' refs/meta/config

# Create a revert commit. NEVER force-push refs/meta/config on prod —
# `git revert` writes a NEW commit that undoes the previous one and
# preserves history.
git revert --no-edit HEAD

git push origin HEAD:refs/meta/config
```

Expected push output: a single new commit titled `Revert "[OP-806] Tier label + Tier-S/M/L/X submit-requirements (prod)"`. Gerrit re-parses the config immediately on push completion.

If the bad state was the result of multiple chained pushes on `refs/meta/config` (rare but possible), `git revert HEAD~2..HEAD` (replace `2` with the actual count) chains revert commits in the correct order. Do not coalesce them with `--squash` — a one-revert-per-bad-commit history is the audit-trail invariant.

### 3.2 Confirm prod state recovered

```sh
curl -s -u "$GERRIT_AUTH" \
    "$GERRIT_HOST/a/projects/omnisight%2FOmniSight-Productizer/config" \
    | sed '1d' | jq '.labels.Tier // "absent", .submit_requirements | keys'
```

Expected after revert:

- `.labels.Tier` → string `"absent"` (no Tier label)
- `.submit_requirements | keys` → `["Human-Plus-2", "Merger-Plus-2", "No-Veto", "Verified"]` only

If the keys list still contains any `"Tier-*"` entry, the revert did not propagate. Check that the push actually landed (`git log origin/refs/meta/config -3`) and re-attempt.

### 3.3 Re-enable submits

```sh
ssh -p 29418 sora.services gerrit set-project-state --state active omnisight/OmniSight-Productizer
```

Spot-check by looking at any open change's submittability — Human-Plus-2 / No-Veto should be SATISFIED for any change with the appropriate votes; the four Tier-* rules should no longer appear.

### 3.4 Post-revert

- Move OP-806 back to `In Progress` via `transition_back_to_todo` in `backend/agents/jira_dispatch.py` with the failure root cause in the comment.
- Open a META retrospective at `docs/retrospectives/YYYY-MM-DD-op806-rollback.md` if downtime exceeded 30 minutes — link from a META ticket with label `meta:retrospective` per CLAUDE.md L1.
- Check `docs/sop/architecture-anti-patterns.md` for an existing match before authoring a new lessons-learned entry.

---

## 4. Surgical recovery procedures

Use these when you understand the failure mode and want to keep most of OP-806 live.

### 4.1 Wrong / typo'd `applicableIf` on one block

If only one block (e.g. Tier-X-Architecture-Review) has a query syntax error, editing the project.config and pushing a one-block correction is faster and lower-risk than a full revert.

```sh
git checkout refs/meta/config
$EDITOR project.config
# Fix the offending [submit-requirement "..."] block in place.
# DO NOT delete other Tier-* blocks — they are independent.

git diff project.config   # eyeball before commit
git commit -s -am "[OP-806] hotfix: correct applicableIf in Tier-X-Architecture-Review"
git push origin HEAD:refs/meta/config
```

Then re-run the §3.2 verification. If the offending block was Human-Plus-2's applicableIf (the only existing block this patch modified), the same surgical fix applies.

### 4.2 Group-membership recovery

This requires NO `refs/meta/config` change — group membership is server-side state.

```sh
# Missing architecture-reviewer:
ssh -p 29418 sora.services gerrit create-group architecture-reviewer \
    --visible-to-all \
    --description "ADR-0005 Tier X reviewers"
ssh -p 29418 sora.services gerrit set-members architecture-reviewer --add sora

# Empty ai-reviewer-bots (claude-bot / codex-bot dropped during rotation):
ssh -p 29418 sora.services gerrit set-members ai-reviewer-bots --add claude-bot
ssh -p 29418 sora.services gerrit set-members ai-reviewer-bots --add codex-bot

# Verify:
ssh -p 29418 sora.services gerrit ls-members ai-reviewer-bots
ssh -p 29418 sora.services gerrit ls-members architecture-reviewer
```

The submit-requirement evaluator picks up group-membership changes on the next change re-evaluation (which Gerrit triggers on the next vote / refresh; it can be forced via `gerrit review --label Tier=l <change>,1` as a no-op nudge).

### 4.3 Make a single block inert

If a block is misbehaving and you can't immediately diagnose, set its `applicableIf = is:false`. The block stays in project.config (so the audit trail of "this rule existed" is preserved) but never fires:

```sh
git checkout refs/meta/config
$EDITOR project.config

# Locate the offending block, e.g.:
# [submit-requirement "Tier-X-Architecture-Review"]
# Add (or replace) its applicableIf line:
#     applicableIf = is:false

git commit -s -am "[OP-806] disable Tier-X-Architecture-Review pending hotfix"
git push origin HEAD:refs/meta/config
```

Tier X changes will then fall through to the global Human-Plus-2 / No-Veto pair (because Human-Plus-2's applicableIf `-label:Tier=s AND -label:Tier=m` is true for Tier=x), giving the same human-+2-only behavior as pre-OP-806 for that tier alone.

When you re-enable, push a new commit removing the `applicableIf = is:false` line — do **not** force-push or amend.

---

## 5. Project lockdown (last resort)

If a P0 incident appears to involve an authorization gap (e.g. a Tier S patchset merged that should have been blocked), lock the project read-only **immediately**:

```sh
ssh -p 29418 sora.services gerrit set-project-state --state read-only omnisight/OmniSight-Productizer
```

This blocks all submits and pushes until reversed. While locked:

1. Snapshot current state per §1.3.
2. Audit recent submitted changes — `gerrit query --format=JSON status:merged limit:50 project:omnisight/OmniSight-Productizer` and inspect each for tier consistency.
3. Decide whether a full revert (§3) plus a re-push of corrected blocks is the path, or whether the deeper authorization model itself needs a re-review (escalate to ADR amendment).
4. Reverse the lockdown only after either §3 is complete OR a hot-fix commit is verified by the operator.

---

## 6. Cross-references

- Install procedure: `deploy/gerrit/install-tier-submit-requirements.md`
- Diff applied by OP-806: `deploy/gerrit/refs-meta-config-tier-blocks.diff`
- Tier model spec: `docs/adr/ADR-0005-tier-authority-levels.md`
- Predecessor dual-+2 runbook (group design, REST query patterns): `docs/ops/gerrit_dual_two_rule.md`
- Disaster recovery (Gerrit DB-level): `docs/operations/disaster-recovery.md`
- AS rollout/rollback (similar lever-menu pattern): `docs/operations/as-rollout-and-rollback.md`
- Memory: `reference_gerrit_submit_requirements.md` — historical record of declarative-submit-requirement migration after OP-697
