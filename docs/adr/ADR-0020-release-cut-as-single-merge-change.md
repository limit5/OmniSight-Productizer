---
id: ADR-0020
title: Release cut to main as a single merge change (submit-type MERGE_ALWAYS + conditional submit-requirement)
status: Accepted
date: 2026-05-12
---

# ADR-0020 — Release cut to `main` as a single merge change

- **Status**: Accepted (2026-05-12, AUDIT-26c / OP-982) — Gerrit-side deployment is AUDIT-26e; `auto_promote_main` rewrite is AUDIT-26d.
- **Deciders**: operator (`nanakusa sora`) + AI fleet; pre-flight evidence: AUDIT-26a / OP-980 (Phase 0 verification)
- **Tickets**: OP-982 (AUDIT-26c — this ADR + the `project.config` change), OP-980 (AUDIT-26a — Phase 0 verification), OP-925 (the R3 cascade that motivated it), OP-960 / ADR-0016 (the mechanism this supersedes), AUDIT-26d / AUDIT-26e / AUDIT-26f (downstream)
- **Supersedes**: ADR-0016 (D5 develop→main promotion via Gerrit review change). ADR-0016 stays in the record as the prior decision; its "path C" forward-compat hook (`milestone:R3-fastforward` hashtag) is the seam this ADR builds on. The formal status flip on ADR-0016 — `Accepted → Superseded by ADR-0020` — is AUDIT-26f's job (it owns the AUDIT-13/13a/13b cleanup pass), not this ticket's.
- **Blocks**: AUDIT-26d (must push the single merge change this ADR specifies), AUDIT-26e (must deploy the `project.config` in this ADR)

## Context

A *release cut* — advancing `refs/heads/main` to the accepted release content
after the `RELEASE-vX.Y.Z` milestone passes — is **not a code review**. The
content of `develop` was already reviewed commit-by-commit on the way in; the
release cut is a *bookkeeping* event: "this is the snapshot we are shipping". The
machinery we have today treats it as a code review anyway, and that mismatch is
the root of two production incidents.

### What we have today (ADR-0016 / OP-960)

`backend/agents/auto_promote_main.py` pushes `develop` to the `refs/for/main`
magic ref with `topic=develop-to-main` and hashtags
`auto-promote` + `milestone:R3-fastforward`. Because Gerrit's default submit
type for `refs/heads/main` is fast-forward-ish (`MERGE_IF_NECESSARY` /
`REBASE_IF_NECESSARY`), pushing the *commit range* `main..develop` to
`refs/for/main` creates **one Gerrit change per intervening commit** — a chain of
N changes that an operator must `+2` and submit *in order*. ADR-0016 documented
three submit paths (A: operator clicks Submit; B: a future web-UI button; C: a
future conditional merger-bot `+2`), but the bulk chain is the part that bites.

### Why it bites — OP-925 and AUDIT-13/13a's bulk-chain failure

The first live `RELEASE-v0.5.0-rc1` R3 attempt (OP-925, 2026-05-12) cascaded into
AUDIT-11/12/13. Even after those, the bulk-chain shape is fragile:

- **`receive.maxBatchChanges` ceiling.** Gerrit's default is 10. If `develop` is
  more than ~10 commits ahead of `main` — routine after a sprint — the *single*
  push is rejected wholesale (`BatchTooLarge`). `auto_promote_main` has to detect
  this and emit an operator alert instead of letting `CalledProcessError` escape.
  The release cut then can't happen automatically *at all*.
- **N-way operator submit.** Even under the ceiling, the operator must submit a
  chain. Submit one out of order, rebase a sibling, miss one — and `main` lands
  partially advanced. AUDIT-13a's "promote the whole range as a chain" approach
  inherited exactly this brittleness.
- **`main` is required to stay linear.** Every consumer that did
  `git log develop..main` (chiefly `auto_promote_main.evaluate_fast_forward`)
  assumed `main` is an ancestor-or-equal of `develop`. That holds for a
  fast-forward chain but not for anything more robust, and it makes the FF
  pre-check the *only* model the code can express.

The deeper problem: a release cut shouldn't *look like* N code-review changes.
It is **one event** and should be **one change**.

## Decision

A release cut is **one Gerrit change whose head is a single merge commit** on
`refs/for/main`, submitted under **`submit-type: MERGE_ALWAYS`**, gated by a new
**conditional submit-requirement** `release-cut-promote` that — *in addition to*
the standing human `+2` — requires a `merger-agent-bot` `+2`, but **only** when
the change matches the auto-promote fingerprint (branch + topic + hashtag +
author). Concretely:

