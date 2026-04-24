# Phase 5-1 — Multi-account `git_accounts` data model

> Date: 2026-04-24
> Owner: Software-beta agent
> Scope: row 5-1 of Phase 5 (Multi-account forge integrations) — schema
> + migration + drift-guard tests only. NO call-site sweep, NO UI, NO
> credential resolver refactor — those land in 5-2 through 5-11.

---

## 1. Why a new table

Today GitHub / GitLab credentials are stored in `Settings`:

```python
github_token:     str = ""              # scalar fallback
github_token_map: str = ""              # JSON {host: token}
gitlab_token_map: str = ""              # JSON {host: token}
gerrit_instances: str = ""              # JSON list of dicts
notification_jira_token:   str = ""     # scalar
```

Three structural problems make this unfit for the four-platform
multi-account future Phase 5 promises:

1. **`{host: token}` cannot represent multiple accounts on the same
   host.** `Object.fromEntries` (and Python `dict`) silently drop
   duplicate keys; the UI's `MultipleInstancesSection` looks like it
   accepts multiple rows but the second `github.com` entry overwrites
   the first on save.
2. **No per-tenant scope.** `Settings` is process-global; in a multi-
   tenant deploy, tenant A's GitHub PAT is available to tenant B.
3. **Plaintext on disk.** `.env` holds the secret in clear text;
   `secret_store` Fernet (already used for `tenant_secrets`) is the
   established pattern but forge tokens were never migrated to it.

The fix is the `git_accounts` table — one row per account, scoped to
a tenant, with the token at-rest encrypted via `secret_store.encrypt`
and looked up by URL pattern (`github.com/acme-corp/*`) to support the
"this org uses the company PAT, that org uses the consultancy PAT"
case that motivated this Phase.

## 2. Schema

PostgreSQL (production):

```sql
CREATE TABLE IF NOT EXISTS git_accounts (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 't-default'
                                    REFERENCES tenants(id) ON DELETE CASCADE,
    platform           TEXT NOT NULL                     -- 'github'|'gitlab'|'gerrit'|'jira'
                                    CHECK (platform IN ('github','gitlab','gerrit','jira')),
    instance_url       TEXT NOT NULL DEFAULT '',         -- e.g. 'https://github.com'
    label              TEXT NOT NULL DEFAULT '',         -- operator-visible name
    username           TEXT NOT NULL DEFAULT '',         -- account identity on the forge
    encrypted_token    TEXT NOT NULL DEFAULT '',         -- Fernet ciphertext (PAT/API token)
    encrypted_ssh_key  TEXT NOT NULL DEFAULT '',         -- Fernet ciphertext (private key body)
    ssh_host           TEXT NOT NULL DEFAULT '',         -- gerrit only
    ssh_port           INTEGER NOT NULL DEFAULT 0,       -- gerrit only; 0 = unset
    project            TEXT NOT NULL DEFAULT '',         -- gerrit project path / jira project key
    encrypted_webhook_secret TEXT NOT NULL DEFAULT '',   -- Fernet ciphertext (HMAC secret)
    url_patterns       JSONB NOT NULL DEFAULT '[]'::jsonb, -- list[str] glob patterns
    auth_type          TEXT NOT NULL DEFAULT 'pat',      -- 'pat'|'oauth' (5-12 hook)
    is_default         BOOLEAN NOT NULL DEFAULT FALSE,   -- per (tenant, platform)
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_used_at       DOUBLE PRECISION,                  -- LRU analytics; NULL = never used
    created_at         DOUBLE PRECISION NOT NULL,
    updated_at         DOUBLE PRECISION NOT NULL,
    version            INTEGER NOT NULL DEFAULT 0        -- optimistic-lock (J2 pattern)
);

CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant
    ON git_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant_platform
    ON git_accounts(tenant_id, platform);
CREATE INDEX IF NOT EXISTS idx_git_accounts_last_used
    ON git_accounts(tenant_id, last_used_at DESC NULLS LAST);

-- Unique partial: at most one default per (tenant, platform).
CREATE UNIQUE INDEX IF NOT EXISTS uq_git_accounts_default_per_platform
    ON git_accounts(tenant_id, platform)
    WHERE is_default = TRUE;
```

SQLite (dev parity, no JSONB / no partial indexes — fall back to
`TEXT` JSON storage and skip the partial-index constraint; the app
layer enforces the "one default per (tenant, platform)" invariant on
write):

