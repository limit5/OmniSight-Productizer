# OP-806 — Install Tier-Authority Submit-Requirements on Prod Gerrit

> **Created**: 2026-05-09
> **Owner**: G4 (META OP-802) — Phase 3 governance migration plan
> **Status**: Draft — operator action required (subscription-claude — needs operator confirm before push)
> **Audience**: Gerrit admin / operator with `refs/meta/config` push rights on `omnisight/OmniSight-Productizer` (sora.services:29418).
> **Scope**: Step-by-step procedure to (1) create the three Gerrit groups required by ADR-0005, (2) dry-run the `refs/meta/config` patch on a sandbox project, (3) push to prod after verification, (4) confirm AC items.
>
> **Out of scope**: Authoring the path-classifier hook (G3 — separate ticket; this runbook explains how to verify the rules fire once that hook ships); rollback procedure (lives at [`docs/operations/tier-authority-rollback.md`](../../docs/operations/tier-authority-rollback.md)); Tier S/M/L/X authority model rationale (see [`docs/adr/ADR-0005-tier-authority-levels.md`](../../docs/adr/ADR-0005-tier-authority-levels.md)).

---

## 0. Quick reference card

| Step | Tool | Reversible? | Time |
|---|---|---|---|
| Create 3 groups (`ai-reviewer-bots` already exists; create `non-ai-reviewer` if missing, `architecture-reviewer` new) | `gerrit create-group` over SSH | yes — `gerrit set-members --remove` / drop group via DB script | < 2 min total |
| Sandbox dry-run on `omnisight/sandbox` | `git push` to sandbox `refs/meta/config` | yes — push the inverse diff | 5–10 min |
| Prod push to `omnisight/OmniSight-Productizer` | `git push` to prod `refs/meta/config` | yes — see rollback runbook | < 30 s push, < 5 s effect |
| AC verification (REST GET + synthetic Tier S patchset) | `curl /projects/.../config` + `gerrit review` | yes — abandon test change | 10–15 min |

If anything below feels wrong, **STOP** and post in the ops channel — `refs/meta/config` errors gate every future submit until rollback. The sandbox dry-run exists exactly to catch this.

---

## 1. Pre-flight checklist

### 1.1 Hard prerequisites

1. **G3 status**: Has the path-classifier server-side hook (G3) been deployed yet? If **no**, this runbook still ships the rules — they are inert until G3 stamps the Tier label on patchsets. The risk in pushing the rules first is zero (see §6 truth table — unset-Tier behavior is identical to pre-OP-806). Pushing G3 first without rules is also safe (the label vote will simply have no enforcement effect).
2. **`ai-reviewer-bots` group**: must already exist with members `claude-bot`, `codex-bot`, `merger-agent-bot`. Verify per §2.1 before proceeding.
3. **`non-ai-reviewer` group**: must already exist with `sora` (operator). Verify per §2.1.
4. **Sandbox project**: confirm `omnisight/sandbox` exists. If it does not exist, create one for this dry-run:
   ```
   ssh -p 29418 sora.services gerrit create-project omnisight/sandbox \
       --description "Throwaway sandbox for refs/meta/config dry-runs" \
       --empty-commit
   ```
   This is reversible (`gerrit set-project-state --state read-only`, then `gerrit delete-project` from the delete-project plugin).
5. **Local clone of `omnisight/OmniSight-Productizer`**: a clean working tree with credentials configured for `claude-bot` (or whichever account has `refs/meta/config` push rights).
6. **Operator confirmation**: per `agent_class: subscription-claude` in the OP-806 ticket, do **NOT** push to prod without operator sign-off in the ticket comments. The dry-run can proceed without sign-off; the prod push cannot.

### 1.2 Required tools

- `git` ≥ 2.34
- `ssh` access to `sora.services:29418` with key-pair registered for the operator account
- `curl` + valid Gerrit HTTP password (operator has it; `claude-bot` has its own from `git_accounts` table per `reference_backend_credentials_model.md`)
- `python3` (only for the AC verification helper invocation in §5)

### 1.3 What this push does NOT touch

- ACL blocks for `refs/heads/*`, `refs/for/*`, `refs/*` — unchanged. AI bots remain DENY-locked from submit/abandon/delete/rebase exactly as OP-693/OP-697 left them.
- `[label "Code-Review"]`, `[label "Verified"]`, `[label "Submit-Ready"]` — unchanged.
- `[submit-requirement "Merger-Plus-2"]`, `[submit-requirement "No-Veto"]`, `[submit-requirement "Verified"]` — unchanged. They keep applying to every tier as before.
- The webhooks plugin config (lives in `webhooks.config`, separate file on the same ref) — untouched.

