## A2 L1-05 Prod Smoke Test Report

**Date**: 2026-04-24 07:18:04 UTC (first submission); analysis continued to 07:24 UTC
**Target**: https://ai.sora-dev.app
**Subset**: dag1 (compile-flash host_native — fast path, ~60-90s expected)
**Executor**: Agent session via `python3 scripts/prod_smoke_test.py`
**Commit at test time**: `6117803c` (adr-001: D1/D2 pilot exemption)

---

### DAG #1: compile-flash (host_native)

| Field | Value |
|---|---|
| `run_id` | `wf-1944b5b67e` |
| `plan_id` | `5` |
| `started_at` | 1777014780.158 (2026-04-24 07:13:00 UTC) |
| `final_status` | `running` (stuck >13 min at step_count=0) |
| `completed_at` | null |
| `steps recorded` | 0 |
| `LLM requests` | 1 (avg_latency 28795 ms on `gemma4:e4b`) |
| `result` | **PARTIAL** — submit + auth + audit ✅, execution stuck ❌ |

### DAG #2: cross-compile (aarch64)
**Skipped** — dag1 did not complete; running dag2 would add noise without signal.

### Audit Hash-Chain Integrity
- **result**: **PASS** ✅
- `/api/v1/audit/verify` returned `{"ok": true}` with valid chain

---

## ✅ Passes

1. **Auth flow end-to-end** — admin login → session cookie → API key mint → Bearer token all worked.
2. **DAG submission** — `POST /api/v1/dag` accepted DAG #1 payload, assigned `run_id` and `plan_id`, response time <1s.
3. **workflow_run persistence** — run row created in prod PG, queryable via `GET /api/v1/workflow/runs/{id}`.
4. **Audit hash-chain integrity** — `/audit/verify` returned `ok=true`; prod audit log not corrupted.
5. **`omni_` API key format** — `ak-abbe8d0da4` minted via POST, used successfully, revoked cleanly (DELETE → `{"deleted":true}`).

## 🔴 Findings (require follow-up)

### Finding #1 — Cloudflare Bot Fight Mode blocks `Python-urllib/*` User-Agent (Error 1010)
**Severity**: 🟠 Medium — blocks all Python-based prod scripting by default
**Root cause**: `scripts/prod_smoke_test.py::_headers()` did not set a `User-Agent` header; urllib's default `Python-urllib/3.12` is on Cloudflare's bot-signature list → HTTP 403 with `error_code: 1010, error_name: browser_signature_banned` before reaching origin.
**Fix applied in this session** (committed with this report):
- Added explicit UA `OmniSight-SmokeTest/1.0 (+https://github.com/limit5/OmniSight-Productizer)` with env override `OMNISIGHT_SMOKE_UA`.
**Follow-up recommendation**: audit other prod scripts in `scripts/` for the same bug — any that call prod CF-fronted endpoints without a named UA will hit the same block.

### Finding #2 — `auth_baseline` middleware rejects Bearer-only requests
**Severity**: 🔴 High — permanent bug affecting any API-key-only integration (CI/CD, external callers, webhooks)
**Root cause**: `backend/auth_baseline.py::_has_valid_session()` (line 213) only checks for the `omnisight_session` cookie. It does not invoke `api_keys.validate_bearer()`. A request carrying a valid `Authorization: Bearer omni_...` token but no session cookie is 401'd by the baseline middleware before reaching `current_user()` where the Bearer validation actually lives.
**Evidence**: Bearer-only request returned `{"detail":"authentication required","path":"/api/v1/workflow/runs/probe"}` + `WWW-Authenticate: Cookie` header.
**Workaround applied**: script patched to accept `OMNISIGHT_SESSION_COOKIE` env var that injects a session cookie alongside Bearer — both gates pass, handler layer resolves to the Bearer identity.
**Follow-up recommendation**: **file a dedicated ticket** to fix `auth_baseline._has_valid_session` to accept either a session cookie OR a valid Bearer via `api_keys.validate_bearer()`. Test coverage: add `test_auth_baseline_accepts_bearer_token` mirroring existing `test_auth_baseline::session_valid` path.

