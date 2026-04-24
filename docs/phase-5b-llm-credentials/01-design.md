# Phase 5b-1 — `llm_credentials` data model

> Date: 2026-04-24
> Owner: Software-beta agent
> Scope: row 5b-1 of Phase 5b (LLM API key persistence) — schema
> + migration + drift-guard tests only. NO resolver refactor, NO
> CRUD endpoints, NO UI rewrite, NO legacy auto-migration,
> NO deprecation path — those land in 5b-2 through 5b-6.

---

## 1. Why a new table

Today LLM API keys live in `Settings` as 8 scalar fields (plus the
Ollama base-URL which isn't secret but is the same configuration
shape):

```python
anthropic_api_key:  str = ""
google_api_key:     str = ""
openai_api_key:     str = ""
xai_api_key:        str = ""
groq_api_key:       str = ""
deepseek_api_key:   str = ""
together_api_key:   str = ""
openrouter_api_key: str = ""
ollama_base_url:    str = "http://localhost:11434"
```

Three structural problems make this unfit for the operator-rotate /
multi-account / per-tenant future Phase 5b promises:

1. **Runtime-only on `PUT /runtime/settings`.** The
   `integration-settings` UI can call `PUT /runtime/settings` to
   override the provider keys, but the override only lives in one
   worker's `Settings` object. `docker compose restart backend-a`
   evaporates the value; multi-worker (`OMNISIGHT_WORKERS=2`) means
   the override only takes effect on the worker that served the PUT
   — the other worker keeps the stale `.env` value. Operator
   reported this as "filled in Google API key, hit SAVE & APPLY,
   nothing happened" in the session that motivated this Phase.
2. **No per-tenant scope.** `Settings` is process-global; in a
   multi-tenant deploy, tenant A's Anthropic PAT is available to
   tenant B. The per-tenant rate-limit + budget envelope
   (SP-8.1c) starts leaking when the underlying credential does.
