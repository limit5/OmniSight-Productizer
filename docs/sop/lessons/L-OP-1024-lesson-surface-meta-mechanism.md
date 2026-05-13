---
id: L-OP-1024
ticket: OP-1024
title: The lesson-surface meta-mechanism — lessons must be pushed into the pickup prompt, not left for the agent to pull
date: 2026-05-13
tags: [runner, cognee, lessons, meta, architecture]
---

# The lesson-surface meta-mechanism — lessons must be pushed into the pickup prompt, not left for the agent to pull

**Situation**: The project accumulated 78 per-file lessons under
`docs/sop/lessons/` and 14 architecture anti-patterns in
`docs/sop/architecture-anti-patterns.md`, plus an L1 CLAUDE.md rule telling
agents to "scan `docs/sop/architecture-anti-patterns.md` for matching symptoms
before filing or completing architecture/process tickets". In practice nothing
was loaded into a pickup unless the agent happened to go read it — a pull model
with no forcing function. The same traps kept re-occurring (the
"shipped-but-not-deployed" cascade OP-925, the runner self-revert loop OP-829,
the JIRA `blockedBy` direction trap) *after* a lesson had been written about
each one. Writing the lesson down was not enough; the corpus was inert.

**Fix**: AUDIT-29b-6 (OP-1024) wired the corpus into the runner's pickup prompt
so it is *pushed*, per pickup, ranked by relevance to the ticket:

1. `backend/agents/cognee_integration.py` already routed lesson retrieval
   through the Cognee KG with a BM25 fallback (`retrieve_lessons_via_cognee`,
   OP-852/OP-848). OP-1024 added the anti-pattern half: `parse_antipatterns`
   (splits the cookbook on `## N. Title`, reads each pattern's `**Domains**:`
   line, falls back to keyword inference), `collect_antipattern_sources` (one
   `IngestSource` per pattern in its own `antipattern` dataset), and
   `retrieve_antipatterns_via_cognee` (Cognee top-N similarity → keyword-overlap
   fallback, then biased toward patterns whose `Domains` intersect the ticket's
   `area:` labels — so an `area:db` migration ticket auto-surfaces pattern #10).
2. `scripts/cognee-ingest-lessons.py` is the one-shot bootstrap: it loads all
   `L-*.md` lessons + every cookbook pattern into Cognee. `--dry-run` collects +
   counts without touching Neo4j (CI smoke); exit codes distinguish "ok",
   "partial" (some sources failed), "unavailable" (KG down).
3. `auto-runner-jira.py::_build_prompt` gained `_build_lesson_recall_block`
   (gated by the `cognee_recall` flag) and `_build_antipattern_block` (gated by
   `antipattern_inject`). Both emit a `[runner] lesson_recall.*` /
   `[runner] antipattern_inject.*` stderr line per pickup and inject a
   "Relevant lessons" / "Anti-patterns matching this ticket" block *before* the
   Documentation-rules / AC-verification sections. Both degrade to an empty
   string on any retrieval error — a pickup must never fail because lesson
   recall is unavailable.
4. `docs/sop/architecture-anti-patterns.md` gained a `**Domains**:` line on each
   of the 14 patterns and an "Auto-injection" note in its header so future
   pattern authors keep the domain mapping accurate.

**Verification**: `backend/tests/test_cognee_integration.py` covers
`parse_antipatterns` (14 records, domains parsed from the `**Domains**:` line),
`collect_antipattern_sources` (one source per pattern, stable identifiers,
`SOURCE_KIND_ANTIPATTERN` dataset) and `retrieve_antipatterns_via_cognee` (the
Cognee-ranked path with a stub adapter, the keyword fallback when Cognee is not
installed, and the area-match bias). `backend/tests/test_auto_runner_prompt_builder.py`
adds synthetic-ticket cases: `cognee_recall` on → "Relevant lessons" block
present and positioned before the AC marker; flag off → block absent;
`antipattern_inject` on + `area:db` → pattern #10 surfaced with an "area match"
tag; retrieval exception → pickup still builds. `scripts/cognee-ingest-lessons.py
--dry-run --json` is smoke-tested end-to-end (reports 78+ lesson sources, 14
anti-pattern sources). The Deploy/Exercised ACs (live Cognee node counts, ≥10
observed pickups with a recall block) are operator bring-up steps gated on
29b-1/29b-3/29b-5 — they are not code-verifiable in this ticket.

**Generalisation**: **A knowledge corpus that an agent has to remember to go
read is, in practice, not consulted — relevance-ranked context must be *pushed*
into the prompt, not left to be *pulled*.** Whenever you write down "agents
should check X before doing Y", ask "what mechanism makes X land in the prompt
when Y happens?" — if the answer is "the agent will read the rule and go look",
the rule is inert. The push path needs: (1) the corpus in a retrievable store
(here Cognee, BM25 as fallback), (2) a ranking signal tied to the task (ticket
title + AC + `area:` labels), (3) injection at prompt-build time gated by a
default-off flag so it can be rolled out incrementally and rolled back instantly,
and (4) a per-pickup log line so you can verify it is actually firing. The
fallback path matters as much as the happy path: lesson recall that hard-fails
when Neo4j is down would make the KG a pickup dependency — degrade to empty + log
instead. (Adjacent: anti-pattern #13 "shipped-but-not-deployed" — this very
mechanism's Deploy/Exercised ACs are themselves at risk of that trap, which is
why they are tracked as explicit operator bring-up items, not assumed done.)
