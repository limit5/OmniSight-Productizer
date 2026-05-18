# Cognee KG runbook (OP-899)

**Status**: Enabled runtime bundle — backend containers install Cognee,
and runtime degrades to B8 + B10 baselines when the package or Neo4j is
unavailable. Spec source:
`docs/audit/2026-05-11-sprint-c-readiness-for-sprint-f.md` §1-2.

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
  pagecache 512 M, mem_limit 3 G, mem_reservation 1 G. Persistent
  storage is pinned to `/var/lib/omnisight/neo4j/` (FHS-aligned).
  New prod hosts MUST run the one-time bootstrap
  (`sudo mkdir -p /var/lib/omnisight/neo4j && sudo chown user:user
  /var/lib/omnisight/neo4j`) **before** the first
  `docker compose --profile cognee up` — see
  [`neo4j-path-migration-2026-05-18.md`](neo4j-path-migration-2026-05-18.md)
  §4. Older hosts on the `${HOME}/.local/share/omnisight/neo4j/...`
  path are deprecated; OP-1493 documents the cutover.
* **pgvector** — reuses the existing OmniSight Postgres instance for
  embeddings (no new service).
* **Adapter** — `backend/agents/cognee_integration.py`. Lazy-imports
  `cognee` so the runtime keeps working when the package is absent.
* **Nightly rebuild** — `scripts/cognee_full_rebuild.py`. Idempotent
  per-source — safe to re-run.

## 3. Initial bring-up

### 3.0 Dependency resolution path (OP-915 / AUDIT-1)

The audit (`docs/audit/2026-05-11-deep-system-audit-late.md` §P0-1)
flagged two ways the original OP-899 bring-up could stall:

1. `backend/requirements.in` pins `aiosqlite==0.21.0`, but the old
   `cognee==0.1.44` line carried `aiosqlite<0.21` as a transitive cap.
2. The runtime image strips `pip` (Dockerfile.backend §runner) so
   `docker compose exec backend pip install cognee` raises
   `ModuleNotFoundError: No module named 'pip'`.

**Resolution — Option B + Option C** (chosen 2026-05-11 under OP-915):

* **B (aiosqlite).** Stay on `aiosqlite==0.21.0`. `cognee==1.0.9` (the
  release the OP-899 bake step pins) relaxed the upstream
  `aiosqlite<0.21` ceiling, so no lockfile churn is required. The bake
  step uses `pip install --no-deps cognee==1.0.9 ...` to keep cognee's
  resolver out of the picture entirely; the hashed base lock owns
  aiosqlite + every other shared transitive.
* **C (container).** `Dockerfile.backend` pre-bakes `cognee`,
  `instructor`, `litellm`, `claude-agent-sdk`, plus the second-stage
  extras (`aiolimiter`, `fakeredis[lua]`, `lancedb`, `pylance`,
  `rdflib`, `tokenizers`, …) before pip is stripped from the runtime
  layer. The container image therefore ships with cognee importable;
  `docker compose exec backend pip install ...` is **not** part of any
  bring-up path and would fail by design.

Verify after rebuild with:

```bash
# Host-side (a freshly built image hasn't been started yet):
scripts/verify_cognee_install.sh path/to/python   # any python3 with the wheel set
# Container-side (once `backend` is up):
scripts/verify_cognee_install.sh --container omnisight-productizer-backend-a-1
```

Either invocation prints `OK cognee=<v> aiosqlite=<v> claude_agent_sdk=<v>`
on success and a `FAIL <module> ...` line + traceback on regression.

### 3.1 Compose bring-up sequence

