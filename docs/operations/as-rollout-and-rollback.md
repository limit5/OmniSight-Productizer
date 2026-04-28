# AS Rollout & Rollback Runbook

> **Created**: 2026-04-28
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library) — operator-facing companion to AS.8.2 ADR (`docs/design/as-auth-security-shared-library.md`)
> **Status**: AS.8.3 — runbook landed.
> **Audience**: SRE / on-call ops responsible for OmniSight production cluster (`backend-a` + `backend-b` behind Caddy, per `docker-compose.prod.yml`).
> **Scope**: Step-by-step SOPs for the three operator levers that govern the AS shared library at runtime — (1) **per-tenant feature flag** (`tenants.auth_features.{oauth_login, turnstile_required, honeypot_active}`), (2) **single-knob global rollback** (`OMNISIGHT_AS_ENABLED`), (3) **Turnstile three-phase rollout** (Phase 0 → 1 → 2 → 3 advance + per-phase rollback). Translates the design freezes in `docs/security/as_0_5_*` / `as_0_6_*` / `as_0_7_*` / `as_0_8_*` into ops-executable procedures.
>
> **Out of scope**: Disaster recovery for PG primary failover (see `docs/ops/db_failover.md`), Cloudflare Tunnel provisioning (see `docs/operations/cloudflare_tunnel_wizard.md`), routine deployment (see `docs/operations/deployment.md`), schema/data restore from backup, encryption-key rotation. AS knob covers **code-path level** rollback only — never use it to paper over schema/data-level incidents (per AS.0.8 §13.4).

---

## 0. Quick reference card

| Lever | Scope | Reversible? | Time to apply | When to use |
|---|---|---|---|---|
| `auth_features.oauth_login` flip | 1 tenant | yes — DB UPDATE | ≤ 5 s (next request) | per-tenant OAuth opt-in/out |
| `auth_features.turnstile_required` flip | 1 tenant | yes — DB UPDATE | ≤ 5 s | per-tenant Phase 3 fail-closed opt-in/out |
| `auth_features.honeypot_active` flip | 1 tenant | yes — DB UPDATE | ≤ 5 s | per-tenant honeypot enable/disable |
| `auth_features.captcha_provider` flip | 1 tenant | yes — DB UPDATE | ≤ 5 s | per-tenant captcha vendor override |
| `auth_features.automation_ip_allowlist` edit | 1 tenant | yes — DB UPDATE | ≤ 5 s | extend AS.0.6 IP bypass list (per tenant) |
| `OMNISIGHT_AS_ENABLED=false` + restart | **all tenants, global** | yes — env flip + restart | ≤ 60 s | **catastrophic AS rollback** (Turnstile global outage / OAuth provider down / honeypot mass false-positive / Phase 3 false-positive 401 storm) |
| Turnstile Phase 1 → 2 advance | global, observability only | yes — revert is "stop emitting alerts" | minutes | after ≥ 28 d Phase 1 + acceptance gate green |
| Turnstile Phase 2 → 3 advance | **per tenant** opt-in | yes — admin flip back | ≤ 5 s | after tenant audit ≥ 56 d + admin opt-in |
| Phase 3 per-tenant revert | 1 tenant | yes — DB UPDATE | ≤ 5 s | tenant admin reports "users blocked" |
| Phase-X global revert | global | yes — `OMNISIGHT_AS_ENABLED=false` | ≤ 60 s | last-resort, hits all tenants |

If you don't know which lever you need: **the answer is almost always per-tenant first** (lever 1–5). Only use `OMNISIGHT_AS_ENABLED=false` if the incident affects **all tenants simultaneously** and you can't wait for a per-tenant fix.

---

## 1. Pre-flight checklist

Before flipping any AS lever in production, confirm the following. None of these are optional — every step has bitten us in past rollouts.

### 1.1 Cluster topology

```
┌──────────────┐
│   Caddy LB   │  :443 (round-robin, /readyz eject)
└──────┬───────┘
       │
   ┌───┴────┬────────┐
   ▼        ▼        ▼
┌────────┐ ┌────────┐
│backend-│ │backend-│  uvicorn worker
│   a    │ │   b    │  (per docker-compose.prod.yml)
│ :8000  │ │ :8001  │
└────────┘ └────────┘
       │        │
       └────┬───┘
            ▼
       ┌─────────┐
       │   PG    │  (auth_features, audit_log)
       └─────────┘
```

Both replicas read the **same** `.env` (mounted from `/opt/omnisight/.env`); flipping `OMNISIGHT_AS_ENABLED` requires restarting **both** replicas in lockstep — see §3.3 for the anti-pattern of half-restarted clusters.

### 1.2 Required tools

- SSH access to the ops admin host (`ssh ops@omnisight-prod`).
- `docker compose` v2.x on the ops host.
- `psql` connectivity to the production PG primary (read-write for per-tenant flag flips; `OMNISIGHT_DATABASE_URL` is in the environment).
- `curl` for smoke tests against `https://omnisight.example.com`.
- Read access to the audit log via `journalctl -u omnisight-backend-a` / `docker compose logs backend-a` and the AS dashboard at `/admin/dashboard`.

### 1.3 Record the incident

Before flipping any lever, open an incident ticket and record:

1. **Trigger**: which symptom prompted the rollback (Turnstile error rate spike, OAuth 5xx storm, honeypot mass-block, etc.).
2. **Scope**: which tenants are affected (one? a few? all?).
3. **Lever you intend to use**: per-tenant flag flip vs. `OMNISIGHT_AS_ENABLED=false`.
4. **Expected blast radius**: how many users / how many requests are affected by the flip itself.
5. **Restore plan**: what condition you'll wait for before flipping back.

