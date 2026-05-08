---
id: L-OP-693
ticket: OP-693
title: Production Readiness Gate Q1 must check DB image, not just app image
date: 2026-05-07
tags: [ci, events, gerrit, git, jira, runner]
legacy_lesson: 19
---

# Production Readiness Gate Q1 must check DB image, not just app image

**Situation**: OP-693 deploy attempt (16:25) hit alembic 0193 (BP.Q.4 embedding_chunks) failing with `extension "vector" is not available`. The migration's docstring (line 36-38) says "PostgreSQL deployments must enable the existing vector extension" but the running pg-primary container used `postgres:16-alpine` which doesn't ship pgvector. SOP §1 Production Readiness Gate Q1 ("這條 code path 在 production image 真的跑得起來嗎") had been checked against the backend image (correctly verified `import backend.merger_agent` works), but never against the DB image — pgvector is a DB-side dependency that the alembic apply touches.

**Fix**: SOP §1 Production Readiness Gate Q1 needs to be answered for EACH artifact the migration / deploy / runtime touches: backend image, DB image, sidecar images (caddy, cloudflared), external services (Gerrit, JIRA). For DB migrations specifically, run `docker compose run backend-a python -m alembic upgrade --sql heads` against fresh DB locally (or staging) to surface CREATE EXTENSION / CREATE INDEX / CHECK constraint issues that only fail at apply time.

The operator-side fix landed: deploy/postgres-ha/Dockerfile.pgvector builds `omnisight-postgres-pgvector:16-alpine` from `postgres:16-alpine` + `apk build-base + git + clone pgvector v0.8.0 + make install`. Compose updated to use this image. Replication preserved (alpine uid 70 stays consistent across primary + standby).

**Verification**: After image swap, alembic upgrade heads applied all 13 migrations cleanly. CREATE EXTENSION vector + CREATE INDEX hnsw both succeeded. Subsequently exposed a separate bug (HNSW on bare-dim vector — fixed via OP-707 #72) but that's downstream of the pgvector availability issue.

**Generalisation**: "Static lists / catalogs aligned with live" (SOP § Production Readiness Gate Q2) needs to extend beyond the app's TABLES_IN_ORDER / drift-guard tests — DB-level extension requirements (`CREATE EXTENSION`), DB-level CHECK constraints, sidecar version pins, and runtime feature flags all need their own pre-flight verification against the live image they'll run on.
