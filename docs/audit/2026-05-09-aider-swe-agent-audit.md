# External-Tool Audit — Aider + SWE-agent vs OmniSight SDK Runner

**Date**: 2026-05-09
**Triggered by**: User request during 24h absence — *"看看對我們的 api-anthropic runner 能不能派上用場，或是改善我們的系統，或是有什麼值得我們借鑑的地方"*
**Context**: 3 SDK-runner pilot failures (W14.5 / S1 launcher v1+v2+v3) — model loops on `find | grep` despite explicit `_SHELL_METACHARS` blacklist + `❌/✅` examples in system prompt. Lesson L-OP-802 hypothesised "structural capability gap"; this audit tests that hypothesis against two SoTA reference systems.
**Methodology**: WebFetch on `Aider-AI/aider` README + repomap docs + edit-formats + SWE-bench post + lint-test docs; `SWE-agent/SWE-agent` README + `tools/edit_anthropic/config.yaml` + `default.yaml` agent bundle config; cross-reference against Anthropic platform docs for built-in tool schemas.
**Scope**: identify borrowable patterns + integration paths, NOT full migration. Output mapped against existing Sprint A children (OP-808 META → OP-809–OP-823).

---

## Executive summary

| Source | Borrowable patterns | Integration cost | Priority |
|---|---|---|---|
| **Anthropic platform** (the smoking gun) | 1. **Built-in `text_editor_20250728` (str_replace_based_edit_tool)**<br>2. **Built-in `bash_20250124`** | LOW — schema-less, drop-in via `tools=[{type:..., name:...}]` | **P0 — addresses the pilot-failure root cause** |
| **SWE-agent** | 1. ACI bundle pattern (YAML-driven tool packs)<br>2. Cache control on last 2 messages<br>3. Submit-review checklist final gate<br>4. Docker-per-task isolation | MEDIUM — bundle structure can be partially borrowed; Docker overlaps with our worktree plan | P1 — composes well with Anthropic builtins |
| **Aider** | 1. Repo-map via PageRank on tree-sitter AST<br>2. Auto edit-format selection per model<br>3. Reflection loop with `max_reflections` bound<br>4. Auto-lint/test with auto-fix retry<br>5. Architect/editor model split | HIGH — tree-sitter integration is a non-trivial new dep; reflection loop has subtle interaction with our CostGuard | P1–P2 — repo-map + reflection are the high-impact borrows |

**Bottom line — three findings**:

1. **The single highest-leverage fix for the pilot failure is migrating to Anthropic's built-in `text_editor` + `bash` tools.** SWE-agent uses `str_replace_based_edit_tool` (the *exact* tool spec Sonnet is RLHF-trained on). Our generic `Read/Write/Edit/Bash` dispatcher forces the model to *infer* tool semantics from JSON schema descriptions — Sonnet handles this reliably for one-off use, but degrades on multi-step shell-heavy work, exactly the pattern our pilots exhibited. **This finding refactors Sprint A's P1: A1+A2 are partially obsoleted by a new `A18 — Anthropic-builtin-tool migration`.**
2. **Aider's repo-map is a strict superset of what we currently give the model**, which is "no codebase overview, only ticket text + AC". 70.3% file-identification rate on SWE-bench Lite *without* RAG/embeddings is the metric to beat — and it costs only ~1k input tokens at default config.
3. **What we already do better than both**: tier-aware governance (ADR-0005), JIRA-aware pickup loop, multi-bot coordination via `mutex_with`, CostGuard 80/100/120 alerts, production audit trail. Neither Aider nor SWE-agent has these — they are interactive tools with no persistent governance layer. Audit conclusion: *borrow tactics, keep our governance posture intact*.

---

## Finding A — Anthropic built-in tools (P0)

### What we observed

SWE-agent's `tools/edit_anthropic/config.yaml` defines a single tool: `str_replace_editor`, which is **not** SWE-agent's invention — it is Anthropic's built-in `text_editor_20250728` (alias `str_replace_based_edit_tool`). The official tool exposes 5 commands: `view` (line-numbered file or 2-level dir tree), `create` (no-overwrite), `str_replace` (exact-match find/replace), `insert` (line-N insertion), `undo_edit` (revert last). State is persistent across calls.

The Anthropic platform docs confirm:
- **Schema-less**: `{"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}` — the model has the schema baked in, no `input_schema` needs to be supplied.
- **Same for bash**: `{"type": "bash_20250124", "name": "bash"}` — built-in, with persistent shell state and `restart` action.
- **The model is trained on these specific tool names + schemas**. Custom tools require the model to RL-infer tool behaviour from prose descriptions; built-in tools are part of the model's RLHF distribution.