3. **Plaintext on disk.** `.env` holds the key in clear text;
   `secret_store` Fernet (already used for `tenant_secrets` and
   Phase 5's `git_accounts`) is the established pattern but LLM
   keys were never migrated to it.

The fix is the `llm_credentials` table — one row per (tenant,
provider) account, scoped to a tenant, with the API key at-rest
encrypted via `secret_store.encrypt` and resolved by provider name
at every `get_llm()` call.

## 2. Why a new table (vs. extending `tenant_secrets` or `git_accounts`)

`tenant_secrets` is a generic `(tenant_id, secret_type, key_name,
encrypted_value)` KV store; it would host the encrypted key fine
but it has no first-class `is_default` flag, no `last_used_at`
LRU column, no per-provider CHECK constraint to catch typos like
`"Anthropic"` vs `"anthropic"`, and no partial-unique index to
stop the "two defaults on the same provider" race. Phase 5b-3's
CRUD needs all four.

`git_accounts` is structurally very similar (almost copy-paste per
the TODO row header), but it carries forge-specific columns
(`ssh_host`, `ssh_port`, `project`, `url_patterns`, three separate
`encrypted_{token,ssh_key,webhook_secret}` columns) that are
meaningless for LLM providers. Merging them would either force
LLM rows to carry junk columns or bloat `git_accounts` with LLM
concerns. A dedicated table is the right shape.

The columns that DO match `git_accounts` (PK, `tenant_id` FK,
`is_default`, `enabled`, `last_used_at`, `created_at`,
`updated_at`, `version`, `auth_type`) are intentionally named
identically so rows 5b-2 / 5b-3 can lift the resolver and CRUD
patterns directly from Phase 5's matching modules.

## 3. Schema

### 3.1 PostgreSQL (production)

```sql
CREATE TABLE IF NOT EXISTS llm_credentials (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 't-default'
                            REFERENCES tenants(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL                       -- 'anthropic'|...|'ollama'
                            CHECK (provider IN (
                                'anthropic','google','openai','xai',
                                'groq','deepseek','together',
                                'openrouter','ollama'
                            )),
    label             TEXT NOT NULL DEFAULT '',           -- operator-visible name
    encrypted_value   TEXT NOT NULL DEFAULT '',           -- Fernet ciphertext (API key)
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb, -- base_url / org_id / scopes
    auth_type         TEXT NOT NULL DEFAULT 'pat',        -- 'pat'|'oauth' (future hook)
    is_default        BOOLEAN NOT NULL DEFAULT FALSE,     -- per (tenant, provider)
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at      DOUBLE PRECISION,                   -- LRU analytics; NULL = never used
    created_at        DOUBLE PRECISION NOT NULL,
    updated_at        DOUBLE PRECISION NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0          -- optimistic-lock (J2 pattern)
);

CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant
    ON llm_credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant_provider
    ON llm_credentials(tenant_id, provider);
CREATE INDEX IF NOT EXISTS idx_llm_credentials_last_used
    ON llm_credentials(tenant_id, last_used_at DESC NULLS LAST);

-- Unique partial: at most one default per (tenant, provider).
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_credentials_default_per_provider
    ON llm_credentials(tenant_id, provider)
    WHERE is_default = TRUE;
```

### 3.2 SQLite (dev parity)

JSONB → `TEXT`; BOOLEAN → `INTEGER` 0/1; DOUBLE PRECISION → `REAL`.
Partial indexes have been supported since SQLite 3.8, so the
`WHERE is_default = 1` partial-unique index lands on the dev path
too. The app layer in row 5b-3 will still enforce the invariant
on write (for belt+braces + a cleaner error at the application
boundary), but the database-layer guard catches races.

### 3.3 Per-column rationale

| Column | Type (PG / SQLite) | Rationale |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | App-generated (e.g. `lc-<uuid>`), matches `tenant_secrets` / `git_accounts` convention. Not INTEGER IDENTITY so it isn't listed in migrator `TABLES_WITH_IDENTITY_ID`. |
| `tenant_id` | `TEXT NOT NULL DEFAULT 't-default'` | FK → `tenants(id) ON DELETE CASCADE` mirrors Phase 5-1. When a tenant is deleted, their LLM credentials follow — same invariant as forge credentials. |
| `provider` | `TEXT NOT NULL CHECK (...)` | Enumerated over the 9 providers from `backend.agents.llm.list_providers()`: 8 key-based (`anthropic` / `google` / `openai` / `xai` / `groq` / `deepseek` / `together` / `openrouter`) + `ollama`. Ollama is keyless but benefits from a table row so its `base_url` (currently `Settings.ollama_base_url`) can live in `metadata` without a special-case scalar. |
| `label` | `TEXT NOT NULL DEFAULT ''` | Operator-visible name like `"Anthropic — production budget"` / `"Anthropic — test/dev"`. Lets the UI disambiguate multi-account-per-provider rows. |
| `encrypted_value` | `TEXT NOT NULL DEFAULT ''` | Single Fernet ciphertext column (LLM credential is a single API key — no SSH / webhook secret). Empty string for `ollama` rows since Ollama is keyless. |
| `metadata` | `JSONB / TEXT-of-JSON NOT NULL DEFAULT '{}'` | Per-account extras: `base_url` for OpenAI-compatible gateways + `ai_engine:11434` for ollama, `org_id` for OpenAI org-scoped keys, `scopes` for future OAuth, `model_overrides` for per-account model allowlists, free-form `notes`. Deliberately JSONB (not a column-per-field) because the mix of fields-per-provider is open-ended and we don't want a schema migration every time a provider adds a new knob. |
| `auth_type` | `TEXT NOT NULL DEFAULT 'pat'` | `'pat'` (today) vs `'oauth'` (future 5b follow-up). Symmetric with row 5-12 `git_accounts.auth_type`. |
| `is_default` | `BOOLEAN / INTEGER NOT NULL DEFAULT FALSE` | At most one row per `(tenant, provider)` may have `is_default = TRUE`, enforced by the partial unique index. Row 5b-2's `pick_default(provider)` resolver reads this first; Row 5b-3's CRUD serialises the "demote old default on new-default create/patch" transition inside a single transaction. |
| `enabled` | `BOOLEAN / INTEGER NOT NULL DEFAULT TRUE` | Soft-disable without deleting — keep the row for audit + history but skip it in the resolver chain. Matches Phase 5-1 semantic. |
| `last_used_at` | `DOUBLE PRECISION / REAL` (nullable) | LRU timestamp updated best-effort on every successful `get_llm()` resolve. Populates the third-level `ORDER BY last_used_at DESC NULLS LAST` tiebreak when multiple rows match `is_default=FALSE` + same provider. Nullable because fresh rows haven't been used yet. |
| `created_at` | `DOUBLE PRECISION / REAL NOT NULL` | Seconds since epoch; `time.time()` flows through unchanged. |
| `updated_at` | `DOUBLE PRECISION / REAL NOT NULL` | Bumped by every CRUD mutation (row 5b-3) and by every rotate / is_default flip. |
| `version` | `INTEGER NOT NULL DEFAULT 0` | Optimistic-lock column; row 5b-3 will accept an `If-Match` header carrying the expected version and return 412 on mismatch. Day-1 reservation matches J2 / Q.7 / Phase-5-1 precedent — adding it retroactively is expensive because existing rows would need a backfill. |

## 4. Indexes

| Index | Columns | Purpose |
|---|---|---|
| `llm_credentials_pkey` | `(id)` (PG auto) | PK uniqueness |
| `idx_llm_credentials_tenant` | `(tenant_id)` | Per-tenant list (row 5b-3 `GET /llm-credentials`) |
| `idx_llm_credentials_tenant_provider` | `(tenant_id, provider)` | Per-provider resolver (row 5b-2 `pick_default(provider)`) |
| `idx_llm_credentials_last_used` | `(tenant_id, last_used_at DESC NULLS LAST)` | LRU tiebreak for the resolver's `ORDER BY` clause |
| `uq_llm_credentials_default_per_provider` | `(tenant_id, provider) WHERE is_default = TRUE` | Race-safe "one default per (tenant, provider)" guard |

## 5. Drift guards

Row 5b-1 ships `backend/tests/test_llm_credentials_schema.py`
with three layers matching the `test_git_accounts_schema.py`
template:

1. **Migration file sanity** (pure unit, no DB) — asserts the
   alembic file exists, the revision chain is `0029 → 0028`,
   and the SQLite + PG branches carry the load-bearing fragments
   (CHECK constraint, partial unique index, FK, `DOUBLE
   PRECISION`, `JSONB NOT NULL DEFAULT '{}'::jsonb`, `version
   INTEGER NOT NULL DEFAULT 0`).
2. **Migrator `TABLES_IN_ORDER` alignment** — asserts
   `scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER` includes
   `'llm_credentials'` and does NOT include it in
   `TABLES_WITH_IDENTITY_ID` (PK is TEXT).
3. **Live SQLite contract** — fresh `backend.db.init` against a
   tmp DB, introspect the table + columns + indexes.
4. **Live PG contract** (gated on `OMNI_TEST_PG_URL`) — fresh
   `alembic upgrade head` against a clean PG schema, assert the
   table + columns + indexes + partial-unique + FK cascade all
   exist on the PG side.

These guards are the schema lock that fails loud at CI time when
someone adds an alembic migration that drops or renames a column.

## 6. Deliberate non-goals for row 5b-1

Row 5b-1 ships the schema ONLY. The following are intentionally
deferred:

- **CRUD endpoints** — `GET/POST/PATCH/DELETE /api/v1/llm-credentials`
  lives in row 5b-3. Until then the table is read by nobody.
- **Resolver swap** — `backend/agents/llm.py::get_llm()` continues
  to read `settings.{provider}_api_key` scalars. Row 5b-2 swaps
  it to an async resolver that consults `llm_credentials` first.
- **UI rewrite** — `integration-settings.tsx` LLM PROVIDERS section
  still talks to `PUT /runtime/settings`. Row 5b-4 switches it
  to the CRUD endpoints from 5b-3.
- **Legacy auto-migration** — row 5b-5 adds the lifespan hook that
  reads `settings.*_api_key` and creates `llm_credentials` rows.
  Until then the table is empty; row 5b-2 will fall back to the
  legacy scalars so empty-table deployments behave identically
  to pre-5b.
- **Deprecation path** — `Settings.{provider}_api_key` fields stay
  normal (not `deprecated=True`) until row 5b-6 lands the
  registry + write-path audit hook. This keeps read-OK contract
  intact for empty-`llm_credentials` deployments.
- **Ollama base-URL migration** — `ollama_base_url` stays as a
  scalar Settings field until row 5b-5 (lifespan hook) seeds a
  keyless `llm_credentials` row for `provider='ollama'` carrying
  `metadata.base_url`.
- **Rate-limit integration** — the per-tenant rate-limit envelope
  (SP-8.1c / Phase 4) does not yet key on credential `id`. Future
  work once multi-account-per-provider is live in prod.

## 7. Module-global state audit (SOP Step 1, qualified answer #2)

**What reads/writes module globals:** nothing. This row is
schema-only: one alembic migration + one SQLite bootstrap mirror
+ one line in the migrator table list. Alembic upgrade is a
single-writer lifespan op on the canonical DB; every worker reads
the same PG / SQLite row set after upgrade. No singleton, no
cache, no ContextVar introduced.

**How consistency holds across `uvicorn --workers N`:** by
construction — the PG row set is the only source of truth. When
row 5b-2 lands the resolver, the shim path it may synthesise (for
empty-`llm_credentials` deployments, reading the same
`Settings.{provider}_api_key` that every worker loads from the
same `.env`) is the Phase 5-2 `_build_registry` pattern — "each
worker derives the same value from the same source", qualified
answer #1 from the SOP.

## 8. Read-after-write timing audit

**Zero new write paths.** Row 5b-1 adds a table but nothing writes
to it yet. The schema tests exercise INSERT under the partial-
unique constraint and the CHECK constraint, but they commit
between writes (no parallelism for downstream tests to depend on).
When row 5b-3's CRUD lands it will sit on the same
`ON CONFLICT ... DO UPDATE` atomic upsert pattern SP-4.6 /
Phase-5-4 established — no compat-wrapper serialisation is
being replaced, so there is no timing-visible behaviour change
for existing callers.

## 9. Production status (SOP Step 6)

**Production status: dev-only**

Code is in main, migration + SQLite bootstrap + drift tests green,
but the table is read by nobody until row 5b-2 lands and written
by nobody until row 5b-3 / 5b-5 land. On next backend image
rebuild + rolling recreate, the lifespan `alembic upgrade head`
path will create the empty table on the PG primary; operators
need **zero** manual action (idempotent migration, `CREATE TABLE
IF NOT EXISTS` on both paths).

**Next gate**: dev-only → dev-only (schema reservation only) until
row 5b-2 lands the async resolver. Phase 5b overall flips to
`deployed-active` only once row 5b-4 (UI rewrite) + row 5b-6
(operator rotate drill) both land + operator observes 24 h clean.

**Rollback**: `alembic downgrade -1` drops the table (empty at
rollback time; no data loss). The SQLite bootstrap mirror edit
and the migrator `TABLES_IN_ORDER` entry are non-breaking to
revert — neither is read at rollback time.
