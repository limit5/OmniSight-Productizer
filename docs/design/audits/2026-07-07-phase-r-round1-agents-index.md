# Phase R design audit — round 1, agent findings index (condensed)

> Two adversarial subagent audits of design v1 (full codex report:
> `2026-07-07-phase-r-round1-codex.md`, verdict NO-GO, 11 findings). Full agent report
> texts lived in the working session; every BLOCKER/HIGH/MED below is either resolved in
> design v2's Revision history/body or in the Deferred register. This index preserves the
> finding titles + verdicts for the audit trail.

## Agent A — correctness / root-cause lens (verdict: GO-WITH-CHANGES, 16 findings)

Root-cause table: R2/R4/R5 claims VERIFIED exactly (stub commit 708f95a9; dispatcher assigned
nowhere; git-subprocess "unknown"); R1 "Neo4j present and correct" REFUTED; R3 framing REFUTED.

- F1 BLOCKER: no live PG writer ever existed — `_persist_runner_incident` = in-memory
  deque(1024); ALL 1,935 rows have 64-hex sha256 ids = backfill-only (live path would be
  32-hex uuid4); "stopped 6/9" = "nobody ran backfill after 6/9".
- F2 HIGH: v1's "zero warnings in 30-day journal" evidence doubly invalid (warning guards an
  unfailable append; runner stdout goes to files, journal's first boot = 2026-06-15).
- F3 HIGH: R1 exit unreachable — no JIRA-kind ingest exists anywhere (collect_jira_sources:
  0 callers); nightly rebuild timer disabled + crash-looping since May 23
  (ModuleNotFoundError: backend; stale v0.5.0 image pin).
- F4 HIGH: Option A not config-only — ingest + search must share one store across 2 replicas
  + the ephemeral rebuild container.
- F5 HIGH: live probe — cognee graph provider = embedded `ladybug`, empty graph_url; our
  NEO4J_* env never reaches the SDK; neo4j DNS-unreachable from backend; no
  LLM_API_KEY/EMBEDDING_* set.
- F6 MED: R4's overlay source EMPTY on prod today (build_git_sha=None, /etc/omnisight empty)
  — overlay writer itself is an AUDIT-23 seam; fallback env name should be
  OMNISIGHT_BUILD_GIT_SHA (existing), not an invented one.
- F7 MED: R0's R4 exit said 64-hex for a git SHA (wrong — 40-hex; 64 = image digest).
- F8 MED: idempotency `attempt` is process-local (~always 1 under ephemeral runners) → use
  OP-977 claim fencing token.
- F9 MED: webhook invalidation is per-replica (process-local caches) + ADR-0015 contains no
  invalidation contract (it lives in OP-904 AC) — v1 overclaimed.
- F10 MED: structural keeps its own fake-empty (degraded halves render as "no blockers") —
  per-half source markers required.
- F11 MED: the only real consumer renderer lives in the runner tree with a separate deploy
  channel (tree-pull + restart) — v1's rollout plan ignored it; FE panel consumes only
  /metrics traces; 3 pinned tests break on new shapes.
- F12 MED: R3's write-failure path activates a consumer-less retry queue
  (incident_queue dir + "operator hourly cron" that was never created) — new hollow seam.
- F13 MED: the 6/9→now transcripts live ONLY in volatile /tmp (13–112 MB, actively written)
  — archive before any code work; backfill parser's mtime fallback skews created_at for
  ever-appended files; DEFAULT_LOG_DIR no longer receives transcripts.
- F14 LOW: "phase heuristic already sketched" is false (line 251 is a passthrough).
- F15 LOW: R1 exit must grep all four degrade tokens (degrade / adapter_unavailable /
  inner_timeout / axis_timeout).
- F16 LOW: two write seams (jira_dispatch + memory_writeback._do_incident) must unify or
  double-write once persistence is real; only ~1/15 transition sites passes failure_class
  (~7 writeback sites do).
