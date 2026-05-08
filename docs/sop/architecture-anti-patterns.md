# Architecture Anti-patterns Cookbook

**Status**: Living document. Add a new pattern when the same incident class
recurs in 2+ tickets. Format below is mandatory; cookbook is optimised for
quick lookup by ticket authors before they file work.

**Origin**: 2026-05-08 hardening sprint extracted these patterns retroactively
across ~70 tickets and ~6 cascade-MTTR incidents. The same trap had already
caught us multiple times before being documented; the goal of this file is
to break that cycle.

**How to use**: When filing a new ticket, scan headings for matching symptoms.
If you recognise one, follow the Cure section's recipe and reference the
existing tickets in your new ticket's Spec References section. Don't redesign
the fix from scratch — these patterns have already been costed.

---

## Index

| # | Pattern | Symptom (one-liner) |
|---|---|---|
| 1 | [Numbered flat-file registry](#1-numbered-flat-file-registry) | Single file with sequential IDs + N writers = guaranteed conflicts |
| 2 | [Shared mutable git config across worktrees](#2-shared-mutable-git-config-across-worktrees) | Race on `.git/config` between sibling workers |
| 3 | [Idempotency-blind external mutation](#3-idempotency-blind-external-mutation) | Retry creates duplicate side effects (comments, audit rows, etc.) |
| 4 | [Push without commit](#4-push-without-commit) | Runner pushes when CLI made no commits → "no new changes" rejection |
| 5 | [Daemon without event cursor](#5-daemon-without-event-cursor) | Daemon crash drops in-flight events → state mismatch downstream |
| 6 | [Strict state machine on terminal events](#6-strict-state-machine-on-terminal-events) | Event handler skips the work because predecessor state was unexpected |
| 7 | [Synchronous external call without circuit breaker](#7-synchronous-external-call-without-circuit-breaker) | External service down → caller retries forever, no backoff |
| 8 | [Worktree state-leak across ticks](#8-worktree-state-leak-across-ticks) | Half-done git operation from previous tick poisons next tick |
| 9 | [Auto-resolver brittle to nested markers](#9-auto-resolver-brittle-to-nested-markers) | Regex resolver matches first conflict region, leaks remaining markers into commit |
| 10 | [Migration ticket fighting in-flight tickets](#10-migration-ticket-fighting-in-flight-tickets) | Structural ticket lands while siblings still write old format |
| 11 | [Self-referential text-match false positive](#11-self-referential-text-match-false-positive) | Bulk-action filter matches the meta-document about pattern X (which mentions X by name) |
| 12 | [Spike + final-version add/add scaffold race](#12-spike--final-version-addadd-scaffold-race) | "Validate framework" spike ships full scaffold; sibling "initial scaffold" PR add/add conflicts on every shared file |

---

## 1. Numbered flat-file registry

**Symptom**: Single mutable file (e.g. `lessons-learned.md`, `docs/adr/ADR-0010-*.md`,
`backend/main.py` router list, `lib/api.ts` exports) with sequential identifiers
or appended sections. Every new feature ticket touches it. Conflicts on every
parallel pair of PSes.

**Root cause**: Two writers competing for the same ID slot or the same line
range in a shared file. Git auto-merge cannot resolve because the contention
is *semantic* (whose entry takes slot N?), not structural.

**Cure**:
1. Decompose into per-file entries with content-addressable filenames
   (`L-OP-<ticket>-<slug>.md` style).
2. Auto-generate the index file from per-file frontmatter via a build script.
3. Add pre-commit + CI gate that asserts the index is in sync.
4. Update the DoD template so new tickets create per-file entries, not edit
   the index.

**Examples**:
- `docs/sop/lessons-learned.md` — fixed via OP-745 (per-file in `docs/sop/lessons/`)
- `docs/adr/` — H11.6 in OP-758 META
- `backend/main.py` router list — H11.1 in OP-758 META
- `lib/api.ts` exports — H11.2 in OP-758 META
- `auto-runner-jira.py` main() init checks — H11.3 in OP-758 META
- `backend/config.py` Settings class — H11.4 in OP-758 META
- `docs/sop/jira-ticket-conventions.md` numbered §1-§16 — H11.5 in OP-758 META

**Reference tickets**: OP-745, OP-758 (META), and any of its H11.1-H11.6 children.

**Spec template**: `docs/sop/jira-ticket-conventions.md` §14 was updated by OP-745
to reference per-file entries instead of editing the rolled-up file. Use that
as the template when applying this cure to a new resource.

---

## 2. Shared mutable git config across worktrees

**Symptom**: Two sibling workers (e.g. claude-bot and codex-bot runners) both
write `git config user.email` to satisfy their bot identity. The last one to
write wins. The other runner subsequently commits with the wrong author and
Gerrit rejects with "email not registered / lack 'forge author' permission".

**Root cause**: Without `extensions.worktreeConfig=true`, `git config` writes
to the shared `.git/config` regardless of which worktree the call originated
from. All sibling worktrees see the same value.

**Cure**:
1. Set `git config core.repositoryformatversion 1` and `git config extensions.worktreeConfig true` in main repo.
2. Set per-worktree identity with `git config --worktree user.email <bot>` after enabling.
3. Add a runtime assertion at runner startup that fails closed if extensions.worktreeConfig is not enabled.
4. Same defence applies to ANY config that needs per-worker isolation (signing key, commit-msg hook, signing identity, etc.).

**Examples**:
- 2026-05-08 incident: claude/codex runner forge-author cascade (16 tickets stuck 5-7h)
- 2026-05-07 OP-108 was the first instance — manually salvaged via cherry-pick + `--reset-author`

**Reference tickets**: OP-729 (the permanent fix), OP-108 (one-shot salvage that didn't generalise).

**Generalisation**: Any "shared mutable resource where N workers expect their writes to be isolated" has the same anti-pattern. Look for:
- Shared OS environment variables in cron context
- Shared lockfiles claimed without exclusivity check
- Shared cache directories without per-worker namespacing

---

## 3. Idempotency-blind external mutation

**Symptom**: When an external API call (JIRA `add_comment`, audit log insert,
notification send) is retried after a transient failure, duplicate side
effects appear (multiple comments saying the same thing, multiple audit rows
for one logical event).

**Root cause**: Caller does not pass an idempotency key. Server assumes each
request is a new logical event. Most retry frameworks (urllib, requests,
exponential backoff helpers) do not add idempotency keys by default.

**Cure**:
1. Each external mutation generates a stable idempotency key from inputs
   (e.g. `f"add_comment:{ticket}:{hash(body)}:{minute}"`).
2. Server-side dedup table records (idem_key → response, ts) with TTL.
3. Before making the call, check local SQLite cache; if key exists, return cached response.
4. After successful call, record (idem_key, response).
5. Pair with circuit breakers (pattern #7) so transient failures do not
   spawn N retries each spawning N comments.

**Examples**:
- JIRA `add_comment` duplicate cascade during runner crash recovery (multiple "stuck-recovery" comments on the same ticket)
- Audit log `merger_agent_vote` rows duplicated when bridge daemon restarted mid-write

**Reference tickets**: OP-749 (H8 idempotency keys + circuit breakers).

**Counter-example (already idempotent)**: Gerrit `git push refs/for/develop` is idempotent by Change-Id; pushing the same commit twice produces zero new changes (just "no new changes" message). Use this property where possible.

---

## 4. Push without commit

**Symptom**: `git push` returns `! [remote rejected] HEAD -> refs/for/develop (no new changes)`. Runner posts a `[runner-gerrit-push-fail]` comment and exits rc=1, leaving the JIRA ticket stuck at 進行中 forever.

**Root cause**: CLI completed (rc=0) but did not actually create a commit
(e.g. self-aborted as out-of-scope, or made empty changes). Runner blindly
attempted push because the gate was "rc==0" not "rc==0 AND HEAD moved".

**Cure**:
1. Pre-push check: `git rev-list HEAD ^origin/develop --count` must be >= 1
   before invoking `git push`. If 0 commits, transition ticket back to To Do
   with reason "CLI produced no committable changes".
2. If push fails despite pre-check (race with parallel merge), categorize
   the failure (`no_new_changes` / `invalid_author` / `merge_conflict` / etc.)
   and dispatch to the right action — see pattern #6 for state-machine details.

**Examples**:
- 2026-05-08 OP-214 series — codex CLI halted as "operator: refine before pickup"; runner pushed empty; rc=1; ticket stuck for hours
- 59 occurrences in single-night codex log (~10% of all ticks)

**Reference tickets**: OP-720 Phase 2 (empty-push suppress), OP-760 (H13 push-fail classifier with `no_new_changes` action).

---

## 5. Daemon without event cursor

**Symptom**: Long-running event-stream daemon (gerrit-jira-bridge,
notification consumer, etc.) crashes or reconnects. Events that occurred
during the gap are lost. Downstream state diverges from upstream truth (e.g.
Gerrit shows "merged" but JIRA stays at "Under Review").

**Root cause**: Daemon's connection-recovery logic (systemd-restart, SSH
reconnect-on-error) restarts at the *current* event position, not at the
last successfully-processed event. There is no cursor; replay is impossible.

**Cure**:
1. Persist `(last_processed_event_id, timestamp)` to disk after each
   successful handle. Atomic write (tmp + rename).
2. On restart/reconnect, load cursor and replay missed events by querying
   upstream with a `--since cursor.timestamp` filter.
3. Handlers must be idempotent (pattern #6 — terminal events permissive)
   so replay is safe.
4. Audit log distinguishes live vs replayed events with `_backfilled=True`.

**Examples**:
- 2026-05-08: bridge `gerrit_reconnects=1` heartbeat → 15 JIRA tickets stuck "ticket_unexpected_status_skip" for 1.5+ days
- OP-689 daemon's catchup phase was a partial fix; full event cursor came in OP-748 H4

**Reference tickets**: OP-748 (H4, the cursor + replay).

**Note**: This pattern is the dual of pattern #3 — pattern #3 prevents duplicate effects on retry; pattern #5 ensures retry actually happens. Both are needed for at-least-once delivery.

---

## 6. Strict state machine on terminal events

**Symptom**: Event handler ignores a terminal event because the entity is in
an "unexpected" predecessor state. The work that triggered the event still
happened upstream, but downstream never converges. Logs show `unexpected_status_skip`.

**Root cause**: Handler implements the canonical happy-path transition (e.g.
`Approved → Published` on Gerrit-merged event) and silently skips when state
is anything else. But real systems often skip intermediate states (operator
direct-submit from Under Review, runner transition fail mid-flight, etc.).

**Cure**: Terminal events should converge state from any safe predecessor:

```python
if state == TERMINAL: return  # idempotent, already done
if state == ARCHIVED: return  # operator force-closed; don't unarchive
# Otherwise force-walk through all intermediate states to TERMINAL
for intermediate in path_from(state, TERMINAL):
    transition(intermediate)
```

**Examples**:
- Bridge daemon's `change-merged → 公開済み` transition skipped 15 tickets in single session because they were at 進行中 / Under Review (not the expected 承認済み)
- Same pattern: runner-side `pre_pickup_ok` was strict on initial state; relaxed in OP-759 H12 to also handle "ticket To Do but Gerrit already merged" via direct-walk to 公開済み

**Reference tickets**: OP-743 (bridge force-walk), OP-759 (H12 runner-side equivalent).

**Generalisation**: Anywhere an event "informs about something already authoritatively true upstream", the handler should converge state, not gate on assumed predecessor.

---

## 7. Synchronous external call without circuit breaker

**Symptom**: External service is unreachable. Code retries every tick (cron / loop) at full rate, generating noise without progress. When the service recovers, all retries succeed simultaneously, creating a thundering herd.

**Root cause**: No circuit breaker. No backoff. No notion that "if 5 calls in a row failed, the service is probably down — stop trying for a minute."

**Cure**:
1. CircuitBreaker per external service (gerrit_ssh / gerrit_rest / jira_rest / backend_rest / notification).
2. State machine: closed (normal) → open (after N consecutive failures, recent_open=True for M seconds) → half-open (one probe call) → closed (probe succeeded) or open (probe failed, reset counter).
3. Open state: caller skips operations + posts "external service unreachable" alert (pattern #5 + idempotency-keyed so alerts dedupe).
4. Tunable thresholds via env (`OMNISIGHT_CIRCUIT_BREAKER_FAILURE_THRESHOLD=5`, `_RECOVERY_TIMEOUT=60`).

**Examples**:
- 2026-05-08 codex runner kept retrying every 90s while Gerrit was unreachable — 30 min outage produced 20+ stuck JIRA tickets
- bridge daemon retries gerrit query × forever during sora.services maintenance

**Reference tickets**: OP-749 (H8 — combined idempotency + circuit breaker).

---

## 8. Worktree state-leak across ticks

**Symptom**: Subsequent runner tick fails with "cannot switch branch while rebasing" / "cherry-pick in progress" / "merge conflict not resolved". The current tick did nothing wrong; the leak came from a previous tick that crashed mid-operation.

**Root cause**: `.git/rebase-merge/`, `.git/CHERRY_PICK_HEAD`, `.git/MERGE_HEAD`, `.git/BISECT_LOG`, `.git/REVERT_HEAD` artifacts persist across ticks. The next tick's `git switch -C ...` cannot operate while these exist.

**Cure**:
1. Pre-sync guard at tick start: detect any of the 6 artifact paths.
2. Auto-recover: invoke matching `--abort` or `--quit` command per artifact.
3. Failure to recover → halt + escalate (don't make it worse).

**Examples**:
- 2026-05-08 codex runner stuck 1.5h / 60 ticks after OP-167 left in-progress rebase

**Reference tickets**: OP-736 (worktree state-leak prevention).

**Cause prevention** (separate from cure): the original mid-tick crash should
also be addressed — e.g. `git rebase ... --keep-empty` to avoid pause on
empty commits (the specific bug that triggered OP-167's stuck rebase).

---

## 9. Auto-resolver brittle to nested markers

**Symptom**: An automated conflict resolver "successfully" resolves a conflict, commits, pushes — but the resulting PS has unresolved conflict markers (`<<<<<<<` / `=======` / `>>>>>>>`) embedded as literal text in a tracked file. Subsequent rebase attempts fail because git reads the markers as text.

**Root cause**: Resolver uses `re.search` (returns first match) and assumes exactly one conflict region per file. When a file has nested or sequential conflicts (e.g. from prior failed rebases), the resolver misses the outer markers.

**Cure**:
1. After resolution, validate `git status` does not show file as `UU` (unmerged).
2. Validate file contents do not contain `^<<<<<<<` / `^=======` / `^>>>>>>>` lines.
3. Use `re.findall` not `re.search`, and iterate until no markers remain.
4. Reject commits that contain conflict markers via pre-commit hook.

**Examples**:
- 2026-05-08 #186 PS4 corruption: my own resolver script wrote nested conflict markers into committed file; ~25 min to manually clean

**Reference tickets**: OP-745 evidence section (the live evidence for OP-745's per-file restructure).

---

## 10. Migration ticket fighting in-flight tickets

**Symptom**: A structural / migration ticket (e.g. "split lessons-learned.md into per-file") merges into develop. Within minutes, multiple in-flight PSes that wrote to the OLD format hit conflict, requiring each to be reformatted.

**Root cause**: Migration tickets land via the same pickup queue as normal feature tickets. While the migration is in flight, sibling tickets are still picking the same file scope (because they don't know a migration is happening). After migration merges, the in-flight PSes are immediately stale.

**Cure**:
1. Migration ticket is labeled `migration:in-flight` + `migration:scope=<glob>` (e.g. `docs/sop/lessons/*`).
2. Runner `pre_pickup_ok` checks for active migrations with overlapping scope; skips with reason "blocked by migration OP-XXX".
3. Operator can manually override with `migration:override` label (audit logged).
4. When migration ticket transitions to 公開済み, in-flight PS authors get a notification "your work conflicts with merged migration X; please rebase".

**Examples**:
- 2026-05-08 OP-745 (lessons restructure) shipped while OP-746 was writing L32 in old format → conflict + manual reformatting
- Same ticket OP-745 itself hit the conflict (its own PS conflicted on lessons-learned.md), demonstrating the bootstrap-during-migration trap

**Reference tickets**: OP-752 (H2 migration freeze label + runner pickup gate).

**Generalisation**: Any "we're rewriting how X works" ticket needs a freeze window. The freeze should be expressed as a label that automation honours, not a Slack message that humans hopefully read.


## 11. Self-referential text-match false positive

**Symptom**: A bulk-action filter that uses full-text search to identify tickets matching pattern X (e.g. `text ~ "refine before pickup"`) accidentally matches the META ticket *about* pattern X. The meta-ticket gets the bulk action applied to itself, often disabling its own ability to ship the fix.

**Root cause**: Full-text JQL / grep matches on substring presence regardless of meaning. A ticket whose Goal is "build a script to handle the 'refine before pickup' placeholder pattern" contains the search term as its subject matter, not as a placeholder. The filter cannot distinguish "uses X" from "discusses X".

**Cure**:
1. **Whitelist the meta-document**: bulk-action scripts must exclude tickets whose body indicates discussion-of-pattern, e.g. `AND text !~ "META"`, `AND labels != "meta:about-pattern"`, or excluding paths under `docs/sop/`.
2. **Negative match heuristic**: if ticket Goal section contains the placeholder phrase OR the ticket has more than one occurrence of the phrase (true placeholders are 1-shot insertions), skip from bulk action.
3. **Two-stage filter**: bulk-action proposes targets first (dry-run output), operator eyeballs the list before committing the action. Self-references stand out at this review step.
4. **Tag meta-tickets explicitly**: add a `meta:about-X` label when filing. Bulk-actions exclude this label class.

**Examples**:
- 2026-05-08 OP-781 (the AI-assisted refinement helper itself) was caught in the "refine before pickup" bulk-pause sweep — its description discusses the placeholder phrase as its subject, hitting the JQL filter. Required immediate manual restoration (Story type + label removal).
- Hypothetical: a bulk-action over tickets that "mention `eslint`" would catch the ticket *about adding eslint*, possibly excluding it from a queue it needs to be in.

**Reference tickets**: OP-737 (ticket creation helper — could grow detection logic for self-reference patterns), OP-781 (concrete instance + post-incident audit comment).

**Generalisation**: Any time a tool decides "do X to all things matching pattern Y", consider whether the tool's own definition or test artifacts contain Y. Static rules of thumb:
- Documentation files (`docs/sop/**`, ADRs, retrospectives) discuss patterns — they should rarely be touched by content-pattern bulk actions.
- META tickets (label `priority:meta`) discuss children — children are the action targets, not the META.
- Test fixtures often contain example bad data deliberately — exclude `*/tests/**`, `*/fixtures/**` paths from production-content scans.

---

## 12. Spike + final-version add/add scaffold race

**Symptom**: A sprint splits "validate framework choice" into one ticket (E1: spike + ADR) and "build the proper scaffold" into a sibling ticket (E2). The spike ticket ships an *entire* working scaffold (mkdocs.yml, requirements.txt, docs/, theme files) instead of the minimum proof-of-concept it was scoped for. E2 is then written against a clean base, ADDs the same files, and `git merge` reports `add/add` conflicts on every shared scaffold file once E1 merges first.

**Root cause**: "Spike to validate" and "build the thing" overlap unbounded. The spike implementer doesn't know which exact files they shouldn't write — and from inside the spike, writing the full scaffold is the *easiest* way to demonstrate the framework works end-to-end. The sprint planner's intent ("E1 = throwaway, E2 = canonical") never becomes a code-level constraint, only a Goal-section english sentence.

Add/add is structurally different from edit/edit: there is no shared base for git to 3-way merge. Both sides claim to be the file-creator, and the resolver must pick one wholesale (or hand-merge). Auto-resolvers (the kind we'd build for OP-782) cannot pick between two greenfield versions semantically.

**Cure**:
1. **Spike-as-throwaway, written into spec**: E1's AC says "spike output stored in `_spike/`, NOT in the canonical path. Delete or move into E2 by hand." E2's AC starts with "remove `_spike/` if present, then build canonical scaffold."
2. **Single-PS scaffold ownership**: collapse E1+E2 into one ticket if the spike's natural output is the same file set as the final scaffold. Splitting only makes sense when the spike output is genuinely separable (e.g. spike is a rendered screenshot or benchmark report, not code).
3. **File-set lock at planning time**: each child ticket declares its `Files / Paths` section explicitly. CI / pre-pickup gate refuses to start E2 if E1's open PS already declares the same file paths. This is OP-731 (file-mutex pickup gate) — shipped at the JIRA-sibling layer. **OP-800 extends the same gate to the runtime layer** (`backend/agents/jira_dispatch.py::file_mutex_check`): every pickup attempt also queries `_open_bot_owned_file_owners()` and refuses to start a candidate whose declared paths overlap a currently-open Gerrit PS, regardless of the sibling ticket's JIRA status. The skip comment cites the blocker's Gerrit change number, links back here, and lists the three resolution paths (merge / rebase / re-scope). Operator escape hatch: label `runner-mutex-override:file-overlap` on the candidate ticket bypasses the gate when the operator has an explicit hand-merge plan.
4. **Resolution playbook for when it fires**: take *theirs* (the final-version PS) — wholesale replace the spike's version. Don't try to merge spike + canonical; the final-version author has the more complete mental model.

**Examples**:
- 2026-05-08 Sprint E: OP-785 (E1 framework decision + 1h spike) shipped a full `docs-site/` scaffold with mkdocs.yml, requirements.txt, docs/index.md, docs/lessons/index.md, stylesheets/extra.css. OP-786 (E2 initial scaffold) was implemented against clean develop, ADDed all the same files. After E1 merged, E2's PS hit add/add on 5 files. Operator manually rebased + took theirs (E2's canonical version, which had a more complete 80-line mkdocs.yml + Makefile + ADR/lessons/operations/runbook/sop indices). See Gerrit #264 PS2.
- General: any "spike" that produces the same file extension and path as the "final" version is at risk — frontend prototypes vs. final components, alembic spike migrations vs. proper migration, dockerfile experiments vs. canonical Dockerfile.

**Reference tickets**: OP-785 (E1 spike that overshot), OP-786 (E2 canonical that hit add/add), OP-784 (Sprint E META — should grow a "spike scope discipline" line in its DoD), OP-800 (cure 3 runtime-layer gate: extended OP-731's file-mutex to detect overlap with currently-open Gerrit PSes).

**Generalisation**: Two tickets that legitimately need to write to the same final files cannot run in parallel without a chosen winner. Either merge them, or scope one to a non-canonical output path with an explicit promotion step. "Spike" + "implementation" feels orthogonal in planning but is structurally identical at the file-system level.

---

## Cross-cutting principles

After 12 patterns, common threads:

1. **Idempotency is non-negotiable** for any retry-eligible operation.
2. **Convergence over correctness-of-predecessor** for terminal events.
3. **Per-file with auto-generated index** beats numbered shared file every time.
4. **Circuit breakers + cursors + audit trails** are the trinity for any long-running event-driven daemon.
5. **Make the cure mechanical** (script / label / hook) so future tickets cannot accidentally re-enter the trap.
6. **Validate post-resolution**, not just pre-input. (Pattern #9 caught after corrupt commit was already pushed.)
7. **Migration is a state, not a moment** — has a beginning, freeze period, and end.
8. **Filters cannot distinguish "uses X" from "discusses X"** — exclude documentation + META + test paths from content-pattern bulk actions. (Pattern #11.)
9. **Two tickets writing to the same final file path cannot run in parallel without a chosen winner** — merge the tickets or scope one to a non-canonical output path with an explicit promotion step. (Pattern #12; "spike" is not orthogonal to "implementation" at the filesystem level.)

If you see a new symptom not in this cookbook, file it as the 13th pattern after the same incident class hits 2+ tickets. Don't add patterns for one-off hypothetical concerns.

---

## See also

- `docs/sop/jira-ticket-conventions.md` — referenced by every ticket DoD; includes pointer back here from §14
- `docs/sop/lessons-learned/` — per-file lesson archive with concrete incident notes for each pattern
- META tickets:
  - `OP-720` (stuck-ticket recovery)
  - `OP-721` (fleet observability + escape)
  - `OP-730` (throughput control + conflict prevention)
  - `OP-739` (CI parallel-gate)
  - `OP-747` (cascade prevention + partial-state recovery)
  - `OP-758` (auto-import / decorator registry refactor — H11)
  - `OP-761` (Sprint D — deployment automation)
  - `OP-784` (Sprint E — docs-site build-time generation; Pattern 12 incident)