### 1. One merge commit, not a commit range

AUDIT-26d rewrites `auto_promote_main` to, from `main`'s current tip,
`git merge --no-ff develop` and push **that single merge commit** to
`refs/for/main` (instead of pushing the raw `main..develop` range). One push →
**one** Gerrit change → one `+2` set → one submit. `receive.maxBatchChanges` is
moot (one change). The `noop` short-circuit stays: if `develop` is already an
ancestor of `main^2` (nothing new on `develop`), don't create a change.

### 2. `submit-type: MERGE_ALWAYS` on `refs/heads/main`

`.gerrit/project.config` (mirror: `.gerrit/project.config.example`) gains:

```ini
[submit "refs/heads/main"]
    submitType = MERGE_ALWAYS
```

`MERGE_ALWAYS` records a merge commit on submit *even when a fast-forward was
possible*. Combined with (1), submitting the release-cut change moves
`refs/heads/main` to a freshly recorded merge commit whose **first parent** is
`main`'s prior tip and **second parent** is the pushed merge commit's content.
No rebase of the change occurs. `main`'s history becomes: one merge commit per
release cut, each with a deterministic subject (see §"Schema / contract
lock-in"). The project-global `[project] submitType` stays
`REBASE_IF_NECESSARY` for every other branch.

> **Deployment note (AUDIT-26e).** Whether Gerrit 3.13 honours a *per-ref*
> `[submit "refs/heads/main"] submitType` or requires the *project-global*
> `[project] submitType = MERGE_ALWAYS` is the one open question Phase 0 left
> open (see `docs/audit/2026-05-12-audit-26-phase-0-verification.md` §2). The
> in-repo config encodes the per-ref form (narrowest blast radius — what we
> want). If the deploy-time check shows 3.13 only supports project-global,
> AUDIT-26e moves it to `[project]` and re-confirms the two consumers Phase 0
> flagged tolerate a `MERGE_ALWAYS` `develop` / `release/*` (Phase 0 §4: `develop`
> already takes feature-branch merges; `release/*` cherry-pick fallback needs the
> `-m 1` fix tracked in AUDIT-26a-3). The choice does **not** change anything in
> this ADR's contract — only where the line lives in `project.config`.

### 3. Conditional submit-requirement `release-cut-promote`

`.gerrit/project.config` gains a fourth submit-requirement block (after
`Human-Plus-2`, `Merger-Plus-2`, `MainFastForwardMergerPlus2`, `No-Veto`,
`Verified`):

```ini
[submit-requirement "release-cut-promote"]
    description = Release-cut promotion to main; conditional merger-bot +2 acceptable when topic + hashtag + author match the auto-promote pattern.
    applicableIf = branch:main AND topic:^release-v[0-9]+[.][0-9]+[.][0-9]+.*$ AND hashtag:"milestone:R3-fastforward" AND author:^auto-promote-bot$|^claude-bot$|^codex-bot$
    submittableIf = (label:Code-Review=+2,group=merger-agent-bot) AND (label:Code-Review=+2,group=non-ai-reviewer)
    canOverrideInChildProjects = false
```

(The topic regex is written `^release-v[0-9]+[.][0-9]+[.][0-9]+.*$` — i.e.
`^release-vN.N.N…$` — using `[0-9]`/`[.]` rather than `\d`/`\.` so there is no
backslash for JGit's `project.config` value parser to (mis)interpret;
semantically identical. The `author:` predicate uses top-level alternation
`^a$|^b$|^c$` rather than `^(a|b|c)$` for the same don't-trip-the-parser reason.)

**Applicability is quadruply-keyed** on purpose: `branch:main` **AND**
`topic:^release-v…$` **AND** `hashtag:"milestone:R3-fastforward"`
**AND** `author:` matching one of the three release-cut author identities. A
change missing *any* key does not match and falls through to the standing
`Human-Plus-2` gate — the same `SubmitRuleOverScopes` regression invariant
OP-962 established for `MainFastForwardMergerPlus2`, tightened from two keys to
four.

**`submittableIf` is co-exist, not substitute.** It requires `merger-agent-bot`
`+2` **AND** `non-ai-reviewer` `+2`. The merger-bot `+2` is *additive*: it
attests "this merge commit is a clean, surprise-free promotion of accepted
`develop` content" (the same role O6's merger `+2` plays for a conflict block —
here the "block" is the whole merge commit, and the bot validates author +
fast-forward-equivalence before voting). It does **not** replace the human `+2`,
so **no CLAUDE.md L1 amendment is needed** — the "human `+2` required for merge"
rule still holds, unchanged, including on release cuts. (The Phase-0 "substitute"
alternative — merger `+2` *only*, zero operator click — was rejected; see
Alternatives.)

**Naming:** the ticket text refers to the group as `merger-bot`; the deployed
Gerrit group is `merger-agent-bot` (per `reference_gerrit_self_hosted.md` /
`reference_gerrit_submit_requirements.md`). This ADR and the config use the
deployed name. The `merger-agent-bot` *account* must be a member of the
`merger-agent-bot` *group* for the rule to be effective — recorded as an operator
pre-deploy check in §"Verification" (it was empty-but-defined at Phase 0 time;
`MergerBotGroupMissing` in the error catalog).

### 4. Relationship to the existing rules

| Rule | Before this ADR | After this ADR | Cleanup |
|---|---|---|---|
| `Human-Plus-2` | `applicableIf = -hashtag:"milestone:R3-fastforward" OR -branch:main` (carved out for the doubly-keyed auto-promote change) | unchanged in OP-982 | AUDIT-26f: either drop the carve-out entirely (now redundant — `release-cut-promote.submittableIf` re-imposes the human `+2`) or tighten it to the quad-key |
| `MainFastForwardMergerPlus2` | `applicableIf = hashtag:"milestone:R3-fastforward" AND branch:main`; `submittableIf = (merger +2) OR (non-ai +2)` (substitute) | unchanged in OP-982 — still present, now *redundant* for release cuts (its OR is weaker than `release-cut-promote`'s AND, so the AND wins; on a hypothetical hashtag-only change with no release topic it still grants the old behaviour) | AUDIT-26f: remove it — `release-cut-promote` replaces it, with a stricter key and stricter `submittableIf` |
| `release-cut-promote` (new) | — | added by OP-982 | — |
| `No-Veto` | unconditional `-label:Code-Review=-1 AND -label:Code-Review=-2` | unchanged — still applies to release cuts | — |
| `Verified` | `applicableIf = is:false` (default-OFF, OP-740) | unchanged | — |

OP-982 deliberately **adds** `release-cut-promote` without removing
`MainFastForwardMergerPlus2` or re-keying `Human-Plus-2`: removing/retuning those
also means rewriting the OP-962 policy-contract tests, which is AUDIT-26f's
scope. The two rules co-exist consistently in the interim (the stricter `AND`
gate wins for genuine release cuts).

## Schema / contract lock-in

These are the cross-ticket contracts AUDIT-26d (push side), AUDIT-26e (deploy
side) and any future submit-rule consumer key off. Changing one without the
others breaks the gate.

| Knob | Value | Owner | Notes |
|---|---|---|---|
| Hashtag | `milestone:R3-fastforward` | `auto_promote_main.PROMOTE_HASHTAGS` | Already emitted today (ADR-0016). The submit-requirement matches the literal string via `hashtag:"milestone:R3-fastforward"` (Gerrit's `hashtag:` predicate is exact-string; the quotes are because of the `:`). AUDIT-26d **must keep** this hashtag on the new merge change. |
| Topic | `release-vX.Y.Z` (regex `^release-v[0-9]+[.][0-9]+[.][0-9]+.*$`) | `auto_promote_main` | **Changed** from ADR-0016's `develop-to-main`. The topic now encodes the release version (e.g. `release-v0.5.0-rc1`). AUDIT-26d sets it from the `RELEASE-vX.Y.Z` META's fixVersion. The `.*$` tail tolerates `-rc1` / `+build` suffixes. (Hotfix cuts, if they ever advance `main` through this path, need the regex broadened to also accept `hotfix-v…` — out of scope here; the OP-982 spec is `release-v…` only.) The web-UI "pending release cuts" view (ADR-0016 path B, still a follow-up) will query this topic prefix. |
| Author | one of `auto-promote-bot`, `claude-bot`, `codex-bot` (regex `^auto-promote-bot$\|^claude-bot$\|^codex-bot$`) | the bot identity that pushes the change (= the merge commit's git author, since AUDIT-26d's `git merge --no-ff develop` is run by that identity) | `auto-promote-bot` is the dedicated D5 identity; `claude-bot` / `codex-bot` cover a runner-driven cut. An ai-reviewer-bot cannot forge the `hashtag` (no `editHashtags` on `refs/for/*` — see `.gerrit/project.config` `[access "refs/for/refs/heads/*"]`), so even a compromised reviewer bot can't synthesise a change that matches `release-cut-promote`. |
| Merge-commit subject | `Release cut: vX.Y.Z (develop@<short-sha> → main)` | `auto_promote_main` (AUDIT-26d) | Deterministic so `main`'s log is greppable and the changelog/notes tools (which key on JIRA fixVersion, not `git log`) have a stable anchor if they ever want one. AUDIT-26d owns the exact format; this ADR pins the *shape* (must name the version and the source sha). |
| Submit type | `MERGE_ALWAYS` on `refs/heads/main` | `.gerrit/project.config` | Per-ref preferred; project-global is the AUDIT-26e fallback (see §2 deploy note). |

## Operator workflow

1. **R3 triggers** when `scripts/release_milestone_checker.py` reports
   `milestone_ready` (or `milestone_force_promoted` per ADR-0019) for the
   `RELEASE-vX.Y.Z` META. The runner picks up the R3 child.
2. R3's executor (AUDIT-26d's rewritten `auto_promote_main`) creates **one**
   `refs/for/main` change: head = `git merge --no-ff develop` from `main`'s tip,
   `topic=release-vX.Y.Z`, `hashtag=auto-promote` + `hashtag=milestone:R3-fastforward`,
   pushed by `auto-promote-bot` (or the runner identity).
3. `merger-agent-bot`'s poller (`area:backend` — out of scope here, AUDIT-26d /
   a backend follow-up) re-validates: author ∈ the allow-list? the merge commit's
   second-parent content equals accepted `develop`? no surprise diff? — and if so
   casts `Code-Review: +2`. If validation fails it casts `-1` (which `No-Veto`
   turns into a hard block — see the `MainFastForwardMergerPlus2` comment in
   `project.config`).
4. **What `+2` means here:** the **human** `+2` (the operator, in the
   `non-ai-reviewer` group) is the **release authorisation** — "yes, ship this
   snapshot". The **merger-bot** `+2` is the **mechanical attestation** — "this
   change really is a clean promotion of accepted `develop`, not something else
   wearing the auto-promote costume". Both are required (`release-cut-promote`'s
   `AND`). The operator can still cast the `+2` from the Gerrit UI exactly as
   today (ADR-0016 path A); the difference is the change is *one* change, not a
   chain, and the merger-bot's attestation rides alongside.
