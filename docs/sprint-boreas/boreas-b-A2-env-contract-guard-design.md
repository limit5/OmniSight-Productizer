# Boreas-B A2 — dev env-contract guard (design for codex review)

**Ticket**: OP-1643. **Status**: DRAFT for codex adversarial review (2026-05-24).
**Spec parent**: `docs/sprint-boreas/sprint-boreas-b-deployment-automation-spec.md` Gap A2.
**Builds on**: A1/OP-1642 (DB privilege isolation + isolated dev compose stack) — DONE.

## Problem
A1 made dev↔prod isolation *provable at the DB-credential layer* (the `omnisight_dev` role
is `REVOKE`'d `CONNECT` on the prod `omnisight` DB → fail-closed). But that only catches a
process using the `omnisight_dev` credential. The remaining hole is a **dev process that
resolves a prod-looking DATABASE_URL** — e.g. an operator runs a migration / runner / script
from the dev workdir but with the prod `audit-db.env` still sourced, or with `OMNISIGHT_ENV=dev`
set but a stale prod URL. A2 = an env-contract guard that **fails closed** in that case, modeled
on `infra/staging/verify-env-contract.sh`, covering backend + the 4 runners + pipeline-coordinator
+ any alembic/migration entrypoint.

## Key constraint discovered (the design pivot)
Dev and prod backends BOTH connect to their own compose-internal `postgres:5432`. So
**host:port cannot distinguish dev from prod.** The only reliable discriminators are:
- **DB name**: prod = exactly `omnisight`; dev = `omnisight_dev` (ends `_dev`); staging = `*_staging`.
- **DB user**: prod = `omnisight`; dev = `omnisight_dev`.
- **host-published port** (only when a URL targets the host, not compose-internal): dev = 58432.

This mirrors how the staging guard already keys on the `_staging` db-name suffix / staging ports,
not on host. So the guard keys on **db-name + user**, with host-port as a secondary signal.

## Env markers (confirmed from code)
- `OMNISIGHT_ENV`: `dev` (dev compose), `staging` (staging compose), `prod`/`production` (prod;
  `config._resolve_yaml_path` aliases `production→prod`, `development→dev`). **Prod runners +
  coordinator currently run with OMNISIGHT_ENV UNSET** (pipeline-coordinator.service sets none;
  runner wrappers source prod `audit-db.env`, no env marker).

## The contract (truth table)
Let `env = OMNISIGHT_ENV` (canonicalized: production→prod, development/develop→dev), and classify
the resolved DSN as `dev` / `staging` / `prod` / `unknown` by db-name+user+port.

| OMNISIGHT_ENV | DSN shape | Verdict | Rationale |
|---|---|---|---|
| `dev` | dev | OK | matched |
| `dev` | prod | **FAIL** | the core protection — dev process at prod DB |
| `dev` | staging | **FAIL** | dev process at staging DB |
| `prod` (or production) | prod | OK | matched |
| `prod` | dev / staging | **FAIL** | symmetric — prod process at a non-prod DB |
| *unset* | prod | OK | the current prod runners/coordinator (Exercised AC: prod unaffected) |
| *unset* | dev / staging | **FAIL** | a process at the dev/staging DB that didn't declare itself = misconfig |
| any | sqlite / unknown | SKIP (warn) | legacy SQLite + tests; see exemptions |

**Fail-closed direction**: every "process touching a DB that doesn't match its declared env"
aborts. The only OK-with-unset case is the legacy prod-by-default path (unset + prod DSN), so we
do not break the 4 runners + coordinator as they exist today.

## Two enforcement layers (defense in depth)
1. **In-process guard** — new `backend/config.py::enforce_env_db_contract()` (pure, raises
   `EnvContractViolation`). Called explicitly by each entrypoint:
   - backend: inside `validate_startup_config()` (already invoked in `main.lifespan`, strict when
     `debug=False`) → prod boot refuses on violation.
   - runners: at the top of `auto-runner-jira.py` main.
   - coordinator: at the top of `backend/agents/pipeline_coordinator.py` main.
   - alembic: in `backend/alembic/env.py::_resolve_db_url()` after resolving the URL, before
     handing it to the engine.
   **Why explicit-call, not import-time `sys.exit()`**: a guard at `Settings()` instantiation
   (config import) runs in *every* test that imports `backend.config` (≈ all of them) and would
   abort the suite. Explicit calls at real entrypoints cover all 4 process types without
   poisoning import.
2. **Shell guard** — `infra/dev/verify-env-contract.sh` (mirror of the staging one), wired as
   `ExecStartPre` on the dev compose systemd unit (the A4 unit). Catches a bad dev `.env` before
   the container starts — belt-and-suspenders with layer 1.

## Exemptions (must not break tests / CI / legacy)
The in-process guard is a **no-op** (returns, optionally warns) when ANY of:
- DSN scheme is `sqlite` (legacy/test DBs) or empty/unknown.
- `PYTEST_CURRENT_TEST` is set (running under pytest) **OR** `settings.ci_mode` is true.
- explicit escape hatch `OMNISIGHT_ENV_CONTRACT_DISABLE=1` (documented break-glass; logged loudly).

## Classification helper (shared)
`_classify_dsn(dsn) -> Literal["dev","staging","prod","sqlite","unknown"]`:
- parse db-name = last path segment minus `?query`; parse user from `//user:...@`.
- `sqlite` if scheme sqlite. `dev` if db-name ends `_dev`/`-dev` OR user == `omnisight_dev` OR
  host-port 58432. `staging` if ends `_staging`/`-staging` OR staging ports 55432/55433. `prod` if
  db-name is exactly `omnisight` AND not dev/staging. else `unknown` (treated like sqlite: skip+warn,
  since we can't prove a violation — fail-closed only on a *positive* mismatch, to avoid breaking
  unforeseen valid DSNs).

## AC mapping (4-AC)
- **Code**: `enforce_env_db_contract()` + `_classify_dsn()` in config.py; `infra/dev/verify-env-contract.sh`.
- **Deploy**: dev compose unit references the shell guard as ExecStartPre (lands with A4 unit);
  alembic/runner/coordinator entrypoints call the in-process guard.
- **Integration**: a process with `OMNISIGHT_ENV=dev` + prod DSN aborts with `EnvContractViolation`
  (unit test over the truth table) + the shell guard rejects a prod-DSN dev `.env`.
- **Exercised**: a prod process (env unset OR prod) + prod DSN starts clean; the live prod backend
  /readyz stays green after the guard ships (no false-positive on the real prod config).

## Open questions for codex
1. Truth-table holes — is "unset + dev DSN → FAIL" safe, or is there a legitimate flow (a dev-DB
   integration test run outside pytest?) it would wrongly kill? Should "unknown" ever be a hard fail?
2. Is keying `prod` as "db-name == exactly `omnisight`" too brittle (what if prod DB gets renamed,
   or a tenant DB `omnisight_<tid>` appears)? Better positive prod signal?
3. The in-process guard runs at *import-adjacent* entrypoints but NOT at bare `python -c "import
   backend.config"`. Is the explicit-call coverage (4 sites) actually complete, or is there a 5th
   entrypoint (e.g. a one-off `scripts/*.py` that opens the DB directly via `backend.db`)? Should
   the chokepoint instead be `backend/db.py::_resolve_pg_dsn()` / pool init (runs whenever ANYONE
   actually opens a connection) rather than per-entrypoint?
4. Escape-hatch + pytest exemption: any risk the exemptions become a silent bypass in prod (e.g.
   PYTEST_CURRENT_TEST leaking into a prod shell)? Should the escape hatch itself be refused when
   env==prod?
5. Should the guard also assert the *converse* for the dev stack at the shell layer (dev `.env`
   must NOT carry prod/live API keys), mirroring staging's Stripe/Anthropic/Slack sandbox-prefix
   checks? Or is that out of A2 scope (A2 is DB-URL-focused)?

---

## codex review round 1 (2026-05-24) — folded in
**Verdict: NEEDS-REDESIGN** — truth table directionally right, enforcement topology too weak
(4 explicit entrypoints miss shipped DB-open paths). Audit: `docs/audit/codex-reviews/a2-env-contract-guard-codex-audit-2026-05-24.txt`. Conditions, all verified against shipped code:

**Topology → connection-time, not per-entrypoint.** Final hook set:
- `enforce_env_db_contract(dsn, *, source)` + `_classify_dsn(dsn)` (pure) in `backend/config.py`.
- `backend/db_pool.py::init_pool(dsn)` — guard at top, before `asyncpg.create_pool`. Covers the
  pool family: backend, runners (via `backend.agents`), coordinator, **`backend/audit.py::_ensure_pool`
  (verified: it calls `_resolve_pg_dsn()` + `db_pool.init_pool`)**.
- `backend/db.py::_resolve_pg_dsn()` — guard before returning a PG DSN.
- `backend/db_url.py::DatabaseURL.asyncpg_connect_kwargs()` — guard at top (it's already PG-only).
  **Single chokepoint for the entire direct-`asyncpg.connect` family** (character_card,
  runner_metrics_recorder, agent_drift_report, scripts/*; all call `parse_db_url()` →
  `asyncpg_connect_kwargs()` → `asyncpg.connect()`). One edit covers all of codex's "missing
  entrypoints" except shell `pg_dump`/`psql` tools.
- `backend/alembic/env.py` — explicit guard after `_resolve_db_url()` (because `SQLALCHEMY_URL`
  wins first and bypasses the runtime-env order the other hooks see).

**Classification (corrected):** `urllib.parse` with URL-decoded user+db. **prod = (db == `omnisight`
AND user == `omnisight`)** — db-name alone too weak (Q2). dev = db/user `omnisight_dev` (or host-port
58432). staging = db `*_staging`/`*-staging` (or ports 55432/55433). sqlite = scheme sqlite.
**unknown-Postgres → HARD FAIL** (not skip — a typo/renamed-prod/url-encoding bug must not fail-open);
only non-Postgres/empty/sqlite skip. Canonicalize env both `production→prod` and `develop/development→dev`
(config.py currently special-cases only the literal `production`).

**Exemptions (tightened):** bypass only when `(PYTEST_CURRENT_TEST or ci_mode) AND DSN-not-classified-prod`
— must NOT bypass a known-prod mismatch (tests call `init_pool` directly, so the carveout is required,
but a prod DSN inside a test is still a real violation). `OMNISIGHT_ENV_CONTRACT_DISABLE=1` is
**refused when canonical env is prod/production OR DSN class is prod** (no prod break-glass — prod's
valid states `unset+prod`/`prod+prod` never need it). sqlite always skip.

**API-key sandbox checks (Q5):** keep OUT of the Python DB guard (stays narrow + hard to mis-trigger);
put them in the dev SHELL guard only (mirror staging's Stripe/Anthropic/Slack prefix checks).

**Residual (documented, A1-covered):** shell `pg_dump`/`psql` tools (`backup_postgres_daily.sh`,
`backup_wal_hourly.sh`) bypass Python entirely — covered by A1's credential REVOKE (a dev process
with the `omnisight_dev` cred is rejected by PG itself), not by A2's env-declaration layer. The dev
shell guard's `.env` check is the A2-side mitigation. No further A2 work; note in OP-1643.

**No-false-positive proof:** prod runner/coordinator DSN = `omnisight:***@127.0.0.1:5432/omnisight`
→ classify prod, env unset → `unset+prod = OK`. Live prod backend env=`production` + prod DSN → OK.
Dev backend env=`dev` + `omnisight_dev` DSN → OK. Verified before implementing.
