# /api/v1/project-state — operator runbook

OP-904 (F6) — Cross-task awareness API. The runner's prompt-builder
calls this endpoint at pickup time to hydrate the three-axis project
context (structural / temporal / causal) in a single round-trip.

## Endpoint surface

| Path                                     | Method | Auth         | Purpose                                                 |
|------------------------------------------|--------|--------------|---------------------------------------------------------|
| `/api/v1/project-state?ticket=<key>`     | GET    | bearer / cookie | three-axis payload for one ticket                    |
| `/api/v1/project-state/metrics`          | GET    | admin        | axis-by-axis latency, cache stats, last 50 traces       |

The router is mounted from `backend/main.py` via `_include_versioned_router`,
so the `/api/v2/...` mirror is live too.

## Response shape

```json
{
  "ticket": "OP-904",
  "develop_sha": "<sha-12>",
  "structural": { "parent_meta": "...", "phase": "...",
                  "blockers": [...], "blocking": [...], "siblings": [...],
                  "kg_neighbours": [...] },
  "temporal":   { "prior_similar_tickets": [...],
                  "avg_completion_seconds": <int>,
                  "recent_events": [...] },
  "causal":     { "neighbours": [ {"ticket": "...", "failure_class": "...",
                                   "causality": "..."} ] },
  "generated_at": "<iso-8601>"
}
```

Any axis may render as `null` when its per-axis budget expired or the
backing store was down at fetch time. The runner prompt-builder treats
`null` as "no known context for this axis" rather than as an error.

### `kg_source` / `kg_neighbours` (LEGACY wire names — BM25 lessons since OP-2556)

Since OP-2556 (B1) the structural KG half is backed by the **in-process BM25
lesson retrieval** (`backend/agents/lesson_retrieval.py`, OP-848) over
`docs/sop/lessons/*.md` — it WAS the Cognee knowledge graph. The wire names
are kept for consumer compatibility; do not rename them.

* `kg_neighbours` entry shape: `{"identifier": <lesson file stem>,
  "score": <BM25 score>, "kind": "lesson", "summary": <sanitized ≤200-char
  excerpt>}`. The full lesson body is never injected.
* Query = the ticket's own JIRA summary (internal-only, never on the wire);
  falls back to the bare ticket key when the JIRA half degraded.
* `kg_source`: `live` = BM25 search ran (even 0 hits) · `degraded` = index
  build/search raised · `disabled` = lessons dir missing/unreadable.
* Lessons dir: env `OMNISIGHT_LESSONS_DIR`, else repo-root
  `docs/sop/lessons` (`/app/docs/sop/lessons` in the serving image).
* The index is warmed once per worker at FastAPI startup (~15-20 ms cold
  build); warm searches are ~3 ms and run synchronously after the JIRA fetch
  — no network, no DB, no worker thread.
* Cognee infra is PARKED (not deleted) for Phase U option A; nothing on this
  endpoint's hot path imports it anymore.

## Budgets (pinned in `backend/agents/project_state_aggregator.py`)

| Axis         | Budget  | Backing store                        |
|--------------|--------:|--------------------------------------|
| structural   | 800 ms  | JIRA REST + in-process BM25 lessons  |
| temporal     | 600 ms  | Graphiti MCP                         |
| causal       | 600 ms  | failure_class + failure_graph BFS    |
| **total**    | **2 s** | wall-clock; cancels in-flight on overrun |

The constants `STRUCTURAL_BUDGET_SEC`, `TEMPORAL_BUDGET_SEC`,
`CAUSAL_BUDGET_SEC`, `TOTAL_BUDGET_SEC` are the single source of truth;
the contract test `test_axis_budgets_match_spec` pins them, so a budget
change requires a co-change to the test and to this table.

## Caching

* **Backend:** process-local LRU + TTL in
  `backend.agents.project_state_cache.default_cache`.
* **Key:** `(ticket_key, develop_sha)`.
* **TTL:** 5 minutes (300 s).
* **Cap:** 256 entries per replica.
* **Invalidation paths:**
  * JIRA webhook → call `invalidate_for_webhook(ticket_key)`. Drops
    every entry for that ticket regardless of SHA.
  * Develop merge → call `invalidate_for_develop_merge()`. Clears the
    whole cache; the next request rehydrates against the new SHA.

The cache writes nothing to disk; a replica restart loses its slice and
the first request after restart computes fresh.

## Error catalog

| Error                          | HTTP | Operator action |
|--------------------------------|-----:|-----------------|
| `ProjectStateAxisTimeout`      | 200  | none — axis renders as `null`. Watch the `axis_error` count on the metrics surface. |
| `ProjectStateAllAxesFailed`    | 200  | check JIRA / lessons-dir mount / failure-graph health; the runner is now operating without cross-task context. |
| `ProjectStateCacheCorrupted`   | 200  | auto-evicted + recomputed; investigate the `corruptions` stat if it grows. |
| `ProjectStateBudgetExceeded`   | 200  | partial response shipped; pages on sustained tail. |
| 400 — `ticket must look like…` | 400  | client bug; ticket keys must match `OP-<alphanum>`. |
| 401 — `Authentication required` | 401 | client missing bearer token / cookie. |

## Observability

`GET /api/v1/project-state/metrics?limit=50` returns:

```json
{
  "cache": {"size": <int>, "hits": <int>, "misses": <int>,
            "evictions": <int>, "corruptions": <int>},
  "traces": [
    {"ticket": "OP-904", "develop_sha": "<sha>",
     "cache_hit": false, "total_latency_sec": 0.512,
     "axis_latency_sec": {"structural": 0.31, "temporal": 0.12, "causal": 0.08},
     "axis_error": {}, "budget_exceeded": false,
     "captured_at": "<iso-8601>"}
  ]
}
```

The trace buffer is bounded at 200 entries per replica. The dashboard
should poll this surface and chart:

* `cache.hits / (cache.hits + cache.misses)` — target ≥ 0.7 in steady
  state.
* p95 `total_latency_sec` — page if it crosses 2 s sustained.
* count of `budget_exceeded=true` per 5-min window — page on > 5.
* count of non-empty `axis_error` per axis per 5-min window — paging
  threshold per-axis = > 10 (signals backing-store degradation).

## DoD spot-check

After deploy:

1. `curl -H 'Authorization: Bearer <token>' \
   https://omnisight.example/api/v1/project-state?ticket=OP-904`
   returns 200 with all three axes populated.
2. Run the contract suite locally:
   `pytest backend/tests/test_project_state.py -q` → all green.
3. Issue 10 sample queries against representative ticket keys
   (mix of META, leaf, runner-failure-heavy); confirm each round-trip
   < 2 s on the metrics surface.
4. Synthetic degrade: monkey-patch one axis fetcher to sleep > budget
   and re-run the sample queries; confirm the offending axis comes
   back as `null` and the rest of the payload still ships.
