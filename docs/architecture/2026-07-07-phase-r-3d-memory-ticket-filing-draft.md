# Phase R (3D memory repair) — ticket filing draft (Stage 4/5) — v3 FINAL

Spec-ref: design v2.1 `docs/design/2026-07-07-phase-r-3d-memory-repair-design.md` (GO).
**v3** folds in the Stage-5 blind-test results (3 agents: T4b high-risk, T5 high-risk, T3
control — ZERO drift, all MUST-NOTs held; ~20 ticket-text pins adopted, incl. two real
defects: the 1.5 s-transport-vs-800 ms-axis fence bug, and the prod-DSN test-isolation
hazard). No structural changes vs v2 (no re-split, same edges) → no blind-test re-run
needed per SOP. Each ticket body below is SELF-CONTAINED (embeds its pinned contracts) —
it is exactly what the runner sees.

**Chains (unchanged from v2):** T1→T2→T3→T4b→T7 (chain A); T4a→T4b; T8→T6; T5→{T6,T9};
{T4b,T5,T7}→T10. **Parallel starts: T1, T4a, T5, T8.**
Later: T11/T12 (after Sora's R1.0 spike), T13 (after T5, operator).

## Shared contract reference (embedded per-ticket below; ownership map)

- **Marker keys** (T1 creates keys+plumbing; T3 owns cognee-half VALUES; T4b owns jira-half
  VALUES): payload `structural.jira_source` / `structural.kg_source`; traces
  `axis_content` / `source_markers` / `jira_negative_cache_hits` (cumulative int).
- **Marker value semantics** (all four values, both halves): `live` = source answered (even
  with empty content); `degraded` = attempted but failed/timed out (incl. JIRA 404);
  `disabled` = kill-switch off; `unavailable` = machinery absent (module missing / not
  implemented / not wired).
- **axis_content classification** (T1 pins): axis payload None → `degraded`; dict carrying
  `status:"unavailable"` → `unavailable`; dict with no content → `empty`; else `non_empty`.
- **T4a seam signature**: `fetch_story(ticket, *, fields: str | None = None,
  timeout_s: float | None = None)` — comma-string fields; absent params = today's exact
  behavior; threads through `_api` → `HttpCall` → `curl_json_call` per-call `--max-time`.
- **Link-entry shape**: `blockers`/`blocking` = lists of `{key, status, summary}` dicts
  (summary sanitized ≤200 chars); `parent_meta` = one such dict or None.
- **Writer helper**: `record_incident_durable(ticket_key, failure_class, *,
  claim_token=None, summary="", raw_traceback="", runner_class="", mutex_label="",
  area="") -> RunnerIncidentRecord`; `failure_class: FailureClass | str` (coerced; hash
  uses the coerced `.value`); token → `incident_id = "live-v1-" +
  sha256(f"{ticket_key}|{failure_class.value}|{claim_token}")`; None → `uuid4().hex`, no
  dedup.
- **Temporal unavailable shape**: exactly `{"status": "unavailable"}`.

---

## T1 [FEAT][backend] project-state traces: per-axis content + per-half source markers (R0)

type:feature tier:S class:subscription-codex areas:backend,tests priority:High
**PINNED CONTRACT (this ticket DEFINES these — siblings reuse them verbatim):**
payload keys `structural.jira_source` + `structural.kg_source`; trace keys
`axis_content` (dict axis→`non_empty|empty|degraded|unavailable`), `source_markers`
(the two structural halves), `jira_negative_cache_hits` (cumulative int, init 0).
Value semantics: `live`=answered (even empty); `degraded`=attempted-but-failed/timeout;
`disabled`=switched off; `unavailable`=machinery absent. axis_content classification:
axis None→`degraded`; dict w/ `status:"unavailable"`→`unavailable`; no content→`empty`;
else `non_empty`.
**Goal**: make hollowness VISIBLE. Add the pinned keys: extend `fetch_structural_axis`'s
fixed dict + both halves' return plumbing so TODAY's honest values appear
(`jira_source: "unavailable"` — the stub isn't implemented; `kg_source: "degraded"` — the
store crashes); add the three trace fields to `ProjectStateTrace` (default_factory /
default 0 so the router needs NO change) + the `/project-state/metrics` serializer;
classify `axis_content` in `aggregate_project_state`. NO change to any fetch logic,
budget, or cache behavior — markers reflect current reality only.
**Files**: `backend/agents/project_state_aggregator.py`, `backend/api/project_state.py`,
`backend/tests/test_project_state.py`.
**4-AC**: Code = markers + traces + serializer + tests incl. degraded paths. Deploy = next
release-train cut. Integration = staging `/metrics` shows the fields. Exercised = 10-call
baseline snapshot recorded on staging + prod (operator step; expected: jira_source
unavailable, kg_source degraded, temporal fake-empty→will change under T2 — that IS the
baseline). Go-Live: next vX.
**MUST NOT**: do not change any axis's fetch logic, budgets, or cache; do not rename/add
keys beyond the pinned set; do not touch the FE panel; do not fix `_structural_jira`'s
`if False` stub (T4b's ticket) or the cognee kinds (T3's) even though they sit in the same
file.

## T2 [FIX][backend] temporal axis: honest unavailable, remove the phantom dispatcher seam (R5 API)

type:feature tier:S class:subscription-codex areas:backend,tests priority:High
blockedBy: T1 (chain A)
**PINNED CONTRACT**: temporal payload = exactly `{"status": "unavailable"}` (no extra
keys, no reason string); trace `axis_content.temporal = "unavailable"` (keys defined by
the T1 ticket in this chain — reuse, never invent).
**Goal**: `_temporal_graphiti` + `fetch_temporal_axis` return the pinned shape instead of
shaping `{}` into fake-empty `prior_similar_tickets`/`avg_completion_seconds`/
`recent_events`; delete the `_DEFAULT_DISPATCHER` getattr-probe + the "runner injects the
dispatcher" comment; log once at startup. Update the three pinned shape/timeout tests
deliberately.
**Files**: `backend/agents/project_state_aggregator.py`,
`backend/tests/test_project_state.py`.
**4-AC**: Code = shape + seam removal + tests. Deploy = next cut. Integration = staging API
returns the pinned object; traces show it. Exercised = 20 staging calls, zero fake-empty
temporal shapes. Go-Live: next vX.
**MUST NOT**: do not modify `graphiti_mcp_client.py` beyond what probe removal requires
(module stays for U5); do not touch the runner renderer (sibling ticket); do not add any
Graphiti wiring; no reason/roadmap strings in the payload; do not touch `_structural_jira`
or cognee kinds (siblings').

## T3 [FIX][backend] cognee search: widen kinds to exactly (CODE, LESSON) + honest kg_source values (R1.3)

type:feature tier:S class:subscription-codex areas:backend,tests priority:High
blockedBy: T2 (chain A)
**PINNED CONTRACT** (keys defined by the T1 ticket — reuse): `structural.kg_source` ∈
`live|degraded|disabled|unavailable`. Exception→value map THIS ticket implements for the
cognee half: `CogneeNotInstalled` / `Neo4jPasswordDefault` (unconfigured) → `disabled`;
inner timeout / store-or-query errors (`CogneeNeo4jUnavailable`, `CogneeQueryTimeout`,
`CogneeIndexCorruption`, generic search exceptions) → `degraded`; `cognee_integration`
module import failure → `unavailable`; successful search → `live` (even with 0 hits —
honesty cuts both ways).
**Goal**: (1) `_blocking_cognee_lookup` searches the ORDER-EXACT tuple
`kinds=(SOURCE_KIND_CODE, SOURCE_KIND_LESSON)` instead of `(SOURCE_KIND_JIRA,)` (these are
the only kinds the rebuild pipeline ingests; jira/gerrit/antipattern have no ingest);
(2) every exit path emits the mapped `kg_source` value per the table above (today's bare
`{}` degrades become marked returns).
**Files**: `backend/agents/project_state_aggregator.py`,
`backend/tests/test_project_state.py`.
**4-AC**: Code = kinds + honest values + tests (incl. one per mapping row). Deploy = next
cut. Integration/Exercised = 20 staging calls: NO crash, and `kg_source` HONESTLY reports
whatever the environment truly is — `live`-with-empty, `degraded`, or `disabled` ALL pass;
only a crash or a fake-empty (missing marker) fails. Go-Live: next vX. (`kg_neighbours`
non-empty belongs to the later R1 activation tickets.)
**MUST NOT**: do not add JIRA/gerrit/antipattern kinds; do not touch the cognee adapter
(`cognee_integration.py`), volumes, or any compose/systemd file; do not touch
`_structural_jira` (sibling's) even though it is in the same file.

## T4a [FEAT][backend] JIRA adapter transport seams: per-call fields= + timeout_s= (R2b part 1)

type:feature tier:S class:subscription-claude areas:backend,tests priority:High
**PINNED CONTRACT (this ticket DEFINES the seam — a sibling consumes it):**
`fetch_story(ticket, *, fields: str | None = None, timeout_s: float | None = None)`;
`fields` is a comma-string forwarded as the JIRA `fields=` query param; `timeout_s`
threads `fetch_story` → `_api` → `HttpCall` → `curl_json_call` as a per-call
`--max-time` (+ proportional outer `wait_for`), subprocess killed on cancel/timeout.
ABSENT params = today's exact behavior (no `fields=` in URL; 30/35 s timeouts).
**Goal**: implement the seam exactly as pinned. Tests pin: the emitted query string for
`fields="issuelinks,parent,status,summary"`; timeout propagation to the curl argv;
kill-on-cancel; default-path regression (no params → byte-identical request shape to
today).
**Files**: `backend/jira_adapter.py`, `backend/intent_source.py`,
`backend/tests/test_jira_adapter.py` (create if absent — name pinned).
**4-AC**: Code = seams + tests. Deploy = next cut. Integration = staging: existing JIRA
flows unchanged (default-path regression green). Exercised = the sibling structural-pull
ticket exercises the seam live on staging (recorded there); this ticket's exercised =
default-path no-regression on staging. Go-Live: next vX.
**MUST NOT**: do not change any default behavior; do not touch the aggregator, cache,
webhooks, or feature flags; do not add retries.

## T4b [FEAT][backend] structural-JIRA real pull: mapping + gathered halves + kill-switch + guards (R2b part 2)

type:feature tier:M class:subscription-claude areas:backend,security,tests priority:High
blockedBy: T3 (chain A), T4a (the seam)
**PINNED CONTRACTS** (defined by siblings — reuse verbatim, never invent adjacent names):
seam `fetch_story(ticket, *, fields=..., timeout_s=...)` (comma-string). Payload marker
`structural.jira_source` ∈ `live|degraded|disabled|unavailable` (THIS ticket sets the
jira-half values; the cognee half's `kg_source` belongs to siblings — do not modify it).
Trace `jira_negative_cache_hits` = CUMULATIVE process counter, snapshotted per trace.
Link entries: `blockers`/`blocking` = lists of `{key, status, summary}` dicts (summary
sanitized ≤200 chars); `parent_meta` = one such dict or None. Flag attr name:
`project_state_jira_pull`.
**Goal**: replace `_structural_jira`'s `if False` stub with ONE real call per cache-miss:
(1) `fetch_story(ticket, fields="issuelinks,parent,status,summary", timeout_s=1.5)` —
NOTE: `labels` deliberately NOT requested (nothing consumes it; phase stays null);
(2) **budget fencing (the important one)**: the JIRA half runs under an INNER
`asyncio.wait_for` of 0.6 s (axis budget 0.8 − 0.2 margin, mirroring the cognee half's
precedent) so a slow JIRA yields `jira_source: "degraded"` with a present-but-marked
payload — the whole axis must NEVER null out from a slow half; `timeout_s=1.5` on the
transport is the backstop that also kills the curl subprocess;
(3) both halves run CONCURRENTLY (`asyncio.gather`, never-raising wrappers);
(4) mapping: issuelinks of type **Blocks only**, `inwardIssue`→`blockers`,
`outwardIssue`→`blocking` (direction unit test REQUIRED); `parent`→`parent_meta`;
status → its sanitized `name` string; success → `jira_source: "live"`;
(5) kill-switch `OMNISIGHT_PROJECT_STATE_JIRA_PULL` (AgentFeatureFlag pattern, attr
`project_state_jira_pull`, default ON; OFF → `"disabled"`, zero adapter calls);
adapter import/construction failure → `"unavailable"`;
(6) sanitization: allowlisted fields only (key/status-name/summary per entry), summaries
control-char-stripped + ≤200 chars, `description` never requested and never read;
(7) negative-cache: separate module-level bounded OrderedDict (size 512, TTL 600 s,
normalized+length-capped ticket key, **404-only** — never 429/5xx), consulted before any
JIRA call; a 404 (fresh or cached) yields `jira_source: "degraded"` and increments the
pinned cumulative counter. Adapter acquisition via `build_default_jira_adapter()` behind
an injectable module seam.
Housekeeping note: the pre-existing red canonical-list assertion in
`test_agent_feature_flags.py` (missing `reflection_rag_prompt`) MAY be repaired in the
same change — note it in the change description.
**Files**: `backend/agents/project_state_aggregator.py`,
`backend/agents/agent_feature_flags.py`, `backend/tests/test_project_state.py`,
`backend/tests/test_agent_feature_flags.py`.
**4-AC**: Code = all seven + tests (direction; fields-string pin; ONE call per miss;
concurrency timing; inner-fence degrade w/ non-null axis; kill-switch both ways;
sanitization incl. description-non-leak; negative-cache hit/TTL/evict/counter). Deploy =
next cut. Integration = staging: linked ticket → non-empty `blockers` + `jira_source:
live`; p95 within budget over 20 calls; kill-switch flip degrades to `disabled`.
Exercised = prod R0 re-run shows structural non-empty for a linked ticket (recorded).
Go-Live: next vX.
**MUST NOT**: do not modify `project_state_cache.py`, webhooks, `jira_event_router.py`
(SWR/webhook machine WITHDRAWN), `backend/api/project_state.py`, `jira_adapter.py`, or
`intent_source.py` (sibling owns transport); do not add siblings/JQL calls or a phase
heuristic; do not touch the cognee half, its `kg_source` values, or the search kinds
(siblings'); no description bodies; do not touch the runner tree.

## T5 [FEAT][backend] durable incident writer: live-v1 ids + claim-token param + seam unify + queue removal (R3.1+R3.4)

type:feature tier:M class:subscription-claude areas:backend,db,tests priority:Highest
**PINNED CONTRACT (this ticket DEFINES the helper — a sibling threads real tokens):**
`record_incident_durable(ticket_key, failure_class, *, claim_token=None, summary="",
raw_traceback="", runner_class="", mutex_label="", area="") -> RunnerIncidentRecord`;
`failure_class: FailureClass | str` (coerce like the existing recorder; the hash uses the
COERCED `.value`); token present → `incident_id = "live-v1-" +
sha256(f"{ticket_key}|{failure_class.value}|{claim_token}")` (ONE test asserts this exact
input string); `claim_token=None` → `uuid4().hex` id, NO dedup. Empty-string kwargs
normalize to NULL at write (test-pinned). `created_at` → DB default. Engine: module-cached
per DSN + a `reset_for_tests()` hook.
**Goal**: build the Postgres writer that never existed.
(1) The pinned helper in `incident_recorder.py`: builds the record, still appends to the
in-memory deque (recall read-cache, dedup by incident_id), then INSERTs into the EXISTING
`runner_incidents` table via SQLAlchemy `text()` + `ON CONFLICT (incident_id) DO NOTHING`
(mirror the backfill script's statement shape). NO alembic migration.
(2) DSN: env `OMNISIGHT_DATABASE_URL` (literal, only this name); else parse the file at
`OMNISIGHT_AUDIT_DB_ENV_FILE` (default `~/.config/omnisight/audit-db.env`, KEY=VALUE
lines). Any failure (no DSN / connect / insert) → ONE structured line
`incident_recorder.durable_write_failed ...` + increment a single-int counter file at
`OMNISIGHT_INCIDENT_WRITE_FAIL_COUNTER` (default `~/.local/state/omnisight/
incident_write_failures.count`); the helper NEVER raises.
(3) Seam unify: `transition_back_to_todo` gains keyword-only `claim_token: str | None =
None` passthrough (signature only — claim/pickup LOGIC untouched) and its incident block
calls the helper; `memory_writeback`: `WritebackRequest` gains
`claim_token: str | None = None` (EXPLICITLY AUTHORIZED third signature change) and
`_do_incident` delegates to the helper passing it. Same token through both seams → 1 row
(test).
(4) Queue removal — complete inventory: `_enqueue_incident_for_retry`, its failed-store
call site (~:402), `DEFAULT_INCIDENT_QUEUE_DIR`, `INCIDENT_QUEUE_DIR_ENV`, the
constructor arg/attr (~:315), their tests, and now-orphaned imports.
(5) **TEST ISOLATION IS MANDATORY (safety)**: this dev host's real
`~/.config/omnisight/audit-db.env` contains the PROD DSN. Every test MUST use an autouse
fixture that delenvs `OMNISIGHT_DATABASE_URL`, points `OMNISIGHT_AUDIT_DB_ENV_FILE` and
the counter path into `tmp_path`, and resets the engine cache — unit tests must be
PHYSICALLY UNABLE to write prod. Idempotency tests run against a tmp sqlite DSN with the
table created from the 0206 DDL.
**Files**: `backend/agents/incident_recorder.py`, `backend/agents/memory_writeback.py`,
`backend/agents/jira_dispatch.py`, `backend/tests/test_incident_recorder.py`,
`backend/tests/test_memory_writeback.py`, `backend/tests/test_jira_dispatch.py`.
**4-AC**: Code = helper + both seams + queue removal + tests (exact-hash-input; token→1
row; None→2 rows; deque-on-DB-failure; no-DSN → no raise + log + counter; env-file
fallback; isolation fixture). Deploy = next cut AND runner tree-pull (runner imports
these modules — both channels named). Integration = staging: forced sandbox failure lands
a `live-v1-` row within 60 s. Exercised = prod: first real or operator-forced synthetic
failure produces a live row (watched + recorded). Go-Live: next vX + runner restart
window.
**MUST NOT**: no alembic migration; do not modify `scripts/backfill_runner_incidents.py`
or existing 64-hex rows; do not change claim/pickup/transition LOGIC (the two authorized
signature additions only); do not touch `auto-runner-jira.py` (sibling threads real
tokens); do not add new failure terminals; do not add retry/backoff (best-effort +
canary by design); do not make the C6 memory-recall audit path durable (out of scope —
note it in the change description instead).

## T6 [FIX][tooling] runner terminal-failure matrix: every terminal writes an incident (R3.2)

type:feature tier:M class:subscription-claude areas:tooling,backend,tests priority:High
blockedBy: T5 (helper + parameter), T8 (chain B)
**PINNED CONTRACT** (defined by the T5 ticket — call, never re-implement):
`record_incident_durable(...)` via `transition_back_to_todo(..., claim_token=...)` /
`WritebackRequest(claim_token=...)`. The pickup's OP-977 claim fencing token
(`claim:{instance}:{epoch_us}-{uuid}` — the runner mints it at claim time) is the value
to thread; pre-claim terminals pass `claim_token=None`.
**Goal**: every terminal failure path in `auto-runner-jira.py` passes `failure_class` AND
the claim token — explicitly: CLI-failure revert, zero-commit revert, stoploss/
circuit-trip terminals, `_abandon_gerrit_change`, ops-only transition refusal, grader
refusal (~15 `transition_back_to_todo` sites, only ~1 passes failure_class today). Add a
terminal→failure_class matrix comment at the section top. Use EXISTING failure_class
values only.
**Files**: `auto-runner-jira.py`, `backend/tests/` (2 representative terminal tests).
**4-AC**: Code = all terminals wired + tests. Deploy = runner tree-pull + sequenced
systemd restart (operator; NOT the release train). Integration = staging runner: forced
stoploss + forced abandon each land a row. Exercised = operator-gated synthetic-failure
drill on prod (one sandbox ticket forced through a stoploss terminal → `live-v1-` row
lands; recorded). Go-Live: next runner restart window.
**MUST NOT**: do not modify pickup/claim/JQL/label logic (thread the existing token VALUE
only); do not change what counts as a terminal; do not invent new failure_class values;
do not touch backend files except tests.

## T7 [FIX][backend] develop_sha from deploy overlay + make prod deploys write the lock (R4)

type:feature tier:S class:subscription-codex areas:backend,devops,docs,tests priority:High
blockedBy: T4b (chain A tail — shares `api/project_state.py` + `test_project_state.py`)
**Goal**: (1) `_resolve_develop_sha`: overlay `build_git_sha` → env
`OMNISIGHT_BUILD_GIT_SHA` → dev-checkout `git rev-parse` → `"unknown"`; the git fork
leaves the hot path; remove the dead `invalidate_for_develop_merge` export (+
`subprocess` import if then unused). (2) `scripts/deploy-prod.sh`: `--bundle` REQUIRED
for prod deploys (today it warns-and-skips the overlay write — the reason prod has no
lock); usage text + one runbook line updated. (3) Tests: absent overlay / incomplete
overlay / env fallback / 40-hex git-SHA assertion.
**Files**: `backend/api/project_state.py`, `scripts/deploy-prod.sh`,
`backend/tests/test_project_state.py`,
`docs/operations/2026-05-22-release-train-readiness-handoff.md` (one line).
**4-AC**: Code = resolver + gate + tests. Deploy = next release-train cut run WITH
`--bundle` (operator). Integration = staging payload carries the 40-hex build SHA.
Exercised = post-deploy prod payload `develop_sha` == release SHA (recorded). Go-Live:
next vX cut.
**MUST NOT**: do not modify `write_deploy_overlay_lock.py`; do not change staging deploy
paths; do not rename the payload key; do not touch the aggregator.

## T8 [FEAT][tooling] runner pickup enrichment: issuelinks/parent in the pickup GET + compact renderer + temporal omit (R2a+R5b)

type:feature tier:M class:subscription-claude areas:tooling,tests priority:High
**PINNED CONTRACT** (produced by a backend sibling — consume, never re-shape): temporal
axis may arrive as exactly `{"status": "unavailable"}` → omit the key from the rendered
block (or a single line "temporal: unavailable" max). Link rendering: keys + status +
≤200-char sanitized summaries.
**Goal**: (1) in `_build_prompt`'s issue GET — the ONE currently requesting
`fields=summary,labels,components,issuetype` (~line 1538) — append `issuelinks,parent`
to that fields list ONLY; (2) render a compact `Blockers / Blocking / Parent` block
(keys + status + sanitized ≤200-char summaries; issuelink DIRECTION correct:
inward=blocker — dedicated unit test); (3) `_render_project_state_block`: map the pinned
temporal-unavailable shape → omit; preamble text updated to "null or absent axes = no
context"; (4) pinned prompt-output tests. Whole added block ≤10 lines.
**Files**: `auto-runner-jira.py`, `backend/tests/test_auto_runner_prompt_builder.py`.
**4-AC**: Code = fields + renderer + tests. Deploy = runner tree-pull + sequenced restart
(operator). Integration = staging pickup prompt shows the Blockers block for a linked
ticket + zero temporal noise. Exercised = first prod pickup post-rollout renders it
(log-verified, recorded). Go-Live: next runner restart window.
**MUST NOT**: do not add any new JIRA request (append to the ONE named GET only); block
≤10 lines; no description bodies; do not change pickup/claim logic; do not touch backend
files except the named test.

## T9 [FEAT][devops] incident write-rate canary: dedicated oneshot timer + JIRA escalation (R3.3)

type:feature tier:M class:subscription-codex areas:devops,backend,tests priority:Medium
blockedBy: T5
**Goal**: `scripts/incident_write_canary.py` +
`deploy/systemd/incident-write-canary.{service,timer}` (daily oneshot, prod tree + prod
DSN — mirror the `cognee-drift-detect.timer` pattern): count `runner_incidents` rows
with `live-v1-`/uuid (non-64-hex) ids in the last 7 days; if zero WHILE `runner_metrics`
shows fleet activity in the window → file/update ONE JIRA ticket via `jira_dispatch`'s
public functions (idempotent — reuse an open canary ticket). No auto-remediation.
**Files**: `scripts/incident_write_canary.py`,
`deploy/systemd/incident-write-canary.service`,
`deploy/systemd/incident-write-canary.timer`,
`backend/tests/test_incident_write_canary.py` (name pinned; MOCK `jira_dispatch`'s
public calls).
**4-AC**: Code = script + units + dry-run test. Deploy = operator installs + enables the
timer (explicit `deployed:` step). Integration = staging dry-run prints the count.
Exercised = one real prod timer firing recorded (+ the JIRA ticket if count=0). Go-Live:
with T5's rollout.
**MUST NOT**: do NOT touch `omnisight-slo-monitor.service` or any safety-actuator unit;
no auto-restarts/rollbacks; no Prometheus wiring (Phase S); do not modify
`jira_dispatch.py`.

## T10 [DOCS] ADR-0015 amendment: honest axes, develop_sha semantics, invalidation reality, R2 descope record

type:docs tier:S class:subscription-codex areas:docs priority:Medium
blockedBy: T4b, T5, T7
**Goal**: append an amendment section to ADR-0015: temporal explicitly unavailable until
U5; `develop_sha` carries the release build SHA; the REAL cache story (TTL + per-replica
divergence; webhook/SWR machine deferred with rationale + audit-report pointers); the R2
decision record (runner-side fields enrichment over API-side cache machine — the
zero-round-trip argument); per-half source markers as the anti-hollow contract.
**Files**: `docs/adr/ADR-0015-cross-task-awareness-3d-memory-architecture.md`.
**4-AC**: Code = amendment. Deploy/Integration = n/a. Exercised = renders on docs-site.
Go-Live: on merge.
**MUST NOT**: append-only (no rewriting the original decision text); do not edit other
ADRs; do not touch HANDOFF.md (frozen) or TODO.md.

---

## Dependency edges (Blocks: inward=blocker, outward=blocked — VERIFY each POST)
T1→T2→T3→T4b→T7; T4a→T4b; T8→T6; T5→T6; T5→T9; T4b→T10; T5→T10; T7→T10.
Parallel starts at file time: **T1, T4a, T5, T8**.