- Open-question answers: Q1 kill-switch YES (OMNISIGHT_PROJECT_STATE_JIRA_PULL); Q2 stale-
  marked with age cap + sanitization either way; Q3 claim-token key; Q4 no U5 string in
  prompts (renderer collapses to one line/omit).

## Agent B — ops + security lens (verdict: GO-WITH-CHANGES, 15 findings)

- F1 BLOCKER: stale-while-revalidate unimplementable against the real cache API (get()
  deletes expired at read; whole-payload-only validated set(); SHA-keyed entries; cache-hit
  path never reaches the aggregator).
- F2 HIGH: webhook-invalidate + stale-serve mutually destructive (just-updated tickets are
  guaranteed cold = the common case becomes the worst case; "optional" pre-warm was
  load-bearing).
- F3 HIGH: invalidate-vs-inflight-write race re-caches pre-update data (no epoch/CAS).
- F4 HIGH: degraded-null payloads latch into cache for full TTL; per-replica divergence
  through round-robin LB; under JIRA outage the axis freezes, not flaps.
- F5 HIGH: budget math — JIRA + cognee halves run SEQUENTIALLY inside one 800 ms budget over
  a fork-per-call curl (--max-time 30, no retries); axis cancellation orphans curl for up to
  30 s; to_thread pool starvation reaches the causal axis.
- F6 HIGH: one shared writable volume × two replicas = concurrent cross-container SQLite →
  "database is locked"; "config-only" claim conditional on an unverified search_type/LLM
  dependency; cognee installed --no-deps (silent missing-dep plausible).
- F7 MED: writable persistent state inside a read_only:true container whose content is
  prompt-reachable (kg_neighbours → pickup prompts) — volume integrity = prompt integrity.
- F8 MED: webhook auth = single static fleet-wide bearer (replayable; no HMAC/timestamp);
  _on_jira_event failures swallowed = future degrade-silent invalidation; the bigger load is
  LEGITIMATE runner-farm churn → 1:1 webhook→JIRA-GET amplification against the shared
  rate-limited credential; content-poisoning via webhook payload correctly refuted.
- F9 MED: `^OP-[A-Z0-9]+$` unbounded → any authenticated caller burns the shared JIRA rate
  limit; 256 garbage keys evict the whole LRU (bogus tickets ARE cached); endpoint becomes
  an authenticated JIRA read-proxy. Fix: negative-cache outside LRU + key cap + per-principal
  rate limit.
- F10 MED: stale-vs-null → stale-marked wins WITH (i) age cap ~1h, (ii) fetched_at, (iii) a
  renderer preamble line defining stale semantics; length-cap + control-char-strip all
  JIRA-derived strings (instruction-bearing text passes verbatim inside JSON strings).
- F11 MED: R3.2 canary homed in the D9/D10 auto-rollback actuator couples an unrelated duty
  into a safety path (and it reads prod through the staging tree's env); "WARNING line after
  7 days" reproduces the no-one-reads-logs disease → dedicated oneshot timer + JIRA-ticket
  escalation.
- F12 MED: R5 token argument is a wash (~35 vs ~30 tokens); the misleading-context argument
  decides: honesty API-side, renderer maps unavailable → null/omit; no roadmap strings in
  prompts.
- F13 LOW: R4 survives; add: per-request git-fork removal is a real latency win; rolling SHA
  makes (ticket, sha) key churn real for the first time.
- F14 LOW: R1 image-roll risk refuted (digest-pinned REF) with procedure conditions: edit
  prod checkout, `compose config` diff, sequenced a→readyz→b recreate.
- F15 HIGH: (a) kill-switch REQUIRED — existing switches wrong-grained (global flag kills
  good axes; per-ticket label useless mid-incident); backend failure modes need an env-flip
  remedy, not a release train. (b) THE PIVOT: the runner already GETs the issue at pickup
  (auto-runner-jira.py:1138, fields=summary,labels,components,issuetype) — appending
  `issuelinks,parent` delivers blockers/parent at ZERO extra round-trips, fresher than any
  cache — the design must justify the cache machine against this one-liner or shrink R2 to it.
- Minor: phase heuristic claim false; idempotency key → claim fencing token.
