# AUDIT-26 — Phase 0 verification (Gerrit ACL + MERGE_ALWAYS + merger-bot + linear-history consumers)

**Ticket**: OP-980 (AUDIT-26a) · **Date**: 2026-05-12 · **Tier**: M · **Component**: CRITICAL
**Parent**: AUDIT-26 META (switch Gerrit `omnisight/OmniSight-Productizer` so `refs/heads/main` uses `submit-type: MERGE_ALWAYS`)
**Blocks**: AUDIT-26c (new submit-requirement), AUDIT-26d (redefine the FF pre-check), AUDIT-26e (the project-config flip)
**Memory anchors**: `reference_gerrit_submit_requirements.md`, `reference_gerrit_self_hosted.md`, `project_d5_main_promote_mechanism.md`, `project_audit26_phase0.md`
**Reference paths**: `backend/agents/auto_promote_main.py`, `backend/agents/auto_tag_release.py`, `scripts/release_milestone_checker.py`

---

## 0. tl;dr

| Question | Verdict | Action it gates |
|---|---|---|
| Q1 — can sora-admin edit `refs/meta/config`? | **PASS** (desk evidence: OP-692 `142962be`, OP-694 `8cbd81e9` are both `refs/meta/config` commits landed by the sora-admin path). Live re-confirm is an operator step — see §1. | AUDIT-26e (the flip) |
| Q2 — does Gerrit 3.13 `submit-type: MERGE_ALWAYS` behave as we want? | **EXPECTED-PASS** by spec: one push to `refs/for/main` → one change whose head is the merge commit → +2-able → submit advances `refs/heads/main` to that merge commit. Live sandbox proof is an operator step — see §2. | AUDIT-26e |
| Q3 — does merger-bot's OP-692 conditional +2 already fire on develop→main promotes? | **NO — gap confirmed.** OP-692 `Merger-Plus-2` is keyed on `hashtag:Merge-Conflict-Resolved`; develop→main promote changes carry `auto-promote` + `milestone:R3-fastforward` (topic `develop-to-main`). AUDIT-26c must add a *new* submit-requirement keyed on `hashtag:R3-fastforward`. See §3. | AUDIT-26c |
| Q4 — what breaks when `refs/heads/main` stops being linear? | **2 breaks, 0 blockers.** HARD: `auto_promote_main.evaluate_fast_forward()` wedges once `develop..main` is permanently non-empty → AUDIT-26d. NARROW: `auto_tag_release._update_release_branch()` cherry-pick fallback fails on a merge SHA (needs `-m 1`) → AUDIT-26a-3 (or fold into 26d). Everything else is merge-agnostic. See §4. | AUDIT-26d (+ AUDIT-26a-3) |