```sql
CREATE TABLE IF NOT EXISTS git_accounts (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 't-default'
                                    REFERENCES tenants(id) ON DELETE CASCADE,
    platform           TEXT NOT NULL CHECK (platform IN ('github','gitlab','gerrit','jira')),
    instance_url       TEXT NOT NULL DEFAULT '',
    label              TEXT NOT NULL DEFAULT '',
    username           TEXT NOT NULL DEFAULT '',
    encrypted_token    TEXT NOT NULL DEFAULT '',
    encrypted_ssh_key  TEXT NOT NULL DEFAULT '',
    ssh_host           TEXT NOT NULL DEFAULT '',
    ssh_port           INTEGER NOT NULL DEFAULT 0,
    project            TEXT NOT NULL DEFAULT '',
    encrypted_webhook_secret TEXT NOT NULL DEFAULT '',
    url_patterns       TEXT NOT NULL DEFAULT '[]',
    auth_type          TEXT NOT NULL DEFAULT 'pat',
    is_default         INTEGER NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    metadata           TEXT NOT NULL DEFAULT '{}',
    last_used_at       REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    version            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant            ON git_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant_platform   ON git_accounts(tenant_id, platform);
CREATE INDEX IF NOT EXISTS idx_git_accounts_last_used         ON git_accounts(tenant_id, last_used_at DESC);
```

## 3. Design decisions

### 3.1 Why one column per common field instead of a giant `metadata` blob

`platform`, `instance_url`, `username`, `is_default`, `last_used_at`
are queried — `WHERE platform='github' AND tenant_id=$1 ORDER BY
last_used_at DESC` is the LRU analytics path; `WHERE tenant_id=$1
AND platform=$2 AND is_default=TRUE` is the default-account fast
path. Putting them in JSONB would force every read to either build
a functional index per key or do a full table scan, both of which
defeat the point of a relational store.

Anything that's *not* on a hot read path (provider-specific quirks
like `gitlab_group_id`, `github_app_id`, `jira_workflow_template`,
custom OAuth callback URLs) goes in the `metadata` JSONB column.

### 3.2 Why store SSH key encrypted separately from `encrypted_token`

GitHub / GitLab let you authenticate with EITHER a PAT OR an SSH
key — operators can have a PAT for HTTPS clones and an SSH key for
push (typical when their org's HTTPS PATs are read-only by policy
and SSH is the write path). Both at once is common. Two encrypted
columns keep the resolver logic simple — `pick_account_for_url(url)`
returns the row, the caller picks the right secret based on the URL
scheme (`https://` → PAT, `git@` → SSH key).

### 3.3 Why `ssh_host` / `ssh_port` / `project` as scalar columns

