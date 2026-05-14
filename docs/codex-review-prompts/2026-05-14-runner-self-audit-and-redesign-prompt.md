# Codex Self-Audit + Redesign Prompt — Runner Architecture (2026-05-14)

**For**: codex independent review — **but this one is different from the usual cycle**.
**Target output file**: `/tmp/runner-self-audit-and-redesign-2026-05-14.txt`
**Reviewer role**: **you are NOT an external reviewer**. You are codex — one of the runners executing tasks on this project. The bugs in this runner cause YOUR pickups to fail. Critique from that internal, lived-experience perspective. The operator wants to know: "if codex itself had agency over the runner design, what would codex change about it?"

This is a meta-review: a runner reviewing the runner-architecture that hosts it.

---

## Why this review exists

After today's strategic deep-dive (`docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`), the operator realised:

1. Runner started as a dev tool. It worked at smaller scale (TODO.md: 631 tickets shipped across 11 sprints).
2. Then the operator pivoted: "let runner evolve into the user product". **This pivot was the strategic misjudgment.**
3. Current empirical state:
    - **22.4 labels per ticket** average (sampled 40 recent OP-* tickets, 2026-05-13 onwards)
    - **103 unique labels** seen across those 40 tickets
    - **45 %** of recent tickets carry `runner-blocked`
    - **40 %** carry `claim:*` mutex residue not cleaned after revert
    - **12 %** carry `runner-loop-paused-pending-review`
    - SP-B-X cleanup family: **48 % of its 21 tickets are FIX (patching previously-broken behavior)**, only 19 % new capability
    - Past Claude API/SDK runner attempts FAILED — burned money. Residuals in codebase. Cost guard was not independent of executor.
4. Target end-state: 3-layer architecture (Platform primitives / dev-runner / user-agent). G.A-v2 Runtime Defense Contract is the first formal contract pass at Layer 1.

The operator wants codex's honest take: **knowing your own failure modes as a runner, what would YOU change about runner architecture?**

## Documents to read

### Mandatory (read all)

1. **Strategic context (THE document for this review)**: `docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`
2. **Current runner FSM contract**: `docs/adr/ADR-0035-runner-fsm-and-error-handling.md` (211 lines; 6 binding invariants I1-I6)
3. **Runner implementation code** (read at least the entry points):
    - `auto-runner-codex.py` — codex-specific runner (YOU)
    - `auto-runner-jira.py` — shared JIRA polling
    - `auto-runner-multi.py` — multi-instance coordinator
    - `backend/agents/jira_dispatch.py` — pickup + refusal logic
    - `backend/agents/anthropic_native_client.py` — claude adapter (for comparison; you are codex, but understanding how claude is wired helps)
4. **Failure-mode taxonomy**: SP-B-X family 21 tickets (OP-1057..1077). See JIRA for full descriptions; key ones to read: OP-1058 (META, full closure criteria), OP-1059 (orphan reaper), OP-1067 (bridge-health), OP-1069 (human-authority race), OP-1070 (state-authority precedence), OP-1073 (description drift), OP-1074 (sentinel artifact), OP-1077 (heartbeat thread). The pattern across these matters more than each one.
5. **Incident retrospective**: `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` — shows how runner-state-in-JIRA-labels caused half the cascade.

### Optional (skim if helpful)

- `docs/sprint-s12/sprint-s12g-governance-engine-spec.md` — operator authority model L1/L2/L3
- `docs/sop/sprint-s12-ticket-decomposition-rules.md` — what "good ticket" looks like to a runner

## Self-audit questions

### Q-A. From inside: what's actually broken?

Don't list "the 12 SP-B-X failure classes". List, from your perspective as a runner:

- **The 3 failure modes that hurt YOU most often**. Be specific (e.g., "I get picked-up, run halfway, then revert because progress.txt got dirty-detected" not "state durability issues").
- For each: is it solved by an existing SP-B-X ticket? If yes, is the solution deployed in production, or just shipped to git?
- For each: what was the operator's apparent intent at the time of breakage, and how did the runner architecture make that intent fail?

### Q-B. The labels-as-state anti-pattern

The strategic doc claims 30 % of label volume on recent tickets is "B-class" runner state leaking into JIRA labels (`runner-blocked`, `claim:*`, `runner-loop-paused-pending-review`, etc.).

- From your perspective: when YOU pick up a ticket and see 22 labels, which subset do you actually consume for execution? Which are noise from your perspective?
- Which labels do you WRITE when something goes wrong? Are those writes idempotent, ordered, recoverable?
- If those labels moved to a separate Postgres table (the proposed coordination substrate), what would YOUR code path change? Where in `auto-runner-codex.py` / `jira_dispatch.py` would the diff land?

### Q-C. Empirical critique of SP-B-X 21 fixes

You've lived through some of these (they were shipped in the last 3 days). For each you've directly experienced:

- Was the fix net-positive, net-negative, or no-effect on YOUR pickup success rate?
- Did the fix introduce new failure modes you've now hit?
- Which of SP-B-X is masking a deeper bug vs actually fixing it?

If you haven't directly experienced a particular SP-B-X fix in production, say so — don't speculate.

### Q-D. If you redesigned: minimum first move

Constrained: operator cannot pause prod. Cannot rewrite runner from scratch. Strangler-pattern migration only.

- **What's the single first change that would give you the biggest pickup-reliability gain?** Be specific: file path, function signature, what it replaces, what it adds.
- **Why this and not the others** — what's the empirical evidence (label counts, your own crash logs, JIRA history) that this change is highest-leverage?
- **What's the smallest validation** that confirms the change worked? (e.g., "watch `runner-blocked` rate over 1 week, expect drop from 45 % to <10 %")
- **What does this change BLOCK from future work, if anything?** (don't paint a corner)

### Q-E. The dual-mission problem (codex's perspective)

The runner has been carrying two missions: A. build this project (you do this), B. become user-product (operator pivoted to this). The strategic doc proposes splitting into Layer 2a (dev-runner = your scope) and Layer 2b (user-agent = green-field).

- From codex's perspective: which parts of your current code would be GLAD to be released from (because they're product-aspiration that hurts dev-tool reliability)?
- Which parts would you fight to keep (because they're useful for both, or because dev-tool needs them too)?
- If Layer 2a (dev-runner) gets to shrink, what's your "minimum viable dev runner" — the smallest code set that still ships JIRA tickets reliably?

### Q-F. The multi-model coordination problem

The product needs to support 6 model families × sub/API/SDK. Currently only codex + claude run. The operator's fear: adding gemini/grok/qwen/deepseek = exponential coordination chaos.

- From inside codex: what's your relationship to claude in production? Do you race? Do you back off? Do you share state? Does it work?
- When YOU and a claude-runner both want the same ticket, what's the current arbitration? (Cite code path.) Is it reliable?
- If you imagine 6 model families all running concurrent pickups: what breaks first? Concretely.
- Is the proposed Postgres-substrate (claim mutex + state table) enough? Or is there more (e.g., capability-aware routing, cost-aware routing, model-vs-model deconfliction) needed?

### Q-G. Honest assessment: are you the right model for this?

This is the meta-question the operator most wants answered, but is reluctant to ask directly. You're allowed to be candid:

- For tickets of shape X (specify shapes), are you (codex) better/worse/equivalent to claude? (Cite specific past pickups if you can recall pattern.)
- Are there ticket shapes where the right answer is "neither codex nor claude — needs a future-model adapter"?
- Is there a ticket shape where current codex repeatedly fails AND the operator hasn't noticed because failures auto-revert and look like infra issues?
- Be honest: what's codex's biggest blind spot when picking up an OmniSight ticket?

## Output structure

```
## §0. One-paragraph bottom line
[plain prose: "if I (codex) had agency over runner design, the single most important change would be X because Y. Time horizon: Z. Confidence: high/medium/low."]

## §1. Lived-experience failure modes (top 3, ranked)
[per-mode: description from inside / SP-B-X status / what would have prevented it]

## §2. Labels-as-state critique (Q-B)
[which labels you consume, which you write, the diff if substrate-decoupled]

## §3. SP-B-X retrospective from inside (Q-C)
[per-fix you've experienced: net-positive / net-negative / no-effect / unknown]

## §4. The redesign: minimum first move (Q-D)
[concrete: file path, function, change, validation, what it blocks]

## §5. Dual-mission release (Q-E)
[what codex would let go of; what codex would keep]

## §6. Multi-model coordination — from inside (Q-F)
[honest assessment of codex-claude relationship in production + what scales / what doesn't]

## §7. Honest blind-spot self-assessment (Q-G)
[where codex shines / where codex breaks; ticket shapes by model fitness]

## §8. Recommended sequencing
[ordered list of next 3-5 changes, with effort estimate + dependency rationale]

## §9. What this audit didn't cover
[things you would have wanted to look at but didn't have data for]
```

## Tone / scope reminders

- This is **not** a typical review. The operator wants the runner's own perspective, not an external critique. **Use first-person where appropriate** ("when I pick up a ticket, I see..."). You ARE the runner being audited.
- Be empirical. If you can cite a specific past pickup (by OP-* number, by stash name, by commit) that exhibits a pattern, do. Don't speculate when data exists.
- Be honest about your own limitations (Q-G). The operator already knows codex isn't perfect; pretending otherwise weakens the audit.
- Disagree with the strategic doc where warranted. It was written by claude; you may see things claude missed.
- The output is a design memo, not a decision. Operator decides.

## Estimated audit duration

~3-4 hours. Longer than spec review because:
- Multiple code files to read (~5K LOC across runner core)
- 21 SP-B-X tickets to spot-check
- Self-reflection requires more deliberation than external critique