5. **Submit** advances `refs/heads/main` to the recorded merge commit
   (`MERGE_ALWAYS`).
6. **R8's relationship.** R8 (the human release-approval gate near the end of the
   `RELEASE-vX.Y.Z` chain) is **unchanged and still distinct**. R3 promotes
   `develop → main`; R8 approves the *deploy* of what's on `main`. A release thus
   still has (at least) two human touches: the `+2` on the R3 release-cut change
   and the R8 approval. ADR-0016 already documented this; ADR-0020 keeps it. The
   merger-bot `+2` does **not** collapse R3 into a zero-touch step (that was the
   rejected "substitute" alternative).

## Alternatives considered

### Alt-1 — Cherry-Pick-To `refs/heads/main` (Gerrit's `Cherry Pick To` action) for each release commit

**Rejected.** This is the bulk-chain in a different costume — it still produces
N changes (one cherry-pick per commit) and still requires N operator submits, and
it additionally *rewrites* commit shas on `main` (cherry-pick ≠ the original
commit), breaking `git describe`-equivalence between `develop` content and what
ships. The single-merge-commit model keeps the *content* identical (the merge's
second parent is the `develop` tip's tree) while making `main`'s topology
explicit.

### Alt-2 — Grant `auto-promote-bot` `push` rights on `refs/heads/main` (direct push, no Gerrit change)

**Rejected — same reason ADR-0016 rejected it.** `main` moves through Gerrit
review, full stop: it is the most security-critical ref, every transition must be
reviewable, and a stolen bot key must never be able to push prod code. The
`release-cut-promote` rule's *whole point* is to keep the review gate while
making the *shape* of the change sane.

### Alt-3 — Tag-only release model (no `main` branch advancement; releases are just annotated tags on `develop`)

**Rejected.** `main` as "the branch that is currently in production" is load-
bearing for staging deploys, the prod working-repo mirrors, drift scanners and
the operator's mental model. Collapsing it into tags-on-`develop` would be a far
larger change than the incident warrants, and it loses the "is this commit
shipped?" answer that `git merge-base --is-ancestor X main` gives cheaply.

### Alt-4 — Custom Gerrit `pre-receive` hook that auto-fast-forwards `main` when the milestone is accepted

**Rejected.** A server-side hook is unauditable from the change's perspective
(no Gerrit change object, no `+2`, no review record), it's a bespoke piece of
infra to maintain on the Gerrit host (which we deliberately keep close to stock —
ADR-0003), and it would *bypass* the human `+2` entirely — strictly worse than
even the rejected Alt-2 on the audit axis. `MERGE_ALWAYS` + a declarative
submit-requirement gets us the automation we want using only stock Gerrit
features.

### Alt-5 — `release-cut-promote.submittableIf` = merger-bot `+2` *only* (the Phase-0 "substitute" option)

**Rejected (for now).** Zero-operator-click release cuts are tempting, but the
"human `+2` required for merge" rule is CLAUDE.md L1 — substituting it for *any*
class of change, even a tightly-keyed one, needs an explicit L1 amendment + ADR,
not a `project.config` edit smuggled in under a devops ticket. We took the
**co-exist** option (merger `+2` *and* human `+2`) — it's strictly safer, needs
no policy amendment, and still buys the real win (one change, not a chain, plus a
machine attestation). If operator click-fatigue ever justifies it, that's a
separate META with operator `+1` (cf. ADR-0019's pattern for
`release:force-promote`).

## Consequences

### History

- **One merge commit per release cut in `main`'s history.** `git log main`
  becomes a clean list of "Release cut: vX.Y.Z …" merges (plus any hotfix
  merges). First-parent traversal (`git log --first-parent main`) walks just the
  release-cut spine. This is intentional and is the headline behavioural change.
- **`main` is no longer a linear superset of `develop`.** Every consumer that
  assumed otherwise is enumerated in Phase 0 §4 (OP-980): the only two that break
  are `auto_promote_main.evaluate_fast_forward` (→ AUDIT-26d redesigns it away)
  and `auto_tag_release._update_release_branch`'s cherry-pick fallback (→ needs
  `-m 1`; AUDIT-26a-3). The other 12 key on JIRA milestone state / recorded shas
  / semver tags and are merge-agnostic.

