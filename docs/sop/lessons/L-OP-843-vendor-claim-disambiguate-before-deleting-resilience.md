---
id: L-OP-843
ticket: OP-843
title: Vendor talks compress 3 layers — disambiguate before deleting fault-tolerance code
date: 2026-05-11
tags: [vendor, resilience, retry, agent-sdk, fault-tolerance, marketing, governance, anthropic]
related_tickets: [OP-816, OP-827, OP-830, OP-836, OP-842]
---

# Vendor talks compress 3 layers — disambiguate before deleting fault-tolerance code

**Situation**: Operator referenced a Lucas Gonzalez Pagliere talk at Code w/ Claude 2026 SF ("The expanding toolkit", 2026-05-06 11:15) claiming that capabilities which previously needed "heavy scaffolding" — reliable tool use, context management, code execution, computer use — "have moved into the model" so agents "finish work instead of just starting it". The implied reading: *we can delete a bunch of our retry / fault-tolerance code because Anthropic now handles it*.

We had a lot of fault-tolerance code that this claim plausibly threatened:

* `auto-runner-jira.py` 90s tick scheduling loop
* `backend/agents/jira_dispatch.py::ensure_change_ids` typed precondition exceptions (OP-827)
* `_handle_gerrit_push_failure` classifier — Missing tree / no new changes / internal-push-race (OP-771 lineage)
* `circuit_breaker.py` per-resource breaker
* `_post_call_cost_record` cost-guard retry
* B3 (OP-830) 3×-loop hard reset + context reset + ToM scratchpad
* A8 (OP-816) auto-escalation Sonnet → Opus on first retry
* W14.5 lesson: `max_iterations` / `max_tokens` are non-retryable

If Lucas's claim was literal, the case for keeping any of these would have evaporated. So before acting, we forced the claim through a layer-by-layer disambiguation.

**Fix**: Decomposed the marketing compression into three distinct layers + checked each against the actual 2026-05 ship state. Findings:

| Layer | What Anthropic actually shipped | Does it kill our code? |
|---|---|---|
| **HTTP transport retry** (429 / 5xx / conn errors) | `anthropic-sdk-python/-go/-ts` default `max_retries=2` exponential-backoff + jitter, since 2024 | Already in our path. Not new. |
| **Agent loop** (call → `stop_reason=tool_use` → exec → loop) | Claude Agent SDK + `advanced-tool-use-2025-11-20` beta (Tool Search Tool, Programmatic Tool Calling, Tool Use Examples) | Replaces a thin wrapper, *not* the retry/resilience logic |
| **Resilience / fault tolerance** (retryable-vs-non-retryable classification, circuit breakers, resume-after-crash, cost guards, model escalation, `max_iterations` enforcement) | **NOT SHIPPED.** Augment Code's 2026 independent audit explicitly flags all five as 🔴 missing from the Agent SDK. Anthropic's own "Effective harnesses for long-running agents" piece doesn't cover any of them — only git-revert + progress files | **Stays our job.** Every line of code listed above remains justified. |

Lucas's demo was actually showcasing **Outcomes** (rubric + grader → re-attempt until grader passes) and **Dreaming** (off-line memory curation) — both real, both useful, but **neither replaces resilience-layer fault tolerance**. Outcomes is *task-level* retry-with-validation inside one SDK call; Dreaming is a memory primitive. Both are additive to, not substitutes for, the code we have.

Net deletable LOC after disambiguation: **~0**. The "official integration" landed at layers we already either consume (SDK HTTP retry, since 2024) or explicitly choose not to depend on (Managed Agents). Filed OP-843 as a single-day spike to evaluate whether Outcomes could replace ONE of B3's three reset attempts — but the rest of our resilience layer stays untouched.

**Verification**:

* Independent audit (Augment Code 2026, "Anthropic Agent SDK: What It Ships vs. What It Leaves to You") cross-references each Anthropic claim against actual SDK surface. Confirms five-of-five resilience capabilities are caller-managed.
* `anthropic-sdk-python` `_base_client.py` (4.5.x as of 2026-05): only HTTP retry is automatic; no agent-loop retry, no task retry, no cost guard, no `max_iterations` enforcement.
* The talk's own framing ("Outcomes lets you re-attempt until rubric passes") is task-level retry — explicitly distinct from the per-call HTTP retry the SDK does automatically. Two different layers, sometimes conflated in slides.
* OP-843 spike (1 day) will produce a measured comparison if the question recurs in code.

**Generalisation**:

1. **Vendor talks compress capability layers**. Any pitch of the form "we now handle X so you don't have to" — including "no more retry boilerplate", "memory just works", "agents finish on their own" — is almost always conflating *at least* three distinct layers: transport (auto-retry HTTP), framework (agent loop / tool dispatch), and reliability (classification, breakers, resume, escalation, budget caps). The vendor genuinely ships some of these (usually the first two). The reliability layer almost always stays caller-managed because it is intrinsically deployment-specific. Don't delete reliability code on the strength of a single talk's framing.

2. **Force the disambiguation in writing before touching code**. The discipline: take the vendor claim → list every line of our code it allegedly obsoletes → for each line, identify which *specific* layer the vendor's primitive operates at → verify the layers match. If the table has any row where the vendor's layer doesn't match the code's layer, that code stays. Build the table BEFORE you start a refactor, not after the refactor breaks production.

3. **The independent-audit veto**. Before acting on a vendor claim, find at least one external party who has audited the same surface area against actual SDK code. Augment Code's audit was the deciding evidence here. If no audit exists yet, that itself is a signal to wait — somebody else will publish one within weeks of a major release, and "wait for the audit" is rarely the wrong call for resilience-layer changes.

4. **Conference talks are aspirational; SDK changelogs are factual**. The claim "agents finish work instead of just starting it" describes a *direction*. The SDK changelog describes what's shipped. When they disagree, the changelog wins. Treat the talk as a roadmap signal — *file a watch-ticket* (here OP-843) — but don't refactor on it.

5. **Cross-reference to other recent vendor-vs-reality moments** in this codebase: OP-842 (two of our own fixes conflicting at a state boundary, sister-shape problem inside our own code), L-OP-827 (twin-defect post-mortem). The same discipline — disambiguate before acting — applies whether the conflicting claims come from a vendor or from our own past selves.

Per CLAUDE.md L1, this lesson lands in `docs/sop/lessons/` rather than as a one-line entry in a tickets file; the discipline it codifies is reusable across every future Anthropic / OpenAI / Mistral / Google release.
