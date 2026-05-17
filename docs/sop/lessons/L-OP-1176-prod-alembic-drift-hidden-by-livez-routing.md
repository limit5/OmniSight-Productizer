---
id: L-OP-1176
ticket: OP-1176
title: Caddy /livez-only routing makes /readyz=503 invisible — prod alembic drifted 2 days before discovery
date: 2026-05-17
tags: [deploy, alembic, observability, prod-health, caddy, drift]
---

# Caddy /livez-only routing makes /readyz=503 invisible — prod alembic drifted 2 days before discovery

**Situation**: On 2026-05-16, while attempting an OP-718 deploy to prod
(merger HTTP delegation), it surfaced that backend-b had been returning
`/readyz=503` continuously since 2026-05-14 (~48 hours). The 503 root
cause was `migration_pending: current=0202 latest_file=0200_provider_usage_event.py`
— the prod DB was at alembic head `0202` while the deployed image expected
`0200`. The drift went unnoticed because Caddy's upstream health-check is
wired to `/livez` (cheap pulse, no DB query), which kept returning 200.
Traffic continued to flow normally to backend-b for the entire 48 hours
despite `/readyz` being broken.

The cascade that exposed this:

```
t=−48h  someone applies migrations 0201..0202 to prod DB directly (or via
        a partial deploy); backend code rev still expects 0200 → /readyz=503
t=−48h  Caddy health-check (configured against /livez=200) does not notice
        — backend-b stays "healthy" to the load balancer
t=−2h   OP-718 deploy attempt builds image from develop tip e5549759, expects
        alembic head `m_2026_05_16_3head` (develop's 3-head merge node added
        by OP-1164)
t=−1h   alembic upgrade 0202 → heads starts, runs ~33 linear migrations
        successfully, then hits the m_2026_05_16_3head merge node and raises
        KeyError: '0203' from head_maintainer.remove (0203 had been
        transitively absorbed via the 0237 chain by then)
t=0     emergency hot-patch: drop "0203" from down_revision (filed as OP-1189,
        Gerrit #710); rebuild image; rolling restart; both replicas now
        green on m_2026_05_16_3head
```

**Fix** (already shipped 2026-05-16 / 2026-05-17):

1. **Alembic graph hot-patch** (OP-1189, Gerrit #710 merged into develop;
   then propagated to main via OP-1182 release-cut Gerrit #711). The merge
   node `m_2026_05_16_3head` originally listed
   `down_revision = ("0203", "0237", "m_audit_29_final")`. By the time
   alembic applies this merge node, head_maintainer's in-memory head-set
   no longer contains `0203` (transitively absorbed through 0237's chain
   0237 → ... → 0203). Trying to remove `0203` raises `KeyError`. Fix is
   to list only the actually-current heads at merge-application time:
   `("0237", "m_audit_29_final")`.

2. **Manual deploy protocol** (today's session):
   - Backup PG before any DDL: `docker exec omnisight-pg-primary pg_dump
     -U omnisight -d omnisight --create --clean --if-exists >
     prod-omnisight-<date>-pre-deploy.sql`.
   - Set image tag via `.env` (`sed -i 's|^OMNISIGHT_IMAGE_TAG=.*|...|'`),
     **not** via `docker -e` flag — compose only honours `.env`.
   - Rolling-restart **one replica at a time**, verify `/livez=200`
     **AND** `/readyz=200` before touching the next replica. (Caddy may
     never tell you `/readyz` is broken — you must curl it yourself.)
   - Cleanup duplicate `api_keys` rows (the migration 0203a_kse trips on
     `idx_api_keys_lookup` unique-constraint conflicts) — keep the
     canonical `ak-legacy-<sha256(legacy_secret)[:12]>` form, delete the
     pre-Task-#106 UUID-derived legacy stub.

**Verification**:

- prod 2026-05-16 23:57 onwards: both `backend-a` + `backend-b` on image
  `ghcr.io/your-org/omnisight-backend:main-3967b980` (baked from main HEAD
  with `GITHUB_REF_NAME=main` build arg); MANIFEST.json in container shows
  `git_ref=main, image_sha=3967b9809f..., alembic_head_in_image=m_2026_05_16_3head`.
  `/livez=200` AND `/readyz=200` on both. prod `alembic_version` table =
  `m_2026_05_16_3head`. (See OP-1184 deploy comment.)
- Gerrit Changes #710 (OP-1189 alembic hot-patch on develop) and #711
  (OP-1182 release-cut to main) both merged with human +2 from `sora`.
- 402-commit develop→main delta now zero; `git rev-list --count
  refs/remotes/gerrit/main..refs/remotes/gerrit/develop = 0`.

**Generalisation**:

- **`/livez` ≠ `/readyz`**. `/livez` is a pulse (the process responds at
  all). `/readyz` is the contract (`db.ok`, `migrations.ok`,
  `provider_chain.ok`, `db_pool.ok`). Any load-balancer or health-check
  *must* point at `/readyz` for any check that decides "is this replica
  fit to serve traffic." Pointing at `/livez` masks every DB/migration/
  provider failure mode until something else (operator curl, deploy
  attempt, on-call page) trips over it. The Caddy config for prod
  upstreams must be `/readyz`.
- **A drift window is a silent window unless you *measure* drift**. A DB
  at one alembic head, an image expecting another, and a Caddy that
  loves `/livez` — these three together produce a 48-hour invisible
  outage on the readyz dimension. The mitigation is either (a) a
  staging env that catches the drift before it lands in prod (OP-927 +
  OP-971..974, partly built but **not auto-activated**), or (b) a
  prod-side `alembic_drift_gate` cron that pages on `current ≠ head`,
  or (c) Caddy on `/readyz` so the next request after the migration
  fails immediately and someone notices.
- **Alembic merge nodes are graph operations, not commit metadata**.
  `down_revision = (A, B, C)` is read by alembic as "this node merges
  these N current heads at apply time." If, at apply time, the in-memory
  head-set does NOT actually contain all of A/B/C (because B is reachable
  from C, or vice versa), `head_maintainer.remove` raises `KeyError`.
  Authoring a merge node requires understanding which heads are
  current-and-mutually-unreachable at the point this commit will be
  applied — usually equal to `alembic heads` on the commit's tip *minus*
  the merge node itself. Pre-flight: list `alembic heads` against the
  parent commit; that's what the merge node's `down_revision` should
  enumerate.
- **PG backup is the cheapest insurance you can buy**. Today's session
  generated one 2-MB `pg_dump` *before* the first alembic attempt; that
  let me confidently retry the migration after rolling back via
  `BEGIN; ... ROLLBACK;`-safe DDL. Without the dump I would have hesitated
  to retry. Make the dump unconditional in any deploy-prod runbook.
- **`-e VAR=val` on `docker run` ≠ compose env**. `docker compose up -d`
  reads only the `.env` file; container-side `-e` flags don't reach
  `OMNISIGHT_IMAGE_TAG` because compose resolves the image name BEFORE
  the container exists. Today's session burned 20 minutes on a wrong
  image rolling into the throw-away migration container because
  `-e OMNISIGHT_IMAGE_TAG=$TAG` did not change which image compose pulled.
  Always `sed -i` the `.env` first, then `docker compose up -d`.
- **Filing pattern for emergency deploys**: META + 4-AC children works
  even mid-incident. Today's session filed OP-1176/1180/1183/1186 META
  + 9 children DURING the recovery, which let me close them sequentially
  and gave the operator a single audit-trail point per recovery phase.
  Memory entry [[feedback_4_ac_discipline]] continues to apply at filing
  time; under time pressure, write the AC blocks tersely but DO write them.
