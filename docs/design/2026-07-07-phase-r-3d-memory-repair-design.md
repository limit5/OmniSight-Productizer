# Phase R design — 3D memory repair (revive the hollow axes) — **v2**

**Status: v2.1 — GO (two audit rounds complete; see Revision history). Ticket cut in progress.**

## Revision history
- **v1** (2026-07-07): initial draft. Audited by 3 independent parties (codex CLI: **NO-GO**,
  11 findings; ops+security agent: GO-WITH-CHANGES, 15 findings; correctness agent:
  GO-WITH-CHANGES, 16 findings). Round-1 reports: `docs/design/audits/2026-07-07-phase-r-round1-*.md`.
- **v2.1** (2026-07-07): codex round-2 verdict **GO-WITH-CHANGES** (9 findings, no blockers;
  report: `audits/2026-07-07-phase-r-round2-codex.md`) — all 9 folded in below: live-id
  domain separation (`live-v1-` prefix), pre-claim fallback = uuid4 (hour-bucket withdrawn —
  it collapses distinct failures), R2b transport-seam plumbing made explicit, rebuild via
  one-shot `compose run --rm` (not exec-into-serving-container), kinds pinned to
  CODE+LESSON, R4 `--bundle` made required for prod deploys, R2a renderer scoped as a real
  sub-task, negative-cache concretely specified. **Design is GO for ticket cut.**
- **v2** (2026-07-07): every BLOCKER/HIGH resolved by redesign, all MED/LOW folded in or
  explicitly deferred. Major reshapes: **R3 rewritten on the corrected root cause** (a live
  DB writer NEVER existed — 100% of the 1,935 rows are 64-hex sha256 backfill ids; the
  in-memory `_persist_runner_incident` docstring promise "when 0206 lands" was never
  fulfilled); **R2 descoped 90%** (runner-side one-line fields enrichment replaces the
  cache+webhook+stale machine); **R1 scope corrected** (cognee's graph provider in prod is
  embedded `ladybug`, NOT Neo4j; no JIRA ingest exists; the nightly rebuild timer is disabled
  and crash-looping on a stale v0.5.0 image); **R4 gains a Deploy-AC** (the overlay lock is
  absent on prod — itself an AUDIT-23 seam). Immediate ops action completed 2026-07-07: the
  volatile `/tmp/runner-*.log` transcripts (only copy of the 6/9→now causal history, 377 MB)
  archived to `~/work/sora/logs/runner-archive/2026-07-07-tmp-snapshot/`.

Parent project: 3D memory revival (kickoff 2026-07-07). Scope: **repair only**. Non-goals →
"Deferred register" at the bottom (nothing drops silently).

## The verified failure map being repaired

Five independent shipped-but-never-wired seams in one subsystem (the "deployed-but-hollow"
compound): the `if False` JIRA stub; the buffer-not-Postgres incident writer; the
never-assigned graphiti dispatcher; the disabled+broken cognee rebuild timer; the
never-written prod deploy-overlay lock. Every one degrade-silent; zero alarms in ~1 year.

---

## R0 — Verification baseline (build FIRST)

- **Vantage**: measure from the RUNNER's position, not just raw API calls — authenticated
  fetch (`OMNISIGHT_RUNNER_API_TOKEN`) + the rendered prompt block, so the auth/flag/label
  chain is inside the measurement (audit: the env chain is part of what can hollow out).
