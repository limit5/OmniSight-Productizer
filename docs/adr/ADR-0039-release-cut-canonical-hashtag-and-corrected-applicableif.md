---
id: ADR-0039
title: Release-cut submit-requirement — canonical hashtag + corrected applicableIf (owner / topic / hashtag multi-layer bug)
status: Accepted  # ⚠ conflicts with proposed ADR-0040 (single-trunk release train); the release-cut SR this corrects is removed entirely under ADR-0040 — to be superseded on its acceptance
date: 2026-05-20
supersedes_in_part:
  - ADR-0020 §3 "Conditional submit-requirement release-cut-promote" (the applicableIf only)
relates_to:
  - ADR-0020 (Release cut to main as a single merge change) — parent design; this ADR corrects its release-cut-promote applicableIf
  - ADR-0016 (D5 develop→main promotion via Gerrit review) — origin of the milestone:R3-fastforward path-C hook
  - ADR-0003 (Gerrit Code Review) — the dual-sign gate this rule rides on
tickets:
  - OP-1532 (this ADR)
---

# ADR-0039 — Release-cut submit-requirement: canonical hashtag + corrected `applicableIf`

## Status

Accepted (2026-05-20, OP-1532). **Supersedes ADR-0020 §3's `applicableIf` only** —
the rest of ADR-0020 (one-merge-commit model, `MERGE_ALWAYS`, the *co-exist*
`submittableIf`, the operator workflow) stands unchanged. This ADR records (1)
the multi-layer bug in the shipped `release-cut-promote` `applicableIf`, (2) the
canonical-hashtag decision, and (3) the corrected `applicableIf`.

The in-repo config edit (`.gerrit/project.config` + `.example` mirror) and the
`auto_promote_main` topic/hashtag change are **out of scope for this docs
ticket** — they are the downstream consumers **C2** (config, area:devops /
security) and **C3a** (code, area:backend), which reference this ADR's
canonical hashtag (see §"Downstream consumers — C2 / C3a").

## Context

ADR-0020 (OP-982 / AUDIT-26c) introduced the `release-cut-promote` Gerrit
submit-requirement: a *co-exist* gate that, for a genuine release-cut change on
`main`, requires **both** a `merger-agent-bot` `+2` (mechanical attestation) and
a `non-ai-reviewer` (human) `+2` (release authorisation). Its safety story
rested entirely on its **applicability key** being correct: a change that does
*not* match `applicableIf` falls through to the standing `Human-Plus-2` gate
(the `SubmitRuleOverScopes` regression invariant), and a change that matches
*incorrectly* would expose the merger-bot path to the wrong class of change.

The shipped key (`.gerrit/project.config:289`, identical to ADR-0020 §3) is:

```
applicableIf = branch:main AND topic:^release-v[0-9]+[.][0-9]+[.][0-9]+.*$ AND hashtag:"milestone:R3-fastforward" AND author:^auto-promote-bot$|^claude-bot$|^codex-bot$
```

It is wrong in **three independent layers**, any one of which is sufficient to
make the rule misfire. The dominant layer (identity) makes it misfire in the
*safe* direction — it never matches the real workflow, so it silently degrades
to "permanently `NOT_APPLICABLE`". The rule *looked* like a carefully
quadruple-keyed gate; in production it was a **paper gate** — the merger-bot
attestation ADR-0020 promised was never actually evaluated on a single real
release cut. Nothing merged unsafely (the `Human-Plus-2` fallback held), but
anyone reading `project.config` would believe a machine attestation existed that
did not.

### The multi-layer bug

#### Layer 1 — identity: `author:` is the wrong predicate **and** the wrong value (the dominant defect)

- **Wrong predicate.** Gerrit's `author:` matches the **git commit author**
  string of the change's commit, *not* the authenticated Gerrit account that
  owns/uploaded the change. The git author is free text — anyone can set it with
  `git commit --author="auto-promote-bot <…>"` — so even if the value matched, it
  would be **forgeable** and could never be the gate's trust anchor. The
  authenticated, non-forgeable identity is the **change owner** (`owner:`), the
  account that pushed to `refs/for/main`.
- **Wrong value.** The shipped value enumerates `auto-promote-bot` /
  `claude-bot` / `codex-bot`. But real release cuts are pushed and owned by the
  **operator** (`sora`). Evidence: the 2026-05-16 prod-deploy post-mortem records
  the live main release-cut change — **Gerrit Change #711 (OP-1182), "Merged
  with human +2 from `sora`"** (`docs/retrospectives/2026-05-16-prod-deploy-attempt-post-mortem.md:113`).
  The owner was `sora`, none of the three bot identities.
- **Net effect.** `author:^auto-promote-bot$|^claude-bot$|^codex-bot$` never
  matches a real cut, so the four-way `AND` short-circuits to false and
  `release-cut-promote` is **never applicable**. The whole rule is dead.

#### Layer 2 — topic: version-anchored regex instead of the canonical token

