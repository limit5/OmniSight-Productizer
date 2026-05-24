# SOP — EPIC decomposition → multi-party audit → runner blind-test → file → execute

**Purpose**: a reusable, battle-tested pipeline for turning a large/ambiguous capability into
correctly-scoped, runner-safe JIRA tickets WITHOUT goal-drift, shipped-but-not-deployed, or
blast-radius surprises. Distilled from the DAG-executor EPIC run (2026-05-24). Apply this whenever
a piece of work is bigger than ~1 ticket or touches shared subsystems (runner/release/workflow/etc.).

Companion: `docs/sop/jira-ticket-conventions.md` (the label/format rules this SOP builds on).

---

## When to use
- Any capability that is "an epic, not a ticket" (multi-day, multiple subsystems, or unclear scope).
- Any time the first investigation reveals the premise is shakier than assumed (e.g. "the thing we'd
  build on doesn't exist") — STOP and run the audit stages before committing to a build.

## The pipeline (each stage gates the next)

### Stage 1 — Discovery / premise check
Before designing, verify the premise with first-hand evidence (grep + read, not assumption). Ask:
does the thing we're about to build/extend actually exist + work? Distinguish **what works** from
**what's scaffolding**. If the premise is wrong, surface it and re-decide before building.

### Stage 2 — 3-way audit (me + sub-agent + codex), same brief
For consequential findings, run the SAME written brief through THREE independent auditors in parallel:
1. **me** (primary, first-hand grep/read),
2. a **general-purpose sub-agent** (`Agent`, `run_in_background:true`),
3. **codex** (CLI — see "codex invocation" below).
Write ONE brief file; the brief must instruct each auditor to **try to DISPROVE** the claim, cite
`file:line`, and distinguish evidence vs inference. Then do a **three-way comparison**: consensus =
high confidence; divergences get reconciled explicitly (usually a framing difference, not a fact
conflict). Save codex output under `docs/audit/codex-reviews/<slug>-codex-YYYY-MM-DD.txt`.

Use the 3-way audit again for **expanded impact** (blast radius across runner / deploy / workflow /
work-path / release-flow / external-links / agent-memory) before committing to build a cross-cutting
feature.

### Stage 3 — EPIC design doc → codex review → fold in
Write `docs/architecture/YYYY-MM-DD-<slug>-epic-design.md`: problem, what's reusable vs missing,
target architecture, child-story breakdown, phasing + effort, **open questions for codex**. Run codex
adversarially against it; fold the verdict into a "codex review round N — folded in" section. Iterate
until SOUND / SOUND-WITH-CHANGES.

### Stage 4 — Ticket-split draft → codex audit → fold in
Write `docs/architecture/YYYY-MM-DD-<slug>-ticket-filing-draft.md`. Apply the **granularity
principle**: one ticket = one coherent, independently-testable, single-CLI-session, area-coherent,
independently-revertable deliverable. De-risk order: **data-model/schema changes land FIRST, disabled,
zero behavior change** (the "safe floor"); then the feature, **disabled-by-default**; then
operator-gated activation LAST. Each ticket carries: 6-tag-prefixed title, type/tier/areas/scope,
4-AC (Code/Deploy/Integration/Exercised) + Go-Live, scope-anchor (Goal + Files + Spec-ref §), real
Blocks deps, and a **`MUST NOT` boundary sentence**. Run codex against the draft (granularity, 4-AC
honesty, runner-pickability/tier, **goal-drift risk**, dep edges); fold in.

### Stage 5 — Runner BLIND-TEST dry-run (the goal-drift gate) ⭐
This is the stage that proves the tickets are runner-safe. For 2-3 representative agent:auto tickets
(always include the highest-drift one + a clean control):
- spawn a **fresh `general-purpose` Agent, `isolation:worktree`, `run_in_background:true`**, given
  **ONLY the ticket text a runner would see** (no conversation context),
- instruct: "produce your implementation PLAN only, do NOT write code; list files you'd touch, where
  you STOP, ambiguities, and anything you'd be tempted to do beyond the Files list."
- **Assess for drift**: does it stay within Files? respect the MUST-NOT? avoid grabbing other tickets'
  scope? The control should be clean; the risky one reveals whether the MUST-NOT + scope-anchor hold.
A good MUST-NOT makes the agent say "I would have been tempted to X, but the MUST-NOT stopped me."
Feed every ambiguity the agents flag back into the AC wording (a shared under-spec = a real ticket
defect to fix, not drift). Re-run if you made structural changes.

### Stage 6 — File → wire deps → verify
File via `scripts/file_jira_ticket.py` (see mechanics). Wire Blocks links. Verify labels + links + that
tier:X tickets are runner-excluded. THEN execute (runner picks up the unblocked agent:auto leaves).

---

## `scripts/file_jira_ticket.py` mechanics (gotchas learned)
- Required flags: `--summary --description-file --priority {Highest..Lowest} --tier {S,M,L,X}
  --class {subscription-claude|subscription-codex|api-anthropic|api-openai} --type {bug|feature|docs|meta}
  --areas a,b --scope <name>`. Files **issuetype Story** always; `--type` is only a label.
- **Always stamps `agent:auto` + `priority:meta` + (for subscription-* classes) `capability:enable=gerrit_push`.**
  → For **human/operator-only tickets, file at `--tier X`** (the runner JQL excludes tier:X — that is the
  real gate), then **strip `agent:auto` + `capability:enable=gerrit_push` via the JIRA API** afterward so
  observability isn't misleading. `--no-push-capability` opts out of gerrit_push at file time.
- **`--check` dry-runs** (validate + print the pickup JQL, no POST). ALWAYS `--check` one representative
  ticket first.
- **Area-validator false-positive on Spec-ref citations**: a `docs/...md` spec-ref in the body makes the
  validator think the ticket delivers docs ("references files in ['docs'] but --areas ...") and ABORT.
  A citation is not a deliverable → pass **`--force`** (justified) OR phrase the spec-ref without a
  path-like token. Do NOT add `area:docs` for a mere citation (that would mis-route).
- Output on success: `created: https://soraapp.atlassian.net/browse/<KEY>` → parse `<KEY>`.
- It runs the OP-1124 hot-file mutex auto-inject — keep it (don't bypass for runner-pickable tickets).

## Blocks links (JIRA REST)
`POST /rest/api/3/issueLink` with `type:{name:"Blocks"}`, **inwardIssue = the BLOCKER**, **outwardIssue
= the BLOCKED**. I invert this almost every time — **verify direction after every POST**
([[feedback_jira_issuelink_direction]]). Delete wrong edges before re-POSTing; JIRA has no cycle detection.

## JIRA auth (same as the close/transition helper)
Creds at `~/.config/omnisight/jira-claude.env` + `jira-claude-token`; `OMNISIGHT_JIRA_CLAUDE_EMAIL`;
Basic auth; site `OMNISIGHT_JIRA_SITE_URL`. Transitions: To Do→進行中(21)→Under Review→Approve(4)→
承認済み→公開済み. statusCategory/locale-safe.

## codex invocation (for the audit/review stages)
PATH `codex` (/mnt/c Windows) is BROKEN. Use `/home/user/.nvm/versions/node/v24.14.1/bin/codex exec
--skip-git-repo-check < /tmp/prompt.txt` (prompt via STDIN — positional arg hangs). Run from the repo
workdir so file:line cites resolve. Output can be large; grep for `VERDICT|SOUND|NEEDS`. See
[[reference_codex_cli_binary]].

## What per-ticket review + blind-test MISS (the end-to-end run catches) ⭐
Stage 4 (codex ticket audit) + Stage 5 (runner blind-test) are excellent at
*per-ticket* scope/safety/MUST-NOT — they keep each ticket from over-reaching. But
the DAG-executor first-run (P1-9b, 2026-05-24) proved they have two blind spots,
because both reason about ONE ticket at a time:
1. **Cross-ticket integration glue.** Each component shipped its function "inert /
   not wired into the loop." No single ticket owned *assembling* them into the run
   loop — so `DagExecutor.run()` stayed a heartbeat skeleton and nothing actually
   executed a plan. **When decomposing, always file an explicit "wire/assemble the
   components into the live entrypoint" ticket** (and make it the integration gate
   the others block), or the pieces sit disconnected and every per-ticket review
   passes while the whole does nothing.
2. **Payload / data correctness.** The smoke DAG's `compile` task shipped
   `inputs:[]` → the executor copies no source into the scratch workspace → cmake
   has nothing to build → it can never reach `completed`. Scope review can't see
   this; only running the real payload end-to-end does.
**Mitigation: before declaring an epic "done," do a real end-to-end run against a
real (dev) DB with the real payload — not just the green unit suite.** A passing
component suite + clean per-ticket scope is necessary, NOT sufficient. Budget a
"first real run finds 2-3 integration/payload gaps" follow-up round; that is normal,
not failure. Verifying from develop *source* against the dev DB is a fine substitute
when the container image is stale (don't block the verification on an image rebuild).

## Hard rules that bit us (carry forward)
- **HANDOFF.md is FROZEN** — codex keeps appending an "analysis entry"; `git checkout -- HANDOFF.md`.
- **TODO.md is Tier-B-forbidden for runners** — never in an agent:auto ticket's scope; doc-honesty
  edits to it go in a tier:X/Claude-owned ticket.
- **test_assets/ is read-only** — test fixtures go in a test-local tmp dir.
- **Doc commits use a META/parked key, never an impl key** (H12 auto-walk trap).
- After landing: update the memories the work makes stale (cold-start inventory, the "unimplemented"
  finding, staging-gate gap, etc.).