### Events / bridges

- **`gerrit-jira-bridge` fires once per release cut** — the single
  `refs/for/main` change creates one `patchset-created` / `change-merged` event
  pair, not N. Any bridge consumer that counted on "one event per promoted
  commit" (none known) would see the shape change. The `change-merged` event for
  the release-cut change is the clean signal "main advanced for vX.Y.Z" that
  ADR-0016's "R4 picks up before main advanced" race-mitigation note asked for.

### Audit

- `release_audit` (and the generic `audit` sink it currently writes to — see
  `project_release_audit_sink_mismatch.md` / OP-964) gets **one** row per cut
  with the merge sha, not N rows. The Gerrit receive-log + the `change-merged`
  event are the corroborating trails. A future `outcome=auto_submitted` is *not*
  added — submit stays operator-gated (co-exist), so the outcome is the same
  `success` / `push_rejected` / `non_ff_refused` set, minus `batch_too_large`
  (impossible now — one change).

### SSOT mirror — follow-up, not this ticket

`backend/submit_rule.py::evaluate_submit_rule` is the Python mirror of the Gerrit
submit policy used by the Merge Arbiter / GitHub-Actions fallback. It currently
mirrors `Human-Plus-2` / `Merger-Plus-2` / `No-Veto`; it does **not** yet know
about `MainFastForwardMergerPlus2` or `release-cut-promote`. Mirroring
`release-cut-promote` into it is `area:backend` — **out of scope for OP-982**
(declared areas `devops`, `tests`). Flagged here and on the ticket as a backend
follow-up (fold into AUDIT-26d, which is already `area:backend`, or file
separately).

