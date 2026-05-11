# Stuck 承認済み tickets — 2026-05-11 cleanup sweep (OP-920 / AUDIT-6)

* **Trigger**: `docs/audit/2026-05-11-deep-system-audit-late.md` §P2-3 flagged
  RPG / MP tickets parked at `status = 承認済み` (statusCategory `進行中`)
  with no forward transition to 公開済み.
* **Source ticket**: OP-920 (claude class; re-classed from codex on 2026-05-11
  because codex was stopped until weekly reset 2026-05-12 08:11).
* **Operator**: rt3628 — approved AUDIT-6 sweep before resuming Sprint F.
* **Bot**: claude-bot (`subscription-claude`).

---

## Scope clarification

The OP-920 description AC-1 says "9 RPG / MP tickets" but lists only 8 keys
(OP-214, OP-211, OP-205, OP-200, OP-164, OP-163, OP-160, OP-116). The same
8 keys appear in `docs/audit/2026-05-11-deep-system-audit-late.md` §P2-3.
A live JQL query at sweep time

```
project = OP AND status = Approved
```

returned exactly 8 issues, matching the listed set. **Actual scope: 8 tickets.**
The "9" in AC-1 is a header typo; no missing ticket.

---

## Method

For each ticket I checked, in order:

1. JIRA `GET /issue/{key}?fields=status` — confirm current state is `承認済み`.
2. Gerrit SSH query `message:OP-XXX` (against the live
   `omnisight/OmniSight-Productizer` project) — locate merged or abandoned
   change sets that reference the ticket. (The earlier
   `status:merged ... <key>` form used in the audit doc missed several
   merged changes because Gerrit indexes the JIRA key in commit messages,
   not in the change subject as a separate field; switching to
   `message:<key>` returned all of them.)
3. `git log --grep '[<key>]'` on `main` — confirm the merge commit is
   reachable from develop / main (sanity-check that the merged Gerrit
   change actually landed).

---

## Per-ticket evidence + decision

All 8 tickets resolve to the same disposition: shipped + on develop →
transition `承認済み` → `公開済み` (JIRA transition id 7 "Deploy").

| Ticket | Title | Gerrit # | Merge commit | Branch | Decision |
|---|---|---|---|---|---|
| OP-214 | RPG.W21.1 — Quarterly boss task | [#197](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/197) | `21b3c700` (Merge into develop, 2026-05-08 07:41 UTC) | develop | → 公開済み |
| OP-211 | RPG.W20.1 — Operator define multi-task campaign | [#148](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/148) | `7f351b7a` (Merge into develop, 2026-05-07 23:59 UTC) | develop | → 公開済み |
| OP-205 | RPG.W18.1 — Lv 50 secondary class unlock | [#141](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/141) | `b1ec3afe` (Merge into develop, 2026-05-07 23:58 UTC) | develop | → 公開済み |
| OP-200 | RPG.W15.2 — Debuff registry `Burnout` | [#154](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/154) | `a2eb59d2` (commit on develop, 2026-05-08 08:34 CST) | develop | → 公開済み (earlier PS #136 ABANDONED is the rework history, not a blocker) |
| OP-164 | RPG.W10.3 — Help dropdown "Replay agent tour" | [#152](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/152) | `20d7895c` (Merge into develop, 2026-05-08 00:20 UTC) | develop | → 公開済み (earlier PS #132 ABANDONED is the rework history) |
| OP-163 | RPG.W10.2 — `seen_rpg_tour` flag in user_preferences | [#151](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/151) | `be1ec768` (commit on develop, 2026-05-08 08:16 CST) | develop | → 公開済み (earlier PS #131 ABANDONED is the rework history) |
| OP-160 | RPG.W9.4 — Empty Guild placeholder + Recruit CTA | [#129](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/129) | `b9f4e980` (Merge into develop, 2026-05-07 23:55 UTC) | develop | → 公開済み |
| OP-116 | MP.W17.6 — System-prompt tool catalog injection | [#323](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/323) | `3b32e868` (commit on develop, 2026-05-09 07:52 CST) | develop | → 公開済み |

### Why these were stuck

These tickets were merged on Gerrit between 2026-05-07 23:55 UTC and
2026-05-09 07:52 CST. The Approved → Published transition is "bridge-only"
per ADR-0003 (see `TRANSITION_IDS` comment in
`backend/agents/jira_dispatch.py:347`): the
`gerrit-jira-bridge` daemon is meant to catch the `change-merged` event
and walk the ticket forward. The audit doc P0-3 / earlier triage suggests
the bridge missed these `change-merged` events (likely Sprint D
high-volume merge wave 2026-05-08 overran the consumer). The runner's
H12 self-heal (`force_walk_to_published`) is exactly the recovery path
for this case, and it is what this sweep used.

### Escalation gate (AC-4)

> "if 2+ tickets need revert-to-TODO, halt + file P0 follow-up before
> touching them."

Result: **0 reverts needed.** All 8 had a MERGED Gerrit change on
`develop`, so all 8 took the forward path. The gate did not trip — no
P0 follow-up required.

### Error-catalog outcomes

* `TransitionPermissionRefused` — **did not occur.** claude-bot has
  Deploy permission on the OP workflow; the test transition on OP-214
  succeeded cleanly and was used as the canary before processing the
  remaining 7.
* `EvidenceAmbiguous` — **did not occur.** Every ticket had exactly
  one MERGED Gerrit change on `develop`. Earlier ABANDONED patchsets
  (OP-200 #136, OP-164 #132, OP-163 #131) are rework history, not
  competing-branch ambiguity.
* `MultipleRevertNeeded` — **did not occur** (0 reverts).

---

## Execution

Per-ticket comment + transition fired by:

```python
# backend/agents/jira_dispatch.py helpers
jd.add_comment(client, key, "[2026-05-11 stuck-状況審計] ...",
               idem_key=f"op920-audit-comment-{key}-2026-05-11")
jd.force_walk_to_published(client, key,
               idem_key=f"op920-publish-{key}-2026-05-11")
```

Idempotency keys are stable so a re-run is a safe no-op (JIRA returns
409 / no-op on duplicate transition POSTs and the
`_request_idempotent` shim caches the response).

Final verification: after the sweep,

```
project = OP AND status = Approved
```

returns **0 issues**. DoD met:

> 0 tickets remain in 承認済み (statusCategory != 進行中) OR audit doc
> explains why a residual ticket is intentionally parked.

No residual tickets to explain.

---

## Lesson (for `docs/sop/lessons-learned.md`)

When auditing Gerrit-vs-JIRA hygiene, query Gerrit with
`message:<JIRA-key>` rather than just `<JIRA-key>`. The audit doc P2-3
used the bare-key form and missed 7 of 8 merged changes, which made the
audit reader think only OP-200 had shipped — when in fact all 8 had.
Same applies whenever you need "did this JIRA key reach a MERGED Gerrit
change at all". The `message:` qualifier matches the commit-message body
where the `[OP-XXX]` token lives.

---

## Cross-references

* Source audit doc: `docs/audit/2026-05-11-deep-system-audit-late.md` §P2-3
* Related P0 (bridge missed merges): same audit doc §P0-3
* SOP for stuck-ticket sweeps: `docs/sop/jira-ticket-conventions.md` §11
* Helper used: `backend.agents.jira_dispatch.force_walk_to_published`
  (`backend/agents/jira_dispatch.py:1362`)
