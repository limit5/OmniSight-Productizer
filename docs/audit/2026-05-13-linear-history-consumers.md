# Linear-history-consumer audit — post-AUDIT-26 re-verification

**Ticket**: OP-1032 (AUDIT-29d-4) · **Date**: 2026-05-13 · **Tier**: M · **Component**: AUDIT-29
**Parent**: AUDIT-29d (Phase 4) → AUDIT-29 META (pre-rc2 stabilization)
**Deferred from**: AUDIT-26a / OP-980 §4 ("what breaks when `refs/heads/main` stops being linear")
**Memory anchors**: `project_release_cut_mechanism`, `project_audit26_phase0`, `project_audit_29_structure`
**Reference paths**: `backend/agents/auto_promote_main.py`, `backend/agents/auto_tag_release.py`, `backend/submit_rule.py`, `docs/adr/ADR-0020-release-cut-as-single-merge-change.md`, `docs/audit/2026-05-12-audit-26-phase-0-verification.md`

---

## 0. tl;dr

| Question | Verdict |
|---|---|
| Are the 14 consumers OP-980 §4 enumerated still correctly classified after AUDIT-26d (OP-983) / 26c (OP-982) / 26e (OP-984) / 26f (OP-985) landed? | **Mostly yes — 12 remain merge-agnostic (OK).** |
| Did AUDIT-26d (OP-983) actually fix consumer #1 (`auto_promote_main`) as ADR-0020 §Consequences promised ("→ AUDIT-26d redesigns it away")? | **No — partially.** OP-983 rewrote the *push* side (one merge commit instead of an N-change bulk chain — that part is done) but **kept the `evaluate_fast_forward` non-FF pre-check unchanged**. On the **second** release cut after `MERGE_ALWAYS` is active, `git log develop..main` is non-empty (it contains the prior cut's merge commit), so `evaluate_fast_forward` returns `status="blocked"` and `promote_on_milestone_ready` fires a `critical` alert + records `outcome=non_ff` instead of building the next merge commit. The original Q4 wedge is still live. → **needs-fix; follow-up ticket required** (see §3, §5). |
| Was AUDIT-26a-3 (the `auto_tag_release` cherry-pick `-m 1` one-liner OP-980 §4 named) ever filed / landed? | **No.** `_update_release_branch` at `backend/agents/auto_tag_release.py:175-176` still calls `git cherry-pick <main_sha>` with no `-m`. Once `main` tips are merge commits this fallback errors (`is a merge but no -m option was given`). → **needs-fix; follow-up ticket required** (see §3, §5). |
| Is the Exercised AC ("no consumer regression observed after `MERGE_ALWAYS` active") satisfiable today? | **No.** `MERGE_ALWAYS` on `refs/heads/main` is not live yet — the `refs/meta/config` flip is the still-pending AUDIT-26e operator step (`docs/audit/2026-05-12-audit-29-phase-0-state-audit.md` §4.2 shows `auto-promote-main.service` not installed). The single-cut path is exercised against an in-process mock Gerrit in `backend/tests/test_release_cut_e2e.py::test_release_cut_end_to_end_success`; the **second-cut** path (where consumer #1 wedges) has no coverage. |

**Bottom line:** the *content* of OP-980 §4's audit holds — the OmniSight release machinery keys on JIRA milestone state + recorded SHAs + semver tags, not on "`main` is a linear superset of `develop`", so almost everything is merge-agnostic. But the **two `needs-fix` consumers OP-980 §4 flagged are both still unfixed in code**: consumer #1's fix was only half-implemented by OP-983, and consumer #2's one-liner was never filed. Both must be filed as new tickets (AC §2) before `MERGE_ALWAYS` is switched on, or the first post-flip release cut after the *initial* one will wedge `auto_promote_main`.

---

## 1. Scope & method

OP-980 §4 (Phase 0 of AUDIT-26) enumerated every tool / script / pipeline that
touches `refs/heads/main` and asked whether a non-linear (`MERGE_ALWAYS`) `main`
breaks it. That audit was a **desk audit done before** any of the AUDIT-26
implementation tickets landed. Since then:

- **OP-982 (AUDIT-26c)** — added ADR-0020 + the `.gerrit/project.config`
  `[submit "refs/heads/main"] submitType = MERGE_ALWAYS` block + the
  `release-cut-promote` submit-requirement (config only — not yet deployed).
- **OP-983 (AUDIT-26d)** — rewrote `backend/agents/auto_promote_main.py`: instead
  of pushing the raw `main..develop` commit range to `refs/for/main`, it now
  builds **one** local `git merge --no-ff develop` commit (parents `[main, develop]`)
  and pushes that single commit, tagged `auto-promote` + `milestone:R3-fastforward`,
  topic `release-v<version>`.
- **OP-984 (AUDIT-26e)** — added `backend/tests/test_release_cut_e2e.py`, a
  behavioural e2e against an in-process mock Gerrit that models `MERGE_ALWAYS` +
  `release-cut-promote`. (The `area:tests` ticket; the AC#4 runbook/findings docs
  were left as an `area:docs` follow-up.)
- **OP-985 (AUDIT-26f)** — flipped ADR-0016 to `Superseded by ADR-0020`; added
  the `L-OP-985` release-cut-vs-code-review lesson.

This document re-runs OP-980 §4 against the **as-shipped** code and assigns each
consumer a disposition: **OK** (merge-agnostic, no change needed), **needs-fix**
(breaks on a non-linear `main`; a code change is required), or **superseded** (the
construct OP-980 §4 examined no longer exists / was replaced by the AUDIT-26
design and the new construct is merge-safe).

`MERGE_ALWAYS` is **not yet live** on the prod Gerrit (`refs/meta/config` flip =
AUDIT-26e operator step, still pending), so the "no regression *observed*" check
(AC §4) can only be argued from code inspection + the single-cut e2e test, not
from a production trace. The two `needs-fix` rows below are exactly the
regressions that *will* be observed on the second post-flip cut if not fixed
first.

The wedge in consumer #1 and the cherry-pick failure in consumer #2 were each
reproduced locally with a throwaway repo (two simulated `MERGE_ALWAYS` cuts);
the reproduction transcripts are summarised inline in §3.

---

## 2. Consumer inventory (re-audit)

Numbering follows OP-980 §4 for cross-reference. "OP-980 verdict" is what the
Phase 0 desk audit said; "Post-AUDIT-26 status" is the as-shipped finding.

| # | Consumer | What it does with `main` | OP-980 verdict | Post-AUDIT-26 status | Disposition |
|---|---|---|---|---|---|
| 1 | `backend/agents/auto_promote_main.py` — `evaluate_fast_forward()` (`:303-323`) + `promote_on_milestone_ready()` (`:377-481`) | `git log target..source` / `git log source..target`; `status = "ff_possible" if develop_only and not main_only else "blocked"`; if `main_only` non-empty → `critical` alert + audit `outcome=non_ff`, no promote. | **HARD BREAK** → AUDIT-26d "redesigns it away". | **needs-fix.** OP-983 rewrote the *push* side (✓ one merge commit, not an N-change chain; `receive.maxBatchChanges` moot; `noop` short-circuit kept). **But `evaluate_fast_forward` is unchanged** — it still does `git log develop..main` and blocks if non-empty. On cut #2+ after `MERGE_ALWAYS`, `develop..main` always contains the prior cut's merge commit (it is a *descendant* of the promoted develop tip, never an ancestor), so `status="blocked"` → wedge. The ADR-0020 §Consequences claim "AUDIT-26d redesigns it away" was not fully delivered. | **needs-fix** — file follow-up (§5). |
| 2 | `backend/agents/auto_tag_release.py` — `_update_release_branch()` (`:154-180`); the `_checkout_release_branch` → `merge-base --is-ancestor` → fallback `git cherry-pick main_sha` (`:175-176`) | When the release branch already exists and `main_sha` is *not* an ancestor of its HEAD: `git cherry-pick main_sha`. Also `tag_release_on_staging_passed` tags `main_sha` directly. | **NARROW BREAK** → file AUDIT-26a-3 (`-m 1`) or fold into 26d. | **needs-fix — AUDIT-26a-3 was never filed or landed.** `auto_tag_release.py:176` is still `_git(worktree, "cherry-pick", main_sha, ...)` with no `-m`. Once `main` tips are merge commits this fallback errors `is a merge but no -m option was given`. (Tagging a merge `main_sha` is fine — `_create_tag` at `:117-128` is unaffected. The common path `merge-base --is-ancestor` → `switch` is also fine.) | **needs-fix** — file follow-up (§5). |
| 3 | `scripts/auto_promote_develop_to_main.sh` | Thin CLI/systemd-timer wrapper around #1. | No separate break (inherits #1). | **superseded-ish / inherits #1.** Still a wrapper; fixed transitively whenever #1's `evaluate_fast_forward` is fixed. No own change. | inherits #1 |
| 4 | `scripts/release_milestone_checker.py` (R-chain audit incl. OP-975 R5 continuous-staging tip-match) | Reads JIRA milestone state; R5 audit compares staging-env tip vs a *recorded* SHA. | No break — merge-agnostic. | **OK.** Unchanged; keys on recorded SHAs / JIRA state, never on `main` linearity. | OK |
| 5 | `scripts/cut_release_branch.sh` | `git branch -f release/$VERSION $MAIN_SHA`, then `auto_changelog.py`. | No break — branching at a merge commit is normal git. | **OK.** Unchanged. | OK |
| 6 | `scripts/auto_changelog.py` (OP-880), `scripts/release_notes_from_milestone.py` (OP-888), `backend/agents/release_notes_generator.py` | Render release notes from the **JIRA fixVersion** ticket list, not from `git log main..`. | No break — not git-history-driven. | **OK.** Unchanged. | OK |
| 7 | `scripts/deploy.sh`, `scripts/deploy-prod.sh` | `git describe --tags --always [--dirty]` version banner. | No break — `git describe` walks to the nearest tag through any topology. | **OK.** Unchanged. | OK |
| 8 | `backend/release.py` (`get_version`) | `git describe --tags` → `VERSION` file → `package.json` → fallback. | No break — same as #7. | **OK.** Unchanged. | OK |
| 9 | `scripts/build_image.sh` (`docker buildx build`; semver ref) | Tags images by **semver / commit SHA**, never `docker build -t …:main`. | No break — SHA/tag-keyed. | **OK.** Unchanged. | OK |
| 10 | `backend/agents/auto_deploy_staging.py`, `scripts/staging_deploy.sh`, `scripts/sync_staging_to_develop.sh` | Staging deploys key on the **webhook-delivered SHA** / `--image-tag <tip>`; sync scripts track `develop`. | No break. | **OK.** Unchanged. | OK |
| 11 | `scripts/sync_omnisight_main.sh`, `scripts/sync_sora_bridge.sh` | Mirror `develop` (and tags) to the prod working repos; do not assert `main` linearity. | No break. | **OK.** Unchanged. | OK |
| 12 | `scripts/drift_scanner.py` (`main_branch_drift`) | `git rev-list --left-right --count {remote}/main...main` — *local vs remote* `main` symmetric diff. | No break — merge commits on both sides cancel. | **OK.** Unchanged. | OK |
| 13 | `scripts/fallback_rebase.py`, `scripts/bootstrap_prod.sh` | `fallback_rebase` walks `main..origin/main` for `.fallback` replay; `bootstrap_prod` does `git rev-list --count HEAD..origin/main`. | No break — local-vs-remote `main` counts (DR / provisioning), not "is main linear". | **OK.** Unchanged. | OK |
| 14 | `.gerrit/rules.pl` (dead Prolog) + `backend/submit_rule.py` (SSOT mirror) | Vote-tallying only (`Code-Review` labels, `had_conflict`). No git-history inspection. | No break — vote-based, not topology-based. | **OK** for the linear-history question. **Side-note (not a linear-history break):** `backend/submit_rule.py::evaluate_submit_rule` mirrors `Human-Plus-2` / `Merger-Plus-2` / `No-Veto` but **not** `MainFastForwardMergerPlus2` or the new `release-cut-promote` SR (ADR-0020 §"SSOT mirror" already flags this as an `area:backend` follow-up — fold into the #1 fix or file separately). `.gerrit/rules.pl` remains dead code. | OK (linear-history); separate backend follow-up noted |

**Score: 14 consumers — 12 OK · 2 needs-fix (#1, #2) · 0 net-new breaks discovered.**

---

## 3. The two `needs-fix` consumers — detail & reproduction

### 3.1 Consumer #1 — `auto_promote_main.evaluate_fast_forward` still wedges on cut #2+

ADR-0020 (§"Decision" 1, §"Consequences/History") says AUDIT-26d "redesigns
[the FF pre-check] away" — push **one** merge commit, drop the linear assertion,
keep only the `noop` short-circuit and let a *real* divergence surface as a merge
conflict. OP-983 implemented the **push half** (`_build_promote_merge_commit` +
single `refs/for/main` push, `auto_promote_main.py:339-374`, `:481-560`) but
**left `evaluate_fast_forward` intact** (`:303-323`) and still routes on it:

```python
# auto_promote_main.py:435-481 (abridged)
check = evaluate_fast_forward(repo=repo, source_branch=source_branch, target_branch=target_branch)
...
if check.status == "noop": ...                       # develop already in main — fine
if check.status != "ff_possible":                    # <-- main_only non-empty lands here
    notify("release-auto-promote", "critical", ...)   #     -> critical alert
    audit_sink(..., {"after": {"status": "non_ff", ...}})
    return PromotionResult("blocked", ...)            #     -> NO merge commit built
```

`evaluate_fast_forward` itself:

```python
# auto_promote_main.py:303-318
develop_only = _git_lines(repo, "log", "--oneline", f"{target_branch}..{source_branch}")
main_only    = _git_lines(repo, "log", "--oneline", f"{source_branch}..{target_branch}")
status = "ff_possible" if develop_only and not main_only else "blocked"
if not develop_only:
    status = "noop"
```

Once `MERGE_ALWAYS` is live, **every** release cut leaves a merge commit on
`main` that `develop` does not contain (the merge commit is a *child* of the
promoted develop tip, not an ancestor of it). So from the second cut onward,
`git log develop..main` (= `main_only`) is permanently non-empty → `status =
"blocked"` → critical alert + `outcome=non_ff` and the cut never gets built.

**Local reproduction** (throwaway repo, two simulated `MERGE_ALWAYS` cuts):

```
# after cut v1 is "submitted" (Gerrit MERGE_ALWAYS = a merge of main + the pushed merge commit),
# then develop advances by 2 commits, then we run the cut-v2 pre-check:
develop_only = git log main..develop  -> 2 commit(s)
main_only    = git log develop..main  -> 2 commit(s)   #  <- the two prior-cut merge commits
  49b1f12 Gerrit MERGE_ALWAYS submit of release-cut v1
  c60dee9 [release-cut v1] Merge develop into main
status = 'ff_possible' if develop_only and not main_only else 'blocked'  =>  BLOCKED
```

**Fix shape (for the follow-up ticket, not this one):** finish what ADR-0020
specified — drop the `main_only`-non-empty → `blocked` classification; keep the
`noop` short-circuit (`develop` already an ancestor of `main` ⇒ nothing to do);
build the `git merge --no-ff develop` commit unconditionally otherwise and let a
genuine divergence surface as `MergeCommitConflict` (already handled at
`auto_promote_main.py:484-503`). The companion test
`backend/tests/test_auto_promote_main.py::test_promote_refuses_when_main_not_ancestor_of_develop`
encodes the *old* "hotfix-on-main ⇒ blocked" contract and would change with this
fix — that contract change is a design decision for the follow-up ticket (with
operator sign-off), which is why it is **not** done here. The follow-up should
also pick up the `submit_rule.py` mirror gap noted in row #14.

### 3.2 Consumer #2 — `auto_tag_release._update_release_branch` cherry-pick lacks `-m 1`

`backend/agents/auto_tag_release.py:174-176`:

```python
if not _git_ok(worktree, "merge-base", "--is-ancestor", main_sha, "HEAD"):
    _git(worktree, "cherry-pick", main_sha, timeout=120)
```

When `main_sha` is a merge commit (its normal shape after `MERGE_ALWAYS`):

```
$ git cherry-pick <merge_sha>
error: commit <merge_sha> is a merge but no -m option was given.
fatal: cherry-pick failed
```

OP-980 §4 named the fix — `_git(worktree, "cherry-pick", "-m", "1", main_sha, ...)`
on the fallback path only — and proposed filing it as **AUDIT-26a-3** (or folding
the one-liner into AUDIT-26d). Neither happened: AUDIT-26a-3 is not in JIRA (no
`AUDIT-26a-3` reference exists anywhere except OP-980's doc and ADR-0020's
forward-reference) and OP-983 touched only `auto_promote_main.py` + its tests.
Tagging is unaffected (`auto_tag_release.py:117-128` tags `main_sha` directly —
you can annotate-tag a merge commit). Not a blocker for the `MERGE_ALWAYS` flip
*unless* a release branch is re-cut after the first post-flip cut.

---

## 4. AC §4 — "no consumer regression observed after `MERGE_ALWAYS` active"

`MERGE_ALWAYS` on `refs/heads/main` is **not active**. The `.gerrit/project.config`
block exists (OP-982) but the `refs/meta/config` push that activates it is the
AUDIT-26e operator pre-deploy step (sora-admin creds; see ADR-0020 §"Verification"
and `docs/audit/2026-05-12-audit-29-phase-0-state-audit.md` §4.2 — `auto-promote-main.service`
still not installed). So there is no production trace to inspect.

What *is* exercised:

- `backend/tests/test_release_cut_e2e.py::test_release_cut_end_to_end_success` —
  a **single** release cut against an in-process mock Gerrit that models
  `MERGE_ALWAYS` + `release-cut-promote`: build merge commit → push → merger-bot
  +2 → human +2 → submit → `main` advances to the merge commit → bridge emits
  `release_cut_main_advanced`. Green.
- `test_release_cut_handles_long_develop_chain_as_single_change` — a 60-ish-commit
  develop chain still produces exactly one change. Green.

What is **not** exercised: a **second** cut with `main` already carrying the
prior cut's merge commit — i.e. exactly the state in which consumer #1 wedges.
That gap should be closed by the consumer #1 follow-up ticket's test (an
`area:tests` change, out of scope here).

**Conclusion for AC §4:** cannot be marked satisfied. The honest status is "no
regression observable yet (feature not deployed); two known regressions (#1, #2)
will manifest on the second post-deploy cut unless fixed first — both filed as
follow-ups per AC §2."

---

## 5. Follow-up tickets to file (AC §2 — DoD item)

Both are `area:backend`, both small, both must land before AUDIT-26e flips
`MERGE_ALWAYS` on (or, at minimum, before the *second* release cut after the
flip):

1. **`auto_promote_main` — finish the ADR-0020 `evaluate_fast_forward` redesign.**
   Drop the `main_only`-non-empty → `blocked` classification; keep the `noop`
   short-circuit; let `MergeCommitConflict` be the only "real divergence" signal.
   Update `backend/tests/test_auto_promote_main.py` (the
   `test_promote_refuses_when_main_not_ancestor_of_develop` contract changes) and
   add a "second cut after `MERGE_ALWAYS`" case. While in there, also mirror
   `MainFastForwardMergerPlus2` + `release-cut-promote` into
   `backend/submit_rule.py::evaluate_submit_rule` (ADR-0020 §"SSOT mirror"
   follow-up). Tier S–M. *This is the AUDIT-26d deliverable that OP-983
   under-delivered — file as a 26d follow-up / re-open scope.*

2. **`auto_tag_release._update_release_branch` — `-m 1` on the cherry-pick
   fallback** (the long-named-but-never-filed **AUDIT-26a-3**). One line:
   `_git(worktree, "cherry-pick", "-m", "1", main_sha, timeout=120)` on the
   `not merge-base --is-ancestor` branch only. Add a test that cherry-picks a
   merge `main_sha`. Tier S.

> **Note on filing:** the OP-1032 runner pickup runs with the
> `add_comment` / `transition_back_to_todo` JIRA helpers only — it cannot create
> JIRA issues. These two follow-ups are therefore listed here and called out in
> the OP-1032 closing comment for the operator / coordinator to file (cf.
> ADR-0021 §8 routing). They are **not** out-of-area dependencies of OP-1032
> itself (OP-1032's deliverable is this audit doc + the ADR-0020 follow-up
> section, both `area:docs`), so OP-1032 does not surrender per §11 — it completes
> and hands the two fix tickets forward.

---

## 6. ADR-0020 follow-up

A summary of §0/§2/§3 above is recorded in
`docs/adr/ADR-0020-release-cut-as-single-merge-change.md` §"Follow-up — linear-history
consumer re-audit (OP-1032)". The headline correction for the ADR record: the
§"Consequences/History" line "the only two that break are
`auto_promote_main.evaluate_fast_forward` (→ AUDIT-26d redesigns it away)" is
**aspirational, not yet true** — OP-983 redesigned the push side but not the
pre-check; tracked by follow-up §5.1.

---

## 7. References

- `docs/audit/2026-05-12-audit-26-phase-0-verification.md` §4 — the original Q4 14-consumer desk audit (OP-980 / AUDIT-26a) this document re-verifies
- `docs/adr/ADR-0020-release-cut-as-single-merge-change.md` — the release-cut-as-single-merge-change decision (OP-982 / AUDIT-26c); §"Follow-up — linear-history consumer re-audit (OP-1032)" added by this ticket
- `docs/adr/ADR-0016-*` — superseded by ADR-0020 (OP-985 / AUDIT-26f)
- `backend/agents/auto_promote_main.py` — consumer #1 (the push-side rewrite landed OP-983; the `evaluate_fast_forward` redesign did not)
- `backend/agents/auto_tag_release.py` — consumer #2 (`-m 1` cherry-pick fix never filed)
- `backend/tests/test_release_cut_e2e.py` — single-cut e2e against a mock `MERGE_ALWAYS` Gerrit (OP-984 / AUDIT-26e)
- `backend/submit_rule.py` — SSOT submit-policy mirror (missing `MainFastForwardMergerPlus2` + `release-cut-promote`; ADR-0020 §"SSOT mirror" follow-up)
- `docs/audit/2026-05-12-audit-29-phase-0-state-audit.md` §4.2 — confirms `auto-promote-main.service` / the `MERGE_ALWAYS` flip is not yet deployed (AC §4 context)
- memory: `project_release_cut_mechanism`, `project_audit26_phase0`, `project_d5_main_promote_mechanism`, `project_audit_29_structure`
