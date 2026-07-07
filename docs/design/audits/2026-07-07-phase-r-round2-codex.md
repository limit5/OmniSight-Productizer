e` at [auto-runner-jira.py:1538](/home/user/work/sora/OmniSight-sora/auto-runner-jira.py:1538). Appending fields is same request. But `_render_project_state_block` currently dumps raw JSON and tells the model to treat null axes as no context at [auto-runner-jira.py:1131](/home/user/work/sora/OmniSight-sora/auto-runner-jira.py:1131), so R5 temporal omission and compact blockers require renderer logic, not just data fetch.
   Fix: ticket R2a/R5 renderer changes together with pinned prompt-output tests.

9. **MEDIUM — R2b negative cache remains under-specified**
   Claim: negative-cache 404s outside the LRU.
   Evidence: existing cache is one positive payload LRU keyed by `(ticket, develop_sha)`, max 256 entries at [project_state_cache.py:45](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:45), with `get/set` only for full project-state payloads at [project_state_cache.py:97](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:97). There is no separate negative-cache surface. Payload validation only checks top-level required keys at [project_state_cache.py:181](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:181), so per-half source markers inside `structural` will pass validation.
   Fix: define a separate bounded negative-cache object for JIRA 404/key-abuse, with TTL, key normalization, metrics, and per-replica behavior.

**Verdict: GO-WITH-CHANGES.** The v2 direction mostly holds, but R3 ID shape/fallback, R2b transport seams, R1 rebuild execution, and R4 bundle gating need concrete edits before ticket cut.
tokens used
160,007
1. **MEDIUM — R3 live IDs are indistinguishable from backfill IDs**
   Claim: no-migration idempotency uses `incident_id = sha256(ticket|terminal|claim_token)`.
   Evidence: table accepts arbitrary `TEXT PRIMARY KEY` at [0206_runner_incidents.py:83](/home/user/work/sora/OmniSight-sora/backend/alembic/versions/0206_runner_incidents.py:83), and backfill already inserts 64-hex hashes with `ON CONFLICT (incident_id) DO NOTHING` at [backfill_runner_incidents.py:40](/home/user/work/sora/OmniSight-sora/scripts/backfill_runner_incidents.py:40). Collision probability is negligible, but v2’s R0 exit criterion says live rows should be “32-hex uuid or claim-token-derived id (NOT 64-hex backfill fingerprint)” while R3.1 proposes a 64-hex sha256. That breaks observability.
   Fix: make live deterministic IDs domain-separated and visually distinct, e.g. `live-v1-<sha256>` or UUIDv5 hex from the tuple. `incident_id` is `TEXT`, so no migration needed.

2. **HIGH — R3 pre-claim fallback can drop real distinct incidents**
   Claim: pre-claim terminals fall back to `(ticket|terminal|wallclock-hour-bucket)`.
   Evidence: some recovery callers explicitly “predate the claim” per [jira_dispatch.py:2696](/home/user/work/sora/OmniSight-sora/backend/agents/jira_dispatch.py:2696). Current live recorder uses fresh UUIDs by default at [incident_recorder.py:196](/home/user/work/sora/OmniSight-sora/backend/agents/incident_recorder.py:196), so distinct repeated failures are preserved today in memory. The proposed hour bucket would collapse two legitimate same-ticket same-terminal pre-claim failures in the same hour under the PK conflict.
   Fix: include a stable event discriminator for pre-claim paths: traceback hash + summary hash + minute/second, or just use UUID for pre-claim incidents and reserve deterministic IDs for claim-token paths.

3. **MEDIUM — R2b timeout and fields are not currently threadable through `fetch_story`**
   Claim: `_structural_jira` can call `fetch_story` with explicit `fields=` and its own ~1.5s subprocess timeout.
   Evidence: `JiraAdapter.fetch_story(self, ticket)` has no fields/timeout parameters at [jira_adapter.py:204](/home/user/work/sora/OmniSight-sora/backend/jira_adapter.py:204), `_api` has no timeout parameter at [jira_adapter.py:172](/home/user/work/sora/OmniSight-sora/backend/jira_adapter.py:172), `HttpCall` accepts only `(method, url, headers, body)` at [intent_source.py:392](/home/user/work/sora/OmniSight-sora/backend/intent_source.py:392), and `curl_json_call` hardcodes `--max-time 30` plus `wait_for(..., timeout=35)` at [intent_source.py:416](/home/user/work/sora/OmniSight-sora/backend/intent_source.py:416).
   Fix: R2b must explicitly add the seam: protocol/API signature, adapter `_api` params, curl timeout args, and tests pinning query `fields=`.

4. **MEDIUM — Structural halves are currently sequential; `gather` is compatible but not implemented**
   Claim: JIRA and Cognee halves run concurrently inside the structural axis.
   Evidence: current structural axis awaits JIRA then Cognee sequentially at [project_state_aggregator.py:246](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_aggregator.py:246). The outer per-axis budget uses `asyncio.wait_for(fetcher(...), timeout=deadline_sec)` at [project_state_aggregator.py:573](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_aggregator.py:573), so inner `asyncio.gather` is compatible with the existing budget machinery.
   Fix: change `fetch_structural_axis` to gather both halves under the same 0.8s axis budget, with per-half degrade markers.

5. **HIGH — R1.2 exec-into-live-backend rebuild is operationally risky**
   Claim: run rebuild per replica by execing into `backend-a` then `backend-b`.
   Evidence: backend replicas are live uvicorn services: backend-b command execs `python -m uvicorn ... --workers` at [docker-compose.prod.yml:322](/home/user/work/sora/OmniSight-sora/docker-compose.prod.yml:322); both replicas have 4g limits at [docker-compose.prod.yml:287](/home/user/work/sora/OmniSight-sora/docker-compose.prod.yml:287) and [docker-compose.prod.yml:381](/home/user/work/sora/OmniSight-sora/docker-compose.prod.yml:381). Current rebuild unit is host-python, not docker exec, at [cognee-nightly-rebuild.service:16](/home/user/work/sora/OmniSight-sora/deploy/systemd/cognee-nightly-rebuild.service:16).
   Fix: use a one-shot `docker compose run --rm` rebuild container with replica-specific cognee volume/env, or drain one replica before exec and cap CPU/I/O. Do not run a long ingest inside serving containers without a resource/drain plan.

6. **MEDIUM — R1.3 must widen to the actually ingested kinds, not all constants**
   Claim: widen aggregator search from `kinds=(JIRA,)` to what R1.2 ingests.
   Evidence: current search is JIRA-only at [project_state_aggregator.py:352](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_aggregator.py:352). Full rebuild ingests code and lessons by default, with JIRA/Gerrit empty unless supplied, at [cognee_integration.py:859](/home/user/work/sora/OmniSight-sora/backend/agents/cognee_integration.py:859). Constants include extra kinds (`jira`, `gerrit`, `antipattern`) at [cognee_integration.py:85](/home/user/work/sora/OmniSight-sora/backend/agents/cognee_integration.py:85).
   Fix: specify exact R scope: `kinds=(SOURCE_KIND_CODE, SOURCE_KIND_LESSON)`. Keep JIRA/Gerrit/antipattern out unless their ingest is activated.

7. **MEDIUM — R4 overlay writer exists, but prod write is conditional**
   Claim: `deploy-prod.sh` writes the overlay lock on prod.
   Evidence: writer exists and writes atomically at [write_deploy_overlay_lock.py:184](/home/user/work/sora/OmniSight-sora/scripts/write_deploy_overlay_lock.py:184). `deploy-prod.sh` calls it only when `--bundle`/`OMNISIGHT_CANDIDATE_BUNDLE` is present; otherwise it warns and leaves the overlay unchanged at [deploy-prod.sh:280](/home/user/work/sora/OmniSight-sora/scripts/deploy-prod.sh:280). No conflict with `OMNISIGHT_RUNNING_IMAGE_DIGEST_BACKEND`: compose exports it separately at [docker-compose.prod.yml:210](/home/user/work/sora/OmniSight-sora/docker-compose.prod.yml:210), and deploy script sets it before writing overlay at [deploy-prod.sh:302](/home/user/work/sora/OmniSight-sora/scripts/deploy-prod.sh:302).
   Fix: make bundle required for R4 production deploys, or define a non-bundle fallback that can still populate all six lock fields.

8. **MEDIUM — R2a zero-extra-cost is true, but renderer work is larger than “one-line enrichment”**
   Claim: appending `issuelinks,parent` to the runner pickup GET is zero extra round-trip.
   Evidence: `_build_prompt` already performs one GET with `fields=summary,labels,components,issuetype` at [auto-runner-jira.py:1538](/home/user/work/sora/OmniSight-sora/auto-runner-jira.py:1538). Appending fields is same request. But `_render_project_state_block` currently dumps raw JSON and tells the model to treat null axes as no context at [auto-runner-jira.py:1131](/home/user/work/sora/OmniSight-sora/auto-runner-jira.py:1131), so R5 temporal omission and compact blockers require renderer logic, not just data fetch.
   Fix: ticket R2a/R5 renderer changes together with pinned prompt-output tests.

9. **MEDIUM — R2b negative cache remains under-specified**
   Claim: negative-cache 404s outside the LRU.
   Evidence: existing cache is one positive payload LRU keyed by `(ticket, develop_sha)`, max 256 entries at [project_state_cache.py:45](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:45), with `get/set` only for full project-state payloads at [project_state_cache.py:97](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:97). There is no separate negative-cache surface. Payload validation only checks top-level required keys at [project_state_cache.py:181](/home/user/work/sora/OmniSight-sora/backend/agents/project_state_cache.py:181), so per-half source markers inside `structural` will pass validation.
   Fix: define a separate bounded negative-cache object for JIRA 404/key-abuse, with TTL, key normalization, metrics, and per-replica behavior.

**Verdict: GO-WITH-CHANGES.** The v2 direction mostly holds, but R3 ID shape/fallback, R2b transport seams, R1 rebuild execution, and R4 bundle gating need concrete edits before ticket cut.