### Why this explains the pilot failure

Our `scripts/run_s1_via_anthropic_sdk.py` registered:
```python
RUNNER_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]
```
…which are **OmniSight-Productizer-internal names** with custom schemas. The model treats these as user tools and falls back to its general "shell-y" prior — `find | grep` is the natural shell pattern for "find files containing X", and the `_SHELL_METACHARS` blacklist is a constraint the model has not internalised because nothing in its training distribution couples that constraint to those tool names.

When we instead present `bash_20250124`, the model has *exact prior knowledge* that this tool's `command` parameter accepts a single non-pipelined command (or whatever the trained schema specifies), and the failure mode shifts from "model emits `find | grep` and gets blocked" to "model decomposes the search into discrete commands the tool will accept".

### Recommendation

**File OP-824 — Migrate SDK runner to Anthropic-built-in tools** (NEW Sprint A child, supersedes parts of A1+A2).

Concrete steps:
1. Replace the `RUNNER_TOOLS` array with:
   ```python
   BUILTIN_TOOLS = [
       {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
       {"type": "bash_20250124", "name": "bash"},
   ]
   CUSTOM_TOOLS = [Grep, Glob, Skill, Agent]   # OmniSight-specific
   ```
2. Implement the tool handler for `str_replace_based_edit_tool` against our existing `Read/Write/Edit` primitives — translate the 5 commands (`view`/`create`/`str_replace`/`insert`/`undo_edit`) into our internal ops. The `undo_edit` requires per-tool-call file snapshots; this is genuinely new state the dispatcher must track.
3. Implement `bash_20250124` handler — wrap our existing `bash_handler` but with the built-in tool's schema (single `command` param + `restart` action). Persistent shell state is required — current `_run_bash` spawns a fresh subprocess per call.
4. Pilot-test on the same 3 tickets that failed S1 (OP-113-style placeholders) to validate hypothesis.
5. If pilot passes, **A1 (loosen `_SHELL_METACHARS`) becomes unnecessary and can be closed `Won't Do`** — the model no longer needs to compose pipes once it has the built-in `bash`.

**Estimated cost**: ~2 days dev + ~$15 pilot budget. If pilot passes, accelerates Sprint A by ~5 children (A1/A2 partial; net new A24 absorbs them).

**Risk**: Built-in tool schema is opaque to us — if Anthropic changes the spec, we have less control than our custom tools. Mitigation: pin the dated tool type (`bash_20250124`, not the latest); upgrade is explicit.

---

## Finding B — Aider's repo-map (P1)

### What we observed

Aider's `aider/repomap.py` builds a graph from tree-sitter AST: nodes = files, edges = symbol references. PageRank-style ranking surfaces the most-central files for the current task. Default `--map-tokens=1000` — when no files are explicitly in chat, the budget expands automatically to give the LLM more context. **70.3% file-identification rate on SWE-bench Lite tasks** without any RAG/embeddings infrastructure.

This is strictly cheaper than embeddings:
- No vector DB to maintain
- No embedding model API cost per file
- Updates incrementally on each commit (just re-parse changed files)
- AST is deterministic; ranking is deterministic given the seed file set

### Gap in our system

Our SDK runner currently feeds the model: ticket title + description + AC + (optionally) file paths from the ticket's "Files Touched" comment. **The model gets zero structural overview of the codebase.** This forces it to issue many `Read` / `Glob` calls just to orient itself — exactly the loop that hits the bash-pipe failure.

### Recommendation

**File OP-825 — Repo-map preamble for SDK runner** (NEW Sprint A child).

Concrete steps:
1. Add `tree-sitter` + `tree-sitter-python` (and per-lang grammars we ship) as runner deps.
2. Build a static `RepoMap` that runs once per ticket pickup:
   - Parse all `*.py` / `*.tsx` / `*.ts` files
   - Extract symbol-graph (function defs + their refs)
   - PageRank seeded on files mentioned in the ticket (Files Touched + AC body)
   - Output top-N files (token-budgeted) as a system-prompt prefix
3. Token budget: 1000 tokens default (matches Aider) — bump to 2000 for tickets without "Files Touched" hints.

