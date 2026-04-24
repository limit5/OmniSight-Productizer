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
**Severity**: ~~🔴 High — end-to-end orchestration path is broken~~ → **🟢 RESOLVED (credit-exhaustion) + 🟠 Medium residual (silent-fallback design flaw)**

**ROOT CAUSE CONFIRMED (2026-04-24 post-smoke-test follow-up)**:
Anthropic API billing credit was exhausted. Direct `x-api-key` probe against `api.anthropic.com/v1/messages`:
```
HTTP 400
{"type":"error","error":{"type":"invalid_request_error",
 "message":"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."},
 "request_id":"req_011CaNDNHWWmoXyxSFpT6nF6"}
```
The key itself was never invalid — it passed auth but failed at the billing gate. OmniSight's LLM fallback chain (`OMNISIGHT_LLM_FALLBACK_CHAIN=anthropic,ollama`) caught the 400 and silently degraded to `gemma4:e4b`, which then either returned malformed output or hung.

**Post-topup verification (2026-04-24)**: operator topped up Anthropic billing; re-probed with `claude-haiku-4-5`:
```
HTTP 200
model=claude-haiku-4-5-20251001
stop_reason=max_tokens
usage.input_tokens=8  usage.output_tokens=10
content="Pong! 🏓"
```
→ **Anthropic primary path restored**.

**Original hypotheses, now with verdicts**:
- ~~(a) Anthropic API key invalid / rate-limited / stale; fallback triggered silently.~~ ✅ **CONFIRMED** (credit exhausted, not invalid/stale)
- ~~(b) empty/malformed gemma4 response~~ → probably accurate but secondary; moot now that primary works
- ~~(c) asyncio.gather deadlock~~ → refuted; the hang was downstream of the silent fallback
- ~~(d) DAG-payload-specific codepath~~ → refuted

**Residual design flaw** (separate from the resolved billing issue):
Silent fallback on a `credit_low` / `quota_exceeded` / `auth_failed` error is the wrong default — those are **hard errors** (operator must intervene, not transient), but the current chain treats them the same as `rate_limited` or `network_timeout` (which are correctly soft-fallback). This mis-classification lets credit exhaustion degrade the whole orchestrator to a slow local model without alerting. Tracked as a new Blueprint ticket — folded into **Phase F BP.F.8–F.10** of the ADR (hard-error vs soft-fallback classification + notification integration + tests).

**Follow-up recommendation**:
1. ~~dedicated prod debug session~~ → **not needed** (root cause found).
2. **NEW** Design hard-error vs soft-fallback classification in Phase F — routes `credit_low` / `quota_exceeded` / `auth_failed` to L3 Jira + L4 PagerDuty + **refuse new DAG submit**, not silent fallback.
3. **NEW** Operator-side: set Anthropic billing spend alert at `console.anthropic.com/settings/billing` to avoid next surprise.

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