## Verification

This ADR is the design + the in-repo `project.config` change. Live Gerrit
deployment + the sandbox-push verification (AC #3) require **sora-admin**
credentials (the bots are regular Gerrit users) and the deployed config — so they
are an **operator pre-/post-deploy checklist for AUDIT-26e**, not agent work,
exactly as Phase 0 (OP-980) recorded its Q1/Q2/Q3 live pokes. Recorded here so
AUDIT-26e has the script.

### Static verification (done, OP-982)

- `pytest backend/tests/test_merger_bot_main_promote.py` — the OP-962 policy-
  contract suite, extended in OP-982 with `release-cut-promote` cases
  (`test_release_cut_promote_*`, including the quad-key
  `SubmitRuleOverScopes`-style negatives) and the `.example`-sync check. Green at
  OP-982 PS-final.
- `.gerrit/project.config` and `.gerrit/project.config.example` carry the
  `[submit "refs/heads/main"]` block and the `release-cut-promote` block,
  byte-identical between the two files (the test asserts it).

### Operator pre-deploy checklist (AUDIT-26e)

```bash
GERRIT=ssh://sora@sora.services:29418/omnisight/OmniSight-Productizer
git clone "$GERRIT" cfg && cd cfg
git fetch origin refs/meta/config:refs/remotes/origin/meta/config
git checkout -b meta origin/meta/config
cp ../.gerrit/project.config.example project.config   # the OP-982 version
git commit -am "[AUDIT-26e] ADR-0020: MERGE_ALWAYS on main + release-cut-promote submit-requirement"
git push origin HEAD:refs/for/refs/meta/config
# → sora-admin Code-Review +2 + Submit (known-good path — OP-692 142962be, OP-694 8cbd81e9)
```

Then confirm the `merger-agent-bot` account is a member of the
`merger-agent-bot` group (`gerrit ls-members merger-agent-bot` or the Groups
UI) — create/populate if empty (`MergerBotGroupMissing`). And confirm 3.13
accepted the per-ref `[submit "refs/heads/main"]` form; if it rejected it, switch
to project-global `[project] submitType = MERGE_ALWAYS` and re-run the §2
`develop` / `release/*` impact check.

### Operator post-deploy verification (AUDIT-26e — AC #3 of OP-982)

| Probe | Expectation |
|---|---|
| Push a sandbox change to `refs/for/main` **with** `topic=release-v9.9.9` + `hashtag=milestone:R3-fastforward`, authored by `auto-promote-bot` | `release-cut-promote` appears in the change's Submit Requirements list, status `UNSATISFIED` until both a `merger-agent-bot` `+2` and a `non-ai-reviewer` `+2` are present; abandon the sandbox change afterwards |
| Push a sandbox change to `refs/for/main` **without** the topic (or wrong topic, or wrong author, or no hashtag) | `release-cut-promote` does **not** appear (or shows `NOT_APPLICABLE`); the change is still gated by `Human-Plus-2` |
| `gerrit query --format=JSON 'change:<sandbox> --submit-records'` for both | matches the above; paste the JSON into this section's table when AUDIT-26e runs |

(Screenshots / API output to be appended to this section by the AUDIT-26e
operator, mirroring how OP-980 recorded its Phase-0 evidence.)

## References

- ADR-0016 — D5 develop→main promotion via Gerrit review change (superseded by this ADR; the `milestone:R3-fastforward` path-C hook is reused)
- ADR-0003 — Gerrit Code Review (the dual-sign gate; keep Gerrit close to stock)
- ADR-0019 — `release:force-promote` operator override (sibling: how a META amendment + operator `+1` is the right path for policy relaxations)
- OP-980 / AUDIT-26a — Phase 0 verification: `docs/audit/2026-05-12-audit-26-phase-0-verification.md` (Q1 ACL, Q2 `MERGE_ALWAYS` semantics, Q3 the merger-rule gap this ADR fills, Q4 the 14-consumer linear-history audit)
- OP-962 / AUDIT-13b — `MainFastForwardMergerPlus2` (the doubly-keyed conditional `+2` this ADR generalises to a quad-key)
- L-OP-692 — Gerrit 3.13 declarative submit-requirements pattern (`rules.pl` rejected)
- `reference_gerrit_submit_requirements.md` — live submit-rule architecture (updated for `release-cut-promote` by OP-982)
- `.gerrit/project.config` — the live policy file; `.gerrit/project.config.example` — the `refs/meta/config` mirror
- `backend/agents/auto_promote_main.py` — the push side (rewritten by AUDIT-26d)
- `backend/submit_rule.py` — the Python SSOT mirror (release-cut-promote mirroring is a backend follow-up)
- CLAUDE.md L1 Safety Rules — "human `+2` required for merge" (un-amended; co-exist option chosen precisely to avoid amending it)