The audit chain captures the **what** of every flip; the incident ticket captures the **why** — both are needed for the post-mortem.

---

## 2. Per-tenant feature flag flip SOP

The per-tenant gate lives in the `tenants.auth_features` JSONB column (added by Alembic migration `0056_tenants_auth_features.py`). Default seeding:

| Tenant kind | Default `auth_features` |
|---|---|
| Pre-existing tenants (rows present before migration 0056 ran) | `{"oauth_login": false, "turnstile_required": false, "honeypot_active": false}` |
| New tenants (created after migration 0056) | `{"oauth_login": true, "turnstile_required": true, "honeypot_active": true}` |

This split is intentional: existing tenants keep their pre-AS behaviour until their admin opts in; new tenants get the full AS treatment by default. The runbook lever is **flipping individual keys** in this JSONB.

### 2.1 Use cases

| Symptom | Flip |
|---|---|
| Tenant admin reports a wave of legitimate users hitting Turnstile 429 | `turnstile_required` → `false` (revert tenant to fail-open) |
| Tenant signup form is being spammed by bots that bypass honeypot | (verify with audit first) `honeypot_active` → `true` if not already on |
| Tenant wants to disable OAuth login during a vendor-side incident | `oauth_login` → `false` |
| Tenant wants to swap Turnstile for hCaptcha (regional reachability) | `captcha_provider` → `"hcaptcha"` |
| Tenant has a new automation client whose IP keeps getting challenged | append CIDR to `automation_ip_allowlist` (per AS.0.6 §4 axis B) |

### 2.2 Procedure — flipping a Boolean key

Replace `<TID>` with the target tenant id (UUID or integer per your schema) and `<KEY>` with one of `oauth_login`, `turnstile_required`, `honeypot_active`.

```bash
# 1. SSH to ops admin host
ssh ops@omnisight-prod

# 2. Inspect the tenant's current auth_features
docker compose exec -T backend-a python3 - <<'PY'
import asyncio, json
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, auth_features FROM tenants WHERE id = $1", "<TID>"
        )
        print(json.dumps(dict(row), indent=2, default=str))
asyncio.run(main())
PY
# Expect: a row with auth_features JSON visible. If null/empty → migration 0056
# has not yet been applied for this tenant; run `alembic upgrade head` first.

# 3. Flip the key. Use admin Settings UI if available; if you must do it
#    by SQL, go through the same audit-emitting helper rather than raw UPDATE.
#    Direct SQL is documented here as a last-resort bypass when admin UI
#    is itself down.
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tenants
               SET auth_features = jsonb_set(
                   COALESCE(auth_features, '{}'::jsonb),
                   '{<KEY>}',
                   to_jsonb(<NEWVAL>::boolean),
                   true
               )
             WHERE id = $1
            """,
            "<TID>",
        )
asyncio.run(main())
PY

# 4. Verify the change took effect (next request reads new value — there is
#    no in-process cache that would mask the flip; helpers re-read PG per
#    request via the form-verifier wrappers).
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT auth_features FROM tenants WHERE id = $1", "<TID>"
        )
        print(row["auth_features"])
asyncio.run(main())
PY

# 5. Smoke test: simulate a tenant request and confirm the new behaviour.
#    For turnstile_required=false, a browser POST to /api/v1/auth/login
#    with a missing Turnstile token should now succeed (200) instead of
#    being challenged.
curl -sS -X POST https://omnisight.example.com/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -H "X-OmniSight-Tenant-Id: <TID>" \
     -d '{"email": "<canary-account>@example.com", "password": "<canary>"}' \
     -o /dev/null -w "HTTP %{http_code}\n"

# 6. Confirm an audit row was emitted for the flip itself
#    (admin UI flips emit `tenant.<key>_flip` per AS.0.6 §5.2; raw SQL
#    flips do NOT emit audit — record the manual edit in the incident
#    ticket explicitly).
```

### 2.3 Procedure — extending `automation_ip_allowlist`

Per AS.0.6 §4 axis B, the per-tenant CIDR allowlist lives in `tenants.auth_features.automation_ip_allowlist` as a JSON array of strings (IPv4 or IPv6 CIDRs). The bot challenge module reads this column and short-circuits the bypass before contacting the captcha vendor.

```bash
ssh ops@omnisight-prod
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tenants
               SET auth_features = jsonb_set(
                   COALESCE(auth_features, '{}'::jsonb),
                   '{automation_ip_allowlist}',
                   COALESCE(auth_features->'automation_ip_allowlist', '[]'::jsonb)
                       || '["203.0.113.0/24"]'::jsonb,
                   true
               )
             WHERE id = $1
            """,
            "<TID>",
        )
asyncio.run(main())
PY
```

**Wide-CIDR guard** (per AS.0.6 §4): the bot challenge helper rejects entries wider than `/24` (IPv4) or `/48` (IPv6) at runtime — they appear in the column but are skipped during evaluation, with an audit emit. Don't paste `0.0.0.0/0` thinking it'll work; it'll be silently dropped and the request will still be challenged. Add a precise CIDR per automation client.

### 2.4 What the flip does NOT do

- **Does not** reset Phase 3 advance metric or Turnstile rolling counters — those are global and continue to accumulate (per AS.0.5 §7.3).
- **Does not** invalidate active sessions — already-authenticated users keep their cookies; the flip only affects future challenges.
- **Does not** retroactively rewrite audit rows — past `bot_challenge.unverified_*` / `honeypot.fail` rows from before the flip remain in the audit chain.
- **Does not** touch any other tenant — strictly per-tenant scope.