The only existing block this patch modifies is `[submit-requirement "Human-Plus-2"]` (one new `applicableIf` line); everything else is additive.

---

## 2. Group provisioning

Group state changes are independent of the `refs/meta/config` push and should be applied **before** the sandbox dry-run, so the new submit-requirements have valid groups to reference when Gerrit re-parses the config.

### 2.1 Verify pre-existing groups

```sh
ssh -p 29418 sora.services gerrit ls-members ai-reviewer-bots
ssh -p 29418 sora.services gerrit ls-members non-ai-reviewer
ssh -p 29418 sora.services gerrit ls-members merger-agent-bot
```

Expected:

- `ai-reviewer-bots` → at minimum `claude-bot`, `codex-bot`, `merger-agent-bot` (and any `class:api-*` future bots already onboarded)
- `non-ai-reviewer` → `sora` (humans only — fail the run if any bot account appears here)
- `merger-agent-bot` → the merger service account, included via `--include ai-reviewer-bots` per OP-692 setup

If any of the three groups is missing, create it now. `ai-reviewer-bots` and `non-ai-reviewer` predate this ticket; if they are absent, halt and check with the operator — something else is broken.

### 2.2 Create `architecture-reviewer`

```sh
ssh -p 29418 sora.services gerrit create-group architecture-reviewer \
    --visible-to-all \
    --description "ADR-0005 Tier X reviewers — sole architect for now (sora). Add future architects here. Members vote Code-Review on architecture-impacting changes; Tier-X submit-requirement reads from this group."

ssh -p 29418 sora.services gerrit set-members architecture-reviewer --add sora
```

### 2.3 Verify the new group

```sh
ssh -p 29418 sora.services gerrit ls-members architecture-reviewer
# → must print exactly: sora
```

`architecture-reviewer` members already have full Code-Review rights via their (existing) `non-ai-reviewer` membership — the ACL needs no change. The new group exists purely as a "tag" the Tier-X submit-requirement reads to identify who is qualified to architecture-review.

### 2.4 AC #1 evidence

Save the three `gerrit ls-members` outputs to `~/op806-evidence/gerrit-groups-$(date -u +%Y%m%dT%H%M%SZ).log`. Quote the file path in the AC verification comment on JIRA OP-806.

---

## 3. Sandbox dry-run

**Do this even if the diff looks trivial.** A single typo in a `submit-requirement` block syntax causes Gerrit to reject the entire `project.config` push, leaving the previous version live; but a syntactically-valid block with a wrong `applicableIf` query goes live silently and can lock the project. The sandbox dry-run is the only way to catch the second class.

### 3.1 Prepare the sandbox clone

```sh
git clone "ssh://sora.services:29418/omnisight/sandbox" sandbox-meta
cd sandbox-meta
git fetch origin refs/meta/config:refs/meta/config
git checkout refs/meta/config

# Confirm sandbox baseline matches prod (it should, if sandbox was
# bootstrapped from OP-692/OP-697 templates):
diff -u project.config /path/to/OmniSight-Productizer/.gerrit/project.config || true
```

If the sandbox baseline diverges from the in-repo `.gerrit/project.config`, regenerate the patch against the sandbox baseline first — `git apply` will refuse a patch whose context lines do not match.

### 3.2 Apply the patch

```sh
cp /path/to/OmniSight-Productizer/deploy/gerrit/refs-meta-config-tier-blocks.diff .
git apply --check refs-meta-config-tier-blocks.diff
git apply       refs-meta-config-tier-blocks.diff
git add project.config
git commit -s -m "[OP-806] Tier label + Tier-S/M/L/X submit-requirements (sandbox dry-run)"
git push origin HEAD:refs/meta/config
```

If `git apply --check` fails, **stop**. Do not edit the patch in-place; regenerate it from the corrected base file and re-run the dry-run from the top.

### 3.3 Verify on sandbox via REST

```sh
GERRIT_HOST="https://sora.services:29419"
GERRIT_AUTH="sora:$GERRIT_HTTP_PASSWORD"   # from `git config --get user.email` + Gerrit HTTP password

curl -s -u "$GERRIT_AUTH" \
    "$GERRIT_HOST/a/projects/omnisight%2Fsandbox/config" \
    | sed '1d' | jq '.labels.Tier, .submit_requirements'
```

Confirm:

- `.labels.Tier` is non-null and lists values `s/m/l/x` plus `-1`
- `.submit_requirements` lists keys `Tier-S-AI-Self-Plus-2`, `Tier-M-Mixed-Plus-2`, `Tier-L-Default-Human-Plus-2`, `Tier-X-Architecture-Review`, `Human-Plus-2` (with the new `applicableIf`), `Merger-Plus-2`, `No-Veto`, `Verified`

