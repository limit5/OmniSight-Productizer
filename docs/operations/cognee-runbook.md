# Cognee KG runbook (OP-852)

**Status**: Optional bundle — runtime degrades to B8 + B10 baselines when
disabled. Spec source: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.4.

This runbook covers bring-up, daily operations, recovery, and the
fallback verification procedure that satisfies the OP-852 DoD ("5
sample queries comparing Cognee vs B8/B10 baselines + B8 + B10 marked
as fallback-only").

---

## 1. What this is

The Cognee Knowledge-Graph layer ingests four content kinds — codebase
files (Python/TS), JIRA tickets, Gerrit patchsets, and `L-*.md`
lessons — into a Neo4j graph backed by pgvector embeddings. Two runtime
helpers replace the previous baseline retrievers and gracefully fall
back to them when Cognee is unreachable:

| Replaces | New entry point | Old fallback (still wired) |
|---|---|---|
| B8 — `repo_map.build_repo_map_system_prefix` (PageRank) | `cognee_integration.build_repo_map_via_cognee` | calls B8 on `CogneeNeo4jUnavailable` / `CogneeQueryTimeout` / no hits |
| B10 — `lesson_retrieval.retrieve_lessons` (BM25) | `cognee_integration.retrieve_lessons_via_cognee` | calls B10 on the same conditions |

The C1 Memory Tool is **separate** — it is a filesystem-based scratchpad
for runtime state, not a retrieval index. The two systems carry
non-overlapping data and never read each other's storage (AC #6).

## 2. Components

```
┌──────────────────────────┐    ECL pipeline    ┌──────────────────────┐
│  scripts/                │ ─────────────────► │  Neo4j (graph)       │
│  cognee_full_rebuild.py  │                    │  pgvector (vectors)  │
└──────────────────────────┘                    └──────────────────────┘
              ▲                                              ▲
              │ nightly cron                                 │
              │                                              │
┌──────────────────────────┐                                 │
│  post-receive hook       │ ─── incremental ECL ────────────┘
│  (sora-bridge / OP-837)  │
└──────────────────────────┘
              ▲
              │ commit
        developers
```

* **Neo4j** — runs in the `cognee` Docker compose profile. Heap 2 G,
  pagecache 512 M, mem_limit 3 G, mem_reservation 1 G.
* **pgvector** — reuses the existing OmniSight Postgres instance for
  embeddings (no new service).
* **Adapter** — `backend/agents/cognee_integration.py`. Lazy-imports
  `cognee` so the runtime keeps working when the package is absent.
* **Nightly rebuild** — `scripts/cognee_full_rebuild.py`. Idempotent
  per-source — safe to re-run.

## 3. Initial bring-up

```bash
# 1. Install the optional bundle on the host that will host the worker.
./backend/.venv/bin/pip install \
    cognee==0.1.46 \
    cognee-integration-claude==0.1.5 \
    neo4j==5.24.0 \
    claude-agent-sdk==0.0.10

# 2. Set the Neo4j password (default 'neo4j' is fine for dev).
export NEO4J_PASSWORD='change-me-in-prod'

# 3. Bring up Neo4j under the cognee profile.
docker compose --profile cognee up -d neo4j

# 4. Wait for the healthcheck to settle.
docker compose ps neo4j

# 5. Seed the KG with a full rebuild.
PYTHONPATH=. python -m scripts.cognee_full_rebuild --repo-root .

# 6. Sanity-check that a query path returns a Cognee preamble (instead
#    of falling back to B8 / B10).
PYTHONPATH=. python -c "
from pathlib import Path
from backend.agents.cognee_integration import build_repo_map_via_cognee
print(build_repo_map_via_cognee(Path.cwd(), ticket_text='backend/agents/cognee_integration.py'))
"
# Expect a 'Repo Map Context (Cognee KG)' header — if you see plain
# 'Repo Map Context' (no '(Cognee KG)') the call fell back to B8.
```

## 4. Day-2 operations

### Cron schedule

| Cron | Cadence | Command |
|---|---|---|
| Nightly full rebuild | `0 4 * * *` | `PYTHONPATH=/app python -m scripts.cognee_full_rebuild --repo-root /app` |
| Backup snapshot | `30 4 * * *` | `docker compose exec neo4j neo4j-admin database dump neo4j --to-path=/backups` |

### Incremental ingestion on commit

The post-receive hook (sora-bridge / OP-837) calls
`run_ecl_pipeline(..., incremental_paths=[...changed code paths...])`
for the change set. Other source kinds (lessons, JIRA, Gerrit) are
managed by their own event hooks; the post-receive hook bounds itself
to code only.

### Environment knobs

| Variable | Default | Purpose |
|---|---|---|
| `OMNISIGHT_COGNEE_NEO4J_URL` | `bolt://localhost:7687` | Bolt URL |
| `OMNISIGHT_COGNEE_NEO4J_USER` | `neo4j` | Auth |
| `OMNISIGHT_COGNEE_NEO4J_PASSWORD` | `neo4j` | Auth — override in prod |
| `OMNISIGHT_COGNEE_QUERY_TIMEOUT` | `30` | Seconds before `CogneeQueryTimeout` |
| `OMNISIGHT_COGNEE_TENANT_ID` | `t-default` | Dataset namespace prefix |
| `NEO4J_PASSWORD` | `neo4j` | Compose env for the `neo4j` service |

## 5. Error catalog → operator action

| Error code | Adapter exception | Operator action |
|---|---|---|
| `cognee_not_installed` | `CogneeNotInstalled` | (Expected on hosts without the optional bundle.) Install per §3 if you want Cognee here. |
| `cognee_neo4j_unavailable` | `CogneeNeo4jUnavailable` | Check `docker compose ps neo4j` + `docker compose logs neo4j`. Restart the service. Runtime stays on B8 + B10 in the meantime — no operator-visible regression. |
| `cognee_index_corruption` | `CogneeIndexCorruption` | Schema mismatch — usually after a `cognee` package upgrade. Run `scripts/cognee_full_rebuild.py` (idempotent). |
| `cognee_query_timeout` | `CogneeQueryTimeout` | Inspect `OMNISIGHT_COGNEE_QUERY_TIMEOUT`; check Neo4j load. The single query falls back to B8 / B10 — no data loss. |
| `cognee_ingest_failed` | _(logged, not raised)_ | One source failed during ECL; report counts the failure but the rebuild continues. Re-run the full rebuild to retry. |

## 6. Rollback / recovery

### Pause Cognee at runtime

Set `NEO4J_PASSWORD=invalid` (or stop the `neo4j` service). The next
adapter call raises `CogneeNeo4jUnavailable` and every helper falls
back to B8 / B10. No code change required.

### Rebuild from scratch

```bash
# Stop dependents, dump state, drop store, rebuild.
docker compose stop worker backend
docker compose exec neo4j neo4j-admin database dump neo4j --to-path=/backups
docker compose down neo4j
docker volume rm <project>_neo4j-data
docker compose --profile cognee up -d neo4j
PYTHONPATH=. python -m scripts.cognee_full_rebuild --repo-root .
docker compose start backend worker
```

`run_ecl_pipeline` is per-source idempotent — re-running it on a
populated KG is a no-op for unchanged identifiers.

### Restore from backup

```bash
docker compose stop neo4j
docker compose exec neo4j neo4j-admin database load neo4j --from-path=/backups --overwrite-destination
docker compose start neo4j
```

## 7. DoD verification — Cognee vs B8/B10 sample queries

Five hand-picked queries that exercise the KG against the baseline so
the operator can record the AC #4 / #5 comparison evidence in the JIRA
ticket. Run from a host with the Cognee bundle installed and Neo4j up.

```python
from pathlib import Path
from backend.agents.cognee_integration import (
    build_repo_map_via_cognee, retrieve_lessons_via_cognee,
)
from backend.agents.repo_map import build_repo_map_system_prefix
from backend.agents.lesson_retrieval import retrieve_lessons

repo, lessons = Path.cwd(), Path.cwd() / "docs/sop/lessons"

probes = [
    ("repo-map for OP-852 ticket", "OP-852 cognee migration"),
    ("repo-map for jira_dispatch refactor", "jira_dispatch retry budget"),
    ("repo-map for tree-sitter parser", "treesitter_parser grammar missing"),
]
for label, seed in probes:
    print(f"\n=== {label} ===")
    print("--- Cognee ---")
    print(build_repo_map_via_cognee(repo, ticket_text=seed))
    print("--- B8 baseline ---")
    print(build_repo_map_system_prefix(repo, ticket_text=seed))

lesson_probes = [
    ("Webhook retries", "Add idempotency and retry budget handling"),
    ("Anthropic cache fallback", "Last message cache control + TTL"),
]
for title, ac in lesson_probes:
    print(f"\n=== lesson: {title} ===")
    print("--- Cognee ---", retrieve_lessons_via_cognee(lessons, ticket_title=title, acceptance_criteria=ac))
    print("--- B10 baseline ---", retrieve_lessons(lessons, ticket_title=title, acceptance_criteria=ac))
```

Compare the two outputs per probe and record the verdict (Cognee
better / parity / fallback-needed) in the JIRA ticket comment as the
DoD evidence.

## 8. References

* Spec: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.4
* Adapter: `backend/agents/cognee_integration.py`
* Tests: `backend/tests/test_cognee_integration.py`
* Compose service: `docker-compose.yml` `neo4j` (under `cognee` profile)
* B8 baseline (still wired): `backend/agents/repo_map.py`
* B10 baseline (still wired): `backend/agents/lesson_retrieval.py`
* Memory Tool (C1 — coexists, separate data): `backend/agents/memory_tool.py`
* Cognee upstream: https://github.com/topoteretes/cognee
* Claude SDK wrapper: https://github.com/topoteretes/cognee-integration-claude
* Lesson `L-OP-843` — Anthropic-published Cognee citation discipline