### 2.5 Reverting a per-tenant flip

Run the same UPDATE with the original value (typically `false` to roll back an opt-in). Same ≤5s eventual consistency. If the admin UI is reachable, prefer the admin UI flip (writes a `tenant.*_flip` audit row) over raw SQL.

---

## 3. Single-knob global rollback SOP (`OMNISIGHT_AS_ENABLED`)

This is the **catastrophic-rollback** lever — the one you reach for when the AS shared library itself is causing the incident and per-tenant flips are too slow or won't fix it (e.g., the bug is in the AS module code path, not in any tenant's configuration). Refer to `docs/security/as_0_8_single_knob_rollback.md` for the full design freeze; this section is the operator slice.

### 3.1 What flipping the knob does

When `OMNISIGHT_AS_ENABLED=false` and the backend is restarted:

| Subsystem | Behaviour |
|---|---|
| OAuth client (`backend/security/oauth_client.py`) | `is_enabled()` returns False, `/api/v1/auth/oauth/login/{provider}` returns **503** `{"error": "as_disabled"}`, no audit row emitted |
| Token vault (`backend/security/token_vault.py`) | `write/read/revoke` raise; existing `oauth_tokens` rows preserved (per AS.0.8 §3.3 schema-decoupling invariant) |
| Bot challenge (`backend/security/bot_challenge.py::verify`) | First-line short-circuit — returns passthrough with `outcome="bypass_knob_off"`, **no captcha verify call**, **no audit row** |
| Honeypot (`backend/security/honeypot.py::validate_honeypot`) | First-line short-circuit — returns passthrough with `bypass_kind="knob_off"`, **no audit row** |
| Per-tenant `auth_features.*` columns | Preserved untouched in PG; AS modules simply stop reading them |
| Existing password / MFA / API key / webhook signature | **Completely unaffected** — these paths are pre-AS and the knob does not gate them |
| AS dashboard banner (when AS.5.2 lands) | Will display "AS roadmap globally disabled — phase metrics paused"; for now (AS.5.2 deferred, per ADR §12.6) operators rely on the lifespan WARN log |

### 3.2 Catastrophic rollback procedure (≤ 60 s)

**Triggers** (any one is sufficient):

- Cloudflare Turnstile global outage → users worldwide stuck on challenge widget.
- An OAuth provider (Google / GitHub / Microsoft) is returning 5xx for `> 30 %` of callbacks.
- AS.4 honeypot is mass-blocking legitimate users (validate by tailing audit for `honeypot.fail` rate spike vs. the 7-day baseline).
- AS.3 phase verify is returning 401 for a wave of legitimate Phase-3 tenants (false-positive low-score storm).
- Any other AS-introduced code path is implicated in a production incident and rollback is faster than fix-forward.

```bash
# 0. (≤ 5 s) Verify the symptom is AS-shaped before pulling the lever.
#    If the incident is PG-side, network-side, or auth-baseline-side
#    (password / MFA / API key), the AS knob will NOT help and may waste
#    your 60-second window.
docker compose logs --tail 200 backend-a 2>&1 | grep -E "bot_challenge|honeypot|oauth|token_vault" | tail -50

# 1. (≤ 5 s) SSH to the ops admin host
ssh ops@omnisight-prod
cd /opt/omnisight

# 2. (≤ 5 s) Flip the env knob
sed -i.bak 's/^OMNISIGHT_AS_ENABLED=.*/OMNISIGHT_AS_ENABLED=false/' /opt/omnisight/.env
# If the line does not exist yet, append it:
grep -q '^OMNISIGHT_AS_ENABLED=' /opt/omnisight/.env \
    || echo 'OMNISIGHT_AS_ENABLED=false' >> /opt/omnisight/.env

# 3. (≤ 30 s) Restart BOTH replicas in lockstep
docker compose -f /opt/omnisight/docker-compose.prod.yml restart backend-a backend-b
# graceful-shutdown timeout is 30 s; in-flight requests at the moment of
# restart finish on the old (knob=true) code path. Any audit rows emitted
# during that ~30 s window are normal AS rows, not stragglers — they
# represent real pre-restart traffic.

# 4. (≤ 10 s) Wait for /readyz on both replicas
for svc in backend-a backend-b; do
    until docker compose exec -T "$svc" curl -sf http://localhost:${svc##*-a:8000}/api/v1/readyz >/dev/null 2>&1 \
       || docker compose exec -T "$svc" curl -sf http://localhost:8000/api/v1/readyz >/dev/null 2>&1; do
        sleep 1
    done
done

# 5. (≤ 5 s) Confirm the lifespan WARN log appeared (AS.0.8 §2.2 contract)
#    NOTE (AS.8.2 ADR §12.6): the explicit lifespan validate hook is a
#    deferred item. Until it ships, the knob takes effect because every
#    AS module reads `getattr(settings, "as_enabled", True)` at call time
#    (per backend/security/bot_challenge.py:619 and honeypot.py:442) — the
#    short-circuit is in module bodies, not the lifespan. Verify the env
#    var instead of grepping for a WARN that may not yet emit:
docker compose exec -T backend-a env | grep '^OMNISIGHT_AS_ENABLED='
# Expect: OMNISIGHT_AS_ENABLED=false

# 6. (≤ 10 s) Smoke test that AS is actually noop now
# 6a. Existing password login — must return 200 with no bot_challenge.* audit row
curl -sS -X POST https://omnisight.example.com/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email": "<canary>@example.com", "password": "<canary>"}' \
     -o /dev/null -w "login: HTTP %{http_code}\n"

# 6b. OAuth login init — must return 503 as_disabled
curl -sS https://omnisight.example.com/api/v1/auth/oauth/login/google \
     -o /dev/null -w "oauth: HTTP %{http_code}\n"

# 6c. Audit chain: confirm no bot_challenge.* row was written by step 6a
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) FROM audit_log
             WHERE action LIKE 'bot_challenge.%'
               AND created_at > now() - interval '60 seconds'
            """
        )
        print(f"bot_challenge.* in last 60s: {row[0]}")
asyncio.run(main())
PY
# Expect: 0 (or only rows from in-flight requests during the restart window)
```