This is AC #2 + AC #3 evidence on the sandbox — the same query against the prod project after §4 is what the AC actually requires.

### 3.4 Synthetic Tier S patchset (AC #4 dry-run)

On the sandbox, push a no-op change to `develop` and manually set `Tier=s` (since the path-classifier hook is not yet wired on the sandbox, mimic it by hand):

```sh
git checkout -B test-tier-s
echo "# OP-806 sandbox dry-run" >> README.md
git commit -am "sandbox: test Tier S submit-requirement"
git push origin HEAD:refs/for/develop

# In the change UI, set Tier=s manually (operator-cast vote stands in
# for the G3 hook), then have claude-bot vote Code-Review +2:
ssh -p 29418 sora.services gerrit review --label Tier=s <change-num>,1
ssh -p 29418 sora.services gerrit review --label Code-Review=+2 <change-num>,1
# (Switch to claude-bot identity for the +2 — operator vote does not
# count as ai-reviewer-bots.)
```

Now query submit eligibility:

```sh
curl -s -u "$GERRIT_AUTH" \
    "$GERRIT_HOST/a/changes/<change-num>/revisions/current/submit_requirements" \
    | sed '1d' | jq '.[] | {name, status}'
```

Expected output (only the rules whose `applicableIf` matches Tier=s should be `SATISFIED`; the others should be `NOT_APPLICABLE`):

```
{"name": "Human-Plus-2",                      "status": "NOT_APPLICABLE"}
{"name": "Merger-Plus-2",                     "status": "NOT_APPLICABLE"}
{"name": "No-Veto",                           "status": "SATISFIED"}
{"name": "Verified",                          "status": "NOT_APPLICABLE"}
{"name": "Tier-S-AI-Self-Plus-2",             "status": "SATISFIED"}
{"name": "Tier-M-Mixed-Plus-2",               "status": "NOT_APPLICABLE"}
{"name": "Tier-L-Default-Human-Plus-2",       "status": "NOT_APPLICABLE"}
{"name": "Tier-X-Architecture-Review",        "status": "NOT_APPLICABLE"}
```

**Submit the change** via `gerrit review --submit ...` — it should succeed.

Then push a second test change, set `Tier=s`, but cast Code-Review +1 only (no AI +2):

- `Tier-S-AI-Self-Plus-2` → status `UNSATISFIED`
- `gerrit review --submit ...` → must reject with "blocked by submit requirements"

Both of these — the success path and the rejection path — together are AC #4 evidence on the sandbox. Capture the JSON outputs into `~/op806-evidence/`. Abandon both test changes after recording the evidence.

### 3.5 Sandbox rollback (only if dry-run fails)

```sh
git checkout refs/meta/config
git reset --hard HEAD^
git push --force-with-lease origin HEAD:refs/meta/config
```

`--force-with-lease` is acceptable on the sandbox `refs/meta/config` (this branch has no humans cohabiting). On prod it is **NOT** — see the rollback runbook for the safe revert-and-replay procedure.

---

## 4. Prod push

Only after §3 is fully green and the operator has confirmed in the JIRA OP-806 ticket, proceed with prod.

### 4.1 Prod clone + apply

```sh
git clone "ssh://sora.services:29418/omnisight/OmniSight-Productizer" prod-meta
cd prod-meta
git fetch origin refs/meta/config:refs/meta/config
git checkout refs/meta/config

# Confirm prod baseline matches the in-repo deployable copy:
diff -u project.config /path/to/in-repo/.gerrit/project.config
# If non-empty, the prod ref has drifted — STOP and reconcile before
# applying the patch.

cp /path/to/in-repo/deploy/gerrit/refs-meta-config-tier-blocks.diff .
git apply --check refs-meta-config-tier-blocks.diff
git apply       refs-meta-config-tier-blocks.diff
git diff project.config | tee /tmp/op806-prod-applied.diff
# Operator MUST eyeball /tmp/op806-prod-applied.diff vs the dry-run diff
# from §3.2 and confirm they are identical except for the project name in
# any commit message (there should be none in the diff itself).
```

### 4.2 Commit + push

```sh
git add project.config
git commit -s -m "[OP-806] Tier label + Tier-S/M/L/X submit-requirements (prod)"
git push origin HEAD:refs/meta/config
```

The push must complete without warnings beyond standard "remote: Processing changes" output. If Gerrit prints **anything** about parse errors or rejected blocks, halt and consult the rollback runbook — do NOT attempt a corrective push without first reverting.