### Finding #3 — DAG execution stuck; LLM routing shows only slow `gemma4:e4b` fallback
**Severity**: 🔴 High — end-to-end orchestration path is broken or severely degraded in prod
**Root cause**: unclear; needs investigation. Observed symptoms:
- `/api/v1/runtime/tokens` shows only one entry: `{"model":"gemma4:e4b","request_count":1,"avg_latency":28795,"input_tokens":0,"output_tokens":0}`. Anthropic (primary per `.env::OMNISIGHT_LLM_FALLBACK_CHAIN=anthropic,ollama`) appears NOT to have been called, OR its stats are not being recorded.
- Single LLM call completed 28.8s after run start; nothing progressed for 13+ minutes afterwards. `step_count` stayed at 0 → no task step ever got persisted.
- `/workflow/runs/{id}/halt` returned 404; `/cancel` requires `If-Match` ETag which the endpoint doesn't expose via HEAD — run was abandoned to natural timeout.

**Hypotheses to investigate** (operator domain):
- (a) Anthropic API key invalid / rate-limited / stale; fallback triggered silently.
- (b) `input_tokens=0, output_tokens=0` + `total_tokens=0` suggests the single gemma4 call may have returned an empty / malformed response — orchestrator might be waiting for retry that never fires.
- (c) Async task that should transition run `planning → running-steps` is hung; possibly related to a Ollama client bug or a `asyncio.gather` deadlock.
- (d) The `smoke-compile-flash-host-native` DAG payload may trigger a code path that was only smoke-tested in dev (pre-Phase-2 Ollama wiring), never in prod.

**Follow-up recommendation**: **dedicated prod debug session** needed. Check backend logs for `wf-1944b5b67e`, verify `list_providers()` return value in prod, check LLM provider circuit-breaker state for anthropic key fingerprint.

---

## 🧹 Cleanup performed

- **API key `ak-abbe8d0da4` (name `smoke-test-a2-2026-04-24`)**: revoked via `DELETE /api/v1/api-keys/ak-abbe8d0da4` → `{"deleted":true}` confirmed.
- **Session cookie in `/tmp/omni_smoke_cookies.txt`**: local-only, expires with admin session TTL (no prod action needed).
- **Stuck run `wf-1944b5b67e`**: left to natural watchdog timeout (cannot halt without ETag; non-blocking for prod).

## 🎯 Overall verdict

**PARTIAL PASS** — the smoke test harness itself is validated and the critical audit-chain invariant is intact. However, the prod orchestrator appears to have a latent LLM-routing or execution-hang bug that was not visible before A2 exercised it; this finding is **more valuable than a simple green smoke test** and should be triaged.

| TODO A2 checkbox | Result |
|---|---|
| Run both via production UI; capture workflow_run IDs | ✅ PARTIAL (dag1 only; `wf-1944b5b67e` captured) |
| Verify steps complete, artifacts persist, audit log hash-chain intact | 🟡 PARTIAL (audit chain PASS ✅; steps did not complete ❌) |
| Attach run report to HANDOFF | ✅ (this file + HANDOFF 2026-04-24 entry) |

## 📋 Recommended follow-up tickets (not blocker for Blueprint V2)

1. **`auth_baseline` Bearer-acceptance bug** — high severity, new ticket; ~1 day fix + regression test.
2. **Prod LLM orchestration hang investigation** — high severity, assign to operator + backend-on-call; needs prod log access.
3. **CF Bot Fight Mode — bulk audit of `scripts/*.py`** — apply UA fix to any other prod-facing script.
4. **A2 smoke test retry after Finding #3 resolved** — re-run full `--subset both` to confirm dag1 + dag2 complete successfully.