**Bottom line**: AUDIT-26 is safe to proceed. No verification *failed* — Q3 surfaced
the expected gap (that's what AUDIT-26c exists for), and Q4 surfaced exactly the
two consumers AUDIT-26d/26a-3 will fix. **Land order**: AUDIT-26d (code, with a
`deployed:` AC) → AUDIT-26c (submit-requirement) → AUDIT-26e (project-config flip).
The live Gerrit pokes in Q1/Q2/Q3 require **sora-admin** credentials (the bots are
regular users — `reference_gerrit_self_hosted.md`); they are recorded here as an
operator pre-flip checklist, not as agent work.

---

## 1. Q1 — sora-admin can edit `refs/meta/config`

**Desk evidence (sufficient to proceed):** the live submit-rule architecture
(`reference_gerrit_submit_requirements.md`) was *installed* by editing
`project.config` on `refs/meta/config` and submitting through Gerrit — OP-692
(`142962be`) added the three declarative submit-requirement blocks and OP-694
(`8cbd81e9`) followed up. Both landed. So the access path "fetch `refs/meta/config`
→ edit `project.config` → push to `refs/for/refs/meta/config` → sora-admin +2 →
submit" is **known-good** for this project. No group needs to gain Owners.

**Operator pre-flip checklist (do this immediately before AUDIT-26e):**

```bash
GERRIT=ssh://sora@sora.services:29418/omnisight/OmniSight-Productizer
git clone "$GERRIT" cfg && cd cfg
git fetch origin refs/meta/config:refs/remotes/origin/meta/config
git checkout -b meta origin/meta/config
# trivial no-op edit, e.g. add a comment line to project.config
git commit -am "[AUDIT-26e probe] refs/meta/config write check"
git push origin HEAD:refs/for/refs/meta/config
# → expect: a NEW change; sora-admin can Code-Review +2 and Submit it.
```

If that push is rejected (`prohibited by Gerrit: not permitted: create change`),
the `Owners`/`Push`/`Submit` permission on `refs/meta/config` is missing — grant
it to the group sora-admin is in (per `reference_gerrit_self_hosted.md` ADR-0003
that is `non-ai-reviewer`-adjacent admin, not a bot group) and re-run. **Not
expected** given OP-692/694.

---

## 2. Q2 — Gerrit 3.13 `submit-type: MERGE_ALWAYS` behavior

**What we want for `refs/heads/main`:** today every develop→main promote pushes
`develop` to `refs/for/main`, which (with the default `MERGE_IF_NECESSARY` /
fast-forward submit-type) creates **N changes — one per intervening commit** — that
an operator must submit in order. With `MERGE_ALWAYS`, submitting a change always
records a merge commit even when a fast-forward was possible; combined with the
AUDIT-26d redesign (push a *single* `develop`-into-`main` merge commit to
`refs/for/main` rather than the raw commit range), the promote becomes **one
change → head = the merge commit → one +2 → one submit → `refs/heads/main`
advances to the merge commit**, which is exactly what makes the merger-bot's
scoped auto-+2 (AUDIT-26c) tractable.

**Spec basis:** Gerrit's `submit-type: MERGE_ALWAYS` is documented as "always
produce a merge commit, even if it's a fast-forward". On submit, `refs/heads/main`
moves to the (newly created) merge commit; the change's commit becomes the second
parent, `main`'s prior tip the first. No rebasing of the change occurs. This is
stable behavior across 3.x including 3.13.5.

**Operator sandbox proof (do once, before AUDIT-26e):**

```bash
# in the refs/meta/config worktree from §1:
# add, scoped to a throwaway branch:
#   [access "refs/heads/test-audit-26-merge-always"]
#     ... (inherit)
#   [branchOrder]  (n/a)
#   submit-type is set per-ref via the "submit" section, e.g.
#   [submit "refs/heads/test-audit-26-merge-always"]  -> not how Gerrit does it;
#   actually: set `[project] submit-type = MERGE_ALWAYS` is global, OR use a
#   branch-scoped `[access "refs/heads/test-..."] ... submitType = ...` — verify
#   the 3.13 syntax in the Gerrit docs at flip time; the global-vs-per-ref choice
#   is itself an AUDIT-26e decision (we want it ONLY on refs/heads/main).
git push origin :refs/heads/test-audit-26-merge-always   # cleanup when done
```

Observe: push one merge commit (`git merge --no-ff <develop-equiv>`) to
`refs/for/test-audit-26-merge-always` → exactly one change, head = the merge
commit → +2-able → submittable → after submit, `git log refs/heads/test-...`
shows the branch at the merge commit. **Confirm this matches** before configuring
`refs/heads/main`. Delete the sandbox branch and revert the temporary
`project.config` block afterwards.

> **AUDIT-26e open question (not for this ticket):** whether `submit-type` can be
> scoped to `refs/heads/main` alone in 3.13 (per-ref `access` block) or is
> project-global. If global, the release branches (`release/*`) and `develop`
> would also get `MERGE_ALWAYS` — `develop` is fine (it already takes merges from
> feature branches), but confirm `release/*` cherry-pick / tag flows (§4) tolerate
> it. Flag for the AUDIT-26e author.

---

## 3. Q3 — merger-bot conditional +2: current state and the gap

**Current state (from `reference_gerrit_submit_requirements.md`, OP-692/694):**
the live policy on `refs/meta/config` is three declarative submit-requirement
blocks —

1. `Human-Plus-2` — unconditional, `label:Code-Review=+2,group=non-ai-reviewer`
2. `Merger-Plus-2` — **conditional**: `applicableIf = hashtag:Merge-Conflict-Resolved`, `label:Code-Review=+2,group=merger-agent-bot`
3. `No-Veto` — unconditional, `-label:Code-Review=-1 AND -label:Code-Review=-2`

`Merger-Plus-2` becomes *applicable* only when the O6 Merger Agent sets the
`Merge-Conflict-Resolved` hashtag after resolving a conflict. On any other change
it is `NOT_APPLICABLE`, so the human +2 is the only path.

**The develop→main promote change does NOT carry that hashtag.** Per
`backend/agents/auto_promote_main.py:92-93`:

```python
PROMOTE_HASHTAGS: tuple[str, ...] = ("auto-promote", "milestone:R3-fastforward")
PROMOTE_TOPIC = "develop-to-main"
```

So today, on a develop→main promote, `Merger-Plus-2.status = NOT_APPLICABLE` and
an operator submits via the Gerrit UI (exactly as the module docstring at
`auto_promote_main.py:18-26` says). The merger-bot does nothing.

**Gap → AUDIT-26c must add a fourth block**, e.g.:

```
[submit-requirement "Merger-Plus-2-Promote"]
  description = merger-bot may cast a scoped +2 on a develop->main promote merge commit
  applicableIf = hashtag:R3-fastforward
  submittableIf = label:Code-Review=+2,group=merger-agent-bot
  # ... and decide co-exist vs substitute with Human-Plus-2:
  #   co-exist  -> promote still needs BOTH a human +2 AND merger +2 (safest; no policy change)
  #   substitute -> promote needs ONLY merger +2 (faster; requires a CLAUDE.md L1 / ADR
  #                 amendment to the "human +2 required for merge" rule, scoped to R3-fastforward)
```

Notes for the AUDIT-26c author:

- Use the **bare** hashtag `R3-fastforward`, not the `milestone:R3-fastforward`
  form, in `applicableIf` — Gerrit's `hashtag:` predicate matches the literal
  hashtag string; `PROMOTE_HASHTAGS` currently emits `"milestone:R3-fastforward"`,
  so either (a) the predicate is `hashtag:"milestone:R3-fastforward"` or (b)
  AUDIT-26d additionally tags the merge change with a clean `R3-fastforward`
  hashtag. Pick one and make `auto_promote_main.PROMOTE_HASHTAGS` and the
  submit-requirement agree — this is a cross-ticket contract (26d ↔ 26c).
- The merger-bot account (`merger-agent-bot`) must be a *member* of the
  `merger-agent-bot` Gerrit group (per `reference_gerrit_self_hosted.md` the group
  exists but may still be empty — verify at AUDIT-26c time).
- Mirror the new rule into `backend/submit_rule.py::evaluate_submit_rule` (the
  SSOT Python mirror used by the Merge Arbiter / GitHub Actions fallback) — that
  is **out of scope for OP-980** (area `backend`); call it out in AUDIT-26c's AC.

---

## 4. Q4 — linear-history-consumer audit

Inventory of every tool/script/pipeline that touches `refs/heads/main`, and
whether a `MERGE_ALWAYS` (non-linear) `main` breaks it.

| # | Consumer | What it does with `main` | Verdict |
|---|---|---|---|
| 1 | `backend/agents/auto_promote_main.py` — `evaluate_fast_forward()` (`:279-300`), `promote_on_milestone_ready()` (`:303-...`) | `git log target..source` and `git log source..target`; sets `status = "ff_possible" if develop_only and not main_only else "blocked"` (`:290`); if `main_only` is non-empty → `status="blocked"`, fires a `critical` alert, audit `status:"non_ff"`. | **HARD BREAK.** A `MERGE_ALWAYS` main is *never* an ancestor of `develop` after the first promote: every promote leaves a merge commit on `main` that `develop` doesn't have, so `develop..main` (i.e. `main_only`) is permanently non-empty → every subsequent promote is mis-classified `blocked / non_ff` and wedges. **AUDIT-26d** must redefine this: drop the FF pre-check, instead create a merge commit (`git merge -s ort --no-ff develop` from `main`'s tip), push *that* one commit to `refs/for/main`. Keep the `receive.maxBatchChanges` guard moot (one change now). Keep the `noop` short-circuit when `develop..main^2` is empty (nothing new on develop). |
| 2 | `backend/agents/auto_tag_release.py` — `_update_release_branch()` (`:156-181`) | When the release branch already exists and `main_sha` is *not* an ancestor of its HEAD: `git cherry-pick main_sha` (`:175-176`). Also `tag_release_on_staging_passed` tags `main_sha` directly (`:185-...`). | **NARROW BREAK.** `git cherry-pick` on a *merge* commit fails with `mainline was specified but commit … is not a merge` unless `-m 1` is given. Once `main` tips are merge commits this fallback path errors. Fix: `_git(worktree, "cherry-pick", "-m", "1", main_sha, ...)` (and only on the fallback path; the common path is `merge-base --is-ancestor` → `switch`, which is fine). Tagging `main_sha` is unaffected — you can tag a merge commit. → file **AUDIT-26a-3** or fold the one-liner into AUDIT-26d. |
| 3 | `scripts/auto_promote_develop_to_main.sh` | Thin CLI wrapper around #1 (the D5 systemd timer entrypoint, `auto-promote-develop.timer`). | Inherits #1's break; fixed transitively by AUDIT-26d. No separate change. |
| 4 | `scripts/release_milestone_checker.py` (R-chain audit, incl. OP-975 R5 "continuous-staging tip-match") | Reads JIRA milestone state; the R5 audit compares staging-env tip vs a recorded SHA — keys on **recorded SHAs**, not on `main` linearity. | **No break.** Merge-agnostic. |
| 5 | `scripts/cut_release_branch.sh` | `git branch -f release/$VERSION $MAIN_SHA` (`:118,:146`); then runs `auto_changelog.py`. | **No break.** Branching at a merge commit is normal git. |
| 6 | `scripts/auto_changelog.py` (OP-880), `scripts/release_notes_from_milestone.py` (OP-888), `backend/agents/release_notes_generator.py` | Render release notes from the **JIRA fixVersion** ticket list — not from `git log main..`. | **No break.** Not git-history-driven. |
| 7 | `scripts/deploy.sh:358`, `scripts/deploy-prod.sh:311` | `git describe --tags --always [--dirty]` for the version banner. | **No break.** `git describe` walks back to the nearest tag; a merge commit on the path is fine. |
| 8 | `backend/release.py` (`get_version`, `:30-47`) | `git describe --tags` → `VERSION` file → `package.json` → fallback. | **No break.** Same as #7. |
| 9 | `scripts/build_image.sh` (`docker buildx build`, `:109`; semver ref `:189-191`) | Tags images by **semver / commit SHA** (`SEMVER_REF`, digest inspect), not a moving `:main` tag. No `docker build -t …:main`. | **No break.** SHA/tag-keyed. |
| 10 | `backend/agents/auto_deploy_staging.py`, `scripts/staging_deploy.sh`, `scripts/sync_staging_to_develop.sh` | Staging deploys key on the **webhook-delivered SHA** / `--image-tag <tip>`; sync scripts track `develop`, not `main`. | **No break.** |
| 11 | `scripts/sync_omnisight_main.sh`, `scripts/sync_sora_bridge.sh` | Mirror `develop` (and tags) to the prod working repos; do not assert `main` linearity. | **No break.** |
| 12 | `scripts/drift_scanner.py` (`:583`, `main_branch_drift`) | `git rev-list --left-right --count {remote}/main...main` — *local vs remote* `main` symmetric diff, to tell the operator "push" vs "pull". | **No break.** Compares two refs of the same branch; merge commits on both sides cancel. |
| 13 | `scripts/fallback_rebase.py`, `scripts/bootstrap_prod.sh:180` | `fallback_rebase` walks `main..origin/main` for the `.fallback` manifest replay; `bootstrap_prod` does `git rev-list --count HEAD..origin/main`. | **No break.** Both are local-vs-remote `main` counts (provisioning / disaster-recovery paths), not "is main linear" assertions. |
| 14 | `.gerrit/rules.pl` (dead Prolog, ~90 LOC) + `backend/submit_rule.py` (SSOT mirror) | Vote-tallying only (`Code-Review` labels, `had_conflict` flag). No git-history inspection. | **No break.** Vote-based, not topology-based. (`rules.pl` is dead code per `reference_gerrit_submit_requirements.md`; mirror update for AUDIT-26c is a separate `backend` ticket.) |

