# Agent Memory Architectures — literature survey (through mid-2026)

> Research input for the 3D-memory revival/upgrade project (2026-07-07). Produced by a
> dedicated survey agent; all load-bearing claims verified against primary sources (8/8
> verification fetches confirmed; one correction applied: Agent KB = +4.0pp SWE-bench pass@1,
> +18.7pp GAIA pass@3). Constraints applied: self-hosted; PG+pgvector+Neo4j+Cognee available;
> metered-API orchestrator; ephemeral runners with ONE prompt injection at pickup.
> Companion: `2026-07-07-self-improving-agents-survey.md`. Consumed by the R/S/U roadmap.

## 1. Temporal knowledge graphs

**Zep paper** (arXiv:2501.13956, Jan 2025) — agent memory as dynamically built temporal KG
(Graphiti engine): episode subgraph (raw, non-lossy) → semantic entity subgraph → community
subgraph. DMR 94.8% vs MemGPT 93.4%; LongMemEval up to +18.5% accuracy with ~90% latency
reduction vs full-context — with regressions on single-session-assistant (−9 to −17.7%) and
knowledge-update (−3.4%) categories.

**Graphiti** (github.com/getzep/graphiti) — Apache-2.0, ~28.5k stars, v0.29.2 (June 2026).
Backends: **Neo4j 5.26+ (default)**, FalkorDB, Neptune; **pgvector NOT supported**. Any
OpenAI-compatible endpoint for ingestion (small models often fail structured-output
compliance). Production-grade OSS (~25k weekly PyPI downloads).

**Bi-temporal mechanics:** every edge carries event-time (`valid_at`/`invalid_at`) and
transaction-time (`created_at`/`expired_at`). At ingestion an LLM compares new edges against
semantically-related existing edges; contradictions close the old edge's validity window —
**invalidated, never deleted**. Retrieved facts carry explicit date ranges; as-of queries
supported. Known gap: issue #1489 documents temporal-correctness bugs in **historical
backfill** (directly relevant to backfilling runner_incidents).

**Zep CE deprecated April 2025** — self-hosting means raw Graphiti; rebuild the "Context
Block" assembly yourself (≈ one templated search call).

