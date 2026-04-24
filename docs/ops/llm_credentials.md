# LLM Provider Credentials — Operator Runbook

> Date: 2026-04-24
> Scope: Phase 5b `llm_credentials` — day-to-day operator tasks for
> **Anthropic / OpenAI / Google / OpenRouter / xAI / Groq / DeepSeek
> / Together / Ollama** API keys. For the one-time migration from
> legacy `.env` scalars (`OMNISIGHT_ANTHROPIC_API_KEY` etc.) to
> `llm_credentials`, see Phase 5b-5's lifespan auto-migration in
> `backend/legacy_llm_credential_migration.py`.

---

## TL;DR

| Task                                                            | Where                                             |
| --------------------------------------------------------------- | ------------------------------------------------- |
| Add a new API key for a provider                                | Modal § LLM PROVIDERS tab                         |
| Rotate a key (operator leaves, compromise suspected, etc.)      | Per-row `ROTATE` button                           |
| Temporarily disable a credential without deleting               | Per-row `ENABLED` toggle                          |
| Delete a credential                                             | Per-row `DELETE` button                           |
| Find which credential a tenant's `get_llm("anthropic")` uses    | `GET /api/v1/llm-credentials?provider=anthropic`  |
| Test a saved credential without rotating                        | Per-row `TEST` button                             |
| Inspect the fallback chain (`anthropic → openai → ...`)         | `GET /api/v1/runtime/settings` → `llm.fallback_chain` |

All operations are **per-tenant**. An admin sees only their tenant's
credentials; tenant isolation is enforced at the PG `WHERE tenant_id
= $1` layer (Phase 5b-1 schema + Phase 5b-3 CRUD).

---

## 1. Conceptual model

Phase 5b-1 through 5b-5 replaced the old `Settings.anthropic_api_key`
/ `google_api_key` / etc. scalars with one row per (tenant, provider,
label) in `llm_credentials`:

```
llm_credentials
├── id               # lc-xxxxx (stable; UI uses this for PATCH / DELETE)
├── tenant_id        # t-default / t-customer-N (per-tenant RLS)
├── provider         # anthropic | openai | google | openrouter |
│                    #   xai | groq | deepseek | together | ollama
├── label            # operator-chosen; shown in UI; grep-friendly
├── encrypted_value  # Fernet-ciphertext API key (empty for ollama — keyless)
├── metadata         # JSONB: base_url (ollama) / org_id (openai) / notes
├── auth_type        # pat | oauth (5b-12 reservation)
├── is_default       # one TRUE per (tenant, provider) via partial-unique index
├── enabled          # FALSE → resolver skips this row
├── last_used_at     # LRU analytics (touched on successful resolve)
├── version          # optimistic-lock guard for PATCH (If-Match)
├── created_at / updated_at
```

The resolver chain used by `get_llm(provider)` (see
`backend/llm_credential_resolver.py`) is:

1. **Pool path (async)** — query `llm_credentials` by `(tenant_id,
   provider)` filtered to `enabled = TRUE`, ordered
   `is_default DESC, last_used_at DESC NULLS LAST, id`. First row
   wins.
2. **Legacy fallback** — if no row, fall back to
   `settings.{provider}_api_key` (a whitespace-stripped read of the
   deprecated scalar). One deprecation warn log per worker on first
   fallback.
3. **Raise** — both empty → `LLMCredentialMissingError(LookupError)`.
   The caller (`backend.agents.llm::_create_llm`) catches this and
   the failover cascade (`llm_fallback_chain`) walks to the next
   provider.

Keyless providers (Ollama) always resolve — `api_key` carries the
empty string, `metadata.base_url` threads through to the adapter.

---

## 2. Add a new API key

### Via UI (recommended)

1. Open the OmniSight dashboard → gear icon → **SYSTEM INTEGRATIONS**
   → **LLM PROVIDERS** tab.
2. Pick the provider tab (**anthropic** / **openai** / **google** /
   **openrouter** / **xai** / **groq** / **deepseek** / **together**
   / **ollama**).
3. Fill the form:
   - **Label**: operator-visible name (e.g. `prod-anthropic` or
     `tenant-acme-openai`).
   - **API key**: paste the secret. *(For Ollama: enter the **Base
     URL** instead, e.g. `http://ai_engine:11434`.)*
   - **Default for provider**: tick to make this credential the
     `get_llm("anthropic")` default for this tenant.
4. Click **SAVE**. Row appears in the list immediately. The
   fingerprint (`sk-ant-…abc4`) is the only artefact echoed back —
   the plaintext key never leaves the browser after the POST.
5. Click **TEST** on the new row — backend decrypts the stored key,
   hits the provider's `/v1/models` endpoint, reports OK + the
   number of models listed. If the key is wrong, the response
   surfaces the upstream HTTP status + error tail (truncated to 500
   chars).

