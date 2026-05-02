# Multi-Agent Coordination

> **Status**: Active operating contract for Claude (Opus) + Codex (GPT)
> + human operator. Last updated 2026-05-02.
> **Companions**: `CLAUDE.md` (Claude's L1 rules), `AGENTS.md` (Codex's
> L1 rules), `docs/operations/runner-strategy.md` (architecture
> rationale).

---

## What this doc is

This is the **single source of truth** for "who does what" on
OmniSight-Productizer when more than one agent (or operator) is active
on the repository simultaneously. It exists because:

  * Multiple Claude runners on the same git branch is safe (proven
    empirically — same model, consistent judgement, file-level
    non-overlap when sections differ).
  * Adding Codex breaks the "consistent judgement" assumption →
    needs explicit boundaries.
  * Without those boundaries, throughput goes negative (time spent
    cleaning up exceeds time saved by parallelism).

If you are a new agent / operator joining this repo, read this doc
first, then `runner-strategy.md`, then your model-specific rules
(`CLAUDE.md` if you're Claude, `AGENTS.md` if you're Codex).

---

## Section ownership (default routing)

| TODO area | Default owner | Reason |
|---|---|---|
| `backend/agents/*` infra | Claude | Load-bearing internal runtime; consistency critical |
| `backend/auth.py` / `audit.py` / multi-tenant boundary | Claude | Security domain, design judgement |
| `KS.*` (encryption / multi-tenant) | Claude | Compliance + design |
| `BP.A` (templates) / `BP.B` (Guild reorg) / `BP.C` (T-shirt gateway) / `BP.F` (model mapping) / `BP.H` (penalty) | Claude | Cross-subsystem integration |
| `BP.A2A` / `BP.Q` (RAG) | Claude | New architectural pieces |
| `W11`-`W16` (web sandbox / clone / preview / orchestrator) | Claude | Multi-subsystem; Codex would burn on max_tokens |
| `HD.*` (high-density PCB / SI) | Claude | Domain-specific judgement |
| `WP.*` (Wave-1 platform plumbing) | Claude | Cross-cutting infra |
| `BP.D` (compliance matrices) | Claude | Auxiliary disclaimer + legal review |
| `BP.K` (frontend 6 components) | Mixed | Architectural changes → Claude; UI text/styling → Codex |
| **`FS.*` (full-stack adapter scaffolds)** | **Codex** | Pattern-replicated across adapters |
| **`SC.*` (security scanning integrations)** | **Codex** | Wrapping existing tools (CodeQL/Semgrep/etc.) |
| **`BP.D.7` (10+ audit skills markdown)** | **Codex** | Pattern-heavy markdown, GPT structures well |
| **`BP.W3.1` (D3-D29 27 skill packs rework)** | **Codex** | High-volume pattern replication |
| **`BP.G` (TDD dual-patchset)** | **Codex** | Mechanical Gerrit hooks |
| **`BP.J.2` (post-merge git hook for self_healing_docs)** | **Codex** | Single-file glue, ~50 LOC |
| **Documentation updates** (operator runbooks, READMEs) | **Codex** | Markdown is GPT's strong domain |
| **Tests for already-implemented code** (`*.5 Tests` items in `FS.*` / `SC.*`) | **Codex** | Mirroring existing test classes |
| **ESLint 113 findings cleanup** (`B9`) | **Codex** | High-volume mechanical fixes |

**Default rule when ambiguous**: ownership goes to Claude. Better to
slow Claude down than have Codex make judgement calls in
unfamiliar territory.

The human operator can override any assignment for specific tasks.

---

## Tier rules (per-task safety classification)

Every task an agent picks up is **Tier A** or **Tier B**.

### Tier A — direct commit to current branch

Use when **all** of these hold:

  * Output is structurally constrained (you're following a pattern
    that exists at least 2x in the codebase already)
  * Single file or 1-2 closely-related files
  * If your output is wrong, recovery is `git revert <commit>` and
    nothing else breaks
  * Task description fits in 1 line plus an example

Examples:
  * "Add SES adapter mirroring Resend adapter shape"
  * "Add 5 more test cases to TestApiChangeReport class following
    existing pattern"
  * "Update operator runbook to reflect new env knob X"

### Tier B — commit to isolated branch, review before merge

Use when **any** of these hold:

  * Multi-file change spanning logically separate concerns
  * New module / new public API surface
  * Anything touching auth / encryption / audit / multi-tenant
  * Task description requires interpretation of intent
  * Ownership is "Mixed" in the table above
  * **You're not sure which Tier**

Worktree workflow for Tier B (Codex):
```bash
# Codex operates from this worktree (set up by human, see below)
cd ../OmniSight-codex-worktree
git switch codex-work          # already on this branch by default
# ... do work, commit, etc ...
# When done, human or Claude reviews, then:
cd /path/to/main/checkout
git merge codex-work           # operator merges manually
```

### Decision rule

If you are uncertain, **assume Tier B**. Tier B has higher coordination
cost but lower failure cost. Tier A failures land directly in the main
branch — costlier to clean up.

---

## Worktree layout (local-only, no remote push)

The repo has 1 main checkout at `/home/user/work/sora/OmniSight-Productizer`
(branch: `master`) plus 1 codex-dedicated worktree.

