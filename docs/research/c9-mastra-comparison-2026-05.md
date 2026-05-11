# Spike Report — Mastra Observational Memory vs C1/C3 (OP-859)

**Date**: 2026-05-11
**Scope**: research-only spike. No production traffic, no DB schema, no frontend, no devops, no security, and no tooling changes.
**Deliverables**: `scripts/spike_c9_mastra_compare.py` plus this decision document.

---

## TL;DR — Recommendation

**Reject Mastra as a Sprint C replacement or complement for now.**

Mastra Observational Memory (OM) is a plausible long-running conversation memory system, but it is not a better fit for OmniSight's immediate C1/C3 problem: "find relevant prior ticket from progress.txt + JIRA changelogs." On the deterministic five-ticket bake-off, all three modeled paths hit Recall@3 = 100%, so Mastra adds no retrieval lift over the lighter C1/C3 baseline. Its integration surface is also the heaviest option: TypeScript runtime, Mastra storage adapter, sidecar or REST wrapper, and model-backed Observer/Reflector calls.

File a follow-up only if a real one-week B9 `progress.txt` corpus shows C1/C3 recall misses that Mastra's observation/reflection shape uniquely recovers.

---

## 1. Current Mastra Surface

Mastra docs list Observational Memory as added in `@mastra/memory@1.1.0`; the quickstart enables it with `observationalMemory: true` on a Mastra `Memory` instance. The docs also state that the default OM model is `google/gemini-2.5-flash`, and that OM currently supports `@mastra/pg`, `@mastra/libsql`, and `@mastra/mongodb` storage adapters.

The current architecture is not "Python library plus query API." It is a TypeScript agent memory feature: Observer and Reflector agents compress conversation history into observations and reflections. Mastra's docs describe a default 30k-token observation threshold and 40k-token reflection threshold, with optional experimental retrieval mode.

Install visibility checked during the spike:

| Package | `npm view ... version` on 2026-05-11 |
|---|---:|
| `@mastra/memory` | 1.17.5 |
| `@mastra/core` | 1.32.1 |
| `mastra` | 1.8.1 |

Sources consulted:
- https://mastra.ai/docs/memory/observational-memory
- https://mastra.ai/research/observational-memory
- https://mastra.ai/blog/changelog-2026-02-04
- https://mastra.ai/blog/changelog-2026-03-13

---

## 2. Methodology

Harness: `scripts/spike_c9_mastra_compare.py`.

Dataset:
- Primary intended input: one-week B9 `progress.txt` JSONL plus JIRA changelog JSON.
- Current runnable input: deterministic five-ticket sample because B9's real one-week corpus is not present in this worktree.
- The sample uses recent runner tickets with memory-like failure classes: OP-830, OP-843, OP-847, OP-848, OP-850.

Metric:
- Recall@K for "find relevant prior ticket" with K=3.
- Token cost per query using pinned synthetic token budgets.
- Setup complexity using approximate integration LOC plus infra components.

Adapters:
- C1 Memory Tool: direct filesystem/progress recall.
- C3 Cognee: semantic + graph recall with tag expansion.
- Mastra OM: compressed observations plus recall, with install verification isolated from CI.

The harness is deterministic. It does not call live models, mutate production state, write DB rows, start services, or integrate Mastra into runtime traffic.

---

## 3. Results

Command:

```bash
python scripts/spike_c9_mastra_compare.py --output /tmp/op-859-c9.json
```

Observed summary:

| Path | Recall@3 | Token cost/query | Setup LOC | Infra |
|---|---:|---:|---:|---|
| C1 Memory Tool | 100% | ~$0.0007 | 90 | `progress.txt`, filesystem |
| C3 Cognee | 100% | ~$0.0010 | 260 | Cognee, code graph, vector index |
| Mastra OM | 100% | ~$0.0045 | 390 | Node sidecar, `@mastra/memory`, `@mastra/libsql`, REST wrapper |

Interpretation:
- Recall is tied on the controlled sample. Mastra does not recover a missed C1/C3 cell.
- Mastra has the highest token cost because observation work must be amortized over queries.
- Mastra has the highest setup complexity because OmniSight's runner is Python-first while Mastra OM is TypeScript-native.

---

## 4. Error Catalog Mapping

`MastraInstallFailed`: implemented in the harness around `npm view @mastra/memory`, `@mastra/core`, and `mastra`. A failure aborts live install verification but still allows modeled comparison.

`MastraIngestionTimeout`: implemented as a record-count guard on the Mastra adapter. If it triggers, the harness follows the ticket recovery rule and reduces the sample to one day.

`MastraQuerySchemaMismatch`: implemented as a guard on recall hits. If a future live adapter returns hits without string `ticket_key`, the harness raises this catalog error rather than silently scoring malformed output.

---

## 5. Decision

**Decision: reject Mastra for OP-859.**

Not wholesale-replace:
- C1 is cheaper and simpler for local runner memory.
- C3 is already the planned graph/semantic layer.
- The sample does not show Mastra outperforming either baseline.

Not complement:
- A complement path would require a read-only Cognee adapter or sidecar. That is explicitly conditional on `decision = complement`; this spike does not meet that bar.
- Adding Mastra now would create a second memory stack before the first real one-week B9 corpus exists.

Revisit condition:
- Re-run the harness when B9 provides an actual week of `progress.txt` and when C1/C3 have landed enough production data to expose recall misses.
- If C1/C3 Recall@3 falls below 80% while Mastra reaches at least 90% on the same corpus, reopen a complement-only ticket for a read-only adapter.

---

## 6. What Did Not Change

- No production code path imports Mastra.
- No Cognee adapter was added because the decision is reject, not complement.
- No database, devops, embedded, frontend, security, or tooling files were touched.
- No JIRA transition helper was called; the runner owns forward transitions.
