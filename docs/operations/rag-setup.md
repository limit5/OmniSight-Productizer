# RAG Setup - Operator Guide

> BP.Q.8. Audience: operators enabling, indexing, or debugging the internal
> workspace KnowledgeRetrieval layer.

## Overview

```
[1 Schema ] Alembic 0186 creates embedding_chunks + pgvector indexes + RLS
              |
[2 Embed  ] rag_indexer chunks git-tracked code/docs/TODO/SKILL files
              |
[3 Store  ] PgvectorStore writes tenant-scoped vectors to embedding_chunks
              |
[4 Query  ] KnowledgeRetrieval embeds the query and returns cited top-K chunks
```

BP.Q is for internal codebase, docs, TODO, and skill retrieval. It is not web
search and it is not GraphRAG. Explore sub-agents use RAG first and fall back
to Grep/Glob/Read when the index is unavailable or the query needs exact
syntax matching.

## Prerequisites

| Requirement | Production default | Notes |
|---|---|---|
| PostgreSQL | Existing `OMNISIGHT_DATABASE_URL` | Required for pgvector-backed production retrieval |
| pgvector extension | `CREATE EXTENSION IF NOT EXISTS vector` | Alembic 0186 runs this, but the DB role must be allowed to create/use extensions |
| RAG table | `embedding_chunks` | Created by Alembic 0186 with tenant RLS |
| Vector store | `pgvector` | `OMNISIGHT_RAG_VECTOR_STORE=pgvector` is the default |
| Embedding provider | `local` | Indexer default is `sentence-transformers/all-MiniLM-L6-v2` for air-gap |
| Tenant scope | `t-default` | Override with `OMNISIGHT_RAG_TENANT_ID` per deployment/tenant |

## Enable pgvector

Run the normal backend migration flow against the production PostgreSQL DSN.
Alembic revision `0186` creates the extension, table, `(tenant_id, source_path)`
index, HNSW vector index, and the RLS policy.

```bash
export OMNISIGHT_DATABASE_URL='postgresql://...'
cd backend
.venv/bin/python -m alembic -c alembic.ini upgrade head
```

Verify the extension and table after migration:

```bash
psql "$OMNISIGHT_DATABASE_URL" -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
psql "$OMNISIGHT_DATABASE_URL" -c "\d+ embedding_chunks"
psql "$OMNISIGHT_DATABASE_URL" -c "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'embedding_chunks';"
```

Expected:

- `vector` exists.
- `embedding_chunks` has `embedding vector`, `metadata jsonb`, and
  `idx_embedding_chunks_embedding_hnsw`.
- `relrowsecurity` and `relforcerowsecurity` are both true.

## Configure embeddings

The default operator path is local embeddings so RAG can run in air-gapped
deployments:

```bash
export OMNISIGHT_RAG_VECTOR_STORE=pgvector
export OMNISIGHT_RAG_EMBEDDING_PROVIDER=local
export OMNISIGHT_RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
export OMNISIGHT_RAG_TENANT_ID=t-default
```

For hosted OpenAI embeddings:

```bash
export OMNISIGHT_RAG_VECTOR_STORE=pgvector
export OMNISIGHT_RAG_EMBEDDING_PROVIDER=openai
export OMNISIGHT_RAG_EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY='...'
export OMNISIGHT_RAG_TENANT_ID=t-default
```

Do not commit API keys or provider tokens. Put production secrets in the same
secret-management path used for other runtime environment variables.

## Build the initial index

Run the initial bulk index from the repository root after migrations and env
are in place:

```bash
PYTHONPATH=. backend/.venv/bin/python -m backend.agents.rag_indexer --repo-root .
```

The command prints:

```text
indexed=<files> deleted=<files> skipped=<files> chunks=<chunks>
```

It indexes git-tracked code files, `docs/**/*.md`, `TODO.md`, and `SKILL.md`.
It skips binary files, `test_assets/`, virtualenvs, caches, `.git`, and
`node_modules`.

## Incremental indexing

For developer and staging worktrees, install the post-merge hook:

```bash
PYTHONPATH=. backend/.venv/bin/python -m backend.hooks.post_merge_rag_index --install
export OMNISIGHT_RAG_INDEX_ON_MERGE=1
```