- The key matches `topic:^release-v[0-9]+[.][0-9]+[.][0-9]+.*$` — an
  *unquoted*, version-anchored regex. Two problems:
  1. It couples the gate to the SemVer string. Every variant the regex did not
     pre-imagine (a hotfix topic `hotfix-v…`, a re-cut, an operator who set a
     slightly different topic) silently de-gates. ADR-0020's own comment
     (`project.config:268-271`) already had to hand-wave around `-rcN` / `+build`
     suffixes and JGit backslash-escaping — a sign the predicate is too brittle.
  2. The canonical, version-independent marker of "this change *is* a release
     cut" already exists: `auto_promote_main._build_promote_merge_commit` writes
     the merge-commit subject `[release-cut <version>] Merge develop into main …`
     (`backend/agents/auto_promote_main.py:348-352`). The stable token is
     **`release-cut`**, not the version.
- **Correction:** match the topic *carrying* the `release-cut` token via
  Gerrit's substring/index predicate `intopic:release-cut`. C3a sets the
  release-cut change's topic to carry that token; the gate keys off the stable
  marker, not the version.

#### Layer 3 — hashtag: the bare vs. namespaced form was never made canonical

- The key matches the exact string `hashtag:"milestone:R3-fastforward"`.
  Gerrit's `hashtag:` predicate is exact-string, so it does **not** match a
  change tagged only `R3-fastforward`, and vice-versa. Two forms of the same
  hashtag float through the design lineage and the tree:
  - `auto_promote_main.PROMOTE_HASHTAGS` emits the **namespaced**
    `milestone:R3-fastforward` (`backend/agents/auto_promote_main.py:93`).
  - The AUDIT-26 Phase-0 verification doc proposed the **bare**
    `applicableIf = hashtag:R3-fastforward` and explicitly flagged the
    ambiguity as a cross-ticket contract: *"Pick one and make
    `auto_promote_main.PROMOTE_HASHTAGS` and the submit-requirement agree"*
    (`docs/audit/2026-05-12-audit-26-phase-0-verification.md:144,154-160`).
- OP-982 picked the namespaced form in `project.config`, and the emitter agrees
  with it — but the **decision was never recorded in an ADR**, so the bare form
  keeps reappearing in docs/scripts, and any future emitter that uses the bare
  form would silently de-gate. The forms must be reconciled *and* the
  reconciliation written down once, authoritatively.

#### How the layers compound

Layer 1 alone guarantees the rule never fires for the real (operator-owned)
workflow. Even after fixing Layer 1, Layer 2's version-anchored regex and Layer
3's exact-string hashtag each remain independent de-gating hazards. A correct
key must fix all three at once.

## Decision

### 1. Corrected `applicableIf`

Replace ADR-0020 §3's `applicableIf` with:

```
applicableIf = branch:main AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward) AND intopic:release-cut AND owner:sora
```

Term by term, against the three layers above:

| Term | Fixes | Rationale |
|---|---|---|
| `branch:main` | — | Unchanged — release cuts target `main`. |
| `(hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)` | Layer 3 | Accept **both** hashtag forms via `OR` during the transition window (see §2). |
| `intopic:release-cut` | Layer 2 | Match the stable `release-cut` topic token (substring predicate), not the version-anchored regex. |
| `owner:sora` | Layer 1 | The authenticated, **non-forgeable** operator account that owns the change — replaces the wrong-and-forgeable `author:<bot-list>`. |

The `submittableIf` is **unchanged** — the co-exist `AND` of a
`merger-agent-bot` `+2` and a `non-ai-reviewer` `+2` stands exactly as ADR-0020
§3 specified. This ADR corrects *which changes the rule applies to*, not *what
it requires once it applies*. The CLAUDE.md L1 "human `+2` required for merge"
rule remains untouched.

### 2. Canonical-hashtag decision

The ticket recommended *"a protected `release-cut` hashtag as the primary
unforgeable gate"*. We **adopt its intent** (an unforgeable primary gate) but
**reject its mechanism** (a hashtag), because:

- **A hashtag can never be the unforgeable anchor.** Hashtags are mutable by
  anyone holding `editHashtags`; ADR-0020 already leans on the ACL fact that
  ai-reviewer-bots lack `editHashtags` on `refs/for/*`, but that is a property of
  the *ACL*, not of the hashtag itself. The truly non-forgeable key is the
  **authenticated change owner**. So the unforgeable anchor is **`owner:sora`**,
  not any hashtag.
- **Adding a third `release-cut` *hashtag* would worsen Layer 3.** We already
  have two divergent forms (`milestone:R3-fastforward`, `R3-fastforward`);
  introducing a brand-new `release-cut` hashtag adds a third string to keep in
  sync. Instead, the `release-cut` token lives where it is naturally
  authoritative — the **topic** (matched by `intopic:release-cut`) and the
  merge-commit subject — and the hashtag stays in the existing family.

Therefore:

- **Unforgeable primary gate** = `owner:sora` (authenticated push identity).
- **Canonical release-cut marker** = the `release-cut` **topic token**
  (`intopic:release-cut`), already present in the merge-commit subject.
- **Canonical hashtag** = the namespaced **`milestone:R3-fastforward`**. The
  bare `R3-fastforward` is **accepted via `OR` during a transition window** so
  no flag-day is required while emitters converge. Once C3a confirms every
  emitter emits the namespaced form (and no doc/script references the bare
  form as authoritative), a follow-up may drop the `OR hashtag:R3-fastforward`
  arm. Until then, both are honoured.

### 3. Why this is strictly safer than the shipped key

`owner:sora` is both **correct** (it matches the real, operator-owned workflow —
Change #711) and **stronger** (non-forgeable) than the shipped
`author:<bot-list>` (which matched nothing real and was forgeable if it had).
`intopic:release-cut` is version-independent, so it does not silently de-gate on
SemVer variants. The hashtag `OR` removes the bare/namespaced de-gating hazard.
The fallback semantics are unchanged: a change missing any key still falls
through to `Human-Plus-2`.

## Downstream consumers — C2 / C3a

This ADR is the **single source of truth** for the canonical hashtag and the
corrected key. Its consumers reference it:

| Consumer | Area (out of scope here) | What it does | References |
|---|---|---|---|
| **C2** | devops / security | Replace the `applicableIf` line in `.gerrit/project.config` (and the byte-identical `.example` mirror) with the corrected key in §1; update the rule's comment block; update the OP-962 policy-contract tests (`backend/tests/test_merger_bot_main_promote.py`) that assert the old key. | ADR-0039 §1 (key), §2 (canonical hashtag) |
| **C3a** | backend | In `auto_promote_main`, set the release-cut change's **topic** to carry the `release-cut` token (so `intopic:release-cut` matches) and confirm `PROMOTE_HASHTAGS` emits the canonical `milestone:R3-fastforward`. | ADR-0039 §1 (`intopic`), §2 (canonical hashtag) |

C2 and C3a are out-of-area for this docs ticket; their cross-references to this
ADR land in their own DoD. This ADR exists so they have one unambiguous anchor.

> **Mirror note (backend follow-up, unchanged from ADR-0020).**
> `backend/submit_rule.py::evaluate_submit_rule` does not yet model
> `release-cut-promote`; mirroring the corrected key into it is the same
> area:backend follow-up ADR-0020 flagged, folded into C3a or filed separately.

## Alternatives considered

- **Keep `author:` but fix only the value (`author:^sora$`).** Rejected — still
  the wrong predicate (git author, forgeable). `owner:` is the only
  non-forgeable identity key.
- **Introduce a new protected `release-cut` hashtag (the ticket's literal
  recommendation).** Rejected — see §2: a hashtag cannot be the unforgeable
  anchor, and a third form worsens the divergence. The intent (an unforgeable
  gate) is met by `owner:sora`.
- **Pick one hashtag form and hard-cut (no `OR`).** Rejected for now — a
  flag-day risks a window where the emitter and the gate disagree and every cut
  silently de-gates (exactly the Layer-3 failure). The `OR` transition window
  removes that risk; the hard-cut becomes a safe follow-up once C3a confirms
  convergence.
- **Keep the version-anchored topic regex, just fix identity + hashtag.**
  Rejected — Layer 2 remains a live de-gating hazard on every SemVer variant.
  `intopic:release-cut` keys off the stable token instead.

## Consequences

- The `release-cut-promote` rule will actually fire on real (operator-owned)
  release cuts once C2 ships, so the merger-bot attestation ADR-0020 designed
  becomes live rather than dead.
- The gate's trust anchor moves from a (wrong, forgeable) git-author string to
  the authenticated change owner — a net security improvement.
- A transition window exists where both hashtag forms are honoured; closing it
  is a tracked follow-up, not a blocker.
- ADR-0020 §3's `applicableIf` is superseded; the cross-link is recorded in
  ADR-0020 (see its §3 / Schema lock-in note pointing here).

## References

- ADR-0020 §3 — `release-cut-promote` (the superseded `applicableIf`); §"Schema / contract lock-in" Hashtag row
- `.gerrit/project.config:253-291` — the shipped (buggy) `release-cut-promote` block
- `backend/agents/auto_promote_main.py:93` (`PROMOTE_HASHTAGS`), `:327-329` (`promote_topic_for_version`), `:348-352` (merge-commit subject `[release-cut …]`)
- `docs/audit/2026-05-12-audit-26-phase-0-verification.md:144,154-160` — the bare-vs-namespaced hashtag contract flag (Layer 3 origin)
- `docs/retrospectives/2026-05-16-prod-deploy-attempt-post-mortem.md:113` — Change #711 owned by `sora` (Layer 1 evidence)
- Gerrit predicate semantics: `owner:` (authenticated change owner) vs `author:` (git commit author); `intopic:` (topic substring/index) vs `topic:` (exact/regex)