**Risk**: tree-sitter is a real new dep; install path is non-trivial (per SWE-agent's `tools/edit_anthropic/install.sh` they wrap it in `|| true` because of issue-1179). We should pin a stable version (Aider pins `0.21.3`) and treat install failure as runner-degraded (run without map) rather than runner-down.

---

## Finding C — Aider's reflection loop with bounded retries (P1)

### What we observed

Aider's `base_coder.py` main loop:
- `init_before_message()` resets per-iteration state
- `send_message()` → handles model response
- On edit/lint/test failure: increments `num_reflections` counter, re-sends to model with error context
- Hard cap: `max_reflections` (configurable, default 3)
- `KeyboardInterrupt` 2-second debounce so a stray Ctrl-C doesn't kill mid-iteration

Crucially: each reflection's input context includes the *specific* error from lint/test, not just "it failed". The model sees `pytest` traceback or `ruff` output verbatim.

### Gap in our system

Our SDK runner uses `max_iterations=40` and surrenders on `max_iterations_exceeded` — but every Bash/Read/Write tool call counts as one iteration. The model burns iterations on exploration, then hits the cap before it can reflect on a test failure. There is no separation between "exploration iterations" and "reflection iterations".

### Recommendation

Wire into **OP-812 (A4 run_tests/lint_changed)** — when tests fail, capture the failure as a structured reflection input (not just a Bash result), and reset the iteration counter for a bounded `max_reflections=3` post-failure loop. Keep the global `max_iterations` cap as the hard stop, but allow reflection-mode iterations to be cheaper-counted (e.g. half-weight) so a model that's converging on a fix isn't killed mid-reflection.

---

## Finding D — Architect/Editor model split (P2)

### What we observed

Aider supports two-model mode: "architect" (strong model, e.g. Opus) plans the changes in natural language; "editor" (weak model, e.g. Sonnet or Haiku) executes the diff. Cost reduction: for SWE-bench Lite tasks, Aider achieves 26.3% with GPT-4o + Opus alternating, vs 20.3% with GPT-4o alone. Token cost stays bounded because the editor sees only the architect's plan + the file, not the full conversation history.

### Map to our system

Our subscription-codex pivot already does this implicitly: we pay a flat subscription for the planner-editor combo. For the SDK runner, this would mean:
- Architect: `claude-opus-4-7` plans the change (max 1 call per ticket, ~$0.05)
- Editor: `claude-sonnet-4-6` applies the diff (cheaper, faster)
- The architect's output is a structured plan (not free text) — JSON with `files_to_edit`, `change_summary_per_file`, `tests_to_run`

**Defer**: this is P2. We should validate Finding A (built-in tools) lifts pilot success rate first; if it does, we may not need this split. If it doesn't, this becomes the next lever.

---

## Finding E — SWE-agent's submit-review checklist (P2)

### What we observed

SWE-agent's `default.yaml` ships `tools/review_on_submit_m` — a tool that fires when the agent calls `submit`, presenting a final review checklist:
- "Have you run all tests?"
- "Have you verified each AC item?"
- "Have you checked for unintended file changes?"

The agent must answer each item before the submission is accepted. This is **structurally identical to our `verify_acceptance_criteria` SOP step in `docs/sop/implement_phase_step.md`** — but enforced by the tool, not by prose discipline.

### Recommendation

Wire into **OP-813 (A5 verify_acceptance_criteria tool)** — port SWE-agent's checklist pattern: when the model calls `submit_for_review`, inject a synthesised checklist derived from the JIRA AC field (one question per AC line). The model must respond to each before the submit is accepted. Failure mode: missing AC item → return checklist as tool error, model retries.

---

## Finding F — Cache control on last 2 messages (P2 — quick win)

### What we observed

SWE-agent's `default.yaml`:
```yaml
cache_control:
  - last_message
  - second_to_last_message
```
This applies Anthropic prompt caching breakpoints on the most recent two assistant turns. Per the platform docs, this is the canonical pattern for tool-use loops: the system prompt + early conversation are cacheable across iterations; the recent turns rotate.

### Gap in our system

`scripts/run_s1_via_anthropic_sdk.py` does not set `cache_control` anywhere. On a 40-iteration ticket, this means we re-bill the full conversation history every iteration. At 50k tokens of avg context, that's ~$1.50 of unnecessary input cost per ticket.

### Recommendation

Wire into existing **OP-815 (A7 — already targets cost optimisation)**: add `cache_control` breakpoints on the last 2 messages + the system prompt. ~10 LOC change. Estimated saving: 40-60% input token cost on multi-iteration tickets.

---

## Finding G — Auto edit-format selection per model (defer)

Aider auto-selects edit format per model:
- `whole` (full file rewrite) — for weak models
- `diff` (search/replace blocks) — for Sonnet, Opus
- `udiff` (unified diff) — for GPT-4 Turbo
- `editor-diff` / `editor-whole` — when paired with architect/editor

This is mostly a non-issue once we adopt Anthropic's built-in `str_replace_editor` (Finding A) — the built-in tool *is* the Sonnet-optimal format. **Defer; do not file.**

---

## Mapping to existing Sprint A children

| Existing | Status after this audit |
|---|---|
| **OP-809 A1** Bash shell-mode | Likely **`Won't Do`** post-Finding A (built-in `bash` solves it at root). Hold pending pilot validation. |
| **OP-810 A2** Tool error feedback | Built-in tools have structured errors out of the box; A2 scope shrinks to *custom* tools (Grep/Glob/Skill/Agent) only. |
| **OP-811 A3** Skill/Agent wired | Unchanged. |
| **OP-812 A4** run_tests/lint_changed | Augment with **Finding C** (reflection loop with structured failure input). |
| **OP-813 A5** verify_acceptance_criteria | Augment with **Finding E** (SWE-agent submit-review checklist port). |
| **OP-814 A6** *(check current scope)* | — |
| **OP-815 A7** Cost optimisation | Add **Finding F** (cache_control on last 2 messages) — ~10 LOC, 40-60% input cost saving. |
| **OP-817 A9** Worktree isolation | Cross-reference SWE-agent's Docker-per-task; our worktree is the lighter equivalent. No change. |
| **NEW: OP-824** | **Anthropic-builtin-tool migration** (Finding A) — **P0**, supersedes A1+A2 partial. |
| **NEW: OP-825** | **Repo-map preamble** (Finding B) — P1, ~2 days dev + tree-sitter dep. |
| **DEFER (do not file)** | Finding D (architect/editor split) — re-evaluate after OP-824 pilot. Finding G (edit-format auto-select) — obsoleted by OP-824. |

---

## What we keep that they don't have

For the avoidance of doubt — Aider and SWE-agent are *interactive* tools designed for human-in-loop dev. They lack:

1. **Tier-aware governance** (ADR-0005 S/M/L/X) — neither has authority gating per change risk.
2. **JIRA-aware pickup loop** — both are CLI; user types a query, the agent runs once, exits.
3. **Multi-bot coordination via `mutex_with` labels** — neither has multi-instance orchestration.
4. **CostGuard 80/100/120 tier alerts** — Aider tracks `message_cost` per turn but has no cap-and-alert system; SWE-agent has no cost layer at all.
5. **Production audit trail via JIRA + Gerrit** — neither persists per-ticket activity history.
6. **Bridge daemon stream-events → JIRA** — neither has a long-running governance layer.
7. **Tier-S/M/L/X auto-merge gate** — both expect human review on every change.

These are the OmniSight differentiators. The audit's recommendation is **borrow their tactics, keep our governance posture intact** — specifically: adopt their tool-use patterns (Findings A–F) inside the existing OmniSight orchestration shell.

---

## Decision summary for operator return

When the operator returns:

1. **Approve OP-824** (built-in-tool migration, P0) — single biggest leverage point; ~2 days dev.
2. **Approve OP-825** (repo-map preamble, P1) — second-biggest leverage point.
3. **Approve scope-revision of OP-810 (A2)** — shrink to non-builtin tools.
4. **Approve closure of OP-809 (A1) as `Won't Do`** *contingent on OP-824 pilot success* — do not close pre-emptively.
5. **Augment OP-812 (A4)** with Finding C reflection loop spec.
6. **Augment OP-813 (A5)** with Finding E checklist port.
7. **Augment OP-815 (A7)** with Finding F cache_control change.

Estimated Sprint A scope delta: net **+1 P0 child (OP-824)**, **+1 P1 child (OP-825)**, **−1 child** (A1 → Won't Do contingent), **3 augmentations**. Budget delta: ~+$30 dev pilot + ~−$200 saved Sprint A budget if Findings A+F land (input-token cost reduction across all 15 children).

---

## Cross-references

- Sprint A META: OP-808
- Antecedent failure analysis: lessons-learned.md Lesson 28 (this audit's compact form)
- Pilot failure data: 3× S1 launcher attempts logged on OP-803 / OP-804 / OP-805
- Anthropic platform reference: `https://platform.claude.com/docs/en/docs/agents-and-tools/computer-use` (full schemas for `text_editor_20250728` + `bash_20250124`)
- SWE-agent ACI definition: `https://swe-agent.com/latest/background/aci/`
- Aider repo-map algorithm: `https://aider.chat/docs/repomap.html`