Setup (one-time, done by Claude on 2026-05-02):
```bash
git -C /home/user/work/sora/OmniSight-Productizer branch codex-work master
git -C /home/user/work/sora/OmniSight-Productizer worktree add \
    /home/user/work/sora/OmniSight-codex-worktree codex-work
```

After setup, two physical directories share the same `.git`:
  * `/home/user/work/sora/OmniSight-Productizer/`        → branch `master`
  * `/home/user/work/sora/OmniSight-codex-worktree/`     → branch `codex-work`

**Routing convention**:
  * Claude runners: always work from the main checkout on `master`.
  * Codex Tier A tasks: from main checkout on `master` (rare — only for
    pattern-replication tasks the human pre-approved as Tier A).
  * Codex Tier B tasks (default): from worktree on `codex-work`.

Codex's runner script picks the working directory automatically based
on the task's tier (see `auto-runner-codex.py`).

---

## Same-branch parallel-runner safety contract

When multiple runners (Claude × N + maybe Codex Tier A) work on the
**same** branch simultaneously, the following invariants must hold —
otherwise concurrent commits / TODO writes corrupt state:

1. **Section non-overlap**: each runner has a distinct
   `OMNISIGHT_RUNNER_FILTER` — no two runners pick from the same
   section. Reading the section table above, each section has at most
   one default owner; even when human overrides, no two concurrent
   runners share a section.

2. **File non-overlap**: cross-section file overlap (e.g., two
   sections both touching `backend/main.py`) means the section
   ownership default is wrong — escalate to human, do not work in
   parallel on overlapping files.

3. **TODO marker disjoint**: Claude marks `[x][C]` / `[!][C]` /
   `[~][C]` / `[O][C]`; Codex marks `[x][G]` / `[!][G]` / `[~][G]` /
   `[O][G]`. Each runner only modifies markers with its own letter.
   Reading another's marker is fine; modifying is not.

4. **HANDOFF.md heading prefix**: Claude entries start with `##
   [Claude/Opus]`; Codex entries start with `## [Codex/GPT-5.5]`.
   Append-only, never edit other agent's entries.

5. **Commit messages MUST be atomic and authored**: every commit
   carries Co-Authored-By trailers identifying the agent
   (`Claude Opus 4.7` / `GPT-5.5 (codex-cli)`) plus env user + global
   user. `git log --grep "GPT-5.5"` and `git log --grep "Claude Opus"`
   should cleanly separate authorship for audits.

If any of these invariants is violated, **stop the runner**, clean up
manually, fix the routing, then resume.

---

## What if the agents disagree

Concrete scenarios + resolutions:

| Scenario | Resolution |
|---|---|
| Codex's Tier A commit lands in master with a style that Claude finds inconsistent | Human (or Claude) opens a discussion in HANDOFF; if needed, Claude can adjust style in a follow-up commit |
| Codex marks `[x][G]` but Claude's later session finds the work incomplete | Claude flips it to `[!][G]` (NOT `[!][C]` — preserves authorship) and writes a HANDOFF entry explaining the gap |
| Both agents independently start on the same task | The one running it second sees the section already has a marker change → stops, writes HANDOFF `[blocked]: race detected with <other agent>` |
| Codex needs to modify a Claude-owned file (e.g., `backend/agents/anthropic_native_client.py`) | Codex stops, writes `[codex-blocked]: this task seems to need changes to Claude-owned area; suggesting <X>`. Human / Claude takes over. |
| Claude wants to refactor a Codex-authored module | Claude does it on `master`. The refactor commit references the original Codex commit hash so audit trail is clear. No special protocol — Claude has architectural authority. |

The human operator is the final arbiter for any disagreement that
isn't resolvable via the rules above.

---

## What this doc does NOT cover

  * Remote git operations (push / pull / PR review on GitHub) — you
    may not push to remote per current operator policy.
  * Multi-tenant / multi-customer concerns — out of scope until
    OmniSight ships as a product.
  * Inter-agent direct communication (Claude → Codex via A2A) —
    BP.A2A epic owns this; not implemented yet.
  * Memory sync between agents — each agent has its own auto-memory
    (`~/.claude/projects/...` for Claude, `~/.codex/...` for Codex),
    they don't share. If we need cross-agent learnings, we collect
    them manually into `docs/agents/learnings.md` (deferred until
    needed).

---

## Quick reference card

If you're an agent and not sure what to do RIGHT NOW:

```
Q1: What's my model?
    Claude → CLAUDE.md is your rules; you can work directly on master.
    Codex  → AGENTS.md is your rules; default to codex-work branch.

Q2: Is the task on the section ownership table?
    Yes → that owner does it.
    No  → ask human, or default to Claude.

Q3: Is this Tier A (pattern-mirroring, 1-2 files) or Tier B (anything else)?
    Tier A → commit to current branch.
    Tier B → switch to codex-work (Codex) or stay on master (Claude).

Q4: How do I mark TODO when done?
    Claude → - [x][C]  (or [!][C] / [~][C] / [O][C])
    Codex  → - [x][G]  (or [!][G] / [~][G] / [O][G])

Q5: How do I sign HANDOFF entries?
    Claude → ## [Claude/Opus] <date> <item-id> ...
    Codex  → ## [Codex/GPT-5.5] <date> <item-id> ...

Q6: Stuck or uncertain?
    Stop. Write a [<agent>-blocked]: entry in HANDOFF. Wait for human.
```