**Summary**: of 14 distinct consumers, **2 break** (#1 hard, #2 narrow), both with
a clear, small fix already scoped to downstream tickets (AUDIT-26d, AUDIT-26a-3).
The remaining 12 are merge-agnostic because the OmniSight release machinery keys
on **JIRA milestone state + recorded SHAs + semver tags**, never on "`main` is a
linear superset of `develop`". **No verification fails → AUDIT-26 META is not
blocked.**

### Sub-tickets to file (DoD item)

- **AUDIT-26a-3** — `auto_tag_release._update_release_branch`: add `-m 1` to the
  cherry-pick fallback so it tolerates a merge `main_sha`. Tier S. (Or fold into
  AUDIT-26d as a one-line rider — author's call.) *Not* a blocker for the flip
  *unless* a release branch is re-cut after the first `MERGE_ALWAYS` promote.

(No other sub-tickets — AUDIT-26d already exists for consumer #1; AUDIT-26c
already exists for the Q3 gap.)

---

## 5. Recommendation

1. **Proceed with AUDIT-26.** All four pre-flight questions are answered; no
   blocker.
2. **Land order**: AUDIT-26d (redesign `auto_promote_main` to push one merge
   commit; add the `-m 1` cherry-pick fix or rely on AUDIT-26a-3; carry a
   `deployed:` AC so the systemd timer change is actually activated — see
   `project_audit_shipped_not_deployed.md`) → AUDIT-26c (add the
   `hashtag:R3-fastforward` submit-requirement + decide co-exist vs substitute;
   mirror into `backend/submit_rule.py`) → AUDIT-26e (the `project.config` flip:
   set `submit-type: MERGE_ALWAYS` scoped as narrowly as 3.13 allows — ideally
   `refs/heads/main` only).
3. **Before AUDIT-26e, the operator runs the §1 and §2 live checklists** (needs
   sora-admin creds) and records the outcome as a comment on AUDIT-26e.
4. **File AUDIT-26a-3** (the cherry-pick `-m 1` one-liner) unless AUDIT-26d's
   author folds it in.

## 6. Status

- Q1 / Q2 / Q3 / Q4 desk verification: **complete** (this document).
- Q1 / Q2 / Q3 live Gerrit confirmation: **deferred to operator** (sora-admin
  credentials required; the bots are regular Gerrit users — see
  `reference_gerrit_self_hosted.md`). Checklists in §1–§2.
- Operator (sora) review + approval of this document: **pending**.