**Fit:** best-in-class for the never-wired Temporal axis. Direct Neo4j fit; retrieval is a
single hybrid query with **no LLM call** (vendor p95 0.632s) → compact dated-fact block, ideal
for one-shot pickup injection. Cost risk = ingestion (~6–8+ LLM calls/episode, reconstructed;
issue #1193 cost complaints) — route to a cheap structured-output model, never the metered
orchestrator. Ingestion is async — freshly written episodes may not be queryable at next pickup.

## 2. Memory operating systems / unified layers

- **Mem0** (arXiv:2504.19413, ECAI 2025; ~60k stars) — write-time LLM fact extraction + second
  pass resolving ADD/UPDATE/DELETE/NOOP. Self-reported: +26% over OpenAI memory on LoCoMo, 91%
  lower p95, >90% token savings. **Verified on migration docs: OSS v3 collapsed to ONE LLM
  call, ADD-only (no UPDATE/DELETE), removed external graph stores (Neo4j etc.)** — adopting
  the library is a step backwards for us; borrow the v2 extract→resolve *pattern* only.
- **MemOS** (arXiv:2505.22101 / 2507.03724) — memory as OS resource in three lanes
  (plaintext/activation-KV/parametric) unified in MemCube units w/ provenance+versioning.
  Wants Qdrant+Redis; activation/parametric lanes meaningless on metered APIs. Steal the
  MemCube provenance-metadata idea only.
- **MIRIX** (arXiv:2507.07957) — six memory components each with an LLM manager agent + meta
  router. Native Postgres+pgvector+BM25 (best infra match) but **highest write amplification
  surveyed** — wrong shape for metered budget.
- **MemGPT → Letta** (arXiv:2310.08560) — OS-style paged context, self-editing memory via tool
  calls. 2026: pivot to Letta Code / git-backed MemFS; server-side memory tooling sunset. The
  extractable pattern: **Sleep-time Compute** (arXiv:2504.13171, verified) — offline
  consolidation ≈ **5× less test-time compute at equal accuracy, +13–18% scaled, 2.5× lower
  per-query cost amortized**; benefit proportional to query predictability; includes SWE-agent
  case study. Also the governance detail: **only the sleep-time agent holds memory-write
  tools** (writer/actor separation).
- **Taxonomy verdict:** CoALA working/episodic/semantic/procedural (arXiv:2309.02427) is the
  de-facto vocabulary but not settled science (arXiv:2505.00675 reframes; 47-author
  arXiv:2512.13564 calls human taxonomies insufficient). Counter-architecture: file-based
  memory with no taxonomy (Anthropic memory tool + context editing; Letta MemFS). OmniSight's
  3 axes ≈ Structural→semantic, Temporal→episodic-with-validity, Causal→procedural/failure.

## 3. Self-organizing agentic memory

**A-MEM** (arXiv:2502.12110, NeurIPS 2025) — Zettelkasten: LLM-built atomic notes → link
generation vs top-k neighbours → retroactive **memory evolution** of old notes. ≈2–3 LLM calls
+ 1 embedding per write. On GPT-4o-mini: F1 27.02 vs MemGPT 26.65 — marginal; repo issues
#26–28 document regressions + eval session-mixing. Research-only; **zero production systems
use bidirectional note evolution as of mid-2026**. Successors (all paper-only): D-MEM
(arXiv:2603.14597, surprise-gated evolution, >80% token cut), Memory-R1 (arXiv:2508.19828,
RL-trained ADD/UPDATE/DELETE/NOOP from 152 QA pairs), Memp (arXiv:2508.06433, procedural
distillation w/ deprecation), MemInsight (arXiv:2503.21760, EMNLP 2025, +34% recall),
MemEvolve (arXiv:2512.18746). If ever adopted: batched idle-time job + surprise gating +
append-only note versions.

## 4. Structured retrieval for code/project context

- **MS GraphRAG** (arXiv:2404.16130) — indexing cost sink; LazyGraphRAG (0.1% cost) **still
  not OSS** → avoid.
- **LightRAG** (arXiv:2410.05779, EMNLP 2025; v1.5.4) — retrieval <100 tokens/1 call vs
  GraphRAG ~610k; supports PostgreSQL for all roles + Neo4j; most production-ready OSS in
  family — but functionally overlaps Cognee → lateral move, skip.
- **HippoRAG 1/2** (arXiv:2405.14831; 2502.14802 ICML 2025) — OpenIE KG + **Personalized
  PageRank** seeded from query entities; don't adopt artifact; **steal PPR-seeded retrieval**
  for ticket-graph traversal (Neo4j GDS has native PPR).
- **Code-repo context:** CodexGraph (2408.03910), RepoGraph (2410.14684, +32.8% rel
  SWE-bench), LocAgent (2503.09089, 92.7% file-level localization). **Aider repo-map**:
  tree-sitter symbol graph + PPR under ~1k-token budget — zero LLM cost, deterministic; the
  pragmatic baseline matching one-shot injection. Production engines (Cursor Merkle sync,
  Cognition DeepWiki) converge on **pre-computed deterministic context**, not LLM-built KGs.
- **Tickets/experience:** Agent KB (arXiv:2507.06229, verified: +4.0pp SWE-bench pass@1
  24.3→28.3 OpenHands; +18.7pp GAIA pass@3), SWE-Exp (2507.23361, 41.6% SWE-bench-Verified),
  Prometheus (2507.19942, repo KG). **Issue-tracker KGs have essentially no peer-reviewed
  literature** — OmniSight's JIRA-graph axis is ahead of the public literature.
- **Cognee** — Apache-2.0; **$7.5M seed Feb 2026**; natively targets pgvector+Neo4j; AST-level
  `codify` code-graph pipeline is **LLM-free**. Self-run benchmarks (treat skeptically). Keep
  as Structural-axis engine.

## 5. Memory evaluation

- **LoCoMo effectively discredited**: Penfield Labs audit — 6.4% of answer key wrong, judge
  accepts up to 63% of intentionally wrong answers; conversations fit modern context windows.
  Zep↔Mem0 dispute: same benchmark/systems, 58–84% spread, all configuration artifacts.
- **LongMemEval** (arXiv:2410.10813, ICLR 2025) — current standard; vendor scores self-reported
  and gamed (Emergence AI hardcoded k=42 found).
- **MEMTRACK** (arXiv:2510.01353, NeurIPS 2025 wksp, verified) — memory+state tracking across
  **Slack+Linear+Git software-dev workflows with conflicting info; best model (GPT-5) ~60%** —
  closest public benchmark to OmniSight's domain.
- Also: MemoryAgentBench (2507.05257), MemBench (2506.21605), PrefEval (2502.09597, ICLR 2025
  oral — preference-following collapses <10% by turn 10 without memory), PersonaMem
  (2504.14225), AWM task-success framing (+24.6%/+51.1% relative).
- **How teams measure "did memory help":** ablation on task success (Anthropic +39% internal
  agentic-search from memory+context-editing); token/latency accounting (write-side cost
  typically omitted); the one controlled coding-agent study (Stompy, Mar 2026, n=3): **quality
  unchanged; 14% avg cost reduction (22–32% complex); memory pure overhead on simple tasks**.
- **Negative results:** ConvoMem (2511.10523) — full-context 70–82% vs Mem0 30–45% under 150
  conversations → **retrieval machinery premature below ~150-item corpora**. HippoRAG 2's own
  paper shows prior KG-RAG below vanilla RAG on factual tasks.

## 6. Robustness patterns

- **Write-time vs read-time:** strongest 2026 evidence = "Diagnosing Retrieval vs. Utilization
  Bottlenecks" (arXiv:2603.02473, verified) — **retrieval method moves accuracy 20 points
  (57.1→77.2) while write strategy moves 3–8; raw chunks with zero LLM calls match or beat
  lossy extraction**. Corroborated by arXiv:2412.15266 (no structure dominates; mixed robust)
  and MemFail (2605.26667 — "summary failure": compression strips decisive qualifiers). Third
  way: sleep-time compute (consolidate offline where contexts are re-queried).
- **Decay/TTL:** production converged on **invalidation-not-decay** (Zep/Graphiti supersession;
  Mem0 Cloud rank-biasing decay, nothing deleted; Letta tiers). For OmniSight: a silently
  expired fact mid-task is worse than a stale-but-flagged one.
- **Contradiction:** Graphiti edge invalidation (reversible, diffable, point-in-time) vs
  Mem0's destructive UPDATE/DELETE. MemFail documents both failure directions.
- **Poisoning:** attacks ahead of defenses. AgentPoison (2407.12784, NeurIPS 2024): ≥80% ASR
  at <0.1% poison rate. **MINJA (2503.03704): memory injection via normal queries only, >95%
  success — OmniSight's exact threat shape.** Real-product demo: Gemini long-term-memory
  hijack (Feb 2025). Defenses by maturity: **spotlighting/datamarking of untrusted spans
  (2403.14720; ASR <2%; deployed in Microsoft products)** — production; CaMeL control/data-flow
  isolation (2503.18813); provenance/trust-partitioning + immutable retrieval logs (OWASP
  LLM08:2025 / ASI06); write quarantine/approval = proposal-stage.
- **Retrieval SLOs: essentially unpublished.** No vendor publishes non-empty-rate / relevance /
  staleness SLOs. **OmniSight's "non-empty axis rate" idea is ahead of published practice.**

## Ranked adoption shortlist (as delivered)

1. **Wire Graphiti onto existing Neo4j as the Temporal axis** — cheap/local ingestion model;
   batch episodes; measure ingestion→queryable lag + backfill correctness before trusting
   historical import.
2. **Distilled-experience injection at pickup** (Agent KB / SWE-Exp / AWM pattern) over
   existing lessons + runner_incidents; deterministic token-budgeted retrieval (Aider-style
   PPR via Neo4j GDS + pgvector similarity); heed ConvoMem threshold (<~150 lessons → inject
   curated index wholesale).
3. **Sleep-time consolidation as the write path** — runners append raw; orchestrator
   consolidates offline (matches orchestrator/runner split exactly).
4. **Eval harness before/with the above** — 3-condition ablation on real tickets {no injection
   / retrieved memory / static context}, measuring 公開済み-rate, revert/§11-rate, tokens+turns
   (all already logged in JIRA/Gerrit). Self-defined SLO layer: non-empty rate, sampled judged
   relevance, staleness alerts.
5. **Poisoning hygiene on runner→shared-memory surface** — provenance-tag every write, datamark
   retrieved spans in pickup prompt, quarantine runner-authored writes behind orchestrator
   review, invalidate-don't-delete everywhere.

**Explicit non-adopts:** MemOS (infra tax; lanes useless on metered APIs), MIRIX (write
amplification), Mem0 v3 as dependency (dropped Neo4j + consolidation), A-MEM inline evolution
(research-only), full MS GraphRAG (cost; cheap variant not OSS), LightRAG-for-Cognee swap
(lateral).