```bash
# 1. Build or pull a backend image that includes OP-899's Cognee layer.
docker compose build backend

# 2. Set a strong Neo4j password. The literal default 'neo4j' is refused.
export NEO4J_PASSWORD='change-me-before-running'
export OMNISIGHT_COGNEE_NEO4J_PASSWORD="$NEO4J_PASSWORD"
export OMNISIGHT_COGNEE_NEO4J_URI='bolt://neo4j:7687'
export OMNISIGHT_COGNEE_NEO4J_USER='neo4j'

# 3. Bring up Neo4j under the cognee profile.
docker compose --profile cognee up -d neo4j

# 4. Wait for the healthcheck to settle.
docker compose ps neo4j

# 5. Verify the backend-a runtime imports Cognee and reaches Neo4j.
#    The smoke script (OP-915 / AUDIT-1) checks cognee + aiosqlite +
#    claude-agent-sdk in one shot; the healthcheck adds the Neo4j leg.
scripts/verify_cognee_install.sh --container omnisight-productizer-backend-a-1
docker compose exec backend python -m scripts.cognee_healthcheck

# 6. Apply schema heads before serving traffic.
docker compose exec backend python -m alembic upgrade head
docker compose exec backend python -m alembic current

# 7. Seed the KG with a full rebuild.
PYTHONPATH=. python -m scripts.cognee_full_rebuild --repo-root .

# 8. Sanity-check that a query path returns a Cognee preamble (instead
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
| Nightly full rebuild | `0 3 * * *` | `deploy/systemd/cognee-nightly-rebuild.timer` → `python -m scripts.cognee_full_rebuild --repo-root /opt/omnisight` |
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
| `OMNISIGHT_COGNEE_NEO4J_URI` | `bolt://neo4j:7687` | Bolt URI |
| `OMNISIGHT_COGNEE_NEO4J_USER` | `neo4j` | Auth |
| `OMNISIGHT_COGNEE_NEO4J_PASSWORD` | _(required)_ | Auth — must not be `neo4j` |
| `OMNISIGHT_COGNEE_QUERY_TIMEOUT` | `30` | Seconds before `CogneeQueryTimeout` |
| `OMNISIGHT_COGNEE_TENANT_ID` | `t-default` | Dataset namespace prefix |
| `NEO4J_PASSWORD` | _(required)_ | Compose env for the `neo4j` service; must not be `neo4j` |

## 5. Error catalog → operator action

| Error code | Adapter exception | Operator action |
|---|---|---|
| `CogneePackageImportFailed` | `CogneeNotInstalled` | Backend image is missing Cognee or one of its import-time deps. Rebuild the OP-899 backend image; runtime helpers stay on B8 + B10. |
| `Neo4jStartFailed` | `CogneeNeo4jUnavailable` | Check `docker compose ps neo4j` + `docker compose logs neo4j`. Restart the service. Do **not** auto-recreate `/var/lib/omnisight/neo4j/`; runtime stays on B8 + B10 in the meantime. |
| `Neo4jPasswordDefault` | `Neo4jPasswordDefault` | Set `NEO4J_PASSWORD` / `OMNISIGHT_COGNEE_NEO4J_PASSWORD` to a strong non-default value and restart. |
| `AlembicUpgradeFailed` | backend startup/deploy failure | Refuse backend start; run `python -m alembic upgrade head`, then verify `0206`, `0207`, and `0224` are present in `alembic current`. |
| `cognee_index_corruption` | `CogneeIndexCorruption` | Schema mismatch — usually after a `cognee` package upgrade. Run `scripts/cognee_full_rebuild.py` (idempotent). |
| `cognee_query_timeout` | `CogneeQueryTimeout` | Inspect `OMNISIGHT_COGNEE_QUERY_TIMEOUT`; check Neo4j load. The single query falls back to B8 / B10 — no data loss. |
| `cognee_ingest_failed` | _(logged, not raised)_ | One source failed during ECL; report counts the failure but the rebuild continues. Re-run the full rebuild to retry. |

## 6. Rollback / recovery

### Pause Cognee at runtime

Stop the `neo4j` service or set a wrong non-default password. The next
adapter call raises `CogneeNeo4jUnavailable` and every helper falls
back to B8 / B10. No code change required.

### Rebuild from scratch

```bash
# Stop dependents, dump state, drop store, rebuild.
docker compose stop worker backend
docker compose exec neo4j neo4j-admin database dump neo4j --to-path=/backups
docker compose down neo4j
sudo mv /var/lib/omnisight/neo4j /var/lib/omnisight/neo4j.$(date +%Y%m%d%H%M%S).bak
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
* Path migration runbook (OP-1493, 2026-05-18 cutover): [`neo4j-path-migration-2026-05-18.md`](neo4j-path-migration-2026-05-18.md)
* B8 baseline (still wired): `backend/agents/repo_map.py`
* B10 baseline (still wired): `backend/agents/lesson_retrieval.py`
* Memory Tool (C1 — coexists, separate data): `backend/agents/memory_tool.py`
* Cognee upstream: https://github.com/topoteretes/cognee
* Claude SDK wrapper: https://github.com/topoteretes/cognee-integration-claude
* Lesson `L-OP-843` — Anthropic-published Cognee citation discipline