- **Trace fields now, not in S** (codex#11): extend the existing in-memory metrics traces
  (`/project-state/metrics`) with per-axis `content: non_empty|empty|degraded|stale|unavailable`
  and per-half source markers (see Cross-cutting). R0 reads these; Phase S exports them.
- **Baseline snapshot**: 10 calls over a fixed ticket set; record per-axis non-empty rate
  (today: 0% / 0% / 0%).
- **Exit criteria** (staging → prod):
  - R1: `kg_neighbours` non-empty for a ticket with known lesson/code neighbours; zero
    `cognee_degrade` **and** `cognee_adapter_unavailable` **and** `cognee_inner_timeout`
    **and** `axis_timeout axis=structural` over 20 calls (all four tokens — audit F15).
  - R2a: a ticket with real issuelinks renders blockers/parent lines in the pickup prompt
    (runner-side); R2b: API structural carries `jira_source: live` + non-empty
    blockers for the same ticket, p95 within budget over 20 calls.
  - R3: a forced sandbox-ticket failure lands a `runner_incidents` row with a
    **`live-v1-`-prefixed id** (domain-separated from the bare-64-hex backfill fingerprint)
    within 60 s; causal returns ≥1 neighbour for a ticket sharing failure_class+area.
  - R4: `develop_sha` == the release's **40-hex git SHA** (audit F7: v1 said 64-hex — wrong,
    that's an image digest).
  - R5: API temporal = `{"status":"unavailable"}`; runner prompt renders **no temporal noise**
    (omitted / one-liner), and the pinned-shape tests are updated deliberately.

## R1 — structural-Cognee: writable store + revive the ONE ingest pipeline that ever existed

**Corrected root-cause set (audit-verified live)**:
1. Embedded cognee 1.0.9 SDK writes sqlite under
   `site-packages/cognee/.cognee_system` — read-only in the prod container (verified:
   `writable: False`). → the crash we see.
2. **Graph provider resolves to embedded `ladybug`, not Neo4j** — our
   `OMNISIGHT_COGNEE_NEO4J_*` env is consumed only by our own password-check/healthcheck;
   nothing maps it into cognee's `GRAPH_DATABASE_*`. Neo4j is also DNS-unreachable from the
   backend's networks. v1's "Neo4j config present and correct" is **withdrawn**.
3. cognee cognify/search needs LLM/embedding env (`LLM_API_KEY`/`EMBEDDING_*`) — absent.
4. **No JIRA-kind ingest exists anywhere** (`collect_jira_sources`: zero callers). The only
   corpus pipeline (`cognee_full_rebuild.py`, code+lesson collectors) is wired to a
   **disabled timer crash-looping since May 23** (`ModuleNotFoundError: backend` — the unit's
   `docker run --entrypoint sh` drops PYTHONPATH — against a stale pinned `backend:v0.5.0`).

**Design (repair = revive what existed; build-new goes to U):**
- **R1.0 spike (½ day, was 30 min)**: end-to-end `add → cognify → search` inside the
  prod-shaped container with: writable `system_root`/`data_root` on a volume; embedded
  `ladybug` graph (STAY on it for R — Neo4j wiring is new construction → deferred U);
  embedding/LLM env pinned to the **local ollama endpoint** (metered-zero; if 1.0.9's search
  path demands a chat-LLM at query time, pin a non-LLM `search_type` — if none exists,
  R1 exit is re-scoped and that finding goes straight into the U-track Cognee decision).
- **R1.1 per-replica volumes** `cognee-data-a` / `cognee-data-b` (audit F6-ops: one shared
  volume = two containers writing one sqlite = `database is locked`). Compose edit in the
  **prod checkout**, verified by `docker compose config` diff (only volumes/env may change,
  image ref untouched — digest pin verified), sequenced recreate a → readyz → b.
- **R1.2 revive the rebuild pipeline**: fix the unit's PYTHONPATH (`-e PYTHONPATH=/app` or
  `python3 -m` form), re-pin to the **current release digest** (or resolve from prod .env at
  run time), and run it as a **one-shot `docker compose run --rm` rebuild container**
  mounting each replica's volume in turn (`cognee-data-a`, then `cognee-data-b`) with CPU/
  memory caps — **never exec a long ingest inside the serving uvicorn containers** (codex
  r2 #5: live replicas carry 4g limits and serve traffic). Double rebuild cost accepted in
  exchange for zero cross-container sqlite hazards; enable the timer. Ingested kinds = what
  its collectors already produce (**code + lessons**).
- **R1.3 widen the aggregator search** from `kinds=(JIRA,)` to **exactly
  `kinds=(SOURCE_KIND_CODE, SOURCE_KIND_LESSON)`** (codex r2 #6 — the kinds R1.2 actually
  ingests; gerrit/antipattern/jira constants exist but have no ingest), so `kg_neighbours`
  can be non-empty with the revived pipeline. JIRA-kind ingest = **new construction → U**
  (register entry).
- Failure surfacing: the cognee half emits `kg_source: live|degraded|disabled` (Cross-cutting
  marker) — a broken store must never again render as "no neighbours exist".

## R2 — structural-JIRA: the one-line enrichment first, the API pull second, the machine not at all

**Root cause (re-verified)**: `make_client(...) if False else None; return {}` since OP-904.

**The round-1 pivot (ops F15b)**: the runner ALREADY fetches the issue at every pickup
(`auto-runner-jira.py:1138` — `fields=summary,labels,components,issuetype`). Appending
`issuelinks,parent` yields blockers/blocking/parent_meta at **zero extra round-trips**,
fresher than any cache, under the runner's relaxed budget. The v1 cache+webhook+
stale-while-revalidate machine — which two auditors independently proved unimplementable
against the real cache API (expired entries are deleted at read; whole-payload-only set) and
riddled with races (invalidate-vs-inflight-write; null-latch TTL poisoning; per-replica
divergence) — is **withdrawn**. Deferred-register entry: rebuild it only when a second
consumer (Sora / dashboard) actually needs cached structural JIRA.

- **R2a (runner tree — the workhorse)**: add `issuelinks,parent` to the existing pickup GET
  (zero extra round-trips — codex r2 #8 confirms same single request); **the renderer is a
  real sub-task, not a one-liner** (codex r2 #8): a compact `Blockers / Blocking / Parent`
  block (keys + status + ≤200-char summaries) + the R5 temporal-omit logic share
  `_render_project_state_block`, so R2a and R5b ship as ONE ticket family with pinned
  prompt-output tests. Unit-test the issuelink DIRECTION mapping explicitly (inward=blocker
  — the known recurring trap). Cross-repo ticket with its own deploy channel (tree-pull +
  systemd restart — NOT the backend release train).
- **R2b (backend — best-effort, honest)**: `_structural_jira` becomes a real single
  `fetch_story` call with:
  - explicit `fields=issuelinks,parent,labels,status,summary` (pin the response shape +
    fixtures — codex#6);
  - **its own transport timeout** (~1.5 s) — **explicit new plumbing, not a param flip**
    (codex r2 #3): today `fetch_story()` takes no fields/timeout, `_api` takes no timeout,
    `HttpCall` is `(method, url, headers, body)`, and `curl_json_call` hardcodes
    `--max-time 30` (`jira_adapter.py:204/172`, `intent_source.py:392/416`). R2b threads
    `fields=` + `timeout=` through all four seams, passes `--max-time` per call, kills the
    subprocess on cancel, and pins the query string in tests;
  - run **concurrently** with the cognee half (`asyncio.gather` — v1 ran them sequentially
    inside one 800 ms budget; audit F5);
  - on timeout/failure → `jira_source: degraded` marker, NOT fake-empty (audit F10);
  - **kill-switch** `OMNISIGHT_PROJECT_STATE_JIRA_PULL` (default on, AgentFeatureFlag
    pattern, ~10 lines) — the existing switches are wrong-grained (global flag kills the good
    axes too; per-ticket label is useless mid-incident);
  - sanitization (prompt-injection surface): field allowlist (keys/status/summary only — no
    description bodies), 200-char caps, control-char strip;
  - **negative-cache 404s outside the LRU + ticket-key length cap** (audit F9: 256 garbage
    keys currently evict every real entry and burn the shared JIRA credential's rate limit).
    Concrete spec (codex r2 #9 — don't under-specify like v1's SWR): a **separate bounded
    dict** in the aggregator module (size 512, TTL 10 min, key = normalized ticket, evict
    oldest, per-replica by nature), consulted BEFORE any JIRA call, with a hit-counter
    exposed in the metrics traces. Never an entry in the main payload LRU.
- Siblings (needs a parent JQL call) — **dropped from R** (register: U).
- Phase heuristic: v1 claimed it was "already sketched" — false (both audits); R2b passes
  through `status` and leaves `phase` null with a marker; a real heuristic is U-scope.

## R3 — causal feedstock: build the durable writer (it never existed)

**Corrected root cause (conclusive)**: all 1,935 rows carry 64-hex sha256 `incident_id`s =
`runner_log_parser` content hashes → **written exclusively by manual backfill runs**, last
~2026-06-09. `record_runner_incident` has only ever appended to an in-memory
`deque(maxlen=1024)`; the `record_runner_incident_failed` warning guards an append that
cannot fail — v1's "zero warnings" evidence was structurally meaningless (and the runner's
stdout never reaches the user journal anyway: units append to files; journal's first boot
entry is 2026-06-15). v1's R3.0 diagnosis plan is **spent** — these findings are its output.

**Design:**
- **R3.1 one durable write helper**, replacing BOTH existing sinks (`jira_dispatch:2715`'s
  buffer call and `memory_writeback._do_incident` — audit F16: two seams double-write the
  moment persistence is real):
  - PG INSERT into the existing `runner_incidents` table;
  - **idempotency WITHOUT migration**:
    `incident_id = "live-v1-" + sha256(ticket|terminal|claim_token)` + the existing
    `ON CONFLICT (incident_id) DO NOTHING` pattern (resolves codex r1 BLOCKER#2; the
    `live-v1-` prefix domain-separates live rows from bare-64-hex backfill rows — codex r2
    #1 — `incident_id` is TEXT, so no migration). Key uses the **OP-977 claim fencing
    token** (unique per pickup, stable across in-claim retries — audit F8);
    **pre-claim terminals use a plain `uuid4` id with NO dedup** (codex r2 #2: any
    time-bucket fallback collapses two legitimate same-ticket same-terminal failures —
    distinct pre-claim incidents must all land);
  - DSN via the wrapper's existing `audit-db.env` seam (already sourced by
    run-ephemeral.sh);
  - **visible failure**: a failed INSERT logs a structured error line to the runner log
    AND increments a local counter file — silence is detected by R3.3, not by grepping.
- **R3.2 terminal-matrix broadening**: every terminal failure path passes `failure_class`
  through the helper — today only ~1 of ~15 `transition_back_to_todo` sites does (~7
  writeback sites do). Explicitly includes: CLI-failure revert, zero-commit revert,
  stoploss/circuit-trip, `_abandon_gerrit_change`, ops-only refusal, grader refusal.
- **R3.3 write-rate canary — rehomed** (audit F11): a **dedicated oneshot timer** following
  the existing `cognee-drift-detect.timer` pattern (prod tree + prod DSN), NOT inside
  `omnisight-slo-monitor.service` (that is the D9/D10 auto-rollback safety actuator — no
  unrelated duties in a safety path). Escalation on `incidents_written_7d == 0` while the
  fleet was active = **file/update a JIRA ticket via jira_dispatch** — not a WARNING line
  nobody reads (v1's proposal reproduced the exact disease under repair).
- **R3.4 retry queue: DELETE** (audit F12): `_enqueue_incident_for_retry` writes to a queue
  directory whose "hourly operator cron" consumer was never built — once the writer can
  actually fail, it becomes a NEW hollow seam. The write is best-effort + R3.3 catches
  silence; the queue code is removed (revisit in U if evidence demands durability).
- **R3.5 backfill the 6/9→now gap** (logs are archived — see Revision history): AFTER the
  live writer lands; fix the parser's mtime-fallback timestamp skew for ever-appended files
  first (audit F13); dedup is already content-hash + ON CONFLICT.

## R4 — develop_sha: overlay first — and make prod actually WRITE the overlay

**Corrected fact (audit F6)**: prod today has NO overlay lock (`build_git_sha=None`,
`/etc/omnisight/` empty) — the v1 resolve chain lands on "unknown" exactly as before. The
overlay writer is itself shipped-but-not-activated on prod (staging has it).

**Design**: resolve order (1) `get_deploy_overlay().build_git_sha`, (2) env
**`OMNISIGHT_BUILD_GIT_SHA`** (the EXISTING lock-field name — v1 invented a new
`OMNISIGHT_RELEASE_SHA`; withdrawn), (3) dev-checkout `git rev-parse` fallback, (4)
"unknown". **Deploy-AC**: `deploy-prod.sh` writes the overlay lock on prod — the writer
exists (`write_deploy_overlay_lock.py`) but today runs **only when `--bundle` is supplied**
(deploy-prod.sh:280 warns-and-skips otherwise — which is exactly why prod has no lock; the
v0.7.33–37 deploys passed digests but never `--bundle`). R4 makes `--bundle` **required for
prod deploys** (the sealed bundle exists at promote time; release-train runbook updated) —
closing that AUDIT-23 seam is part of R4's definition of done, not a side note.
Also: removes the per-request `git rev-parse` fork (a real latency win on every call),
removes the dead `invalidate_for_develop_merge` export (zero callers), removes the unused
`subprocess` import. ADR/doc note: the payload key `develop_sha` now carries the release
build SHA — documented, not silently repurposed.

## R5 — temporal: honest at the API, invisible in the prompt

**Root cause re-verified** (dispatcher assigned nowhere; docstring's "runner injects it" is
topologically impossible).

**Design** (audit F12-ops + Q4 consensus): the API returns
`{"status": "unavailable"}` (reason string in code comment/runbook — NOT in the payload:
a permanent "planned: U5 Graphiti" roadmap string in every pickup prompt is noise that
invites off-task reasoning). The **runner renderer maps `status=="unavailable"` → omit the
key / one-liner** — cross-repo change, same R2a ticket family + prompt-instruction text
update (the current preamble promises "null axes"). Update the three pinned tests
(`test_project_state.py` shape + timeout pins) deliberately. Remove the dispatcher probe,
the misleading comment, and the doc mentions of runner-injected Graphiti.

## Cross-cutting

- **Per-half source markers everywhere** (audit F10 — the anti-hollow rule applied to
  ourselves): structural carries `jira_source` + `kg_source` ∈
  `live|degraded|disabled|unavailable`; traces expose them; Phase S turns them into the
  non-empty/degraded-rate SLO.
- **ADR-0015 update ticket**: temporal honesty; `develop_sha` semantics; the REAL
  invalidation story (TTL + per-replica divergence documented; the webhook/SWR machine
  deferred with rationale); the R2 descope decision record (API-side vs runner-side fetch).
- **FE**: `CrossTaskAwarenessPanel` consumes only `/metrics` traces — additive fields only.
- **Tests**: direction-trap; fields-pinning fixtures; degrade-marker shapes; unavailable
  shape; writer idempotency (same claim token twice → one row); kill-switch behavior.
- **Rollout**: backend items via release-train; **runner-tree items (R2a, R5-renderer,
  R3.1-3.2 helper call sites) via tree-pull + sequenced systemd restart — a separate,
  explicitly-ticketed deploy channel** (audit F11-correctness: v1's rollout plan ignored
  the only real consumer's deploy path).
- **Ticket cut (post round-2 audit)**: R1.0 spike → R1.1/R1.2/R1.3; R2a (runner) + R2b
  (backend); R3.1→R3.2 (+R3.3 timer, R3.4 delete, R3.5 backfill); R4 (+Deploy-AC); R5 (API)
  + R5b (renderer); ADR update. Each 4-AC + Go-Live; multi-area labels.

## Risks (v2)

| Risk | Mitigation |
|---|---|
| R2b JIRA tail latency | own 1.5 s transport timeout + concurrent halves + degrade marker; runner never depends on it (R2a is the workhorse) |
| Shared JIRA credential rate budget (R2b per-miss + R2a is free) | R2a adds zero calls; R2b guarded by kill-switch + negative-cache + key-length cap |
| cognee volume = writable state in a read-only container, contents prompt-reachable | per-replica named volumes, size cap, non-executable data; volume integrity noted as prompt-integrity input; S monitors size |
| Double rebuild cost (per-replica exec) | acceptable (nightly, local); revisit shared store in U |
| R3 writer failure silence | structured error + counter + R3.3 timer escalating to a JIRA ticket |
| Backfill timestamp skew | parser fix before R3.5; archived logs immutable |
| Prod compose edit rolls the image | digest-pinned REF verified; `compose config` diff gate; sequenced recreate (procedure in R1.1/R4 DoD) |
| Pinned tests break (R5/R2b shapes) | tests updated in the same change, listed per item |

## Resolved v1 open questions
1. Kill-switch: **yes** — `OMNISIGHT_PROJECT_STATE_JIRA_PULL` (both auditors, same reason).
2. Stale-vs-null: **moot** — SWR withdrawn; R2b uses live-or-degraded-marker. Sanitization
   (allowlist/caps/strip) adopted unconditionally.
3. Idempotency: **claim-token key** `(ticket, terminal, claim_token)` via deterministic
   `incident_id` — no migration.
4. U5 reference in prompt: **no** — API-side honesty only; prompt stays operational.

## Deferred register (nothing drops silently)
- SWR + webhook invalidation + warm machine → S/U, gated on a second structural-JIRA consumer.
- JIRA-kind Cognee ingest; Neo4j-as-cognee-graph (network + provider wiring); siblings JQL;
  phase heuristic → U.
- Graphiti temporal (U5). Retry-queue durability (only if R3.3 evidence demands).
- Incident-writer POST-to-backend variant (if direct-PG DSN proves brittle) → revisit in S.