### 4.3 Prod verification (AC #2 + AC #3)

Run the same REST query as §3.3 against the prod project:

```sh
curl -s -u "$GERRIT_AUTH" \
    "$GERRIT_HOST/a/projects/omnisight%2FOmniSight-Productizer/config" \
    | sed '1d' | jq '.labels.Tier, .submit_requirements' \
    | tee ~/op806-evidence/prod-config-$(date -u +%Y%m%dT%H%M%SZ).json
```

Confirm the same expected keys as §3.3. The captured JSON file is the AC #2 + AC #3 evidence.

---

## 5. AC #4 prod verification

Repeat §3.4 against `omnisight/OmniSight-Productizer`. Push the synthetic Tier S patchset to a throwaway branch (`refs/for/<orphan-branch>` — do NOT use `refs/for/develop` for this, to avoid contaminating the develop change list with test data):

```sh
git checkout --orphan op806-tier-s-prod-test
git rm -rf .
echo "OP-806 prod AC #4 evidence — abandon after capture" > README.md
git add README.md
git commit -m "[OP-806] Tier S smoke test"
git push origin HEAD:refs/for/op806-tier-s-prod-test%t=op806-ac4
```

Run the two-vote experiment (AI +2 → submit succeeds; without AI +2 → submit rejected), capture both `submit_requirements` JSONs, then **abandon both test changes** before logging off.

Save evidence to `~/op806-evidence/prod-ac4-{satisfied,unsatisfied}.json` and quote the file paths in the AC verification comment on JIRA OP-806.

---

## 6. Truth table — what each tier does after this push

Assumes G3 hook is also live (when it is not, "Tier value" is always `unset`, which routes to row L behavior — same as pre-OP-806).

| Tier value | Applicable rules | Sufficient signal to merge | Notes |
|---|---|---|---|
| `s` | Tier-S-AI-Self-Plus-2, No-Veto, Verified (when on) | One AI +2 from `ai-reviewer-bots` | Human-Plus-2 / Merger-Plus-2 do NOT apply. 24h revert window backstops misclassification. |
| `m` | Tier-M-Mixed-Plus-2, No-Veto, Verified (when on) | One AI +2 AND one Human +2 | Strictest tier: requires votes from both groups. |
| `l` (default) | Tier-L-Default-Human-Plus-2, No-Veto, Verified, Merger-Plus-2 (if hashtag) | One Human +2 | Identical to pre-OP-806 behavior. The Tier-L block is documentation; Human-Plus-2 with the new applicableIf does not fire here, but Tier-L-Default-Human-Plus-2 enforces the same +2 condition. |
| `x` | Tier-X-Architecture-Review, No-Veto, Verified, Merger-Plus-2 (if hashtag) | One Human +2 AND one architecture-reviewer ≥ +1 | Architecture-reviewer membership = ADR-0005 "sole architect for now (sora)." |
| unset (no Tier vote) | Human-Plus-2, No-Veto, Verified, Merger-Plus-2 (if hashtag) | One Human +2 | Identical to pre-OP-806. This is what every patchset looks like before G3 ships. |
| `-1` (mis-tier rejection) | Tier-* rule for whichever tier the value resolves to, plus No-Veto | Does NOT submit | `-1 = block` participates in the No-Veto check via `Tier`'s `Code-Review`-style negation, **only if** Gerrit treats `-1` as a block-vote on the Tier label — verify via the dry-run that a `-1` Tier vote actually halts submit; if not, file a follow-up. |

The `-1 = block` row is the one operationally-novel piece of behavior introduced here. If §3.4 sandbox testing reveals that a Tier=-1 vote does NOT block submit (because No-Veto only inspects Code-Review, not Tier), open a follow-up ticket to either widen No-Veto's `submittableIf` or drop `-1` from the Tier label values — but do **not** block OP-806 prod-push on it; the existing dual-+2 path stays safe regardless.

---

## 7. Cross-references

- ADR-0005: `docs/adr/ADR-0005-tier-authority-levels.md` — authoritative model description and submit-rule integration spec.
- Existing dual-+2 runbook: `docs/ops/gerrit_dual_two_rule.md` — predecessor context (group design, Gerrit setup).
- Reference baseline config: `.gerrit/project.config` (deployable copy used by OP-692 / OP-694 / OP-697).
- Rollback procedure: `docs/operations/tier-authority-rollback.md`.
- Memory note: `reference_gerrit_submit_requirements.md` — declarative submit-requirement migration history (post OP-697).
- Prerequisite ticket: G3 (path-classifier hook). Without G3, the Tier label is never set; the rules are inert. Pushing in either order is safe.