### Via REST API

```bash
curl -X POST https://ai.sora-dev.app/api/v1/llm-credentials \
  -H "Content-Type: application/json" \
  -H "Cookie: ..." \
  -d '{
    "provider":   "anthropic",
    "label":      "prod-anthropic",
    "value":      "sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx",
    "is_default": true
  }'
```

Response contains only fingerprints (`…last4`) — the plaintext
never round-trips. `audit_log` records
`action=llm_credential.create` with the operator's user id.

---

## 3. Rotate a key

Rotation is the highest-frequency operator flow: provider rotates
their customer-visible key, a PAT leaks to a log, quarterly policy
fires.

### Via UI

1. Open **LLM PROVIDERS** tab, locate the row.
2. Click `ROTATE` — paste new key → SAVE.
3. Click `TEST` to confirm the new key is accepted by the provider's
   `/v1/models` probe.

### Via REST API

```bash
curl -X PATCH https://ai.sora-dev.app/api/v1/llm-credentials/lc-xxxxx \
  -H "Content-Type: application/json" \
  -d '{"value": "sk-ant-newvalue_xxxxxxxxxxxxxxxx"}'
```

- Empty string `""` clears the key (useful for Ollama-style keyless
  rows that shouldn't carry ciphertext).
- `null` / missing leaves the field untouched (partial update).
- `version` auto-bumps on each successful PATCH (`version++`) —
  optimistic lock for concurrent edits from two browser tabs;
  collision returns HTTP 409.
- `last_used_at` does NOT reset — rotation keeps LRU ordering
  stable.

### What to test after a rotation

- Trigger one LLM call the credential is used for (ask the agent to
  say "pong", hit the chat endpoint once).
- Response should include `provider=anthropic, model=claude-opus-4-7`
  (or whatever `llm_provider` / `llm_model` are set to) — success =
  rotation cleanly replaced the credential across all workers. Each
  worker's next `get_llm()` call reads the new row from PG; no
  process restart needed.
- If the call fails with a `LLMCredentialMissingError` chain, run
  the `TEST` button to verify the key is valid upstream. If `TEST`
  returns OK but real inference fails, the failover chain (see §7)
  might be walking past a valid credential because of an unrelated
  upstream issue.

Rotation drill is covered by the Phase 5b-6 soak test
`backend/tests/test_phase5b_6_soak_rotation.py`:

- Save key A → verify `get_llm_credential` returns key A.
- `PATCH {value: key-B}`.
- Re-resolve → returns key B.
- Audit chain has `llm_credential.update` with the fingerprint but
  no plaintext.
- `asyncio.gather` a mock "backend restart" (close + reopen pool)
  → key B still resolves after the reopen.

---

## 4. Disable a credential (without deleting)

Use case: an operator is on holiday, a bill-per-use provider is
running hot, or you want to test fallback behaviour without
permanently removing a credential.

### Via UI

1. **LLM PROVIDERS** tab → toggle the row's `ENABLED` switch off.
2. The resolver skips `enabled=FALSE` rows (see
   `backend.llm_credential_resolver._fetch_enabled_credentials` —
   `WHERE enabled = TRUE` is in the SELECT clause).
3. If this was the provider's default AND the only enabled row for
   that provider, `get_llm("anthropic")` falls through to the legacy
   `settings.anthropic_api_key` path; if both are empty, raises
   `LLMCredentialMissingError` and the fallback chain walks on.

### Via REST API

```bash
curl -X PATCH https://ai.sora-dev.app/api/v1/llm-credentials/lc-xxxxx \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Re-enable by `PATCH {enabled: true}`.

---

## 5. Delete a credential

### Via UI

1. Click row's `DELETE` button.
2. Confirm dialog — if the row is `is_default`, the UI warns
   "backend will auto-elect next".
3. Delete proceeds; if `is_default` and `auto_elect_new_default=true`
   (UI default), the backend picks the next-LRU credential of the
   same provider as the new default inside the same transaction —
   the partial-unique index never observes a 2-TRUE intermediate
   state.

### Via REST API

```bash
# Refuse without replacement (safer default for scripted deletion)
curl -X DELETE "https://ai.sora-dev.app/api/v1/llm-credentials/lc-xxxxx"

# Explicitly allow auto-elect (UI's default behaviour)
curl -X DELETE "https://ai.sora-dev.app/api/v1/llm-credentials/lc-xxxxx?auto_elect_new_default=true"
```

- Deleting the sole credential for a provider is allowed (auto-elect
  finds nothing to promote — the provider simply has no default,
  resolver walks to legacy Settings fallback then raises).
- `audit_log` records `action=llm_credential.delete` with the
  `before` snapshot (minus the plaintext key — fingerprint only).

---

## 6. Find which credential a tenant / provider resolves to

### Via REST API

```bash
# All anthropic rows for this tenant (admin scope — tenant_id is
# implicit via the authenticated session):
curl "https://ai.sora-dev.app/api/v1/llm-credentials?provider=anthropic&enabled_only=true"
```

Returns fingerprint-only rows sorted
`is_default DESC, last_used_at DESC NULLS LAST` — the same order the
resolver walks. The first row is what `get_llm("anthropic")` would
return right now.

### Via Python (debugging shell)

```python
from backend.db_context import set_tenant_id
from backend.llm_credential_resolver import get_llm_credential

set_tenant_id("t-default")
cred = await get_llm_credential("anthropic")
print(cred.source)             # "db" | "settings" | "unset"
print(cred.id)                 # lc-xxxxx (None when source=settings)
print(cred.api_key[:10])       # first 10 chars — for grep in logs
print(cred.metadata)           # {} | {"base_url": "..."} for ollama
```

---

## 7. Fallback chain + per-tenant interaction

`settings.llm_fallback_chain` is a CSV string like
`"anthropic,openai,google,groq,deepseek,openrouter,ollama"`. When a
primary provider fails (rate-limit, outage, missing credential), the
failover cascade in `backend.agents.llm::get_llm` walks the chain in
order, calling `_create_llm(provider)` for each.

Each `_create_llm(provider)` resolves through the Phase 5b-2 chain
(DB → legacy Settings → raise). So:

- **Per-tenant isolation holds throughout the chain.** Tenant A's
  chain walks A's credentials; tenant B's walks B's. There is no
  process-global fallback pool.
- **A missing credential skips, not aborts.** If a tenant has no
  `groq` credential (neither DB nor Settings), the chain walks past
  `groq` silently and tries the next provider. This is intentional —
  a fresh tenant shouldn't have to wire up every provider before the
  primary works.
- **Legacy Settings fallback still applies per-provider.** A tenant
  that only has an `llm_credentials` row for `anthropic` can still
  use `openai` as a fallback if `settings.openai_api_key` is set in
  the .env (same scalar that Phase 5b-5 would auto-migrate on next
  boot).
- **Keyless providers always resolve.** Ollama is always the
  "last-chance" of the chain; the adapter dials
  `http://ai_engine:11434` (or whatever `metadata.base_url` is set
  to) and runs inference locally.