### 3.3 Anti-pattern: half-restarted cluster

**Forbidden**: flipping `.env` and restarting only `backend-a` while `backend-b` still has the old env loaded. Symptoms:

- Caddy round-robins requests; ~50 % of users see AS-active behaviour, ~50 % see AS-disabled.
- Audit chain receives a mix of `bot_challenge.*` rows (from `backend-b`) and zero rows (from `backend-a`); incident triage becomes impossible.
- The AS dashboard `/api/v1/runtime/as-status` endpoint returns whichever replica handled the request, flickering between `enabled: true` and `enabled: false`.

If, during step 3 above, one replica fails to come back up, **do not leave the cluster half-restarted**. Either:

- (a) Roll forward: debug the failing replica's startup and bring it back to the same env state, or
- (b) Stop the failing replica entirely (`docker compose stop backend-b`) and run on a single replica until you can fix it. Caddy will route 100 % of traffic to `backend-a`. This is degraded but coherent.

### 3.4 Re-enabling AS (≤ 60 s)

Only re-enable once the upstream cause is fixed (Turnstile recovered, OAuth provider recovered, AS bug patched and image rebuilt).

```bash
ssh ops@omnisight-prod
cd /opt/omnisight

# 1. Flip back
sed -i 's/^OMNISIGHT_AS_ENABLED=.*/OMNISIGHT_AS_ENABLED=true/' /opt/omnisight/.env

# 2. Restart both replicas
docker compose -f /opt/omnisight/docker-compose.prod.yml restart backend-a backend-b

# 3. Confirm
docker compose exec -T backend-a env | grep '^OMNISIGHT_AS_ENABLED='
# Expect: OMNISIGHT_AS_ENABLED=true

# 4. Smoke test that AS is alive again
# 4a. OAuth login init — must return 302 to the provider
curl -sS https://omnisight.example.com/api/v1/auth/oauth/login/google \
     -o /dev/null -w "oauth: HTTP %{http_code}\n"
# Expect: HTTP 302

# 4b. Audit chain: confirm bot_challenge.* rows resume
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) FROM audit_log
             WHERE action LIKE 'bot_challenge.%'
               AND created_at > now() - interval '60 seconds'
            """
        )
        print(f"bot_challenge.* in last 60s: {row[0]}")
asyncio.run(main())
PY
# Expect: > 0 once normal login traffic resumes

# 5. Observe 1 hour. Watch for:
#    - Phase metric resumption (unverified_rate trending toward pre-incident baseline)
#    - jsfail_rate stable (< 3 % per AS.0.5 §6.2 gate)
#    - No 5xx storm from oauth_client / bot_challenge / honeypot
```

### 3.5 What re-enable does NOT need

- Per-tenant `auth_features.*` is preserved through the disable window — no re-flip required.
- Phase advance state is preserved — Phase-3 tenants remain Phase-3.
- OAuth users do not need to re-link their accounts; the `oauth_tokens` table is intact.
- Do not run any Alembic migration on flip — `OMNISIGHT_AS_ENABLED` is purely a code-path switch (per AS.0.8 §3.3 hard invariant).

### 3.6 What re-enable DOES need

- The image must contain all AS modules (`backend/security/*` per AS.8.2 ADR §12.1). `OMNISIGHT_AS_ENABLED=true` on an image without those modules will fail at boot — but this can only happen if you've shipped a downgraded image while the knob was off, which is itself a rollback anti-pattern.
- All AS-related env knobs (`OMNISIGHT_TURNSTILE_SECRET`, `OMNISIGHT_RECAPTCHA_SECRET`, `OMNISIGHT_HCAPTCHA_SECRET`, OAuth provider client IDs/secrets) must still be set. Re-enabling without these will surface as 5xx on the first OAuth/Turnstile request.

---

## 4. Turnstile three-phase rollout SOP

The Turnstile fail-open phased strategy (designed in `docs/security/as_0_5_turnstile_fail_open_phased_strategy.md`) ships in three observable phases, with explicit advance gates between them. This section operationalises the phase-advance and per-phase rollback procedures.

### 4.1 Phase semantics summary

| Phase | Posture | User-visible behaviour | Advance trigger |
|---|---|---|---|
| **0** | AS code not deployed | identical to pre-AS | AS.3 + AS.6.3 land in main → automatic to Phase 1 |
| **1** | fail-open + warn log | login proceeds even on `unverified_*`, audit emits warn | ≥ 28 d observation + acceptance gate (§4.2) → operator ack |
| **2** | fail-open + per-tenant alert | same login behaviour as Phase 1 + dashboard banner + weekly admin email | per-tenant ≥ 56 d cumulative + tenant admin opt-in |
| **3** | per-tenant fail-closed | confirmed low-score → 401; jsfail / server-error still fail-open | terminal — only revert path |

The phase state lives in two places:

1. **Code path**: which Phase the AS code itself is in is determined by what's deployed in `backend/security/bot_challenge.py` — `verify_with_fallback` already supports all three phase semantics simultaneously (per the implemented module). The phase a request runs under is selected per-call by the tenant's `auth_features.turnstile_required`.
2. **Per-tenant opt-in**: `tenants.auth_features.turnstile_required` is the per-tenant "Phase 3" gate. Phase 1 and Phase 2 are global postures that reflect "what the operator believes about overall traffic"; Phase 3 is per-tenant.