Gerrit (and the corner-case "GitHub Enterprise behind a custom SSH
gateway" deployment) needs all three as part of the credential —
the SSH endpoint is not derivable from the HTTPS `instance_url`.
Forcing them into `metadata` would make the gerrit handler do
JSON parsing on every push. They're empty-string defaults for
non-gerrit rows so they never get in the way.

### 3.4 `url_patterns` as JSONB list of glob strings

Array, not a single column with comma-split semantics — operators
can want one account to cover both `github.com/acme-corp/*` and
`github.com/acme-internal/*`. Pattern syntax is plain glob (`fnmatch`-
compatible); resolver design is row 5-3.

### 3.5 Unique partial index on `(tenant_id, platform) WHERE is_default`

This is the "at most one default per (tenant, platform)" invariant
**at the database layer**. Two concurrent UPDATEs racing to set
`is_default=TRUE` will get a unique-constraint violation on the
second one — much stronger than application-level "make sure
nothing's already set" SELECT-then-UPDATE which has TOCTOU under
the new asyncpg pool (lesson from SP-4.6 `tenant_secrets.upsert_secret`).

SQLite doesn't support partial indexes pre-3.8.0 in a fully reliable
way under `IF NOT EXISTS`; we do support them in 3.8+, but the
app layer also enforces the invariant on write so the partial-index
gap on dev SQLite isn't load-bearing for correctness.

### 3.6 `version INTEGER NOT NULL DEFAULT 0` — optimistic lock from day 1

J2 (#280) and Q.7 (#301) established that any user-mutable row needs
optimistic-lock guards (`If-Match` HTTP header → 409 on stale write)
before a multi-device deployment can race-write. `git_accounts` is
mutable from a UI screen — the operator could flip default on laptop
while editing label on phone. Add the column at table-create time so
row 5-4's `PATCH /git-accounts/{id}` handler can use it without a
follow-up migration.

### 3.7 `last_used_at` as `DOUBLE PRECISION` epoch seconds, nullable

Matches every other recent table (`sessions.created_at`,
`chat_messages.timestamp`) — `time.time()` flows from Python to PG
without a tzinfo dance. NULL = never used (so `ORDER BY
last_used_at DESC NULLS LAST` puts unused accounts last, which is
the correct LRU eviction order — touch on every successful resolve).

### 3.8 FK `tenant_id REFERENCES tenants(id) ON DELETE CASCADE`

Mirrors `tenant_secrets`. Deleting a tenant should drop its
credentials — not leak them to whatever future code reads
`git_accounts` without a tenant filter.

### 3.9 Why migration number 0027, not the 0019 the TODO row mentioned

The TODO row was drafted before alembic head moved past 0019. Live
head as of 2026-04-24 is 0026; the next free number is 0027. The
`0019_session_revocations` migration already owns 0019. Pick the
next sequential free number (0027) — Alembic linearises by
`down_revision`, not by the integer prefix's literal value, so
"0019" in the TODO row was descriptive ("an alembic migration named
git_accounts") not load-bearing.

### 3.10 URL-pattern resolver contract (row 5-3, 2026-04-24)

Row 5-3 locks the pattern grammar and resolution semantics that
`backend/git_credentials.py::pick_account_for_url` /
`pick_default` / `pick_by_id` / `require_account_for_url` honour.

**Pattern syntax — glob via `fnmatch`.** Patterns in
`url_patterns` are plain shell-style globs (Python `fnmatch.fnmatch`,
which is anchored to the full string by construction):

| Token        | Meaning                                  |
| ------------ | ---------------------------------------- |
| `*`          | match any sequence (including `/`)       |
| `?`          | match any single character               |
| `[seq]`      | match any character in *seq*             |
| `[!seq]`     | match any character not in *seq*         |
| anything else | literal — including `.`, `-`, `_`, `/`  |

This is deliberately the most boring grammar that solves the use
case. It is NOT regex — `.` is literal, not a wildcard — which
matches operator intuition for "the same syntax I use in
`.gitignore` / `.dockerignore`".

**Comparison form — scheme-stripped, lowercased.** Before matching,
the URL is normalised so a single pattern covers both HTTPS and
SSH transports:

| Input URL                              | Normalised form              |
| -------------------------------------- | ---------------------------- |
| `https://github.com/acme/app`          | `github.com/acme/app`        |
| `git@github.com:acme/app.git`          | `github.com/acme/app.git`    |
| `ssh://git@github.com/acme/app`        | `github.com/acme/app`        |
| `https://GitHub.com/AcMe/App`          | `github.com/acme/app`        |

So one pattern entry like `github.com/acme/*` is enough; the
operator does not need to write a separate SSH pattern.

**Anchored, not substring.** Patterns must cover the full
normalised URL. `acme/*` does NOT match `github.com/acme/app` —
the pattern would need to be `*acme/*` or `github.com/acme/*` to
match that URL.

**First-match-wins is deterministic.** `_fetch_git_accounts_rows`
SELECTs `ORDER BY is_default DESC, last_used_at DESC NULLS LAST,
platform, id` so the Python `for entry in registry` loop iterates
in that exact order. Two accounts with overlapping patterns
resolve as follows:

1. The `is_default=TRUE` row wins.
2. Among non-defaults, the more-recently-used (newer
   `last_used_at`) wins.
3. NULL `last_used_at` (never used) sorts last.
4. Ultimate tie-break: `platform` then `id`.

The deterministic ordering matters because two operators editing
`git_accounts` independently must converge on the same
resolution behaviour without "it works on my replica" surprise.

**Touch-on-resolve as the LRU primitive.** Every successful
`pick_*` call best-effort UPDATEs `git_accounts.last_used_at =
time.time()` for the matched row. This is what makes the
"more-recently-used wins" tie-break self-maintaining without a
cron job. Touch is best-effort: if the pool isn't initialised,
if the id isn't in the table (legacy shim row), or if the UPDATE
raises, the resolve still returns the row. Pass `touch=False`
to introspection / debug callers (CRUD list, "which account
would resolve this URL" probe) so they don't perturb the LRU.

**Explicit-raise variant — `require_account_for_url`.** Same
contract as `pick_account_for_url` but raises
`MissingCredentialError` (subclass of `LookupError`) instead of
returning `None` when nothing matches. Call sites that cannot
proceed without a credential (clone / fetch / push / webhook
verify) use `require_*`; sites that have a fallthrough path
(anonymous public read) use `pick_*`. The exception message
names the URL and tenant for grep-friendly logs.

**Resolution chain (steps 1-4).** Applied in order; first
successful step returns:

1. **`url_patterns` glob match** against the scheme-stripped
   URL (this section's contract).
2. **Exact host match** against the account's `instance_url`
   or `ssh_host`.
3. **Substring host match** (legacy fallback for shim rows whose
   `url_patterns` is empty).
4. **Platform default** via `pick_default(detect_platform(url))` —
   the `is_default=TRUE` row for the URL's platform, or first
   enabled row if no default is flagged.

Step 4 fires only when steps 1-3 all miss; explicitly *not* a
"fall-through after every loop" — so a specific pattern match
in step 1 always wins over the platform default.

**Special chars in org names.** GitHub forbids `[`, `]`, `*`, `?`
in org / user names — so the only glob metacharacter that could
collide with a real org name is `?` (rare in repos, none seen
in practice). Operators who somehow have a literal `*` in a
URL component should escape it via `[*]` (fnmatch character
class), but this is a hypothetical concern and not implemented
specially.

## 4. What is NOT in this row

Phase 5-1 deliberately ships ONLY the schema + drift guards. The
following are explicitly OUT of scope and land in subsequent rows:

| Out-of-scope item                                  | Lands in   |
| -------------------------------------------------- | ---------- |
| `backend/git_credentials.py` resolver refactor     | row 5-2    |
| `pick_account_for_url()` URL-pattern matcher       | row 5-3    |
| `GET/POST/PATCH/DELETE /api/v1/git-accounts` API   | row 5-4    |
| Legacy `.env` → `git_accounts` auto-migration hook | row 5-5    |
| Call-site sweep across 11 .py files                | rows 5-6/7/8 |
| UI rewrite (`AccountManagerSection`)               | row 5-9    |
| Soak test + per-tenant isolation drill             | row 5-11   |
| OAuth flow (PAT only for MVP)                      | row 5-12   |

Any change touching those concerns is a scope violation — drop it
back into its own row.

## 5. Module-global state audit (SOP Step 1, qualified answer #2)

Schema-only migration. The migration body itself runs once via
`alembic upgrade head` against the live PG (or via `db.init`'s
SQLite bootstrap on dev). No new module-level state, no
singleton, no in-memory cache introduced by this row. Cross-worker
consistency falls under SOP rubric #2 — coordination is via PG
(the only writer is the alembic process; all worker reads land
on the same PG row set).

The `tenant_secrets` precedent (SP-4.6) showed that `INSERT/UPDATE`
helpers needed `ON CONFLICT ... DO UPDATE` to avoid race-condition
writes from concurrent pool connections. The CRUD helper for
`git_accounts` (row 5-4) will follow the same pattern; this row
just lays the schema.

## 6. Read-after-write timing audit (SOP Step 1)

Schema-only. No write path is changed by this row; no test
upstream of this row depends on serialisation timing of an
`INSERT INTO git_accounts`. The existing `git_credentials.py`
registry continues to read from `Settings` until row 5-2 swaps
the data source — until then the table is created but unused.

## 7. Drift guard tests (SOP Step 4)

Two tests land alongside the migration:

1. `backend/tests/test_git_accounts_schema.py` — schema contract:
   creates a fresh SQLite via `backend.db.init`, asserts the table
   exists with the expected column set + types + indexes. Runs the
   PG-equivalent assertion when `OMNI_TEST_PG_URL` is set.
2. `test_migrator_schema_coverage.py` — already exists; the new
   `git_accounts` entry must be added to `TABLES_IN_ORDER` in
   `scripts/migrate_sqlite_to_pg.py`. The drift-guard test will
   fail-loud at CI time if anyone forgets.

## 8. Rollback

`alembic downgrade -1` drops the table. No data loss as long as
the table hasn't been populated (rows 5-2 through 5-11 are still
pending, so it stays empty). Once 5-5 (legacy auto-migration)
ships, downgrade is destructive and operators must back up
`git_accounts` first.

## 9. Production-readiness gate (per SOP)

- **Production status: dev-only** — code merged but no operator
  smoke needed; the table is empty and unread until row 5-2.
- **Next gate:** dev-only stays the appropriate state until
  row 5-2 lands. Phase 5 won't move to `deployed-active` until
  row 5-9 (UI) ships AND row 5-11 (soak test) passes.