The hook refreshes changed indexable paths after a merge and never blocks the
merge itself. To force the same incremental path manually:

```bash
PYTHONPATH=. backend/.venv/bin/python -m backend.agents.rag_indexer --repo-root . --incremental
```

## Cron schedule

Use cron as the production safety net even when post-merge hooks are enabled.
The recommended schedule is:

- Every 15 minutes: incremental refresh for recently merged changes.
- Daily at 03:20 local time: full refresh with stale-source pruning.

Example `/etc/cron.d/omnisight-rag-index`:

```cron
SHELL=/bin/bash
PYTHONPATH=/srv/omnisight
OMNISIGHT_DATABASE_URL=postgresql://...
OMNISIGHT_RAG_VECTOR_STORE=pgvector
OMNISIGHT_RAG_EMBEDDING_PROVIDER=local
OMNISIGHT_RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
OMNISIGHT_RAG_TENANT_ID=t-default

*/15 * * * * omnisight cd /srv/omnisight && backend/.venv/bin/python -m backend.agents.rag_indexer --repo-root /srv/omnisight --incremental >> /var/log/omnisight/rag-index.log 2>&1
20 3 * * * omnisight cd /srv/omnisight && backend/.venv/bin/python -m backend.agents.rag_indexer --repo-root /srv/omnisight >> /var/log/omnisight/rag-index.log 2>&1
```

For multi-tenant deployments, create one cron entry per tenant with a distinct
`OMNISIGHT_RAG_TENANT_ID`. The RLS policy and `PgvectorStore` tenant setting
deny cross-tenant reads/writes; the explicit `tenant_id` query filter remains
in the runtime path as a second guard.

## Smoke test

After indexing, verify the table has tenant-scoped rows:

```bash
psql "$OMNISIGHT_DATABASE_URL" -c "SELECT tenant_id, count(*) FROM embedding_chunks GROUP BY tenant_id ORDER BY tenant_id;"
```

Then run a minimal tool handler query:

```bash
PYTHONPATH=. backend/.venv/bin/python - <<'PY'
import asyncio
from backend.agents.runner_handlers import knowledge_retrieval_handler

async def main():
    result = await knowledge_retrieval_handler({
        "query": "how is pgvector enabled for RAG",
        "tenant_id": "t-default",
        "top_k": 3,
    })
    print(result["tenant_id"], len(result["results"]))
    for row in result["results"]:
        print(row["citation"]["path"], row["citation"]["line_range"], row["citation"]["similarity_score"])

asyncio.run(main())
PY
```

Expected:

- The result tenant matches the requested tenant.
- Each result has a repo-relative citation path and a line range when the
  chunker recorded one.
- Tenant A queries do not return Tenant B chunk IDs or source text.

## Troubleshooting

| Symptom | Check |
|---|---|
| `pgvector unavailable` or migration fails | Confirm the PostgreSQL image has the pgvector extension installed and the migration role can run `CREATE EXTENSION` |
| `OMNISIGHT_DATABASE_URL is required` | Set `OMNISIGHT_DATABASE_URL` or `DATABASE_URL` before indexing/querying |
| `sentence-transformers is required` | Install the optional local embedding runtime in the production image, or switch to `OMNISIGHT_RAG_EMBEDDING_PROVIDER=openai` |
| OpenAI embedding calls fail | Confirm `OPENAI_API_KEY` and `OMNISIGHT_RAG_EMBEDDING_MODEL=text-embedding-3-small` |
| Query returns zero results | Run the bulk index, confirm rows exist for the requested tenant, and check the cron log for indexer errors |
| Tenant leakage suspected | Query `embedding_chunks` grouped by tenant, then smoke Tenant A and Tenant B separately through `KnowledgeRetrieval`; RLS should deny missing or mismatched `omnisight.tenant_id` |

## Related

- `backend/agents/rag.py` - vector store and embedding adapters
- `backend/agents/rag_indexer.py` - bulk and incremental indexer
- `backend/hooks/post_merge_rag_index.py` - git post-merge hook
- `backend/agents/runner_handlers.py` - `KnowledgeRetrieval` handler
- `backend/alembic/versions/0186_embedding_chunks.py` - pgvector table, indexes, and RLS
- `backend/tests/test_rag.py` - adapter, handler, tenant isolation, and optional live pgvector tests