### 4.2 Phase 1 → Phase 2 advance procedure

**Pre-flight gates** (all must be green; collect in incident-style ack ticket):

- [ ] **≥ 28 calendar days** since AS.3 + AS.6.3 first deployed (must span at least one weekend and one month-end / billing cron cycle).
- [ ] **Per-tenant audit volume ≥ 100 rows** of `bot_challenge.*` (tenants below this remain Phase 1 — they don't have statistical signal to advance).
- [ ] **`unverified_rate` 7-day rolling < 5 %** for every tenant being advanced (formula in AS.0.5 §4.2: numerator excludes `bypass_*` and `jsfail_*`).
- [ ] **`jsfail_rate` 7-day rolling < 3 %** for every tenant being advanced.
- [ ] **Bypass-list coverage proof**: query the audit log for `bot_challenge.unverified_*` rows in the past 7 d and inspect `caller_kind` distribution. Every automation client should appear in `bypass_*`, never in `unverified_*`. If you find an automation client in `unverified_*`, **do not advance** — fix the bypass list (AS.0.6) first.
- [ ] **`bot_challenge.unverified_servererr` rate < 1 %** for every tenant — server-side errors at this level are an internal symptom, not user behaviour, and they pollute the advance signal.

**Procedure**:

```bash
# 1. SSH to ops admin host
ssh ops@omnisight-prod

# 2. Pull the per-tenant 7-day rolling metrics
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
QUERY = """
WITH last7 AS (
  SELECT
    tenant_id,
    action,
    COUNT(*) as n
    FROM audit_log
   WHERE created_at > now() - interval '7 days'
     AND action LIKE 'bot_challenge.%'
   GROUP BY tenant_id, action
)
SELECT
  tenant_id,
  SUM(CASE WHEN action = 'bot_challenge.pass' THEN n ELSE 0 END) AS pass_n,
  SUM(CASE WHEN action LIKE 'bot_challenge.unverified_%' THEN n ELSE 0 END) AS unverified_n,
  SUM(CASE WHEN action = 'bot_challenge.blocked_lowscore' THEN n ELSE 0 END) AS blocked_n,
  SUM(CASE WHEN action LIKE 'bot_challenge.jsfail_%' THEN n ELSE 0 END) AS jsfail_n,
  SUM(CASE WHEN action LIKE 'bot_challenge.bypass_%' THEN n ELSE 0 END) AS bypass_n
  FROM last7
 GROUP BY tenant_id
 ORDER BY (pass_n + unverified_n + blocked_n) DESC
"""
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(QUERY)
        print(f"{'tenant':36} {'pass':>8} {'unver':>8} {'blkd':>8} {'jsf':>8} {'byp':>8} {'unv%':>6} {'jsf%':>6}")
        for r in rows:
            denom = (r['pass_n'] or 0) + (r['unverified_n'] or 0) + (r['blocked_n'] or 0)
            jsdenom = denom + (r['jsfail_n'] or 0)
            unv_pct = (r['unverified_n'] or 0) * 100.0 / denom if denom else 0
            jsf_pct = (r['jsfail_n'] or 0) * 100.0 / jsdenom if jsdenom else 0
            print(f"{str(r['tenant_id']):36} {r['pass_n']:>8} {r['unverified_n']:>8} "
                  f"{r['blocked_n']:>8} {r['jsfail_n']:>8} {r['bypass_n']:>8} "
                  f"{unv_pct:>6.2f} {jsf_pct:>6.2f}")
asyncio.run(main())
PY

# 3. Sanity-check the bypass-list coverage. Any automation caller landing
#    in unverified_* (and not bypass_*) is a bypass-list miss — fix AS.0.6
#    before you advance.
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT (metadata->>'caller_kind') AS caller_kind, COUNT(*) AS n
              FROM audit_log
             WHERE action LIKE 'bot_challenge.unverified_%'
               AND created_at > now() - interval '7 days'
             GROUP BY caller_kind
             ORDER BY n DESC
            """
        )
        for r in rows:
            print(f"{r['caller_kind']!s:30} {r['n']}")
asyncio.run(main())
PY
# Expect every row to be either NULL (browser user, fine) or a known
# user-shaped caller_kind. Any "apikey_*", "webhook", "chatops", "metrics_token"
# row here = bypass-list bug. Stop and fix AS.0.6.

# 4. Once gates are green, advance Phase 1 → Phase 2. The advance is
#    primarily an *observability* change — the dashboard widget and the
#    weekly alert cron start emitting (per AS.5.2 § future). Until those
#    components ship, "Phase 2" is defined by:
#      (a) operators have committed to the gate evidence (above), AND
#      (b) the post-advance ack audit row is recorded.
#    Record the ack via a `bot_challenge.phase_advance_p1_to_p2` row.
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log
                (tenant_id, action, actor_user_id, severity, metadata, created_at)
            VALUES (NULL, 'bot_challenge.phase_advance_p1_to_p2',
                    $1, 'info',
                    jsonb_build_object('previous_phase', 1, 'next_phase', 2,
                                       'reason', $2),
                    now())
            """,
            "<ops-actor-id>",
            "28d gates clean: unverified <5%, jsfail <3%, bypass coverage verified",
        )
asyncio.run(main())
PY
# (When AS.5.2 admin Settings UI ships, this step becomes the
# "Advance to Phase 2" button — which calls the same audit helper.)
```

### 4.3 Phase 2 → Phase 3 advance procedure (per tenant)

Phase 3 is **per tenant** and **opt-in** by the tenant admin. Operators do not flip Phase 3 globally — that violates the AS.0.5 §10 #1 design freeze.

**Pre-flight gates** for the tenant being opted in:

- [ ] Tenant has been observed in Phase 2 for ≥ 28 d (Phase 1 + Phase 2 cumulative ≥ 56 d).
- [ ] Tenant `unverified_rate` 7-day rolling **≤ 1 %** (stricter than the Phase 1→2 gate).
- [ ] Tenant admin has received and read at least 4 weekly alert emails (per AS.5.2, when shipped).
- [ ] Tenant admin has explicitly opted in via the admin Settings UI (or signed an off-band ack while AS.5.2 admin UI is still pending).

**Procedure**:

The recommended path is the admin Settings UI flip — it writes a `tenant.turnstile_required_flip` audit row and a `bot_challenge.phase_advance_p2_to_p3` row in one transaction. While the admin Settings UI for AS.5.2 is still pending land, operators can flip on behalf of the tenant **only** with explicit written admin opt-in:

```bash
# (admin Settings UI flow, when shipped) — recommended
# Tenant admin clicks "Enable strict bot challenge" on /admin/settings/security.
# This writes tenant.turnstile_required_flip + bot_challenge.phase_advance_p2_to_p3
# in one transaction.

# (operator-on-behalf flow) — only with written admin opt-in
ssh ops@omnisight-prod
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE tenants
                   SET auth_features = jsonb_set(
                       COALESCE(auth_features, '{}'::jsonb),
                       '{turnstile_required}',
                       'true'::jsonb,
                       true)
                 WHERE id = $1
                """,
                "<TID>",
            )
            await conn.execute(
                """
                INSERT INTO audit_log
                    (tenant_id, action, actor_user_id, severity, metadata, created_at)
                VALUES ($1, 'bot_challenge.phase_advance_p2_to_p3',
                        $2, 'info',
                        jsonb_build_object('previous_phase', 2, 'next_phase', 3,
                                           'reason', $3,
                                           'tenant_id', $1::text),
                        now())
                """,
                "<TID>", "<ops-actor-id>",
                "tenant admin written opt-in (incident #<N>): unverified <1% over 56d cumulative",
            )
asyncio.run(main())
PY
```

**Smoke test after Phase 3 advance**:

```bash
# Confirm the tenant is now fail-closed for low score: a request with a
# scoreless / forged Turnstile token must return 429.
curl -sS -X POST https://omnisight.example.com/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -H "X-OmniSight-Tenant-Id: <TID>" \
     -d '{"email": "<canary>@example.com", "password": "<canary>", "cf_turnstile_response": "invalid"}' \
     -o /dev/null -w "phase3 lowscore: HTTP %{http_code}\n"
# Expect: HTTP 429 (with body {"error": "bot_challenge_failed"})
```

### 4.4 Per-tenant Phase 3 → Phase 2 revert (≤ 5 s)

Use this if a Phase 3 tenant reports legitimate users being blocked.

```bash
ssh ops@omnisight-prod
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE tenants
                   SET auth_features = jsonb_set(
                       COALESCE(auth_features, '{}'::jsonb),
                       '{turnstile_required}',
                       'false'::jsonb,
                       true)
                 WHERE id = $1
                """,
                "<TID>",
            )
            await conn.execute(
                """
                INSERT INTO audit_log
                    (tenant_id, action, actor_user_id, severity, metadata, created_at)
                VALUES ($1, 'bot_challenge.phase_revert_p3_to_p2',
                        $2, 'warn',
                        jsonb_build_object('previous_phase', 3, 'next_phase', 2,
                                           'reason', $3,
                                           'tenant_id', $1::text),
                        now())
                """,
                "<TID>", "<ops-actor-id>",
                "tenant admin escalated: legitimate users blocked by lowscore (incident #<N>)",
            )
asyncio.run(main())
PY
```

The next request from this tenant will hit the Phase 1/2 fail-open branch. Already-blocked sessions can simply retry; no further action needed.

### 4.5 Global emergency revert: Phase-X → Phase 0

If the rollback is too broad to handle per-tenant — for example, a Cloudflare Turnstile global outage affecting every tenant simultaneously — escalate to §3.2 (`OMNISIGHT_AS_ENABLED=false`). This bypasses the Phase machinery entirely and is the **only** way to neutralise Phase 3 across all tenants in one action.

After the upstream cause is fixed, re-enable per §3.4. Per-tenant Phase 3 opt-in state is preserved through the disable window (AS.0.8 §3.3).

### 4.6 Phase 2 → Phase 1 revert (rare)

This is unusual — Phase 2 is essentially Phase 1 plus observability. Reverting Phase 2 → Phase 1 means stopping the dashboard banner / weekly cron emission, which is appropriate only if the alert system itself is causing operator harm (e.g., flapping false positives in the dashboard widget pollute on-call). Once the alert subsystem ships (AS.5.2), this revert path becomes "disable the cron job + remove the dashboard widget from `/admin/dashboard`"; it does **not** require flipping `OMNISIGHT_AS_ENABLED` and does **not** require a per-tenant flag flip. Document the operator-side rationale in the incident ticket and an audit row:

```bash
ssh ops@omnisight-prod
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log
                (tenant_id, action, actor_user_id, severity, metadata, created_at)
            VALUES (NULL, 'bot_challenge.phase_revert_p2_to_p1',
                    $1, 'warn',
                    jsonb_build_object('previous_phase', 2, 'next_phase', 1,
                                       'reason', $2),
                    now())
            """,
            "<ops-actor-id>",
            "alert subsystem flapping; Phase 2 observability disabled while we debug",
        )
asyncio.run(main())
PY
```

---

## 5. Acceptance smoke tests (post-flip verification)

After every flip — per-tenant or global — run a relevant subset of these to confirm the new state matches expectations.

### 5.1 Existing-auth path unchanged

This is the AS.0.8 §7.1 / AS.0.9 critical-regression contract: AS knob false (or any tenant-level disable) must not break the pre-AS auth chain.

```bash
# 1. Password login — 200 + session cookie
curl -sS -i -X POST https://omnisight.example.com/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email": "<canary>@example.com", "password": "<canary>"}' \
     | head -1

# 2. API key bearer auth — 200 on a protected endpoint
curl -sS -H "Authorization: Bearer omni_<canary>" \
     https://omnisight.example.com/api/v1/me \
     -o /dev/null -w "api-key: HTTP %{http_code}\n"

# 3. MFA challenge (if MFA-enabled canary) — 200
# (omitted for brevity; same pattern, hit /api/v1/auth/mfa/challenge)

# 4. Webhook (if any inbound webhook is configured)
curl -sS -X POST https://omnisight.example.com/api/v1/webhooks/<provider> \
     -H "X-Webhook-Signature: <valid-sig>" \
     -d '<payload>' -o /dev/null -w "webhook: HTTP %{http_code}\n"
```

### 5.2 AS surface in expected state

```bash
# Knob ON:
#   /api/v1/auth/oauth/login/google → 302 to provider
# Knob OFF:
#   /api/v1/auth/oauth/login/google → 503 as_disabled

curl -sS -i https://omnisight.example.com/api/v1/auth/oauth/login/google \
     | head -1
```

### 5.3 Audit chain healthy

```bash
ssh ops@omnisight-prod
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. recent action volume — sanity
        rows = await conn.fetch(
            """
            SELECT action, COUNT(*) AS n
              FROM audit_log
             WHERE created_at > now() - interval '5 minutes'
             GROUP BY action
             ORDER BY n DESC
             LIMIT 20
            """
        )
        for r in rows:
            print(f"{r['action']:60} {r['n']}")
asyncio.run(main())
PY
```

Expected pattern after a `OMNISIGHT_AS_ENABLED=false` flip: `bot_challenge.*` and `honeypot.*` rows drop to zero (or to in-flight stragglers from the restart window); existing `user.login_success` / `user.login_failure` continue at the normal rate.

---

## 6. Common pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Flipped `.env` but didn't restart | Knob has no effect; AS still active | Restart both `backend-a` and `backend-b` |
| Restarted only one replica | 50/50 behaviour split | Restart the other replica or stop it |
| Used `OMNISIGHT_AS_ENABLED=false` for a per-tenant problem | All other tenants lose AS protection unnecessarily | Use per-tenant `auth_features` flip instead |
| Used per-tenant flip for a global Turnstile outage | Need to flip 200 tenants individually, doesn't scale | Use `OMNISIGHT_AS_ENABLED=false` |
| Pasted `0.0.0.0/0` into `automation_ip_allowlist` | Wide CIDR is silently dropped at runtime (per AS.0.6 §4) | Use specific CIDRs ≤ /24 (v4) or ≤ /48 (v6) |
| Tried to revert AS-induced changes via `alembic downgrade` | Schema ≠ knob — downgrade is destructive | The knob (or per-tenant flag) is enough; never downgrade |
| Re-enabled AS after fixing a bug, forgot to rebuild image | Old image still has the bug; same incident repeats | Confirm image tag changed before flipping back |
| Didn't record the flip in incident ticket | No paper trail of who/when/why | Always record before flipping; audit row only captures the SQL action, not the human reason |
| Used the operator-on-behalf Phase 3 path without admin opt-in | Tenant admin discovers their users blocked, no record of consent | Require explicit written opt-in from tenant admin; record in incident ticket |

---

## 7. Decision tree

```
Is the incident scoped to one tenant?
├── YES → §2 per-tenant flag flip
│         ├── OAuth provider issue → flip oauth_login=false
│         ├── Turnstile blocking legitimate users → flip turnstile_required=false
│         ├── Honeypot mass-blocking → flip honeypot_active=false
│         └── New automation client being challenged → extend automation_ip_allowlist
│
└── NO (affects all tenants) →
    │
    Is the AS shared library itself the suspect?
    ├── YES → §3 catastrophic single-knob rollback (OMNISIGHT_AS_ENABLED=false)
    │         (Turnstile global outage / OAuth provider mass-down /
    │          AS module crash / Phase 3 false-positive 401 storm)
    │
    └── NO → AS is not the right lever. Investigate:
              ├── PG primary unreachable → docs/ops/db_failover.md
              ├── Cloudflare Tunnel down → docs/operations/cloudflare_tunnel_wizard.md
              ├── Image deploy regression → roll back image tag
              ├── PG schema drift → backup restore
              └── Encryption key compromise → secret_store rotation

Are you mid-Turnstile-rollout and need to advance phase?
├── Phase 1 → Phase 2 → §4.2 (≥ 28 d gate, audit-shaped)
├── Phase 2 → Phase 3 → §4.3 (per-tenant, admin opt-in)
└── Need to revert? → §4.4 (per-tenant) or §4.5 (global emergency)
```

---

## 8. Appendix: handy commands

### 8.1 Tail AS-related logs

```bash
ssh ops@omnisight-prod
docker compose -f /opt/omnisight/docker-compose.prod.yml logs -f backend-a backend-b 2>&1 \
    | grep -E 'bot_challenge|honeypot|oauth_client|token_vault|AS\.0\.8|as_enabled'
```

### 8.2 Inspect a single tenant's AS state

```bash
docker compose exec -T backend-a python3 - <<'PY'
import asyncio, json
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, auth_features FROM tenants WHERE id = $1",
            "<TID>",
        )
        if row:
            print(json.dumps(dict(row), indent=2, default=str))
        else:
            print("tenant not found")
asyncio.run(main())
PY
```

### 8.3 Per-tenant 24h captcha summary

```bash
docker compose exec -T backend-a python3 - <<'PY'
import asyncio
from backend.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              tenant_id,
              SUM(CASE WHEN action='bot_challenge.pass' THEN 1 ELSE 0 END) AS passes,
              SUM(CASE WHEN action LIKE 'bot_challenge.unverified_%' THEN 1 ELSE 0 END) AS unver,
              SUM(CASE WHEN action='bot_challenge.blocked_lowscore' THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN action LIKE 'bot_challenge.jsfail_%' THEN 1 ELSE 0 END) AS jsfail,
              SUM(CASE WHEN action LIKE 'bot_challenge.bypass_%' THEN 1 ELSE 0 END) AS bypass
              FROM audit_log
             WHERE created_at > now() - interval '24 hours'
               AND action LIKE 'bot_challenge.%'
             GROUP BY tenant_id
            """
        )
        for r in rows:
            print(f"{r['tenant_id']}: pass={r['passes']} unver={r['unver']} blkd={r['blocked']} jsf={r['jsfail']} byp={r['bypass']}")
asyncio.run(main())
PY
```

### 8.4 Audit chain integrity check after a flip

```bash
docker compose exec -T backend-a python3 - <<'PY'
# Verify audit hash chain is intact across the flip window. The audit
# helper writes prev_hash↔curr_hash; if anything (including a stray
# manual INSERT) breaks the chain, this surfaces it.
import asyncio
from backend.security.auth_event import verify_chain  # adjust import as needed
async def main():
    ok, broken_at = await verify_chain(window_minutes=15)
    print(f"chain ok={ok} broken_at={broken_at}")
asyncio.run(main())
PY
```

---

## 9. Cross-references

- **AS.8.2 ADR**: `docs/design/as-auth-security-shared-library.md` — design rationale, `§ 12 As-built status` lists the 16 backend modules + 7 templates/_shared TS twins this runbook governs.
- **AS.0.5** (Turnstile fail-open three phases): `docs/security/as_0_5_turnstile_fail_open_phased_strategy.md` — design source for §4 of this runbook; advance/revert gates are the design freeze, this runbook is the operator translation.
- **AS.0.6** (automation bypass list): `docs/security/as_0_6_automation_bypass_list.md` — bypass-list precedence and per-tenant `automation_ip_allowlist` semantics; §2.3 / §6 of this runbook apply the rules.
- **AS.0.7** (honeypot field design): `docs/security/as_0_7_honeypot_field_design.md` — honeypot semantics; §2.1/§2.2 honeypot flip rationale.
- **AS.0.8** (single-knob rollback): `docs/security/as_0_8_single_knob_rollback.md` — design source for §3 of this runbook; the runbook is the §13 ops translation.
- **AS.8.1** (compat regression test suite): `backend/tests/test_as_compat_regression.py` + `backend/tests/test_as_cross_feature_integration.py` — automated coverage that the post-flip smoke tests in §5 mirror manually.
- **DB failover** (out of scope here): `docs/ops/db_failover.md` — when AS knob is **not** the right lever.
- **Cloudflare Tunnel**: `docs/operations/cloudflare_tunnel_wizard.md` — for tunnel-side incidents, also out of AS scope.
- **Production deploy**: `docs/operations/deployment.md` — full image-rebuild SOP that this runbook layers on top of.

---

## 10. Deferred operator-facing pieces

These will land in follow-up rows; until they ship, fall back on the manual procedures in this runbook.

| Deferred piece | Owner row | Manual workaround until then |
|---|---|---|
| AS.5.2 admin Settings UI for per-tenant flag flips | AS.5.2 | §2 raw SQL (with incident-ticket record) |
| AS.5.2 dashboard banner showing `OMNISIGHT_AS_ENABLED` state | AS.5.2 | `docker compose exec backend-a env | grep AS_ENABLED` (per §3.2 step 5) |
| AS.5.2 weekly Turnstile alert email cron | AS.5.2 | manual 7-day metric query (per §4.2) |
| AS.5.2 monthly automation-bypass report email | AS.5.2 | manual 30-day metric query (similar shape) |
| Lifespan validate hook with explicit WARN log on knob-false boot | AS.3.1 / AS.4.1 follow-up | env-var verification (per §3.2 step 5) |
| `OMNISIGHT_AS_FRONTEND_ENABLED` frontend env knob and graceful-degrade UI | AS.7.x | knob-off causes 503 on AS endpoints; users see browser error → operator must communicate via status page |
| `/api/v1/runtime/as-status` endpoint for dashboard banner | AS.5.2 | env-var verification on the box |
| Admin Settings "Advance to Phase 2" button | AS.5.2 | §4.2 manual audit row insert |

When any of these land, the corresponding section of this runbook should be updated to point at the shipped UI rather than the manual procedure. Update `docs/design/as-auth-security-shared-library.md § 12.6` accordingly.

---

**End of AS rollout & rollback runbook.** Operator escalation path: page on-call → record incident ticket → consult §7 decision tree → execute the relevant SOP → run §5 smoke tests → file post-mortem within 48 h. For non-incident routine phase-advance work, schedule during business hours with a peer reviewer — there is no "rush" path for advance.