Edit the chain via:

```bash
curl -X PUT https://ai.sora-dev.app/api/v1/runtime/settings \
  -H "Content-Type: application/json" \
  -d '{"updates": {"llm_fallback_chain": "anthropic,openai,ollama"}}'
```

`llm_fallback_chain` is **still in `_UPDATABLE_FIELDS`** — it's a
routing knob, not a credential. Only the 8 `*_api_key` fields + the
`ollama_base_url` URL were moved to `llm_credentials` in Phase 5b-6.

---

## 8. Troubleshooting

### "After rotation, an LLM call still uses the old key"

- The resolver has NO per-worker cache (Phase 5b-2 design decision —
  `llm_credentials` is O(<10 rows/tenant), caching isn't worth the
  staleness risk). Every `get_llm(provider)` call round-trips to PG.
- More likely: an OS-level connection pool held a kept-alive HTTP
  connection to the provider with the old key. This is
  provider-adapter-specific (LangChain's `ChatAnthropic` etc.) and
  usually clears on the next request. If it persists, rolling-
  recreate the backend.
- Sanity-check via §6: `GET /api/v1/llm-credentials?provider=anthropic`
  should show the new fingerprint on the first row.

### "The UI shows the credential but `get_llm` raises MissingCredentialError"

- Check `enabled` is TRUE. A disabled row is skipped by the resolver
  but still shown in list endpoints.
- Check the `tenant_id` the request is arriving under. A credential
  created under `t-default` is invisible to a request arriving under
  `t-acme` — isolation is enforced at the PG WHERE clause level.
- Check the audit log for a recent `llm_credential.update` that
  cleared the key (`value: ""` is a valid PATCH — it's how you
  convert an anthropic row back to "no key"). The audit row's
  `after.value_fingerprint` will be empty.

### "`PUT /runtime/settings` returns `rejected[anthropic_api_key]='deprecated: use POST /api/v1/llm-credentials'`"

- This is Phase 5b-6's rejection path. The UI was updated in Phase
  5b-4 to POST to `/api/v1/llm-credentials` instead of PUT-ing the
  scalar. If you still see this rejection, you're either (a) hitting
  an older cached version of the UI JS (hard-refresh the browser),
  or (b) calling PUT directly from a script / curl. Migrate the
  script to the CRUD endpoint; the new path is idempotent and
  per-tenant.

### "A credential the `.env` had is not showing up after backend restart"

- Phase 5b-5's lifespan auto-migration only fires if
  `llm_credentials` is **empty**. If another tenant has already
  created rows, your `.env` scalar is NOT auto-imported (the
  migration hook's idempotency guard is per-table-emptiness, not
  per-tenant). Workaround: create the row via the UI or CRUD API.
- Kill-switch: `OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip` disables the
  hook entirely.

### "The fallback chain is walking past a provider that has a credential"

- Confirm the credential is `enabled=TRUE`.
- Confirm the tenant_id is correct. Fallback walks are per-request,
  so they inherit the request's tenant.
- Look for `LLMCredentialMissingError` in backend logs with
  `cred.source=...`. If `source=settings` and the scalar is empty,
  the resolver raised correctly.
- If you're using an Ollama fallback, confirm `metadata.base_url`
  points at a reachable host. `docker exec backend-a python -c "import
  urllib.request; print(urllib.request.urlopen('http://ai_engine:11434/api/tags',
  timeout=2).read()[:100])"` from inside a backend container
  confirms network reachability.

---

## 9. Audit trail

Every credential mutation writes an `audit_log` row:

| Action                                         | Fired when                                                 |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `llm_credential.create`                        | `POST /llm-credentials`                                    |
| `llm_credential.update`                        | `PATCH /llm-credentials/{id}`                              |
| `llm_credential.delete`                        | `DELETE /llm-credentials/{id}`                             |
| `llm_credential_auto_migrate`                  | Phase 5b-5 lifespan hook migrated an `.env` scalar         |
| `settings.legacy_llm_credential_write`         | Phase 5b-6 — `PUT /runtime/settings` attempted a legacy scalar write (rejected) |

Query the chain:

```bash
curl "https://ai.sora-dev.app/api/v1/audit/query?action=llm_credential.update&limit=50"
```

The audit chain is hash-linked (`prev_hash` → `curr_hash`); any
post-write tampering breaks `audit.verify_chain`.

**Plaintext is never in the audit log.** The `after` block of
`llm_credential.*` rows carries `value_fingerprint` (e.g.
`sk-ant-…abc4`) + `provider` + `label` + `is_default` + `enabled` +
`metadata_keys` — never the plaintext value. The Phase 5b-6 soak
test `test_phase5b_6_soak_rotation::test_audit_trail_has_no_plaintext`
is the regression guard on that invariant.

---

## 10. Legacy `.env` fields — still read, write is REJECTED

Phase 5b-6 marked the following `Settings` fields as deprecated.
They are still **readable** via `backend.llm_credential_resolver`'s
legacy-fallback chain (so deployments that never migrated keep
working), but **writing** them via `PUT /runtime/settings` is now
rejected:

```
anthropic_api_key     → llm_credentials(provider='anthropic').encrypted_value
google_api_key        → llm_credentials(provider='google').encrypted_value
openai_api_key        → llm_credentials(provider='openai').encrypted_value
xai_api_key           → llm_credentials(provider='xai').encrypted_value
groq_api_key          → llm_credentials(provider='groq').encrypted_value
deepseek_api_key      → llm_credentials(provider='deepseek').encrypted_value
together_api_key      → llm_credentials(provider='together').encrypted_value
openrouter_api_key    → llm_credentials(provider='openrouter').encrypted_value
ollama_base_url       → llm_credentials(provider='ollama').metadata.base_url
```

On rejection, the PUT response carries:

```json
{
  "rejected": {
    "anthropic_api_key": "deprecated: use POST /api/v1/llm-credentials (→ llm_credentials(provider='anthropic').encrypted_value)"
  },
  "llm_deprecations": {
    "fields":     {"anthropic_api_key": "llm_credentials(provider='anthropic').encrypted_value"},
    "migrate_to": "llm_credentials",
    "endpoint":   "/api/v1/llm-credentials",
    "doc":        "/docs/ops/llm_credentials.md"
  }
}
```

An `audit_log` row with `action=settings.legacy_llm_credential_write`
is also emitted. Deprecated fields: see
`backend.config.LEGACY_LLM_CREDENTIAL_FIELDS` (registry of truth;
dict mapping each legacy field to its replacement hint).

---

## 11. See also

- `backend/llm_credential_resolver.py` — resolver source code.
- `backend/routers/llm_credentials.py` — CRUD + test endpoints.
- `backend/legacy_llm_credential_migration.py` — Phase 5b-5 lifespan
  hook.
- `docs/phase-5b-llm-credentials/01-design.md` — schema + resolver
  contract + rationale.
- `docs/ops/git_credentials.md` — sibling runbook for Phase 5
  forge credentials (same design pattern).
